"""`make verify-ci` must run what CI runs.

The harness is only worth having if its stages mirror
`.github/workflows/ci.yml`. One that runs slightly different commands
reproduces a slightly different gate, which is worse than having none: it
reports confidence it has not earned.

That is not hypothetical. On its first run the harness passed explicit paths
to mypy while CI runs a bare `mypy`, whose scope comes from
`[tool.mypy] packages`. It was checking a different set of files than the
gate it claimed to reproduce.

These tests compare the two and fail when they drift. They deliberately
check the *shape* -- which suites run, which linters, with which arguments --
rather than string-matching whole command lines, because CI wraps commands
in shell and environment that the harness supplies differently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from tools.verify_ci import STAGES

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def ci_run_commands() -> list[str]:
    """Every `run:` command in the workflow, flattened."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    commands: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run")
            if run:
                commands.extend(line.strip() for line in run.split("\n") if line.strip())
    return commands


def stage_commands() -> list[str]:
    return [" ".join(argv) for _, argv in STAGES]


@pytest.fixture(scope="module")
def ci_commands() -> list[str]:
    return ci_run_commands()


def test_every_pytest_suite_in_ci_is_a_stage(ci_commands: list[str]) -> None:
    """A suite CI runs and the harness does not is a suite whose failures
    only ever appear after a push."""
    suites_in_ci = {
        match.group(1)
        for command in ci_commands
        for match in [re.search(r"(tests/\w+)", command)]
        if match and "pytest" in command
    }
    suites_in_harness = {
        match.group(1)
        for command in stage_commands()
        for match in [re.search(r"(tests/\w+)", command)]
        if match
    }

    missing = suites_in_ci - suites_in_harness
    assert not missing, f"CI runs these suites and verify-ci does not: {sorted(missing)}"


def test_the_harness_runs_no_suite_ci_does_not(ci_commands: list[str]) -> None:
    """The other direction. A stage CI has never run is a stage whose green
    means nothing about whether the push will pass."""
    suites_in_ci = {
        match.group(1)
        for command in ci_commands
        for match in [re.search(r"(tests/\w+)", command)]
        if match and "pytest" in command
    }
    suites_in_harness = {
        match.group(1)
        for command in stage_commands()
        for match in [re.search(r"(tests/\w+)", command)]
        if match
    }

    extra = suites_in_harness - suites_in_ci
    assert not extra, f"verify-ci runs these and CI does not: {sorted(extra)}"


def test_ruff_check_covers_the_same_paths(ci_commands: list[str]) -> None:
    """The gap that let a lint error reach CI during M2: the Makefile
    checked `src tests tools` while CI also checked `migrations`."""
    ci_check = next(c for c in ci_commands if c.startswith("ruff check"))
    harness_check = next(c for c in stage_commands() if "ruff check" in c)

    ci_paths = set(ci_check.split()[2:])
    harness_paths = set(harness_check.split()[harness_check.split().index("check") + 1 :])
    assert ci_paths == harness_paths, (
        f"CI checks {sorted(ci_paths)}, harness {sorted(harness_paths)}"
    )


def test_mypy_is_invoked_the_same_way(ci_commands: list[str]) -> None:
    """Bare in both, so `[tool.mypy] packages` decides the scope. Passing
    paths in one and not the other checks different files -- which is
    exactly what happened."""
    assert "mypy" in ci_commands, "CI no longer runs a bare mypy; the harness assumes it does"
    harness = next(c for c in stage_commands() if c.endswith("mypy") or " mypy" in c)
    assert harness.strip() == "-m mypy", f"harness runs `{harness}`, CI runs bare `mypy`"


def test_the_corpus_is_generated_and_loaded_before_dbt_builds(ci_commands: list[str]) -> None:
    """The M2 failure that `assert_the_warehouse_is_not_empty` caught: CI
    generated the corpus and built the warehouse without ingesting it."""
    names = [name for name, _ in STAGES]
    assert names.index("generate corpus") < names.index("load corpus") < names.index("dbt build")

    # And CI itself must still do both, or the harness is reproducing a
    # sequence the workflow no longer has.
    assert any("tools.synthetic" in c for c in ci_commands)
    assert any("tools.load_corpus" in c for c in ci_commands)


GATE_JOB = "verify"


def test_the_gate_depends_on_every_other_job() -> None:
    """A job the gate does not wait for is a job whose failure does not block
    a merge.

    `verify` is the single required check on `main`, so the other jobs are
    advisory to GitHub and become blocking only by sitting in its `needs`
    chain. The chain is not obvious by eye: `test-leakage` is absent from
    the gate's own `needs` list and is nonetheless blocking, because
    `coverage-floor` and `build` both wait on it and the gate waits on them.
    Reading the list alone suggests PD-11's leakage suite is ungated; walking
    the graph shows it is not.

    That gap between the two readings is the reason for this test. The next
    job added is as likely to be wired in through one edge as nine, and only
    the walk can tell the difference.
    """
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert GATE_JOB in jobs, f"the gate job `{GATE_JOB}` is gone; this test is stale"

    def needs_of(job: str) -> list[str]:
        declared = jobs[job].get("needs", [])
        return [declared] if isinstance(declared, str) else list(declared)

    reachable: set[str] = set()
    frontier = needs_of(GATE_JOB)
    while frontier:
        job = frontier.pop()
        if job in reachable:
            continue
        reachable.add(job)
        frontier.extend(needs_of(job))

    ungated = sorted(set(jobs) - reachable - {GATE_JOB})
    assert not ungated, (
        f"these CI jobs can fail without failing the gate, so they cannot block a merge: {ungated}"
    )
