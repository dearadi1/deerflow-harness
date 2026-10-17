#!/usr/bin/env python3
"""通过稳定的子目录历史拆分，让本地定制能够正常合并上游更新。"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


TRACKING_BRANCH = "chore/upstream_module"


def git(directory, *arguments, check=True):
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def resolve_repository():
    root = Path(__file__).resolve().parents[1]
    if git(root, "status", "--porcelain").stdout.strip():
        raise RuntimeError("工作区有未提交修改，请先提交或暂存后再同步。")
    branch = git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    if branch == TRACKING_BRANCH:
        raise RuntimeError("请切换到自己的开发分支或主分支后再运行同步。")
    return root


def read_settings(root):
    return json.loads((root / "UPSTREAM.json").read_text(encoding="utf-8"))


def prepare_cache(root, settings, arguments):
    common_directory = git(root, "rev-parse", "--git-common-dir").stdout.strip()
    cache = arguments.cache or root / common_directory / "upstream-source"
    cache = cache.resolve()
    source_url = arguments.upstream_url or settings["repository"]
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        git(cache.parent, "clone", "--filter=blob:none", "--no-checkout",
            "--single-branch", "--branch", settings["branch"], source_url, str(cache))
    actual_root = git(cache, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(actual_root).resolve() != cache:
        raise RuntimeError("缓存路径必须指向独立的上游 Git 检出。")
    actual_url = git(cache, "remote", "get-url", "origin").stdout.strip()
    if actual_url != source_url:
        raise RuntimeError("缓存的 origin 与指定上游不一致，请选择另一缓存目录。")
    return cache


def fetch_source(cache, settings):
    reference = f"refs/heads/{settings['branch']}:refs/remotes/origin/{settings['branch']}"
    git(cache, "fetch", "--no-tags", "origin", reference)
    source_commit = git(cache, "rev-parse", f"origin/{settings['branch']}").stdout.strip()
    git(cache, "cat-file", "-e", f"{source_commit}:{settings['prefix']}/deerflow")
    return source_commit


def verify_license(root, cache, settings, source_commit):
    upstream_license = subprocess.check_output(
        ["git", "-C", str(cache), "show", f"{source_commit}:LICENSE"]
    )
    expected = settings["license_sha256"]
    if hashlib.sha256(upstream_license).hexdigest() != expected:
        raise RuntimeError("上游许可证已变化，请审查 LICENSE 和 UPSTREAM.json 后再同步。")
    if hashlib.sha256((root / "LICENSE").read_bytes()).hexdigest() != expected:
        raise RuntimeError("本地许可证与记录不一致，请先检查 LICENSE。")


def is_ancestor(root, older, newer):
    result = git(root, "merge-base", "--is-ancestor", older, newer, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip())
    return result.returncode == 0


def import_snapshot(root, cache, settings, source_commit):
    print("正在拆分模块历史；大型上游首次处理可能需要数分钟。", flush=True)
    snapshot = git(cache, "subtree", "split", f"--prefix={settings['prefix']}",
                   source_commit).stdout.strip().splitlines()[-1]
    git(root, "fetch", "--no-tags", str(cache), snapshot)
    reference = f"refs/heads/{TRACKING_BRANCH}"
    previous = git(root, "rev-parse", "--verify", reference, check=False)
    baseline = previous.stdout.strip() if previous.returncode == 0 else settings["initial_split_commit"]
    if not is_ancestor(root, baseline, snapshot):
        raise RuntimeError("拆分历史无法快进；可能发生上游历史重写或拆分规则变化，请人工检查。")
    if not is_ancestor(root, settings["initial_split_commit"], "HEAD"):
        raise RuntimeError("当前分支不包含初始模块历史，无法安全合并。")
    expected = previous.stdout.strip() if previous.returncode == 0 else "0" * 40
    git(root, "update-ref", reference, snapshot, expected)
    return snapshot


def merge_snapshot(root, settings, source_commit, snapshot):
    if is_ancestor(root, snapshot, "HEAD"):
        print("当前分支已包含全部模块更新，无需创建提交。")
        return
    branch = f"chore/sync_upstream_{source_commit[:12]}"
    git(root, "switch", "-c", branch)
    message = ("chore: 同步 DeerFlow 上游模块\n\n"
               f"Upstream-Commit: {source_commit}\n"
               f"Upstream-Path: {settings['prefix']}\n"
               f"Upstream-Split: {snapshot}")
    result = git(root, "merge", "--no-ff", "-m", message, snapshot, check=False)
    if result.returncode:
        print(result.stdout + result.stderr, file=sys.stderr)
        raise RuntimeError("合并未完成。解决冲突后执行 git add 和 git commit；或执行 git merge --abort。")
    print(f"同步已完成，当前分支：{branch}")
    print("检查差异并测试后，推送分支并创建 PR；请使用合并提交保留上游历史。")
    print(f"git push -u origin {branch}")
    print(f"git push origin {TRACKING_BRANCH}")


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="复用专用的上游检出缓存")
    parser.add_argument("--upstream-url", help="覆盖上游地址，可用于 SSH 443 或离线验证")
    return parser.parse_args()


def main():
    arguments = parse_arguments()
    root = resolve_repository()
    settings = read_settings(root)
    print("正在获取上游更新……", flush=True)
    cache = prepare_cache(root, settings, arguments)
    source_commit = fetch_source(cache, settings)
    verify_license(root, cache, settings, source_commit)
    snapshot = import_snapshot(root, cache, settings, source_commit)
    print(f"上游提交：{source_commit}\n模块提交：{snapshot}")
    merge_snapshot(root, settings, source_commit, snapshot)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print(f"同步停止：{error}", file=sys.stderr)
        sys.exit(1)
