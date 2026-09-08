from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mpc_demo.server import find_available_port, run
else:
    from .server import find_available_port, run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="储售集合体三层随机 MPC Demo")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="启动本地可视化页面")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if not raw_argv:
        raw_argv = ["serve"]
    args = build_parser().parse_args(raw_argv)
    if args.command == "serve":
        port = find_available_port(args.host, args.port)
        if port != args.port:
            print("端口 %d 已被占用，改用 %d" % (args.port, port))
        run(args.host, port)


if __name__ == "__main__":
    main()
