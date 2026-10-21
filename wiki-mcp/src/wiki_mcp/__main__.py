"""Command-line entry point.

wiki-mcp                 run the server (default)
wiki-mcp lint            print a lint report for the wiki volume and exit (non-zero on errors)
wiki-mcp hash-password   read a password from stdin and print its argon2id hash
wiki-mcp clients ...     list / revoke OAuth clients, switch an app's private access
                         (see `wiki-mcp clients --help`)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from wiki_mcp import __version__


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wiki-mcp", description=f"wiki-mcp {__version__}")
    parser.add_argument("--version", action="version", version=f"wiki-mcp {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server (default)")
    sub.add_parser("lint", help="lint the wiki and print a report")
    sub.add_parser("hash-password", help="hash a password read from stdin (argon2id)")
    clients = sub.add_parser("clients", help="manage OAuth clients and tokens")
    csub = clients.add_subparsers(dest="action", required=True)
    csub.add_parser("list", help="list registered clients and active grants")
    revoke = csub.add_parser("revoke", help="revoke all tokens of a client and forget it")
    revoke.add_argument("client_id")
    csub.add_parser("revoke-all", help="revoke every token (emergency)")
    private = csub.add_parser(
        "private", help="switch an app's private pages on, off, or back to what it consented"
    )
    private.add_argument("label", help="the app's label from `clients list` (e.g. chatgpt)")
    private.add_argument("access", choices=["on", "off", "default"])
    args = parser.parse_args(argv)

    if args.command == "hash-password":
        from wiki_mcp.auth.passwords import hash_password

        password = sys.stdin.readline().rstrip("\n")
        if len(password) < 12:
            print("password must be at least 12 characters", file=sys.stderr)
            return 2
        print(hash_password(password))
        return 0

    from wiki_mcp.config import Settings

    settings = Settings()
    _configure_logging(settings.log_level)

    if args.command == "lint":
        # Read-only: never starts the store, so it is safe next to a running server.
        from wiki_mcp.wiki.index import SearchIndex
        from wiki_mcp.wiki.lint import lint_bundle
        from wiki_mcp.wiki.store import WikiStore
        from wiki_mcp.wiki.types import TypeRegistry

        registry = TypeRegistry.load(settings.types_file)
        store = WikiStore(settings.wiki_dir, SearchIndex(":memory:"), registry)
        report = lint_bundle(
            list(store.all_markdown()), registry, stale_after_days=settings.stale_after_days
        )
        print(report.to_markdown())
        return 0 if report.ok else 1

    if args.command == "clients":
        from wiki_mcp.auth.admin import run_clients_command

        return run_clients_command(
            settings,
            args.action,
            getattr(args, "client_id", None),
            getattr(args, "label", None),
            getattr(args, "access", None),
        )

    from wiki_mcp.runtime import serve

    asyncio.run(serve(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
