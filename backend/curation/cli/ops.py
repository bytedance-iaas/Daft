"""The ops commands: ``curation ls``, ``curation prune``, ``curation fetch`` (design doc 27 §1, D89).

They are for a person at a terminal in the Pod, not for the Daemon: the output is Chinese text, not a C2
document, and nothing calls them. Exit codes: 0 done, 1 a remote error (TOS, network), 2 bad input.

* ``ls``: one level of a local directory or a ``tos://bucket/prefix`` (directories first, files with their size),
  read with this instance's TOS key;
* ``prune``: the runs under a delivery directory (``<run_id>/`` with the result revisions, design doc 06 §1) with
  their time, episode counts and size; ``--keep-latest N --yes`` deletes the older ones. The run ``latest`` names is
  never deleted, nor is anything outside the run directories;
* ``fetch``: a public dataset from the intranet HuggingFace cache (through oniond) into this site's dataset root
  (``curation/fetch.py``).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys

OPS_COMMANDS = ("ls", "prune", "fetch")

#: a run directory of a delivery: the Daemon's run id, ``YYYYMMDD-HHMMSS`` or ``…-2``, ``…-3`` (orchestr/delivery.py)
_RUN_ID = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")
_REVISION = re.compile(r"^r(\d{4})$")
LATEST = "latest"
#: files one level of ``ls`` lists at most (directories are not capped): hundreds are normal, more is an accident
LS_FILE_CAP = 200


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _tos_error(e: Exception) -> str:
    """A TOS SDK error on one line: the SDK's str(e) is the whole response dict."""
    d = e.args[0] if getattr(e, "args", None) and isinstance(e.args[0], dict) else {}
    code = getattr(e, "code", None) or d.get("code") or ""
    msg = getattr(e, "message", None) or d.get("message") or ""
    plain = {"NoSuchBucket": "桶不存在", "AccessDenied": "本实例的 TOS 密钥没有这个桶的权限",
             "NoSuchKey": "对象不存在"}.get(code)
    if plain:
        return f"{plain}（{code}）"
    if code or msg:
        return f"{code}{'：' if code and msg else ''}{msg}"
    return str(e)[:200]


# ---------------------------------------------------------------- ls

def cmd_ls(path: str, region: str | None) -> int:
    if str(path).startswith("tos://"):
        from .. import tos_store

        try:
            bucket, prefix = tos_store.parse_tos_url(path)
        except Exception as e:  # noqa: BLE001 - a malformed address is bad input
            print(f"[输入错误] {e}", file=sys.stderr)
            return 2
        try:
            dirs, files = tos_store.make_store_for(bucket, region).list_dir(bucket, prefix)
        except Exception as e:  # noqa: BLE001 - network, permission, bucket name
            print(f"[TOS 错误] 列不动 {path}：{_tos_error(e)}", file=sys.stderr)
            return 1
    else:
        if not os.path.isdir(path):
            print(f"[输入错误] {path} 不是目录", file=sys.stderr)
            return 2
        dirs, files = [], []
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir():
                    dirs.append(entry.name)
                elif entry.is_file():
                    files.append((entry.name, entry.stat().st_size))
        dirs.sort()
        files.sort()
    if not dirs and not files:
        print("（空）")
        return 0
    for d in dirs:
        print(d + "/")
    for name, size in files[:LS_FILE_CAP]:
        print(f"{name}  {_human_size(size)}")
    if len(files) > LS_FILE_CAP:
        print(f"……还有 {len(files) - LS_FILE_CAP} 个文件未列出")
    tail = f"共 {len(dirs)} 个目录、{len(files)} 个文件"
    if files:
        tail += f"，文件合计 {_human_size(sum(s for _, s in files))}"
    print(tail)
    return 0


# ---------------------------------------------------------------- prune

def _run_time(run_id: str) -> str:
    d, t = run_id[:8], run_id[9:15]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"


def _counts(storage, run_id: str, keys: list[str]) -> dict | None:
    """The newest committed result revision's counts (report.json overview), or None."""
    revs = set()
    for key in keys:
        parts = key.split("/")
        m = _REVISION.match(parts[2]) if len(parts) >= 4 and parts[1] == "revisions" else None
        if m:
            revs.add(m.group(1))
    for rev in sorted(revs, reverse=True):
        base = f"{run_id}/revisions/r{rev}"
        if f"{base}/commit.json" not in keys or f"{base}/report.json" not in keys:
            continue
        try:
            report = json.loads(storage.read_bytes(f"{base}/report.json"))
        except Exception:  # noqa: BLE001 - an unreadable report leaves the counts unknown
            return None
        counts = (report.get("overview") or {}).get("counts") or {}
        return {"revision": int(rev), **{k: counts.get(k) for k in ("total", "passed", "rejected", "held")}}
    return None


def prune_plan(storage, keep_latest: int | None) -> dict:
    """The runs of a delivery directory, newest first, and which ones ``--keep-latest`` deletes."""
    if keep_latest is not None and keep_latest < 1:
        raise ValueError("--keep-latest 至少是 1")
    objects = storage.list("")
    latest = ""
    if LATEST in objects:
        latest = storage.read_bytes(LATEST).decode("utf-8", "replace").strip()
    by_run: dict[str, list[str]] = {}
    for key in objects:
        head = key.split("/", 1)[0]
        if "/" in key and _RUN_ID.match(head):
            by_run.setdefault(head, []).append(key)
    runs = []
    for run_id in sorted(by_run, reverse=True):
        keys = by_run[run_id]
        runs.append({"name": run_id, "at": _run_time(run_id), "is_latest": run_id == latest,
                     "complete": f"{run_id}/_COMPLETE" in objects,
                     "size": sum(objects[k].size for k in keys), "objects": len(keys),
                     "counts": _counts(storage, run_id, keys)})
    doomed = []
    if keep_latest is not None:
        doomed = [r for r in runs[keep_latest:] if not r["is_latest"]]
    return {"latest": latest, "runs": runs, "delete": doomed, "keys": by_run}


def _delete_run(storage, run_id: str, keys: list[str]) -> None:
    from .storage import LocalStorage

    if isinstance(storage, LocalStorage):
        shutil.rmtree(os.path.join(storage.root, run_id))
        return
    for key in keys:
        storage.delete(key)


def cmd_prune(delivery: str, keep_latest: int | None, yes: bool, region: str | None) -> int:
    """List the runs; delete the older ones only when told how many to keep and ``--yes``.

    Never a "newest replaces older" clean-up of its own (requester, 2026-08-14): which run is worth keeping only a
    person knows; the command lays out the facts (time, counts, size)."""
    from .errors import UsageError
    from .storage import is_remote, open_storage

    if yes and keep_latest is None:
        print("[输入错误] --yes 必须和 --keep-latest N 一起给：得先说清要删哪几次", file=sys.stderr)
        return 2
    if not is_remote(delivery) and not os.path.isdir(delivery):
        print(f"[输入错误] 目录不存在：{delivery}", file=sys.stderr)
        return 2
    try:
        storage = open_storage(delivery, role="output", region=region)
        plan = prune_plan(storage, keep_latest)
    except (UsageError, ValueError) as e:
        print(f"[输入错误] {getattr(e, 'message', None) or e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - network, permission, bucket name
        print(f"[TOS 错误] 列不动 {delivery}：{_tos_error(e)}", file=sys.stderr)
        return 1
    runs = plan["runs"]
    if not runs:
        print(f"{delivery}：没有跑批（交付目录下没有 <run_id>/ 子目录）")
        return 0
    doomed = {r["name"] for r in plan["delete"]}
    print(f"{delivery} 共 {len(runs)} 次跑批：")
    for r in runs:
        c = r["counts"]
        if c:
            facts = f"r{c['revision']:04d}：共 {c['total']} 条，通过 {c['passed']}、拒绝 {c['rejected']}、待补跑 {c['held']}"
        else:
            facts = "还没有结果版本" if r["complete"] is False else "条数未知"
        mark = "  ← latest" if r["is_latest"] else ""
        flag = "删除" if r["name"] in doomed else "保留"
        print(f"  [{flag}] {r['name']}  {r['at']}  {facts}  {_human_size(r['size'])}{mark}")
    if keep_latest is None:
        print("\n（默认只列不删。要删：加 --keep-latest N 看将删哪几次，确认后再加 --yes）")
        return 0
    if not doomed:
        print(f"\n留最新 {keep_latest} 次 → 没有要删的")
        return 0
    freed = _human_size(sum(r["size"] for r in plan["delete"]))
    if not yes:
        print(f"\n将删除上面标「删除」的 {len(doomed)} 次跑批，可释放 {freed}。确认无误就重跑一遍并加 --yes")
        return 0
    for r in plan["delete"]:
        try:
            _delete_run(storage, r["name"], plan["keys"][r["name"]])
        except Exception as e:  # noqa: BLE001 - say which run, keep the rest as they are
            print(f"[TOS 错误] 删 {r['name']} 失败：{_tos_error(e)}（之前的已删，之后的没动）", file=sys.stderr)
            return 1
        print(f"已删除 {r['name']}")
    print(f"共删除 {len(doomed)} 次跑批，释放 {freed}")
    return 0


# ---------------------------------------------------------------- fetch

def cmd_fetch(args) -> int:
    from ..fetch import run_fetch
    from ..pipeline.config import load_config

    return run_fetch(load_config(args.config), args.source, args.ref, into=args.into, name=args.name,
                     includes=args.include, overwrite=args.overwrite)


# ---------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="curation", description="运维子命令（给人在终端里用，输出是文字）",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("ls", help="列一个目录或 tos:// 地址下一层有什么",
                        description="列出本地目录或 tos://桶/前缀 下的一层内容：先目录后文件，文件带大小。"
                                    "凭证用本实例的 TOS 密钥，读不读得到由密钥权限决定。")
    ls.add_argument("path", metavar="地址", help="本地目录，或 tos://桶/前缀")
    ls.add_argument("--region", default=None, metavar="地区", help="tos:// 地址的桶地区（缺省读 TOS_REGION）")
    pr = sub.add_parser("prune", help="列出一份交付目录下的历次跑批，按需删掉旧的（默认只列不删）",
                        description="列出一份交付目录下的历次跑批（时间、最新结果版本的条数、占用），并按需删掉旧的几次。"
                                    "默认只列不删；真删要同时给 --keep-latest 与 --yes。latest 指的那次永远保留。")
    pr.add_argument("delivery", metavar="交付目录", help="本地路径或 tos://桶/前缀")
    pr.add_argument("--region", default=None, metavar="地区", help="交付在 tos:// 时桶的地区")
    pr.add_argument("--keep-latest", type=int, default=None, metavar="N",
                    help="留最新的 N 次，更早的删掉；不给就只列出、不删任何东西")
    pr.add_argument("--yes", action="store_true", help="真的删（必须同时给 --keep-latest）")
    fe = sub.add_parser("fetch", help="从数据来源站拉公开数据集到本站数据集根",
                        description="从内网 HuggingFace 缓存桶（经 oniond）拉公开数据集进本站数据集根，下完即可登记质检。"
                                    "先落本地暂存、逐文件校验后再顺序拷入。")
    fe.add_argument("--source", required=True, metavar="来源",
                    help="数据来源；目前是 ai-infra（内网 HuggingFace 缓存桶），站点配置 fetch_sources 可扩充")
    fe.add_argument("--ref", required=True, metavar="源站名", help="数据集在源站上的名字（如 libero）")
    fe.add_argument("--include", action="append", metavar="模式",
                    help="只下匹配的文件（可重复）；部分拉取的数据不完整，不会被当作可用数据集")
    fe.add_argument("--into", default=None, metavar="数据集根",
                    help="落到哪个数据集根目录；缺省用站点配置第一个 tos_buckets 的 datasets_path")
    fe.add_argument("--name", default=None, metavar="落地名", help="落地目录名（缺省与 --ref 同名）")
    fe.add_argument("--overwrite", action="store_true", help="同名数据集已存在时覆盖重下（缺省跳过）")
    fe.add_argument("--config", default=None, help="站点配置（叠加到出厂默认；缺省读 CURATION_CONFIG）")
    return p


def main(argv: list[str]) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:                        # --help, or an argument error (argparse printed it)
        return e.code if isinstance(e.code, int) else 0
    if args.command == "ls":
        return cmd_ls(args.path, args.region)
    if args.command == "prune":
        return cmd_prune(args.delivery, args.keep_latest, args.yes, args.region)
    return cmd_fetch(args)
