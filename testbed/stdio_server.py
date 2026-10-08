"""Run ONE fleet member over stdio.

    python -m testbed.stdio_server --servers 10 --index 3 [--tools 100]
    python -m testbed.stdio_server --spec-file spec.json

``--spec-file`` holds ``asdict(ServerSpec)`` JSON; tests rewrite it between
discoveries to simulate a server adding/removing/changing tools.

Note: the subprocess inherits only the SDK's default environment, so the
registering side must pass ``PYTHONPATH=<repo root>`` in the server's env.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from testbed.fleet import generate_fleet, spec_from_dict
from testbed.servers import build_server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testbed.stdio_server")
    ap.add_argument("--servers", type=int, default=10)
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--tools", type=int, default=None)
    ap.add_argument("--spec-file", type=Path, default=None)
    args = ap.parse_args(argv)
    if args.spec_file is not None:
        spec = spec_from_dict(json.loads(args.spec_file.read_text()))
    else:
        spec = generate_fleet(args.servers, tools=args.tools)[args.index]
    build_server(spec).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
