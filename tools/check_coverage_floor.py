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

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

FLOOR: Final = 0.90

#: Packages to guard, as import paths under src/.
#:
#: Matched against each file's path resolved through coverage.xml's own
#: `<source>` element rather than against the raw `filename` attribute.
#: coverage writes filenames *relative to the source root*, so with
#: `source = ["src/rcm"]` they read `ingestion/x12/tokenizer.py` and a prefix
#: written as `rcm/ingestion/x12` matches nothing at all. The first version of
#: this script did exactly that and reported every package as "still a stub"
#: for as long as it existed -- a gate that passed because it was looking in
#: the wrong place.
GUARDED: Final[tuple[str, ...]] = (
    "rcm/decision",
    "rcm/ingestion/x12",
    "rcm/domain",
)

#: Below this many statements a package is still a stub and the ratio is noise.
MIN_STATEMENTS: Final = 20


def _has_real_code(package: str) -> bool:
    """Does `src/<package>` contain anything beyond empty `__init__` files?

    Used to tell "this package is still a stub" apart from "this script failed
    to find it", which look identical in a coverage report and are opposite
    conclusions.
    """
    directory = REPO_ROOT / "src" / package
    if not directory.exists():
        return False
    return any(
        path.name != "__init__.py" and path.stat().st_size > 0 for path in directory.rglob("*.py")
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)  # noqa: T201
        return 2
    report = Path(argv[1])
    if not report.exists():
        print(f"coverage report not found: {report}")  # noqa: T201
        return 1

    root = defusedxml.ElementTree.parse(report).getroot()
    sources = [(s.text or "").replace("\\", "/").rstrip("/") for s in root.iter("source")]
    totals: dict[str, list[int]] = {p: [0, 0] for p in GUARDED}

    for cls in root.iter("class"):
        filename = (cls.get("filename") or "").replace("\\", "/")
        # Resolve against every declared source root, so the match works
        # whether coverage wrote absolute paths, repo-relative paths, or
        # paths relative to the package it was pointed at.
        candidates = [filename] + [f"{source}/{filename}" for source in sources]
        for prefix in GUARDED:
            if any(prefix in candidate for candidate in candidates):
                for line in cls.iter("line"):
                    totals[prefix][1] += 1
                    if int(line.get("hits") or 0) > 0:
                        totals[prefix][0] += 1
                break

    failed = False
    for prefix, (covered, total) in totals.items():
        if total < MIN_STATEMENTS:
            # A package that has real code on disk but no statements in the
            # report means the report and this script disagree about paths,
            # not that the package is empty. Reporting "still a stub" in that
            # case is how this gate silently stopped being one.
            if _has_real_code(prefix):
                print(  # noqa: T201
                    f"  FAIL  {prefix:<24} has code on disk but {total} statements in "
                    f"{report.name}.\n"
                    "        The coverage report and this script disagree about paths; "
                    "the gate is not running."
                )
                failed = True
                continue
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
