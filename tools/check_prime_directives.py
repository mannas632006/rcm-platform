"""Grep-enforceable prime directives (CLAUDE.md sections 1 and 6.2).

Some invariants are properties of the *source text*, not of any runtime state,
and those are the ones worth enforcing mechanically:

    PD-16  Modifier 25 and 59 are never auto-applied.
    PD-17  No auto-submit code path for AI-assigned codes exists in the repo.
    S-11   Parameterised queries only; no string-built SQL.
    T20    No print(); output goes through structlog so the scrubber sees it.

CLAUDE.md section 1 calls PD-17 "grep-based CI check" outright. The point is that
a reviewer can forget and a test can be deleted, but a check that reads every
line of the repository on every push cannot be talked out of it.

A match is not automatically a violation -- the modules that *implement* a
refusal necessarily name the thing they refuse. Those are allowed by path, and
each allowance is written down here rather than hidden in a regex, so that
widening one is a visible diff.

Exit code 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

SCANNED_ROOTS: Final = ("src", "tools", "migrations")
SCANNED_SUFFIXES: Final = frozenset({".py", ".sql", ".yaml", ".yml"})

#: Directories never scanned: generated, vendored or virtual.
#: Directories whose contents are not source and must not be scanned.
#:
#: `target`, `logs` and `dbt_packages` are dbt's build output. They matter
#: because dbt writes *compiled* copies of every model there, so a grep for
#: a forbidden pattern would find it twice and report a path nobody edits.
#: Worse, dbt names compiled artefacts after their source, and a directory
#: called `_marts.yml` makes a scanner that assumes "`.yml` suffix means
#: file" raise `IsADirectoryError` mid-run.
#:
#: CI never hit that because `dbt build` and this check run in different
#: jobs with separate checkouts. `make verify-ci` runs both in one
#: workspace, which is how it surfaced -- an instance of the thing that
#: harness exists to find.
SKIP_PARTS: Final = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        ".ci-venv",
        ".ci-run",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        "target",
        "logs",
        "dbt_packages",
    }
)


@dataclass(frozen=True)
class Rule:
    """One textual invariant.

    `allow_paths` are POSIX path prefixes, relative to the repository root, whose
    files may contain the pattern because refusing something requires naming it.
    """

    directive: str
    description: str
    pattern: re.Pattern[str]
    allow_paths: tuple[str, ...] = ()
    remedy: str = ""


RULES: Final[tuple[Rule, ...]] = (
    Rule(
        directive="PD-17",
        description="No auto-submit code path for AI-assigned codes.",
        # Catches auto_submit / autosubmit / auto-submit in any casing, and the
        # submit_without_review shape someone would reach for next.
        pattern=re.compile(
            r"\b(auto[_\-]?submit\w*|submit_without_(review|signoff|sign_off)"
            r"|skip_coder_review)\b",
            re.IGNORECASE,
        ),
        allow_paths=("tools/check_prime_directives.py",),
        remedy=(
            "ML-6 coding automation is out of scope (CLAUDE.md section 2). The path must "
            "not exist, not merely be disabled by a flag."
        ),
    ),
    Rule(
        directive="PD-16",
        description="Modifiers 25 and 59 are never auto-applied.",
        # The shape of the violation is assigning or appending one of these two
        # modifier values in code, rather than merely mentioning the number.
        pattern=re.compile(
            r"(append|add|apply|set|insert)[_a-z]*\s*\(\s*[\"']?(25|59)[\"']?\s*[,)]"
            r"|modifier\s*=\s*[\"'](25|59)[\"']",
            re.IGNORECASE,
        ),
        # The synthetic generator is exempt because it must *emit* claims
        # carrying these modifiers -- a corpus without them gives the rules
        # engine nothing to refuse, and PD-16's own test would have no input.
        # The prohibition is on the platform applying one to a real claim, which
        # is why src/ has no exemption.
        allow_paths=("tools/check_prime_directives.py", "tools/synthetic/"),
        remedy=(
            "Raise ManualCodingRequiredError instead. These modifiers assert clinical "
            "facts about the encounter that no rule can establish."
        ),
    ),
    Rule(
        directive="S-11",
        description="Parameterised queries only; no string-built SQL.",
        pattern=re.compile(
            r"(execute|executemany|text)\s*\(\s*f[\"']"
            r"|(execute|executemany|text)\s*\([^)]*%\s*\(?[a-z_]"
            r"|(SELECT|INSERT|UPDATE|DELETE)\b[^\"'\n]*\"\s*\+",
            re.IGNORECASE,
        ),
        # Migrations are exempt because SQL cannot bind an *identifier* as a
        # parameter -- `GRANT ... TO $1` is not valid syntax. The role and table
        # names interpolated there are module-level constants in the migration
        # itself, never request data, so there is no injection surface. Any
        # migration that interpolates a value rather than an identifier is still
        # a violation and must be caught in review.
        #
        # `verify_ci.py` is exempt on exactly the same grounds and no others:
        # it issues `DROP SCHEMA <name>` and `TRUNCATE <tables>` to reset a
        # local database before running the suite, and both interpolate
        # module-level constants in this repository. It never sees request
        # data, and it never runs against anything but a developer's own
        # database. Widening this list is a visible diff on purpose.
        allow_paths=(
            "tools/check_prime_directives.py",
            "tools/verify_ci.py",
            "migrations/",
        ),
        remedy="Bind parameters. String interpolation into SQL fails review (SR-11).",
    ),
    Rule(
        directive="T20",
        description="No print(); output goes through structlog so the PHI scrubber sees it.",
        pattern=re.compile(r"(?<![\w.])print\s*\("),
        # The synthetic generator and the doc generators are developer tools that
        # never touch PHI and whose output is a console report, not telemetry.
        allow_paths=(
            "tools/",
            "src/rcm/platform/logging.py",
        ),
        remedy="Use rcm.platform.logging.get_logger(). PD-13 covers every output path.",
    ),
)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix not in SCANNED_SUFFIXES:
                continue
            if SKIP_PARTS & set(path.parts):
                continue
            # A matching suffix is not a promise that this is a file: dbt
            # creates directories named after the models they compile.
            if not path.is_file():
                continue
            files.append(path)
    return sorted(files)


def _allowed(rel: str, rule: Rule) -> bool:
    return any(rel == allowed or rel.startswith(allowed) for allowed in rule.allow_paths)


def main() -> int:
    violations: list[str] = []
    files = _scanned_files()

    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for rule in RULES:
            if _allowed(rel, rule):
                continue
            for lineno, line in enumerate(lines, start=1):
                stripped = line.lstrip()
                # A comment or docstring line may name the rule it describes.
                if stripped.startswith("#"):
                    continue
                if rule.pattern.search(line):
                    violations.append(
                        f"{rel}:{lineno}: {rule.directive} -- {rule.description}\n"
                        f"    {line.strip()}\n"
                        f"    remedy: {rule.remedy}"
                    )

    print(f"prime-directive check: {len(files)} files scanned")  # noqa: T201
    if violations:
        print(f"\n{len(violations)} violation(s):\n")  # noqa: T201
        for v in violations:
            print(v)  # noqa: T201
        return 1
    for rule in RULES:
        print(f"  OK  {rule.directive}  {rule.description}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
