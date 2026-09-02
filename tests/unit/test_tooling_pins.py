"""The pre-commit hooks and the project venv must run the same tool versions.

If they drift, `pre-commit` and `make lint` can disagree about whether the tree is
clean. Whichever one is quieter gets trusted, and the loud one gets disabled --
which is how a lint gate stops being a gate.

mypy and import-linter cannot drift by construction: they run from the venv
through `tools/run_venv_tool.py`. Ruff comes from its own upstream pre-commit
repo and is pinned by `rev:`, so it can, and is checked here.
"""

from __future__ import annotations

import re
from importlib.metadata import version
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[2] / ".pre-commit-config.yaml"


def test_ruff_hook_rev_matches_installed() -> None:
    text = CONFIG.read_text(encoding="utf-8")
    match = re.search(r"ruff-pre-commit\n\s+rev:\s*v([0-9][^\s]*)", text)
    assert match is not None, "ruff-pre-commit rev not found in .pre-commit-config.yaml"
    assert match.group(1) == version("ruff"), (
        f"pre-commit pins ruff {match.group(1)}, venv has {version('ruff')}. "
        "Update the rev so the hook and `make lint` agree."
    )


def test_project_aware_hooks_run_from_the_venv() -> None:
    """mypy and import-linter must keep using the shim.

    Switching either to `language: python` would give it an isolated environment
    without the `rcm` package. import-linter fails loudly in that case, but mypy
    degrades quietly: it reports `import-not-found` and stops checking the very
    code it was added to check.
    """
    text = CONFIG.read_text(encoding="utf-8")
    for hook, tool in (("mypy", "mypy"), ("import-linter", "lint-imports")):
        expected = f"entry: python tools/run_venv_tool.py {tool}"
        assert expected in text, (
            f"the {hook} hook no longer runs through tools/run_venv_tool.py; "
            "an isolated environment cannot import the rcm package"
        )
