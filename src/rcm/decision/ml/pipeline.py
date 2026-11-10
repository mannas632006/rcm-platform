"""Train ML-1, judge it, and register it either way (M6.1, M6.2, ADR-0026).

SRS 9.2, in order:

1. The leakage gate (FR-ML-01): `denial_lines` refuses any input that is not
   a feature before it reads a row.
2. Matured rows only (DR-09): the mart it reads is `mart_final_outcome`.
3. The 18 / 3 / 3 month temporal split (FR-ML-02).
4. Train on the first window, stop early on the second.
5. Score the third, once: the model, the rules baseline, and the version
   accepted before this one, on the same lines.
6. Accept only if this model's PR-AUC beats both by more than the
   configured lift. Otherwise reject.
7. Register the version, accepted or rejected, with its data hash, code
   commit, hyperparameters and every number (FR-ML-03), and track the run.

Training-time only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from rcm.analytics.dataset import DENIAL_RISK_FEATURES, LineDataset, LineRow, denial_lines
from rcm.analytics.training import (
    TEST_MONTHS,
    TRAIN_MONTHS,
    VALID_MONTHS,
    Windows,
    temporal_windows,
)
from rcm.decision.ml import provenance, registry, thresholds
from rcm.decision.ml.encoding import Encoding
from rcm.decision.ml.evaluate import binary_report
from rcm.decision.ml.model import BINARY_PARAMS, MAX_ROUNDS, fit_binary
from rcm.decision.ml.registry import CodeVersion
from rcm.decision.ml.registry_models import DataSource, ModelVersion, Verdict
from rcm.decision.ml.tracking import Tracker, flat_metrics
from rcm.decision.rules.evaluate import Verdict as ScrubVerdict
from rcm.decision.rules.scrub import claim_facts, shadow_verdict
from rcm.decision.rules.store import load_ruleset
from rcm.platform.errors import LeakageError, ValidationFailedError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

DENIAL_RISK: Final = "ML-1"


@dataclass(frozen=True, slots=True)
class TrainingReport:
    model_name: str
    version: int
    verdict: Verdict
    reason: str
    data_source: DataSource
    data_hash: str
    windows: Windows
    rows: dict[str, int]
    metrics: dict[str, Any]
    baseline: dict[str, Any]
    mlflow_run_id: str | None


def _xy(rows: Sequence[LineRow]) -> tuple[list[tuple[Any, ...]], list[bool]]:
    return [r.values for r in rows], [r.label for r in rows]


def _usable(name: str, rows: Sequence[LineRow]) -> None:
    labels = {r.label for r in rows}
    if labels != {True, False}:
        raise LeakageError(
            window=name,
            reason="needs both denied and paid lines, or it measures nothing",
        )


def rules_flags(session: Session, rows: Sequence[LineRow], *, today: date) -> list[float]:
    """1.0 where the published ruleset would block or advise the line's claim."""
    ruleset = load_ruleset(session)
    verdicts: dict[uuid.UUID, bool] = {}
    by_claim: dict[uuid.UUID, uuid.UUID] = {r.claim_id: r.client_id for r in rows}
    for claim_id, client_id in by_claim.items():
        facts = claim_facts(session, claim_id=claim_id, client_id=client_id)
        verdict = shadow_verdict(session, facts, ruleset=ruleset, today=today)
        verdicts[claim_id] = verdict is not ScrubVerdict.PASS
    return [1.0 if verdicts[r.claim_id] else 0.0 for r in rows]


def _verdict(
    model: dict[str, Any], rivals: dict[str, dict[str, Any]], *, lift: float
) -> tuple[Verdict, str]:
    ours = model["pr_auc"]
    if ours is None:
        return Verdict.REJECTED, "the test window has one class only; PR-AUC is undefined"
    lines = [f"PR-AUC {ours:.4f}"]
    accepted = True
    for name, report in rivals.items():
        theirs = report["pr_auc"]
        if theirs is None:
            lines.append(f"{name} unmeasured")
            continue
        beats = ours - theirs > lift
        accepted = accepted and beats
        lines.append(f"{'beats' if beats else 'does not beat'} {name} {theirs:.4f}")
    return (Verdict.ACCEPTED if accepted else Verdict.REJECTED), "; ".join(lines)


#: SRS 9.2's months to train, validate and test. Only a corpus too short for
#: them -- the 6-month smoke corpus the CI suite trains on -- passes others.
SPLIT_MONTHS: Final = (TRAIN_MONTHS, VALID_MONTHS, TEST_MONTHS)


def _split(
    dataset: LineDataset, months: tuple[int, int, int]
) -> tuple[Windows, LineDataset, LineDataset, LineDataset]:
    if not dataset.rows:
        raise ValidationFailedError(field="dataset", reason="no matured, adjudicated lines")
    train_months, valid_months, test_months = months
    windows = temporal_windows(
        dataset.rows[0].submission_date,
        train_months=train_months,
        valid_months=valid_months,
        test_months=test_months,
    )
    train = dataset.window(windows.train_start, windows.valid_start)
    valid = dataset.window(windows.valid_start, windows.test_start)
    test = dataset.window(windows.test_start, windows.test_end)
    for name, part in (("train", train), ("validation", valid), ("test", test)):
        _usable(name, part.rows)
    return windows, train, valid, test


def train_denial_risk(
    session: Session,
    *,
    archive_root: Path | str,
    code: CodeVersion,
    trained_by: str,
    today: date,
    tracker: Tracker | None = None,
    months: tuple[int, int, int] = SPLIT_MONTHS,
) -> TrainingReport:
    """Train, judge and register one version of ML-1."""
    config = thresholds.load()
    dataset = denial_lines(session, features=DENIAL_RISK_FEATURES)
    windows, train, valid, test = _split(dataset, months)
    # Read from the files behind the rows, never passed in (ADR-0026).
    data_source = provenance.data_source(
        session, claim_ids={r.claim_id for r in dataset.rows}, archive_root=archive_root
    )

    encoding = Encoding.fit(dataset.features, (r.values for r in train.rows))
    model = fit_binary(encoding, _xy(train.rows), _xy(valid.rows))

    test_x, test_y = _xy(test.rows)
    payers = [r.payer_id for r in test.rows]
    scored = binary_report(test_y, model.predict(test_x).tolist(), payers=payers)
    rivals = {
        "rules baseline": binary_report(
            test_y, rules_flags(session, test.rows, today=today), payers=payers
        )
    }
    previous: ModelVersion | None = registry.latest_accepted(session, DENIAL_RISK)
    if previous is not None:
        earlier = registry.load(previous).predict(test_x).tolist()
        rivals[f"v{previous.version}"] = binary_report(test_y, earlier, payers=payers)
    verdict, reason = _verdict(scored, rivals, lift=config.min_pr_auc_lift)

    data_hash = dataset.data_hash()
    params = {**BINARY_PARAMS, "max_rounds": MAX_ROUNDS, "trees": model.booster.num_trees()}
    rows = {"train": len(train.rows), "validation": len(valid.rows), "test": len(test.rows)}
    version = registry.next_version(session, DENIAL_RISK)
    run_id = None
    if tracker is not None:
        run_id = tracker.record(
            experiment=DENIAL_RISK,
            params=params,
            metrics={
                **flat_metrics(scored, prefix="test."),
                **flat_metrics(rivals["rules baseline"], prefix="baseline."),
            },
            tags={
                "model_name": DENIAL_RISK,
                "version": str(version),
                "verdict": str(verdict),
                "data_hash": data_hash,
                "code_commit": code.commit,
                "code_dirty": str(code.dirty),
                "data_source": str(data_source),
                "thresholds_version": str(config.version),
            },
            artefact=model.text(),
        )
    row = registry.register(
        session,
        model_name=DENIAL_RISK,
        model=model,
        verdict=verdict,
        reason=reason,
        data_source=data_source,
        data_hash=data_hash,
        code=code,
        thresholds_version=config.version,
        params=params,
        metrics={"test": scored, "rows": rows},
        baseline=rivals,
        windows=windows.as_dict(),
        trained_by=trained_by,
        mlflow_run_id=run_id,
    )
    log.info(
        "ml.trained",
        model=DENIAL_RISK,
        version=row.version,
        verdict=str(verdict),
        pr_auc=scored["pr_auc"],
    )
    return TrainingReport(
        model_name=DENIAL_RISK,
        version=row.version,
        verdict=verdict,
        reason=reason,
        data_source=data_source,
        data_hash=data_hash,
        windows=windows,
        rows=rows,
        metrics=scored,
        baseline=rivals,
        mlflow_run_id=run_id,
    )


def per_payer_table(report: dict[str, Any]) -> list[tuple[str, int, int, float | None]]:
    """(payer, rows, denials, PR-AUC), most rows first, for the printed report."""
    return sorted(
        (
            (payer, entry["rows"], entry["positives"], entry["pr_auc"])
            for payer, entry in report["per_payer"].items()
        ),
        key=lambda t: (-t[1], t[0]),
    )
