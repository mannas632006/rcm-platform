"""Coverage floor on the packages where a bug is expensive (CLAUDE.md section 7.1).

    "90% line coverage on src/decision, src/ingestion/x12, src/domain"

Those three are singled out because a silent defect in any of them writes a wrong
number rather than raising: a parser that drops a CAS segment, a rule that
misreads a group code, a calculation that rounds the wrong way. Coverage is a
weak signal in general, but on code that is pure transformation of money and
codes it is a reasonable floor.

The floor applies only once a package contains executable statements. An empty
package reports 100% and the gate would be theatre; a package that has lost its
tests reports 0% and the gate catches it.

Usage: python tools/check_coverage_floor.py coverage.xml
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import defusedxml.ElementTree

FLOOR: Final = 0.90

#: Package path prefixes, as they appear in coverage.xml filenames.
GUARDED: Final[tuple[str, ...]] = (
    "rcm/decision",
    "rcm/ingestion/x12",
    "rcm/domain",
)

#: Below this many statements a package is still a stub and the ratio is noise.
MIN_STATEMENTS: Final = 20


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)  # noqa: T201
        return 2
    report = Path(argv[1])
    if not report.exists():
        print(f"coverage report not found: {report}")  # noqa: T201
        return 1

    root = defusedxml.ElementTree.parse(report).getroot()
    totals: dict[str, list[int]] = {p: [0, 0] for p in GUARDED}

    for cls in root.iter("class"):
        filename = (cls.get("filename") or "").replace("\\", "/")
        for prefix in GUARDED:
            if prefix in filename:
                for line in cls.iter("line"):
                    totals[prefix][1] += 1
                    if int(line.get("hits") or 0) > 0:
                        totals[prefix][0] += 1
                break

    failed = False
    for prefix, (covered, total) in totals.items():
        if total < MIN_STATEMENTS:
            print(f"  skip  {prefix:<24} {total} statements -- still a stub")  # noqa: T201
            continue
        ratio = covered / total
        mark = "OK  " if ratio >= FLOOR else "FAIL"
        print(f"  {mark}  {prefix:<24} {ratio:6.1%}  ({covered}/{total})")  # noqa: T201
        if ratio < FLOOR:
            failed = True

    if failed:
        print(f"\ncoverage floor is {FLOOR:.0%} on these packages (CLAUDE.md section 7.1)")  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
