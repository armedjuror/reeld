"""Small admin CLI for the suite.

    python -m suite.cli create-user --email you@example.com [--name Ajwad]
    python -m suite.cli context --series ballot-math --episode 3 --stage script
"""

from __future__ import annotations

import argparse
import json
import sys

from .context import STAGE_RECIPES, resolve
from .db import session_scope
from .models import Episode
from .package import get_series
from .users import create_user, current_user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="suite")
    sub = parser.add_subparsers(dest="cmd", required=True)

    cu = sub.add_parser("create-user", help="create (or find) a local user")
    cu.add_argument("--email", required=True)
    cu.add_argument("--name", default="")

    cx = sub.add_parser("context", help="print the resolved context for a stage")
    cx.add_argument("--series", required=True)
    cx.add_argument("--episode", type=int, required=True)
    cx.add_argument("--stage", required=True, choices=sorted(STAGE_RECIPES))
    cx.add_argument("--automation")
    cx.add_argument("--manifest", action="store_true", help="print the manifest JSON instead")

    args = parser.parse_args(argv)

    with session_scope() as s:
        if args.cmd == "create-user":
            user = create_user(s, args.email, args.name)
            print(f"user {user.id}: {user.email}")
            return 0

        series = get_series(s, current_user_id(s), args.series)
        if series is None:
            print(f"series '{args.series}' not found", file=sys.stderr)
            return 1
        ep = next((e for e in series.episodes if e.number == args.episode), None)
        if not isinstance(ep, Episode):
            print(f"episode {args.episode} not found", file=sys.stderr)
            return 1
        ctx = resolve(s, ep, args.stage, automation=args.automation)
        if args.manifest:
            print(json.dumps(ctx.manifest(), indent=2, default=str))
        else:
            print(ctx.render())
            if ctx.skill:
                print(f"--- {ctx.skill.name} ---\n{ctx.skill.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
