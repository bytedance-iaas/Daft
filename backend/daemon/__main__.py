"""``python -m daemon`` (the image entry point ``curator-daemon``): configure, then serve.

Exit codes: 0 after a clean shutdown, 2 when the configuration is unusable -
most importantly a missing or malformed master key (fail closed, design doc 08 §2).
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import logconfig
from .masterkey import MasterKeyError, scrub_environment
from .settings import ConfigError, Settings

EXIT_CONFIG = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m daemon",
                                     description="Curator v2 API Daemon")
    parser.add_argument("--host", help="listen address (CURATOR_HOST, default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="listen port (CURATOR_PORT, default 8080)")
    parser.add_argument("--check-config", action="store_true",
                        help="validate the configuration and exit")
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env()
    except (ConfigError, MasterKeyError) as err:
        print(f"curator-daemon: 启动失败：{err}", file=sys.stderr)
        return EXIT_CONFIG
    scrub_environment()                    # CLI children must never inherit key material
    logconfig.configure(settings.log_level, settings.log_format)
    log = logging.getLogger("daemon")
    if args.check_config:
        log.info("configuration ok: base_path=%r data_dir=%s", settings.base_path, settings.data_dir)
        return 0

    import uvicorn

    from .app import create_app

    try:
        app = create_app(settings)
    except (ConfigError, MasterKeyError) as err:
        print(f"curator-daemon: 启动失败：{err}", file=sys.stderr)
        return EXIT_CONFIG
    runtime = app.state.runtime

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            # end SSE streams first, or uvicorn would wait for them before shutting down
            runtime.begin_shutdown()
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host=args.host or settings.host, port=args.port or settings.port,
                            log_config=None, proxy_headers=True, forwarded_allow_ips="*",
                            timeout_graceful_shutdown=15)
    Server(config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
