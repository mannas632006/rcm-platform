"""The grep gate in `tools/check_prime_directives.py` catches what it claims to.

CI runs that script on every push, and until now nothing showed it would ever
fail: a gate that is never seen failing may be a gate that cannot. Each test
plants one violation in a scratch repository and asserts a non-zero exit, and
the named invariant runs the real check over the real tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tools import check_prime_directives as check

#: One planted line per rule, each the shape that rule exists to refuse.
VIOLATIONS = {
    "PD-17": "def auto_submit_codes(claim):\n    return claim\n",
    "PD-16": 'line.modifiers.append("25")\n',
    "S-11": 'session.execute(text(f"SELECT * FROM claim WHERE id = {claim_id}"))\n',
    "T20": 'print("member id", member_id)\n',
}


@pytest.fixture
def scratch_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "src" / "rcm").mkdir(parents=True)
    (tmp_path / "src" / "rcm" / "clean.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
    return tmp_path


def test_a_clean_tree_passes(scratch_repo: Path) -> None:
    assert check.main() == 0


@pytest.mark.parametrize("directive", sorted(VIOLATIONS))
def test_each_rule_fails_on_a_planted_violation(
    scratch_repo: Path, directive: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (scratch_repo / "src" / "rcm" / "planted.py").write_text(
        VIOLATIONS[directive], encoding="utf-8"
    )

    assert check.main() == 1
    assert directive in capsys.readouterr().out


def test_a_comment_naming_the_rule_is_not_a_violation(scratch_repo: Path) -> None:
    """The modules that refuse something have to be able to name it."""
    (scratch_repo / "src" / "rcm" / "doc.py").write_text(
        "# there is no auto_submit path, by PD-17\n", encoding="utf-8"
    )
    assert check.main() == 0


@pytest.mark.invariant
def test_no_coding_autosubmit_path_exists() -> None:
    """PD-17 over the real repository: no auto-submit path for AI-assigned
    codes exists anywhere in src, tools or migrations."""
    assert check.main() == 0
