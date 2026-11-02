"""验证日志归档、并发身份隔离及离线筛选，不调用真实模型。"""

import asyncio
import importlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from contextvars import copy_context
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deerflow.cli import main as cli_main
from deerflow.log_context import get_log_context, log_context, log_operation
from deerflow.log_files import DailyFileHandler, get_log_directory, get_log_level
from deerflow.logging_config import configure_logging


class ClientLoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        root = logging.getLogger()
        self.previous_handlers = root.handlers[:]
        self.previous_level = root.level
        self.previous_loggers = [(item, item.handlers[:], item.level, item.propagate) for item in root.manager.loggerDict.values() if isinstance(item, logging.Logger)]
        self.addCleanup(self.restore_logging)
        self.enterContext(patch.dict(os.environ, {"DEER_FLOW_LOG_DIR": str(self.directory), "DEER_FLOW_LOG_LEVEL": "DEBUG"}))
        self.config = SimpleNamespace(log_level="info", logging=SimpleNamespace(enhance=SimpleNamespace(enabled=True)))
        configure_logging(self.config)
        self.logger = logging.getLogger("deerflow.logging_test")

    def restore_logging(self):
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            if handler not in self.previous_handlers:
                handler.close()
        root.handlers[:] = self.previous_handlers
        root.setLevel(self.previous_level)
        for logger, handlers, level, propagate in self.previous_loggers:
            logger.handlers[:] = handlers
            logger.setLevel(level)
            logger.propagate = propagate

    def records(self):
        return [json.loads(line) for path in sorted(self.directory.glob("*.jsonl")) for line in path.read_text().split("\n") if line]

    def query(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli_main(["logs", *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_default_directory_follows_home_and_override_follows_project_root(self):
        with patch.dict(os.environ, {"DEER_FLOW_LOG_DIR": "", "DEER_FLOW_HOME": str(self.directory / "runtime")}):
            self.assertEqual(get_log_directory(), self.directory / "runtime/logs")
        with patch.dict(os.environ, {"DEER_FLOW_PROJECT_ROOT": str(self.directory), "DEER_FLOW_LOG_DIR": "custom/logs"}):
            self.assertEqual(get_log_directory(), self.directory / "custom/logs")

    def test_environment_level_wins_and_repeated_setup_has_no_duplicate_output(self):
        with patch.dict(os.environ, {"DEER_FLOW_LOG_LEVEL": "warning"}):
            configure_logging(self.config)
            configure_logging(self.config)
            self.logger.info("省略")
            self.logger.warning("保存一次")
        self.assertEqual([record["message"] for record in self.records()], ["保存一次"])
        with patch.dict(os.environ, {"DEER_FLOW_LOG_LEVEL": ""}):
            self.assertEqual(get_log_level("debug"), logging.DEBUG)
        with patch.dict(os.environ, {"DEER_FLOW_LOG_LEVEL": "invalid"}):
            with self.assertRaises(ValueError):
                configure_logging(self.config)

    def test_console_handlers_are_removed_and_child_records_reach_file(self):
        console = io.StringIO()
        root = logging.getLogger()
        root.addHandler(logging.StreamHandler(console))
        child = logging.getLogger("third_party.console_test")
        previous = child.handlers[:], child.propagate, child.level
        self.addCleanup(self.restore_child, child, previous)
        child.addHandler(logging.StreamHandler(console))
        child.propagate = False
        child.setLevel(logging.DEBUG)
        configure_logging(self.config)
        child.info("第三方日志")
        self.assertEqual(console.getvalue(), "")
        self.assertEqual(self.records()[-1]["message"], "第三方日志")

    @staticmethod
    def restore_child(logger, previous):
        logger.handlers[:], logger.propagate, level = previous
        logger.setLevel(level)

    def test_record_date_selects_file_without_renaming_existing_files(self):
        handler = logging.getLogger().handlers[0]
        for day in (1, 2, 1):
            record = logging.LogRecord("deerflow.test", logging.INFO, __file__, 1, "跨日", (), None)
            record.created = datetime(2026, 11, day, 12).timestamp()
            handler.handle(record)
        self.assertEqual(len((self.directory / "deerflow-2026-11-01.jsonl").read_text().split("\n")) - 1, 2)
        self.assertTrue((self.directory / "deerflow-2026-11-02.jsonl").is_file())

    def test_multiple_processes_append_complete_json_lines(self):
        code = """
import logging, os
from pathlib import Path
from deerflow.log_files import DailyFileHandler
handler = DailyFileHandler(Path(os.environ['DEER_FLOW_LOG_DIR']))
handler.setFormatter(logging.Formatter('{"message":"%(message)s"}'))
logger = logging.getLogger('writer')
logger.setLevel(logging.INFO)
logger.addHandler(handler)
for number in range(50):
    logger.info('%s:%s', os.getpid(), number)
handler.close()
"""
        processes = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for process in processes:
            output, errors = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, errors.decode())
            self.assertFalse(output)
        self.assertEqual(len({record["message"] for record in self.records()}), 150)

    def test_exception_traceback_and_urls_are_preserved_and_redacted(self):
        with self.assertRaises(RuntimeError), log_operation(self.logger, component="thread", operation="upload_file", user_id="alice", thread_id="thread_a"):
            self.logger.info("下载 https://user:secret@example.test/private?token=hidden")
            raise RuntimeError("失败 https://example.test/private?token=hidden")
        records = self.records()
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["user_id"], "alice")
        self.assertIn("RuntimeError", records[-1]["exc_info"])
        self.assertNotIn("hidden", json.dumps(records))
        self.assertNotIn("secret", json.dumps(records))
        self.assertEqual(get_log_context(), {})

    def test_async_tasks_and_to_thread_keep_independent_contexts(self):
        async def request(user_id):
            with log_context(user_id=user_id, thread_id=f"thread_{user_id}"):
                await asyncio.sleep(0)
                await asyncio.to_thread(self.logger.info, "异步节点")

        async def run():
            await asyncio.gather(request("alice"), request("bob"))

        asyncio.run(run())
        records = [item for item in self.records() if item["message"] == "异步节点"]
        self.assertEqual({(item["user_id"], item["thread_id"]) for item in records}, {("alice", "thread_alice"), ("bob", "thread_bob")})
        self.assertEqual(get_log_context(), {})

    def test_sync_streams_bind_each_step_and_cleanup_without_leaking_context(self):
        from deerflow.client import DeerFlowClient

        def turn(*args, **kwargs):
            try:
                self.logger.info("流节点")
                yield SimpleNamespace(type="messages-tuple")
            finally:
                self.logger.info("流清理")

        client = object.__new__(DeerFlowClient)
        with patch("deerflow.client.stream_client_turn", side_effect=turn):
            first = client.stream("不记录的消息", thread_id="thread_a", user_id="alice")
            second = client.stream("另一条私密消息", thread_id="thread_b", user_id="bob")
            next(first)
            self.assertEqual(get_log_context(), {})
            next(second)
            self.assertEqual(get_log_context(), {})
            copy_context().run(first.close)
            second.close()
        nodes = [item for item in self.records() if item["message"] in {"流节点", "流清理"}]
        self.assertEqual([(item["user_id"], item["thread_id"]) for item in nodes], [("alice", "thread_a"), ("bob", "thread_b"), ("alice", "thread_a"), ("bob", "thread_b")])
        self.assertNotIn("私密消息", json.dumps(self.records(), ensure_ascii=False))

    def test_managed_stream_reuses_worker_with_correct_identity_after_cancellation(self):
        module = importlib.import_module("test_agent_managed_stream")
        fixture = module.ManagedStreamTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        graph, pool = fixture.harness(synchronous=True)
        observe = graph.observe

        def observe_with_log():
            self.logger.info("托管节点")
            observe()

        graph.observe = observe_with_log
        first = fixture.start(pool, user_id="alice", thread_id="thread_a")
        self.assertTrue(graph.entered.wait(5))
        graph.release.set()
        first.close(timeout=5)
        second = fixture.start(pool, user_id="bob", thread_id="thread_b")
        list(second)
        nodes = [item for item in self.records() if item["message"] == "托管节点"]
        self.assertEqual([(item["user_id"], item["thread_id"]) for item in nodes], [("alice", "thread_a"), ("bob", "thread_b")])
        finished = [item for item in self.records() if item.get("operation") == "managed_stream" and item.get("status") in {"cancelled", "completed", "failed"}]
        self.assertEqual([item["status"] for item in finished], ["cancelled", "completed"])
        self.assertEqual(get_log_context(), {})

    def test_session_chat_and_stream_assign_one_thread_id_and_restore_caller(self):
        from deerflow.agent_client import DeerFlowClientPool
        from deerflow.runtime.user_context import get_current_user

        logger = self.logger

        class FakeClient:
            def chat(self, message, **options):
                logger.info("同步聊天")
                return options["thread_id"]

            def stream(self, message, **options):
                try:
                    logger.info("session 流节点")
                    yield SimpleNamespace(type="messages-tuple", thread_id=options["thread_id"])
                finally:
                    logger.info("session 流清理")

        with DeerFlowClientPool(size=1, client_factory=FakeClient) as pool:
            with pool.session(user_id="alice") as session:
                thread_id = session.chat("私密聊天正文")
                self.assertEqual(get_log_context(), {})
                self.assertIsNone(get_current_user())
                with session.stream("私密聊天正文", thread_id=thread_id) as events:
                    self.assertEqual(next(events).thread_id, thread_id)
                    self.assertEqual(get_log_context(), {})
        records = [item for item in self.records() if item["message"] in {"同步聊天", "session 流节点", "session 流清理"}]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(item["user_id"] == "alice" and item["thread_id"] == thread_id for item in records))

    def test_conversion_worker_inherits_thread_log_context_inside_event_loop(self):
        from deerflow.thread_client import _convert_document

        async def convert(*args, **kwargs):
            self.logger.info("文件转换节点")
            return self.directory / "converted.md"

        async def run():
            with log_context(user_id="alice", thread_id="thread_a", component="thread"):
                return _convert_document(self.directory / "test.pdf", self.directory / "converted.md")

        with patch("deerflow.utils.file_conversion.convert_file_to_markdown", side_effect=convert):
            self.assertEqual(asyncio.run(run()), self.directory / "converted.md")
        records = [item for item in self.records() if item["message"] == "文件转换节点"]
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["user_id"], records[0]["thread_id"]), ("alice", "thread_a"))

    def test_error_event_and_cleanup_exception_have_failed_lifecycle(self):
        from deerflow.log_context import LoggedIterator

        def failed():
            yield SimpleNamespace(type="error")

        self.assertEqual(len(list(LoggedIterator(failed(), self.logger, user_id="alice", thread_id="thread_a"))), 1)

        def cleanup_failure():
            try:
                yield SimpleNamespace(type="messages-tuple")
            finally:
                raise RuntimeError("清理故障")

        iterator = LoggedIterator(cleanup_failure(), self.logger, user_id="bob", thread_id="thread_b")
        next(iterator)
        with self.assertRaisesRegex(RuntimeError, "清理故障"):
            iterator.close()
        failures = [item for item in self.records() if item.get("status") == "failed"]
        self.assertEqual([(item["user_id"], item["thread_id"]) for item in failures], [("alice", "thread_a"), ("bob", "thread_b")])
        self.assertIn("RuntimeError", failures[-1]["exc_info"])
        self.assertEqual(get_log_context(), {})

    def test_thread_operations_record_identity_bytes_area_and_failures(self):
        from langgraph.checkpoint.memory import InMemorySaver
        from deerflow.config.paths import Paths
        from deerflow.thread_client import DeerFlowThreadClient

        client = object.__new__(DeerFlowThreadClient)
        client._checkpointer = InMemorySaver()
        with patch("deerflow.thread_client.get_paths", return_value=Paths(self.directory / "runtime")):
            thread_id = client.create_thread("alice")
            client.upload_file("alice", thread_id, "test.txt", b"private file content")
            self.assertEqual(client.download_file("alice", thread_id, "test.txt", "uploads"), b"private file content")
            client.thread_files("alice", thread_id, "uploads")
            with self.assertRaises(FileNotFoundError):
                client.download_file("alice", thread_id, "missing.txt", "uploads")
            client.delete_file("alice", thread_id, "test.txt", "uploads")
            client.delete_thread("alice", thread_id)
        records = self.records()
        self.assertTrue(all(item["user_id"] == "alice" and item["thread_id"] == thread_id for item in records))
        self.assertTrue(any(item.get("byte_count") == 20 for item in records))
        self.assertTrue(any(item.get("status") == "failed" and item.get("area") == "uploads" for item in records))
        self.assertNotIn("private file content", json.dumps(records))

    def test_skill_operations_record_public_custom_copy_and_exclude_content(self):
        module = importlib.import_module("test_skill_client_public")
        fixture = module.SkillClientPublicTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.public.create_skill("example", "私密描述")
        fixture.public.copy_skill("example", "alice")
        fixture.client.get_skill_info("alice", "example")
        with self.assertRaises(FileNotFoundError):
            fixture.client.download_file("alice", "example", "missing.txt")
        records = self.records()
        self.assertTrue(any(item["user_id"] is None and item.get("category") == "public" for item in records))
        self.assertTrue(any(item["user_id"] == "alice" and item.get("category") == "custom" and item.get("status") == "failed" for item in records))
        self.assertTrue(any(item.get("target_user_id") == "alice" and item.get("operation") == "copy_skill" for item in records))
        self.assertNotIn("私密描述", json.dumps(records, ensure_ascii=False))

    def seed_query_logs(self):
        for user, thread in (("alice", "thread_a"), ("alice", "thread_b"), ("bob", "thread_a")):
            with log_context(user_id=user, thread_id=thread, component="agent"):
                self.logger.info("查询样例\n包含换行\u2028但仍为一条 JSON")
        with log_context(user_id=None, thread_id=None, component="skill", skill_name="example", category="public", target_user_id="alice"):
            self.logger.info("复制技能", extra={"operation": "copy_skill"})
        return datetime.now().date().isoformat()

    def test_cli_lists_threads_and_precisely_filters_user_and_thread(self):
        day = self.seed_query_logs()
        code, output, errors = self.query(day, "alice", "--format", "json")
        self.assertEqual(code, 0, errors)
        self.assertEqual({item["thread_id"] for item in map(json.loads, output.split("\n")[:-1])}, {"thread_a", "thread_b"})
        code, output, errors = self.query(day, "alice", "thread_a", "--format", "json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(output.split("\n")[:-1]), 1)
        self.assertEqual(json.loads(output)["user_id"], "alice")

    def test_cli_skill_filters_and_export_match_printed_results(self):
        day = self.seed_query_logs()
        arguments = ("show", "--date", day, "--user-id", "alice", "--component", "skill", "--skill-name", "example", "--category", "public", "--operation", "copy_skill", "--format", "json")
        code, printed, errors = self.query(*arguments)
        self.assertEqual(code, 0, errors)
        destination = self.directory / "exports/skills.jsonl"
        code, output, errors = self.query(*arguments, "--output", str(destination))
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, "")
        self.assertEqual(destination.read_text(), printed)
        code, output, errors = self.query("skills", "--date", day, "--category", "public", "--format", "json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["skill_name"], "example")

    def test_cli_handles_missing_corrupt_and_partial_logs_and_protects_source(self):
        day = self.seed_query_logs()
        source = self.directory / f"deerflow-{day}.jsonl"
        with source.open("a") as stream:
            stream.write('invalid\n{"user_id":[],"thread_id":"bad"}\n{"partial":')
        code, output, errors = self.query("show", "--date", day, "--level", "ERROR")
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("跳过 3 条", errors)
        before = source.read_bytes()
        code, _, errors = self.query("show", "--date", day, "--output", str(source))
        self.assertEqual(code, 1)
        self.assertIn("不能覆盖", errors)
        self.assertEqual(source.read_bytes(), before)
        code, _, errors = self.query("show", "--date", "2000-01-01")
        self.assertEqual(code, 1)
        self.assertIn("日志查询失败", errors)

    def test_cli_explicit_env_file_sets_directory_without_runtime_configuration(self):
        day = self.seed_query_logs()
        selected = self.directory / "logs.env"
        selected.write_text(f"DEER_FLOW_LOG_DIR={self.directory}\n")
        with patch.dict(os.environ, {"DEER_FLOW_ENV_FILE": str(selected)}):
            os.environ.pop("DEER_FLOW_LOG_DIR")
            code, output, errors = self.query("threads", "--date", day, "--user-id", "alice")
        self.assertEqual(code, 0, errors)
        self.assertIn("thread_a", output)

    def test_cli_can_query_in_fresh_process_without_client_or_tui_imports(self):
        day = self.seed_query_logs()
        code = """
import importlib.abc, sys
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {'deerflow.client', 'deerflow.agent_client', 'deerflow.tui', 'deerflow.thread_client', 'deerflow.skill_client'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, NoRuntime())
from deerflow.cli import main
raise SystemExit(main(sys.argv[1:]))
"""
        result = subprocess.run([sys.executable, "-c", code, "logs", "threads", "--date", day, "--user-id", "alice"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("thread_a", result.stdout)


if __name__ == "__main__":
    unittest.main()
