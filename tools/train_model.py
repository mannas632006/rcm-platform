"""Train ML-1, judge it against the rules, register it, and print the report.

    python -m tools.train_model

`make ml-train` runs it against `rcm_ml` (ADR-0026). It needs a built
warehouse (`dbt build`) and a published ruleset: the rules baseline is what
the model must beat, and with no ruleset there is nothing to beat, so it
refuses rather than measuring a model against an empty baseline.

The data source is not an option. It is read from the files behind the
rows (ISA15, `rcm.decision.ml.provenance`), so a model trained on the
synthetic corpus is registered as SYNTHETIC whatever anyone types, and the
registry will not load it in production (CLAUDE.md section 8).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from rcm.decision.ml.pipeline import TrainingReport, per_payer_table, train_denial_risk
from rcm.decision.ml.registry import CodeVersion
from rcm.decision.ml.tracking import MlflowTracker
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope

ROOT = Path(__file__).resolve().parent.parent


def _say(text: str = "") -> None:
    sys.stdout.write(f"{text}\n")


def code_version() -> CodeVersion:
    """The commit this tree is at, and whether it has uncommitted changes."""
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is needed to record the code commit (FR-ML-03)")
    commit = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    status = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, "status", "--porcelain", "--untracked-files=no"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return CodeVersion(commit=commit, dirty=bool(status.strip()))


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def print_report(report: TrainingReport) -> None:
    _say(f"{report.model_name} v{report.version}: {report.verdict}")
    _say(f"  {report.reason}")
    w = report.windows
    _say(
        f"  windows: train {w.train_start} to {w.valid_start}, "
        f"validate to {w.test_start}, test to {w.test_end}"
    )
    _say(f"  rows: {report.rows}; data source: {report.data_source}")
    _say(f"  data hash: {report.data_hash}")
    if report.mlflow_run_id:
        _say(f"  mlflow run: {report.mlflow_run_id}")
    m = report.metrics
    _say(
        f"\n  test: {m['rows']} lines, {m['positives']} denied "
        f"(prevalence {_rate(m['prevalence'])})"
    )
    _say(f"  {'':<22}{'PR-AUC':>9}{'ROC-AUC':>9}{'Brier':>9}{'ECE':>9}")
    for name, entry in (("model", m), *report.baseline.items()):
        _say(
            f"  {name:<22}{_rate(entry['pr_auc']):>9}{_rate(entry['roc_auc']):>9}"
            f"{_rate(entry['brier']):>9}{_rate(entry['ece']):>9}"
        )
    _say("\n  per payer (model):")
    for payer, rows, denied, pr_auc in per_payer_table(m):
        _say(f"    {payer:<12}{rows:>7} lines{denied:>6} denied   PR-AUC {_rate(pr_auc)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--trained-by", default="tools/train_model")
    parser.add_argument("--no-track", action="store_true", help="skip MLflow")
    args = parser.parse_args(argv)

    tracker = None
    if not args.no_track:
        uri = get_settings().ml_tracking_uri
        if uri.startswith("sqlite:///"):
            Path(uri.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        tracker = MlflowTracker(uri)
    with session_scope() as session:
        report = train_denial_risk(
            session,
            archive_root=get_settings().raw_archive_root,
            code=code_version(),
            trained_by=args.trained_by,
            today=datetime.now(UTC).date(),
            tracker=tracker,
        )
    print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
