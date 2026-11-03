"""CLI: python -m standard_iron fixtures/yard.json"""

from __future__ import annotations

import sys
from pathlib import Path

from standard_iron.yard import format_details, format_table, load_yard, run_yard, to_json

_FLAGS = {"--verbose", "--json", "--help", "-h"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [arg for arg in args if arg.startswith("-") and arg not in _FLAGS]
    if unknown:
        print(f"unknown flag: {unknown[0]}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2
    paths = [arg for arg in args if arg not in _FLAGS]
    if "--help" in args or "-h" in args:
        print(_usage())
        return 0 if len(paths) == 0 else 2
    if len(paths) != 1:
        print(_usage(), file=sys.stderr)
        return 2
    try:
        yard = load_yard(Path(paths[0]))
        report = run_yard(yard)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if "--json" in args:
        print(to_json(report), end="")
    else:
        print(format_table(report))
        if "--verbose" in args:
            print()
            print(format_details(report))
    return 0


def _usage() -> str:
    return "usage: python -m standard_iron [--verbose] [--json] <yard.json>"


if __name__ == "__main__":
    raise SystemExit(main())
