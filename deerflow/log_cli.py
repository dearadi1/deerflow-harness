"""离线查询服务日志，不启动 Agent、数据库或模型。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from deerflow.log_files import get_log_directory

_FILTER_FIELDS = ("thread_id", "component", "skill_name", "category", "operation", "level", "status")
_TEXT_FIELDS = (
    "user_id", "thread_id", "component", "operation", "status", "skill_name", "category",
    "source_user_id", "target_user_id", "file_path", "new_path", "area", "enabled",
    "byte_count", "count", "duration_ms", "trace_id",
)
_STRING_FIELDS = ("timestamp", "user_id", "source_user_id", "target_user_id", *_FILTER_FIELDS)


def _date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return value
    except ValueError as error:
        raise argparse.ArgumentTypeError("日期格式必须为 YYYY-MM-DD") from error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deerflow logs", description="按日期、用户、线程或技能筛选本地日志")
    commands = parser.add_subparsers(dest="command", required=True)
    for command, description in (("threads", "列出出现过日志的线程"), ("show", "查看相关日志"), ("skills", "列出出现过日志的技能")):
        query = commands.add_parser(command, help=description)
        _add_query_arguments(query)
    return parser


def _add_query_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", required=True, type=_date, help="服务所在时区的日期 YYYY-MM-DD")
    parser.add_argument("--user-id", help="精确匹配用户；技能复制也匹配来源与目标用户")
    parser.add_argument("--thread-id", help="精确匹配线程")
    parser.add_argument("--component", choices=("agent", "thread", "skill"))
    parser.add_argument("--skill-name", help="精确匹配技能名称")
    parser.add_argument("--category", choices=("custom", "public"))
    parser.add_argument("--operation", help="精确匹配操作名称，例如 upload_file、managed_stream")
    parser.add_argument("--level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), type=str.upper, help="精确匹配等级")
    parser.add_argument("--status", choices=("started", "completed", "failed", "cancelled"))
    parser.add_argument("--log-dir", type=Path, help="覆盖 DEER_FLOW_LOG_DIR / DEER_FLOW_HOME/logs")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path, help="导出到文件；省略时直接打印")


def _shorthand(arguments: list[str]) -> list[str]:
    if arguments and arguments[0][:1].isdigit():
        if len(arguments) < 2 or arguments[1].startswith("--"):
            return ["threads", "--date", *arguments]
        day, user_id, *remaining = arguments
        if remaining and not remaining[0].startswith("--"):
            return ["show", "--date", day, "--user-id", user_id, "--thread-id", *remaining]
        return ["threads", "--date", day, "--user-id", user_id, *remaining]
    return arguments


def _matches(record: dict[str, Any], options: argparse.Namespace) -> bool:
    if options.user_id is not None and options.user_id not in (record.get("user_id"), record.get("source_user_id"), record.get("target_user_id")):
        return False
    for name in _FILTER_FIELDS:
        value = getattr(options, name)
        if value is not None and record.get(name) != value:
            return False
    return True


def _records(source: Path, options: argparse.Namespace) -> Iterator[dict[str, Any]]:
    malformed = 0
    with source.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or any(record.get(field) is not None and not isinstance(record[field], str) for field in _STRING_FIELDS):
                    raise ValueError
            except ValueError:
                malformed += 1
                continue
            if _matches(record, options):
                yield record
    if malformed:
        print(f"跳过 {malformed} 条损坏或尚未写完的日志", file=sys.stderr)


@contextmanager
def _destination(output: Path | None, source: Path) -> Iterator[TextIO]:
    if output is None:
        yield sys.stdout
        return
    output = output.expanduser().resolve()
    if output == source.resolve() or (output.exists() and output.samefile(source)):
        raise ValueError("导出路径不能覆盖源日志文件")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yield stream
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _summary(records: Iterator[dict[str, Any]], command: str) -> list[dict[str, Any]]:
    grouped: dict[tuple, dict[str, Any]] = {}
    fields = ("user_id", "thread_id") if command == "threads" else ("user_id", "category", "skill_name")
    required = "thread_id" if command == "threads" else "skill_name"
    for record in records:
        if not record.get(required):
            continue
        key = tuple(record.get(field) for field in fields)
        item = grouped.setdefault(key, {**dict(zip(fields, key)), "count": 0, "errors": 0, "first": record.get("timestamp", ""), "last": ""})
        timestamp = record.get("timestamp", "")
        item["first"] = min(item["first"], timestamp)
        item["last"] = max(item["last"], timestamp)
        item["count"] += 1
        item["errors"] += record.get("level") in {"ERROR", "CRITICAL"}
    return sorted(grouped.values(), key=lambda item: tuple(str(item.get(field) or "") for field in fields))


def _text_record(record: dict[str, Any]) -> str:
    identity = " ".join(f"{field}={record[field]}" for field in _TEXT_FIELDS if record.get(field) is not None)
    parts = [f"{record.get('timestamp', '')} {record.get('level', '')} {record.get('logger', '')} [{identity}] {record.get('message', '')}"]
    parts.extend(str(record[field]) for field in ("exc_info", "stack_info") if record.get(field))
    return "\n".join(parts)


def _write_results(source: Path, options: argparse.Namespace, output: TextIO) -> None:
    records = _records(source, options)
    if options.command == "show":
        for record in records:
            print(json.dumps(record, ensure_ascii=False) if options.format == "json" else _text_record(record), file=output)
        return
    for item in _summary(records, options.command):
        if options.format == "json":
            print(json.dumps(item, ensure_ascii=False), file=output)
        else:
            print("\t".join(f"{name}={value}" for name, value in item.items()), file=output)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(_shorthand(list(sys.argv[1:] if argv is None else argv)))
    try:
        from deerflow.env import load_selected_env_file
        from dotenv import load_dotenv

        if not load_selected_env_file():
            load_dotenv()
        source = get_log_directory(options.log_dir) / f"deerflow-{options.date}.jsonl"
        with _destination(options.output, source) as output:
            _write_results(source, options, output)
    except (OSError, ValueError) as error:
        print(f"日志查询失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
