"""Command-line entry point.

portal            run the portal (default)
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from portal import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="portal", description=f"portal {__version__}")
    parser.add_argument("--version", action="version", version=f"portal {__version__}")
    parser.parse_args(argv)

    from portal.app import build_app
    from portal.config import Settings

    settings = Settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("portal").info("portal at %s", settings.portal_url)
    uvicorn.run(
        build_app(settings),
        host=settings.host,
        port=settings.port,
        proxy_headers=False,  # the client IP comes from wiki_mcp.net with trusted proxies only
        server_header=False,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
