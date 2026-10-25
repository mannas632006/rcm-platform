"""Run dbt against the connection the rest of the platform already uses.

dbt wants host, port, user, password and dbname as separate values; the
platform has one `RCM_MIGRATION_DATABASE_URL`. Rather than keep a second
connection string in `profiles.yml` -- which would be a credential in the
repository (S-8) and two things to keep in step -- this shim parses the URL
the platform already has and hands dbt the pieces.

**Why the migration role and not `rcm_app`.** dbt issues DDL: it creates
schemas, drops and rebuilds tables. `rcm_app` deliberately cannot do that, and
giving it the privilege to satisfy a build tool would weaken the control that
M0.4 and the append-only tests rest on. dbt is a migration-shaped activity and
it runs with migration-shaped privileges.

    python tools/run_dbt.py build
    python tools/run_dbt.py test --select marts
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / "src" / "rcm" / "analytics" / "dbt"
SCRIPTS = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")

#: Where dbt is allowed to build. CORE lives in `public` and belongs to
#: Alembic; a dbt target schema that collided with it would let a model
#: silently replace a table a migration owns.
DEFAULT_SCHEMA = "analytics"


def connection_env(url: str) -> dict[str, str]:
    """Split a SQLAlchemy URL into the parts dbt asks for.

    The scheme carries a driver (`postgresql+psycopg`) that dbt does not
    understand and does not need, so it is dropped rather than translated.
    """
    parsed = urlparse(url)
    if not parsed.hostname:
        raise SystemExit(f"cannot read a host from the database URL: {parsed.scheme}://...")

    return {
        "DBT_HOST": parsed.hostname,
        "DBT_PORT": str(parsed.port or 5432),
        "DBT_USER": unquote(parsed.username or ""),
        "DBT_PASSWORD": unquote(parsed.password or ""),
        "DBT_DBNAME": (parsed.path or "/rcm").lstrip("/"),
        "DBT_SCHEMA": os.environ.get("DBT_SCHEMA", DEFAULT_SCHEMA),
    }


def dbt_executable() -> Path | None:
    """Find dbt in the project venv, or on PATH.

    Both arrangements are real and neither is wrong. A local checkout runs
    `make install` and gets a `.venv`, which is what every other tool in this
    repo resolves against. CI installs with `pip install -e ".[dev]"` into
    the runner's own Python and calls tools straight from PATH -- there is no
    `.venv` on a GitHub runner at all.

    Resolving only against `.venv` therefore works perfectly on a developer
    machine and fails on every CI run, which is exactly how it was found:
    green locally, 127 in the integration job.

    The venv comes first so that a developer with both never silently gets
    the wrong version.
    """
    local = SCRIPTS / ("dbt.exe" if os.name == "nt" else "dbt")
    if local.exists():
        return local
    found = shutil.which("dbt")
    return Path(found) if found else None


def main(argv: list[str]) -> int:
    url = os.environ.get("RCM_MIGRATION_DATABASE_URL")
    if not url:
        sys.stderr.write(
            "RCM_MIGRATION_DATABASE_URL is not set.\n"
            "dbt builds with the migration role because it issues DDL; see the\n"
            "module docstring in tools/run_dbt.py.\n"
        )
        return 2

    executable = dbt_executable()
    if executable is None:
        sys.stderr.write(
            f"dbt is not installed in {SCRIPTS} and is not on PATH.\n"
            "Run `make install` for a local checkout, or `pip install -e '.[dev]'`.\n"
        )
        return 127

    env = {
        **os.environ,
        **connection_env(url),
        "DBT_PROFILES_DIR": str(PROJECT),
        # dbt writes compiled SQL and run artefacts; keeping them inside the
        # project directory keeps `make clean` honest.
        "DBT_PROJECT_DIR": str(PROJECT),
    }
    # The executable is this repo's own `.venv/bin/dbt` and the arguments come
    # from the Makefile or a developer's shell, same as `run_venv_tool.py`.
    return subprocess.run(  # noqa: S603
        [str(executable), *argv], cwd=PROJECT, env=env, check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
