"""The dbt shim resolves its executable in both environments (M2.1).

This file exists because of a failure that could only happen in CI. A local
checkout runs `make install` and gets a `.venv`, which is what every other
tool in this repo resolves against; CI installs with `pip install -e
".[dev]"` into the runner's own Python and calls tools straight from PATH.
There is no `.venv` on a GitHub runner.

Resolving only against `.venv` therefore works perfectly on a developer
machine and returns 127 on every CI run. It passed `make verify` locally and
failed the integration job, which is the worst shape a bug can have: the
check that was supposed to catch it is the one that cannot.

The connection tests matter for a different reason. `profiles.yml` holds no
credentials (S-8); it reads the pieces from the environment, and this shim
is what splits the single URL the platform already has into those pieces. If
it split them wrongly, dbt would quietly connect somewhere else.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tools.run_dbt import connection_env, dbt_executable


def test_the_venv_is_preferred_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer with dbt in both places must not silently get the one on
    PATH, which may be a different version than `make install` pinned.

    Both locations are constructed rather than read from the machine this
    runs on. The first version of this test called `dbt_executable()` bare
    and asserted `.venv` appeared in the answer, which is true only where a
    venv exists -- it passed locally and failed on CI, which is the same
    mistake the fix under test exists to correct, one layer up. A test for
    "prefers A over B" has to supply both A and B.
    """
    scripts = tmp_path / "bin"
    scripts.mkdir()
    in_venv = scripts / ("dbt.exe" if os.name == "nt" else "dbt")
    in_venv.touch()

    monkeypatch.setattr("tools.run_dbt.SCRIPTS", scripts)
    monkeypatch.setattr("tools.run_dbt.shutil.which", lambda _: "/usr/local/bin/dbt")

    assert dbt_executable() == in_venv


def test_path_is_used_when_there_is_no_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI's arrangement. This is the case that was broken."""
    monkeypatch.setattr("tools.run_dbt.SCRIPTS", Path("/nonexistent/bin"))
    monkeypatch.setattr("tools.run_dbt.shutil.which", lambda _: "/usr/local/bin/dbt")

    found = dbt_executable()
    assert found == Path("/usr/local/bin/dbt")


def test_missing_everywhere_is_reported_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PD-18. `None` becomes a named error and exit 127; inventing a path
    would produce a `FileNotFoundError` from deep inside subprocess with
    nothing saying what was actually missing."""
    monkeypatch.setattr("tools.run_dbt.SCRIPTS", Path("/nonexistent/bin"))
    monkeypatch.setattr("tools.run_dbt.shutil.which", lambda _: None)

    assert dbt_executable() is None


# ---------------------------------------------------------------------------
# One connection string, split rather than duplicated
# ---------------------------------------------------------------------------


def test_the_sqlalchemy_driver_is_dropped() -> None:
    """`postgresql+psycopg` is a SQLAlchemy spelling. dbt does not understand
    it and does not need it, so it is dropped rather than translated."""
    env = connection_env("postgresql+psycopg://rcm_migrator:secret@db.example:5433/rcm")

    assert env["DBT_HOST"] == "db.example"
    assert env["DBT_PORT"] == "5433"
    assert env["DBT_USER"] == "rcm_migrator"
    assert env["DBT_PASSWORD"] == "secret"
    assert env["DBT_DBNAME"] == "rcm"


def test_a_missing_port_falls_back_to_the_postgres_default() -> None:
    env = connection_env("postgresql://user:pw@localhost/rcm")
    assert env["DBT_PORT"] == "5432"


def test_percent_encoded_credentials_are_decoded() -> None:
    """A password with a `@` or `/` in it is percent-encoded in a URL. Passing
    the encoded form to dbt would authenticate with the wrong string, and the
    failure would read as a bad password rather than a parsing bug."""
    env = connection_env("postgresql://rcm%2Fapp:p%40ss%3Aword@localhost:5432/rcm")

    assert env["DBT_USER"] == "rcm/app"
    assert env["DBT_PASSWORD"] == "p@ss:word"


def test_a_url_with_no_host_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaulting to localhost would point the warehouse build at whatever
    happens to be running on the developer's machine."""
    with pytest.raises(SystemExit):
        connection_env("not-a-url")
