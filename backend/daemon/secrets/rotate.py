"""``python -m daemon.secrets.rotate`` - re-seal stored secrets with the next master key.

Design doc 08, section 2.1 (the ``curator-admin rotate-master-key`` of the design; the
console-script name needs a ``pyproject.toml`` entry, see the W8 report). In the pod:

1. put the new key into the Secret as ``masterKeyNext`` (``CURATOR_MASTER_KEY_NEXT``) and
   restart - the Daemon now opens rows of both versions and seals new ones with the new key;
2. ``kubectl exec <pod> -- python -m daemon.secrets.rotate`` - re-seals every older row;
   interrupted, it is simply run again (exit 0 = complete);
3. move the new key to ``masterKey``, set ``CURATOR_MASTER_KEY_VERSION`` to the printed
   ``target_version``, remove ``masterKeyNext`` and restart.

It runs next to the Daemon on the same database (SQLite serializes the writers, each row
is re-sealed in its own transaction) and does not take the Daemon's instance lock.
Exit codes: 0 complete, 1 rows that did not open (ids printed), 2 configuration error.
"""
from __future__ import annotations

import argparse
import json
import sys

from ..masterkey import MasterKeyError
from ..settings import ConfigError, Settings
from .sealing import rotate

EXIT_INCOMPLETE = 1
EXIT_CONFIG = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m daemon.secrets.rotate",
        description="Re-seal stored secrets with CURATOR_MASTER_KEY_NEXT (design doc 08 §2.1)")
    parser.add_argument("--batch", type=int, default=100, help="rows per batch (default 100)")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except (ConfigError, MasterKeyError) as err:
        print(f"rotate-master-key: 配置不对：{err}", file=sys.stderr)
        return EXIT_CONFIG
    master_key = settings.master_key
    if master_key.next_key is None:
        print("rotate-master-key: 没有配置 CURATOR_MASTER_KEY_NEXT：先把新主密钥写进 "
              "masterKeyNext 并重启，再执行轮换", file=sys.stderr)
        return EXIT_CONFIG

    from ..repo.sqlite import SqliteRepository

    if not settings.db_path.is_file():
        print(f"rotate-master-key: 数据库不存在：{settings.db_path}", file=sys.stderr)
        return EXIT_CONFIG
    repo = SqliteRepository(settings.db_path)
    try:
        report = rotate(repo, master_key, batch=args.batch,
                        on_row=lambda cred_id: print(f"re-sealed {cred_id}", file=sys.stderr))
    finally:
        repo.close()
    print(json.dumps(report.to_json(), ensure_ascii=False))
    if report.failed:
        print(f"rotate-master-key: {len(report.failed)} 条密钥用两把主密钥都解不开，"
              "没有轮换；请重新填写这些密钥后再执行", file=sys.stderr)
    return 0 if report.complete else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
