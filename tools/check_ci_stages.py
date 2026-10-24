"""Guard against a CI stage that exists in the plan but not in the pipeline.

CLAUDE.md section 7.4 fixes the pipeline order and adds: *"No stage may be skipped
or marked continue-on-error."* Four of those stages have no subject yet -- there
is no frontend, no dbt project, no Terraform and no Playwright suite. Writing the
jobs now would give green ticks for running nothing, which is the same defect as
a skipped test wearing a different hat.

So the stages are represented by this check instead. It fails the moment one of
those directories appears, which forces whoever creates it to wire up its gate in
the same pull request rather than six weeks later.

Exit code 0 while a stage is genuinely absent, 1 once it exists unwired.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
WORKFLOW: Final = REPO_ROOT / ".github" / "workflows" / "ci.yml"


@dataclass(frozen=True)
class Stage:
    """A pipeline stage whose subject does not exist yet."""

    name: str
    milestone: str
    #: Paths whose existence means the stage now has something to run against.
    triggers: tuple[str, ...]
    #: Text that must appear in ci.yml once the stage is wired.
    wired_marker: str
    required_by: str


PENDING: Final[tuple[Stage, ...]] = (
    Stage(
        name="frontend lint + typecheck",
        milestone="M5.1",
        triggers=("frontend/package.json",),
        wired_marker="eslint",
        required_by=(
            "CLAUDE.md section 7.4; M5.1 requires the generated client to fail CI on hand edits"
        ),
    ),
    Stage(
        name="leakage suite",
        milestone="M2.4",
        triggers=("src/rcm/analytics/features", "tests/leakage"),
        wired_marker="tests/leakage",
        required_by="PD-11; CLAUDE.md section 7.1 lists the leakage suite as blocking",
    ),
    Stage(
        name="dbt build",
        milestone="M2.1",
        # The project lives in its own subdirectory rather than directly in
        # `analytics/`: dbt writes `target/` and `logs/` beside its project
        # file, and `analytics/` also holds the Python feature builders that
        # CLAUDE.md section 4 puts there. Both spellings stay listed so the
        # guard still fires if someone later moves it up.
        triggers=(
            "src/rcm/analytics/dbt/dbt_project.yml",
            "src/rcm/analytics/dbt_project.yml",
            "analytics/dbt_project.yml",
        ),
        wired_marker="dbt build",
        required_by="M2.1 gate: dbt build green, all tests pass",
    ),
    Stage(
        name="IaC scan (tfsec)",
        milestone="M0 security",
        triggers=("infra/terraform",),
        wired_marker="tfsec",
        required_by="CLAUDE.md section 6.2 lists iac-scan as a blocking gate",
    ),
    Stage(
        name="E2E (Playwright) + accessibility (axe)",
        milestone="M5",
        triggers=("tests/e2e/playwright.config.ts", "frontend/playwright.config.ts"),
        wired_marker="playwright",
        required_by="M5 gate: five highest-volume journeys, WCAG 2.1 AA",
    ),
)


def main() -> int:
    workflow = WORKFLOW.read_text(encoding="utf-8").lower() if WORKFLOW.exists() else ""
    problems: list[str] = []
    waiting: list[str] = []

    for stage in PENDING:
        present = [t for t in stage.triggers if (REPO_ROOT / t).exists()]
        wired = stage.wired_marker.lower() in workflow
        if present and not wired:
            problems.append(
                f"{stage.name} ({stage.milestone}) now has a subject -- {', '.join(present)} "
                f"exists -- but no CI stage runs it.\n"
                f"    required by: {stage.required_by}\n"
                f"    fix: add the stage to .github/workflows/ci.yml in this same change."
            )
        elif not present:
            waiting.append(f"  waiting  {stage.milestone:<12} {stage.name}")

    if problems:
        print(f"{len(problems)} un-wired CI stage(s):\n")  # noqa: T201
        for p in problems:
            print(p + "\n")  # noqa: T201
        return 1

    print("later-stage guard: no un-wired stage")  # noqa: T201
    for line in waiting:
        print(line)  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
