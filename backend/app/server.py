"""Uvicorn entry point for the optional BugLens HTTP/SSE adapter."""

from __future__ import annotations

import argparse
import atexit

from .bootstrap import build_local_service
from .config import Settings
from .web import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the BugLens HTTP/SSE server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised by packaging users
        raise SystemExit(
            "The web server extra is required: python -m pip install 'buglens[web]'"
        ) from exc

    settings = Settings.from_env()
    service, _store, runner = build_local_service(settings)
    atexit.register(runner.close)
    uvicorn.run(
        create_app(
            service,
            cors_origin=settings.cors_origin,
            admin_token=settings.admin_token,
        ),
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
