"""验证嵌入式配置加载后的实际日志输出，隔离宿主进程的日志处理器。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class FileLoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config_path = self.directory / "config.yaml"
        self.config_path.write_text(
            "sandbox:\n  use: deerflow.sandbox.local:LocalSandboxProvider\nlog_level: debug\n",
            encoding="utf-8",
        )
        self.env_file = self.directory / ".env"
        self.env_file.write_text("", encoding="utf-8")
        self.extensions_path = self.directory / "extensions_config.json"
        self.extensions_path.write_text("{}", encoding="utf-8")
        self.log_directory = self.directory / "nested" / "logs"

    def run_logging(self, script, **overrides):
        environment = os.environ.copy()
        environment.update(
            DEER_FLOW_ENV_FILE=str(self.env_file),
            PYTHON_DOTENV_DISABLED="0",
            DEER_FLOW_HOME=str(self.directory / "runtime"),
            DEER_FLOW_EXTENSIONS_CONFIG_PATH=str(self.extensions_path),
            DEER_FLOW_LOG_DIR=str(self.log_directory),
            DEER_FLOW_LOG_LEVEL="INFO",
            TEST_CONFIG_PATH=str(self.config_path),
        )
        environment.update({name: value for name, value in overrides.items() if value is not None})
        for name, value in overrides.items():
            if value is None:
                environment.pop(name, None)
        code = """
import logging
import os
import sys
from unittest.mock import patch

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
from deerflow.config.app_config import AppConfig
with patch.object(AppConfig, '_apply_singleton_configs'):
    config = AppConfig.from_file(os.environ['TEST_CONFIG_PATH'])
logger = logging.getLogger('deerflow.file_logging_test')
""" + script
        result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def read_log(self):
        return (self.log_directory / "deerflow.log").read_text(encoding="utf-8")

    def test_file_replaces_console_and_repeated_configuration_does_not_duplicate(self):
        self.run_logging("""
logger.debug('不应记录调试内容')
logger.info('中文日志内容')
handler = logging.root.handlers[0]
from deerflow.logging_config import configure_logging
configure_logging(config)
assert logging.root.handlers == [handler]
logger.info('重复配置后只记录一次')
""")
        contents = self.read_log()
        self.assertNotIn("不应记录调试内容", contents)
        self.assertIn("中文日志内容", contents)
        self.assertIn("No models are configured", contents)
        self.assertEqual(contents.count("重复配置后只记录一次"), 1)
        self.run_logging("logger.info('追加日志内容')")
        self.assertIn("中文日志内容", self.read_log())
        self.assertIn("追加日志内容", self.read_log())

    def test_environment_level_filters_records_despite_yaml_debug_level(self):
        self.run_logging("logger.info('不应记录信息'); logger.error('应记录错误')", DEER_FLOW_LOG_LEVEL=" warning ")
        self.assertNotIn("不应记录信息", self.read_log())
        self.assertIn("应记录错误", self.read_log())

    def test_blank_directory_and_level_use_runtime_home_and_info(self):
        self.run_logging("logger.info('默认配置日志')", DEER_FLOW_LOG_DIR="", DEER_FLOW_LOG_LEVEL="")
        contents = (self.directory / "runtime" / "logs" / "deerflow.log").read_text(encoding="utf-8")
        self.assertIn("默认配置日志", contents)

    def test_selected_env_file_controls_directory_and_debug_level(self):
        self.env_file.write_text(f'DEER_FLOW_LOG_DIR="{self.log_directory}"\nDEER_FLOW_LOG_LEVEL=DEBUG\n', encoding="utf-8")
        self.run_logging("logger.debug('env 文件中的调试级别')", DEER_FLOW_LOG_DIR=None, DEER_FLOW_LOG_LEVEL=None)
        self.assertIn("env 文件中的调试级别", self.read_log())
        self.run_logging("logger.debug('进程级别不应记录'); logger.info('进程环境优先')")
        self.assertNotIn("进程级别不应记录", self.read_log())
        self.assertIn("进程环境优先", self.read_log())

    def test_json_trace_and_url_redaction_remain_active(self):
        with self.config_path.open("a", encoding="utf-8") as config_file:
            config_file.write("logging:\n  enhance:\n    enabled: true\n    format: json\n")
        self.run_logging("""
from deerflow.trace_context import bind_trace_id
bind_trace_id('test_trace')
logging.getLogger('httpx').info('GET https://example.com/private?token=secret')
""")
        record = json.loads(self.read_log().splitlines()[-1])
        self.assertEqual(record["trace_id"], "test_trace")
        self.assertNotIn("secret", record["message"])
        self.assertNotIn("private", record["message"])


if __name__ == "__main__":
    unittest.main()
