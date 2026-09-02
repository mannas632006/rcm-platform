"""Run a project tool from the repo's own `.venv`, wherever the repo lives.

pre-commit's `language: python` hooks get an isolated environment, which cannot
work for `mypy` or `lint-imports`: both need the `rcm` package importable, and an
isolated env does not have it. `language: system` with a relative path fails on
Windows because pre-commit cannot resolve it, and an absolute path would hard-code
one developer's directory into the repository.

So the hook calls this shim with whatever `python` is on PATH, and the shim finds
`.venv` relative to itself. One version of every tool -- the one `make install`
put in the venv -- used by pre-commit, `make lint`, and CI alike.

    python tools/run_venv_tool.py mypy
    python tools/run_venv_tool.py lint-imports --config .importlinter
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write("usage: run_venv_tool.py <tool> [args...]\n")
        return 2

    tool, *args = argv
    executable = SCRIPTS / (f"{tool}.exe" if os.name == "nt" else tool)
    if not executable.exists():
        sys.stderr.write(
            f"{tool} is not installed in {SCRIPTS}.\n"
            "Run `make install` to create the project environment.\n"
        )
        return 127

    # Shell disabled deliberately: the argument vector is passed through as-is.
    return subprocess.call([str(executable), *args], cwd=ROOT)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
