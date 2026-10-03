"""CLI entrypoint used by the Electron sidecar supervisor."""

from __future__ import annotations

import argparse
import logging

from covalent_desktop.bootstrap import run_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="covalent-desktop-service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve = subparsers.add_parser("serve", help="Start the authenticated loopback service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.command == "serve":
        return run_service(host=args.host, port=args.port)
    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
