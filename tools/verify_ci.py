"""Run the gate the way CI runs it, not the way a developer's machine does.

`make verify` passed three times while CI failed three times, for three
different reasons of the same kind: the local environment quietly supplies
something a runner does not.

    the dbt shim resolved only against `.venv/bin`, which CI has no copy of
    a test asserted `.venv` appeared in a path, true only where one exists
    `dbt build` ran over a database that only test fixtures ever populated

None of those are visible from an environment that has a `.venv`, a database
left loaded by the previous suite, and files on disk from the last run. So
this harness removes all three:

  * the tree is **exported** the way `actions/checkout` produces one -- the
    files git knows about and nothing it ignores -- and the stages run from
    there, so there is no `.venv` to find;
  * a **separate environment** is built from `pyproject.toml` as CI builds
    one;
  * the **database is emptied** and the analytics schemas dropped, so
    nothing is carried in from a previous run;
  * **CI's own step order** runs, including the generate and load steps that
    a test fixture would otherwise perform invisibly.

Removing `.venv` from PATH is *not* enough, and that was measured rather
than assumed: the first version of this harness ran in place, and with the
dbt-resolution bug deliberately reintroduced it reported `dbt build` green.
Code that resolves against the literal path `ROOT/.venv/bin/...` finds it
whatever PATH says. Only running somewhere that has no `.venv` reproduces
the runner.

It is slower than `make verify` and is not a replacement for it. It is what
to run before pushing a change that touches tooling, CI, dependencies, or
anything that reads the filesystem.

**The stages below must mirror `.github/workflows/ci.yml` verbatim.** A
harness that runs slightly different commands reproduces a slightly
different gate, which is worse than having none: it reports confidence it
has not earned. This was caught on the first run -- the typecheck stage
passed explicit paths while CI runs a bare `mypy`, whose scope comes from
`[tool.mypy] packages`.

    make verify-ci
    make verify-ci ARGS=--keep-env    # reuse the environment
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Where the tree is exported to and the stages are run from.
EXPORT = ROOT / ".ci-run"

#: Deliberately not named `.venv`, and not `.venv-ci` either: that name
#: *contains* the substring `.venv`, so a check like
#: `assert ".venv" in str(path)` -- the exact assertion that failed in CI
#: during M2 -- would pass here and fail there. A harness whose own name
#: defeats the check it reproduces is worse than no harness.
CI_ENV = ROOT / ".ci-venv"

SMOKE = "tools/synthetic/config/smoke.yaml"

TABLES = (
    "claim_acknowledgement, claim_line, claim, remittance_adjustment, "
    "remittance_service_line, claim_payment, provider_adjustment, remittance, "
    "raw_file, payer_alias, payer, payer_match_candidate"
)
ANALYTICS_SCHEMAS = (
    "analytics_marts",
    "analytics_core",
    "analytics_staging",
    "asof_marts",
    "asof_core",
    "asof_staging",
)


def python_for(env: Path) -> Path:
    return env / ("Scripts" if os.name == "nt" else "bin") / "python"


def git(*args: str) -> list[str]:
    output = subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in output.split("\n") if line]


def export_tree() -> list[str]:
    """Copy the files git knows about into `EXPORT`, ignoring the rest.

    `git ls-files -co --exclude-standard` is tracked files plus untracked
    ones that are not ignored: precisely the set that would exist on a
    runner if everything in the working tree were committed. Ignored paths
    (`.venv`, `data/`, `target/`, caches) are left behind, which is the
    point.

    Untracked files are included rather than skipped, so work in progress
    can be checked before it is committed. They are reported, because a file
    that is never `git add`ed is absent on the runner, and that is its own
    kind of green-locally failure.
    """
    if EXPORT.exists():
        shutil.rmtree(EXPORT)

    for relative in git("ls-files", "-co", "--exclude-standard"):
        source = ROOT / relative
        if not source.is_file():
            continue
        target = EXPORT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    return git("ls-files", "-o", "--exclude-standard")


def build_env(*, reuse: bool) -> Path:
    """Create the environment the way CI does: a fresh install from pyproject."""
    if CI_ENV.exists() and not reuse:
        shutil.rmtree(CI_ENV)

    if not CI_ENV.exists():
        sys.stdout.write(f"creating {CI_ENV.name} (slow; --keep-env reuses it)\n")
        subprocess.run([sys.executable, "-m", "venv", str(CI_ENV)], check=True)  # noqa: S603
        subprocess.run(  # noqa: S603
            [str(python_for(CI_ENV)), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
            check=True,
        )

    # Always reinstall: the export is replaced every run, and an editable
    # install pointing at a directory that has been recreated is an install
    # pointing at nothing in particular.
    subprocess.run(  # noqa: S603
        [str(python_for(CI_ENV)), "-m", "pip", "install", "--quiet", "-e", ".[dev]"],
        cwd=EXPORT,
        check=True,
    )
    return CI_ENV


def ci_environ(env: Path) -> dict[str, str]:
    """PATH with the CI environment first and the project venv removed.

    The export has no `.venv` in it, so this is belt and braces -- but a
    stage that shelled out to an absolute path inside the developer's venv
    would otherwise still reach it, and the harness would report a green it
    has not earned.
    """
    scripts = str(python_for(env).parent)
    project_venv = str(ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin"))
    path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p != project_venv]
    return {**os.environ, "PATH": os.pathsep.join([scripts, *path]), "VIRTUAL_ENV": str(env)}


def reset_database() -> None:
    """Empty CORE and drop every analytics schema.

    Run with this harness's own interpreter rather than the exported one: it
    is housekeeping before the run, not a stage under test.

    A suite that passes only because the previous one left data behind is a
    suite that fails on a clean runner, which is the whole reason this file
    exists.
    """
    sys.path.insert(0, str(ROOT / "src"))
    from sqlalchemy import create_engine, text

    from rcm.platform.config import get_settings

    engine = create_engine(get_settings().migration_database_url.get_secret_value())
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for schema in ANALYTICS_SCHEMAS:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    engine.dispose()


#: CI's steps, in CI's order, with CI's arguments. Keep in step with
#: `.github/workflows/ci.yml`.
STAGES: list[tuple[str, list[str]]] = [
    ("lint", ["-m", "ruff", "check", "src", "tests", "tools", "migrations"]),
    ("format", ["-m", "ruff", "format", "--check", "src", "tests", "tools"]),
    ("import boundaries", ["-m", "importlinter.cli", "--config", ".importlinter"]),
    # Bare `mypy`, because that is what CI and the Makefile run:
    # `[tool.mypy] packages = ["rcm"]` decides the scope. Passing paths here
    # would check a different set of files than the gate being reproduced,
    # which this harness caught on its first run.
    ("typecheck", ["-m", "mypy"]),
    ("migrate", ["-m", "alembic", "upgrade", "head"]),
    ("generate corpus", ["-m", "tools.synthetic", "--config", SMOKE]),
    ("load corpus", ["-m", "tools.load_corpus"]),
    ("dbt build", ["tools/run_dbt.py", "build", "--select", "staging", "core", "marts"]),
    ("unit", ["-m", "pytest", "tests/unit", "-q", "-p", "no:cacheprovider"]),
    ("property", ["-m", "pytest", "tests/property", "-q", "-p", "no:cacheprovider"]),
    (
        "integration",
        ["-m", "pytest", "tests/integration", "-q", "-m", "integration", "-p", "no:cacheprovider"],
    ),
    (
        "security",
        ["-m", "pytest", "tests/security", "-q", "-m", "security", "-p", "no:cacheprovider"],
    ),
    ("leakage", ["-m", "pytest", "tests/leakage", "-q", "-m", "leakage", "-p", "no:cacheprovider"]),
    ("prime directives", ["tools/check_prime_directives.py"]),
    ("later-stage guard", ["tools/check_ci_stages.py"]),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--keep-env", action="store_true", help="reuse .ci-venv instead of rebuilding it"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="hide stage output; show only the summary"
    )
    args = parser.parse_args(argv)

    sys.stdout.write(f"exporting to {EXPORT.name}/ (git-known files only)\n")
    untracked = export_tree()
    if untracked:
        sys.stdout.write(
            f"  note: {len(untracked)} untracked file(s) included; "
            "they are absent on a runner until added\n"
        )

    env = build_env(reuse=args.keep_env)
    environ = ci_environ(env)
    python = str(python_for(env))

    sys.stdout.write("resetting the database and dropping analytics schemas\n")
    reset_database()

    failed: list[str] = []
    for name, stage_argv in STAGES:
        started = time.monotonic()
        result = subprocess.run(  # noqa: S603
            [python, *stage_argv],
            cwd=EXPORT,
            env=environ,
            check=False,
            capture_output=args.quiet,
        )
        elapsed = time.monotonic() - started
        status = "ok  " if result.returncode == 0 else "FAIL"
        sys.stdout.write(f"  {status}  {name:<18} {elapsed:6.1f}s\n")
        if result.returncode != 0:
            failed.append(name)
            # Keep going. One broken stage should not hide the state of the
            # rest, and the usual reason to run this is to find out how much
            # of CI a change breaks.

    sys.stdout.write("\n")
    if failed:
        sys.stdout.write(f"verify-ci: FAIL ({', '.join(failed)})\n")
        return 1
    sys.stdout.write("verify-ci: PASS\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
