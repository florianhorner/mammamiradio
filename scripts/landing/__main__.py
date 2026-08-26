"""Command-line entry point for repository landing policy."""

from __future__ import annotations

import argparse
import sys

from .errors import LandingError
from .evidence import emit_v2, verify_v2
from .gitops import GitRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.landing")
    commands = parser.add_subparsers(dest="command", required=True)
    evidence = commands.add_parser("evidence", help="emit or verify pre-ship review evidence")
    actions = evidence.add_subparsers(dest="evidence_action", required=True)

    emit = actions.add_parser("emit", help="emit an immutable v2 review receipt")
    emit.add_argument("--target", default="HEAD", help="reviewed commit (must be checked-out HEAD)")

    verify = actions.add_parser("verify", help="verify v2 evidence from trusted base code")
    verify.add_argument("--target", required=True, help="commit whose content must be reviewed")
    verify.add_argument("--base", help="current PR base commit (required in PR mode)")
    verify.add_argument("--mode", required=True, choices=("pr", "main"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        repo = GitRepository.discover()
        if args.evidence_action == "emit":
            path, created = emit_v2(repo, target=args.target)
            verb = "wrote" if created else "already matches"
            print(f"landing-evidence: {verb} {path.as_posix()}")
            return 0

        result = verify_v2(
            repo,
            target=args.target,
            base=args.base,
            mode=args.mode,
        )
        print(
            "landing-evidence: OK — "
            f"{result.mode} content {result.content_sha256} has "
            f"{len(result.matching_receipts)} matching v2 receipt(s)"
        )
        return 0
    except LandingError as exc:
        print(f"landing-evidence: FAIL — {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
