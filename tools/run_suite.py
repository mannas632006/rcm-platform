"""Run a test suite that may not have any tests yet (CLAUDE.md section 7.4).

pytest exits 5 when it collects nothing. For `tests/property` and
`tests/integration` that is the current, correct state: the property tests
belong to the X12 parser (M1.1) and the integration tests to the claim
lifecycle (M3), and neither subject exists. A CI job that fails on exit 5
blocks every merge until M1; a job that swallows it stops being a gate the
moment someone's test file fails to import.

So the suite is run through here, which distinguishes the two cases:

- **No test files on disk, and the subject has not been built.** Expected.
  Reported and passed.
- **No test files on disk, but the subject now exists.** The suite was
  supposed to arrive with it. Failed, naming what is missing.
- **Test files exist but nothing was collected.** A collection error, an
  import failure, or every test deselected by a marker. Failed -- this is the
  case that makes a blanket exit-5 exemption dangerous.
- **Anything else.** pytest's own exit code, unchanged.

Usage:
    python tools/run_suite.py tests/property --subject src/rcm/ingestion/x12 --pytest-args "-q"
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

#: pytest's "no tests were collected" exit code.
EXIT_NO_TESTS_COLLECTED: Final = 5


def _test_files(suite: Path) -> list[Path]:
    return sorted(p for p in suite.rglob("test_*.py") if p.is_file())


def _subject_built(subject: Path) -> bool:
    """Does the package under test contain anything beyond its `__init__`?"""
    if not subject.exists():
        return False
    if subject.is_file():
        return subject.stat().st_size > 0
    return any(p.name != "__init__.py" for p in subject.rglob("*.py"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run_suite")
    parser.add_argument("suite", help="suite directory, e.g. tests/property")
    parser.add_argument(
        "--subject",
        required=True,
        help="the package this suite tests; once it has code, the suite must have tests",
    )
    parser.add_argument("--milestone", default="", help="milestone that introduces the suite")
    parser.add_argument(
        "--pytest-args",
        default="",
        help='arguments passed through to pytest, as one string, e.g. "-q -m integration"',
    )
    args = parser.parse_args(argv[1:])
    pytest_args = shlex.split(args.pytest_args)

    suite = REPO_ROOT / args.suite
    subject = REPO_ROOT / args.subject
    files = _test_files(suite)
    label = f" ({args.milestone})" if args.milestone else ""

    if not files:
        if _subject_built(subject):
            print(  # noqa: T201
                f"FAIL {args.suite} has no tests, but {args.subject} now contains code.\n"
                f"     The suite{label} was supposed to arrive with its subject.\n"
                f"     CLAUDE.md section 7.1 lists it as required, not optional."
            )
            return 1
        print(  # noqa: T201
            f"skip {args.suite}: no tests yet, and {args.subject} is still a stub{label}"
        )
        return 0

    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", str(suite), *pytest_args],
        cwd=REPO_ROOT,
        check=False,
    )
    if completed.returncode == EXIT_NO_TESTS_COLLECTED:
        print(  # noqa: T201
            f"FAIL {args.suite} contains {len(files)} test file(s) but pytest collected nothing.\n"
            "     Usually an import error, or every test deselected by a marker."
        )
        return 1
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
