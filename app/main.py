"""Command-line entry point for the packaged service."""

from .config import Settings


def main() -> None:
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(
        "app.api:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )
