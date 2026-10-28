"""从随包模板初始化调用方目录，不依赖模型配置或框架运行时。"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Sequence
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path


def _read_source_templates() -> list[tuple[Path, bytes]]:
    import tomllib

    # 源码与可编辑安装直接复用仓库模板，避免维护重复的配置副本。
    source = Path(__file__).resolve().parent.parent
    if not (source / "pyproject.toml").is_file():
        raise FileNotFoundError("安装包中缺少初始化模板，请重新安装 deerflow-harness")
    configuration = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    included = configuration["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    templates = []
    # 本地目录含有运行数据；源码模式也只读取打包清单声明的文件。
    for original, destination in sorted(included.items()):
        if destination.startswith("deerflow/_templates/"):
            relative = destination.removeprefix("deerflow/_templates/")
            output = {".env.example": ".env", "config.example.yaml": "config.yaml"}.get(relative, relative)
            templates.append((Path(output), (source / original).read_bytes()))
    return templates


def _read_tree(root: Traversable, relative: Path) -> list[tuple[Path, bytes]]:
    templates = []
    for child in sorted(root.iterdir(), key=lambda entry: entry.name):
        child_path = relative / child.name
        if child.is_dir():
            templates.extend(_read_tree(child, child_path))
        elif child.is_file():
            templates.append((child_path, child.read_bytes()))
    return templates


def _read_templates() -> list[tuple[Path, bytes]]:
    root = files("deerflow").joinpath("_templates")
    if not root.is_dir():
        return _read_source_templates()
    templates = [
        (Path(".env"), root.joinpath(".env.example").read_bytes()),
        (Path("config.yaml"), root.joinpath("config.example.yaml").read_bytes()),
    ]
    templates.extend(_read_tree(root.joinpath(".deer-flow"), Path(".deer-flow")))
    return templates


def _validate_directory(directory: Path) -> None:
    for candidate in (directory, *directory.parents):
        if candidate.is_symlink():
            raise ValueError(f"目标目录包含符号链接：{candidate}")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError(f"目标路径应为目录：{candidate}")


def _validate_destinations(target: Path, templates: list[tuple[Path, bytes]]) -> None:
    for relative, _ in templates:
        destination = target / relative
        _validate_directory(destination.parent)
        if destination.is_symlink():
            raise ValueError(f"目标文件不能是符号链接：{destination}")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"目标路径应为普通文件：{destination}")


def _create_file(destination: Path, contents: bytes) -> str:
    try:
        # 独占创建防止检查后出现的文件被默认覆盖。
        with destination.open("xb") as stream:
            stream.write(contents)
    except FileExistsError:
        if destination.is_symlink() or not destination.is_file():
            raise ValueError(f"目标路径应为普通文件且不能是符号链接：{destination}") from None
        return "跳过"
    return "创建"


def _replace_file(destination: Path, contents: bytes) -> str:
    existed = destination.exists()
    temporary_path = None
    try:
        # 先完整写入再替换，避免覆盖时留下被截断的配置。
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(contents)
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return "覆盖" if existed else "创建"


def initialize_project(target: Path, *, force: bool = False) -> list[tuple[str, Path]]:
    target = target.expanduser().resolve()
    templates = _read_templates()
    # 在任何写入前检查整组目标，避免类型冲突导致只初始化一部分配置。
    _validate_destinations(target, templates)
    results = []
    for relative, contents in templates:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        status = _replace_file(destination, contents) if force else _create_file(destination, contents)
        results.append((status, destination))
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deerflow init", description="从模板创建 .env、config.yaml 和 .deer-flow 初始目录")
    parser.add_argument("--target", type=Path, default=Path.cwd(), metavar="目录", help="目标目录，默认为当前工作目录")
    parser.add_argument("--force", action="store_true", help="覆盖模板对应的已有文件，保留其他文件")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        results = initialize_project(arguments.target, force=arguments.force)
    except (OSError, ValueError) as error:
        print(f"初始化失败：{error}", file=sys.stderr)
        return 1
    print(f"初始化目录：{arguments.target.expanduser().resolve()}")
    for status, destination in results:
        print(f"{status}：{destination}")
    return 0
