"""Train a model, judge it, and register it either way (M6.1-M6.3, ADR-0026).

SRS 9.2, in order, for every model:

1. The leakage gate (FR-ML-01): each dataset reader refuses any input its
   model may not see before it reads a row.
2. Matured rows only (DR-09): the mart they read is `mart_final_outcome`.
3. The 18 / 3 / 3 month temporal split (FR-ML-02).
4. Train on the first window, stop early on the second.
5. Score the third, once: the model, its rules baseline, and the version
   accepted before this one, on the same lines.
6. Accept only if this model beats both by more than the configured lift.
   Otherwise reject.
7. Register the version, accepted or rejected, with its data hash, code
   commit, hyperparameters and every number (FR-ML-03), and track the run.

What differs between models is only what they read, what they answer, and
what they must beat (ADR-0027):

| Model | Rows | Answer | Baseline |
|---|---|---|---|
| ML-1 | adjudicated lines | denied? | the published ruleset, run as the scrubber would |
| ML-2 | classified denials | category | the CARC taxonomy table (BR-TAX-01) |
| ML-3 | denials | recovered? | the prior the work queue ranks a denial by today |

Training-time only.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from sqlalchemy.orm import Session

from rcm.analytics.dataset import (
    CATEGORY_FEATURES,
    DENIAL_RISK_FEATURES,
    RECOVERY_FEATURES,
    UNMAPPED,
    LineDataset,
    LineRow,
    category_lines,
    denial_lines,
    recovery_lines,
)
from rcm.analytics.training import (
    TEST_MONTHS,
    TRAIN_MONTHS,
    VALID_MONTHS,
    Windows,
    temporal_windows,
)
from rcm.decision import prioritisation
from rcm.decision.denials import CARC_CATEGORY
from rcm.decision.ml import provenance, registry, thresholds
from rcm.decision.ml.encoding import Encoding
from rcm.decision.ml.evaluate import binary_report, multiclass_report
from rcm.decision.ml.model import (
    BINARY_PARAMS,
    MAX_ROUNDS,
    MULTICLASS_PARAMS,
    Model,
    fit_binary,
    fit_multiclass,
)
from rcm.decision.ml.registry import CodeVersion
from rcm.decision.ml.registry_models import DataSource, ModelVersion, Verdict
from rcm.decision.ml.thresholds import Thresholds
from rcm.decision.ml.tracking import Tracker, flat_metrics
from rcm.decision.rules.evaluate import Verdict as ScrubVerdict
from rcm.decision.rules.scrub import claim_facts, shadow_verdict
from rcm.decision.rules.store import load_ruleset
from rcm.platform.errors import LeakageError, ValidationFailedError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

DENIAL_RISK: Final = "ML-1"
DENIAL_CATEGORY: Final = "ML-2"
RECOVERY: Final = "ML-3"


class Task(StrEnum):
    BINARY = "BINARY"
    MULTICLASS = "MULTICLASS"


#: What each kind of model is accepted on, and how it is written.
_METRIC: Final[dict[Task, tuple[str, str]]] = {
    Task.BINARY: ("pr_auc", "PR-AUC"),
    Task.MULTICLASS: ("macro_f1", "macro-F1"),
}


@dataclass(frozen=True, slots=True)
class TrainingReport:
    model_name: str
    task: Task
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


def _xy[L](rows: Sequence[LineRow[L]]) -> tuple[list[tuple[Any, ...]], list[L]]:
    return [r.values for r in rows], [r.label for r in rows]


def _usable[L](name: str, rows: Sequence[LineRow[L]]) -> None:
    if len({r.label for r in rows}) <= 1:
        raise LeakageError(
            window=name,
            reason="needs at least two different answers, or it measures nothing",
        )


def rules_flags(session: Session, rows: Sequence[LineRow[bool]], *, today: date) -> list[float]:
    """1.0 where the published ruleset would block or advise the line's claim."""
    ruleset = load_ruleset(session)
    verdicts: dict[uuid.UUID, bool] = {}
    by_claim: dict[uuid.UUID, uuid.UUID] = {r.claim_id: r.client_id for r in rows}
    for claim_id, client_id in by_claim.items():
        facts = claim_facts(session, claim_id=claim_id, client_id=client_id)
        verdict = shadow_verdict(session, facts, ruleset=ruleset, today=today)
        verdicts[claim_id] = verdict is not ScrubVerdict.PASS
    return [1.0 if verdicts[r.claim_id] else 0.0 for r in rows]


def recovery_prior(rows: Sequence[LineRow[bool]]) -> list[float]:
    """What the work queue believes of every denial today: one P(recovery)
    for all of them (`prioritisation.P_RECOVERY`, heuristic-v2).

    A constant ranks nothing, so its PR-AUC is the share recovered. That is
    the honest baseline: it is what ML-3 would replace (SRS 7.8).
    """
    return [prioritisation.P_RECOVERY["DENIAL"]] * len(rows)


def taxonomy_answers(dataset: LineDataset[str], rows: Sequence[LineRow[str]]) -> list[str]:
    """The category the CARC taxonomy gives each line's refusal code."""
    carc = [f.name for f in dataset.features].index("carc_code")
    out: list[str] = []
    for row in rows:
        code = row.values[carc]
        category = CARC_CATEGORY.get(str(code)) if code is not None else None
        out.append(str(category) if category is not None else UNMAPPED)
    return out


def _verdict(
    model: dict[str, Any],
    rivals: dict[str, dict[str, Any]],
    *,
    lift: float,
    task: Task = Task.BINARY,
) -> tuple[Verdict, str]:
    key, title = _METRIC[task]
    ours = model[key]
    if ours is None:
        return Verdict.REJECTED, f"the test window has one class only; {title} is undefined"
    lines = [f"{title} {ours:.4f}"]
    accepted = True
    for name, report in rivals.items():
        theirs = report[key]
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


@dataclass(frozen=True, slots=True)
class _Split[L]:
    windows: Windows
    train: LineDataset[L]
    valid: LineDataset[L]
    test: LineDataset[L]

    @property
    def rows(self) -> dict[str, int]:
        return {
            "train": len(self.train.rows),
            "validation": len(self.valid.rows),
            "test": len(self.test.rows),
        }


def _split[L](dataset: LineDataset[L], months: tuple[int, int, int]) -> _Split[L]:
    if not dataset.rows:
        raise ValidationFailedError(field="dataset", reason="no matured lines to train on")
    train_months, valid_months, test_months = months
    windows = temporal_windows(
        dataset.rows[0].submission_date,
        train_months=train_months,
        valid_months=valid_months,
        test_months=test_months,
    )
    split = _Split(
        windows,
        dataset.window(windows.train_start, windows.valid_start),
        dataset.window(windows.valid_start, windows.test_start),
        dataset.window(windows.test_start, windows.test_end),
    )
    for name, part in (("train", split.train), ("validation", split.valid), ("test", split.test)):
        _usable(name, part.rows)
    return split


@dataclass(frozen=True, slots=True)
class _Context:
    """What every training run needs from its caller."""

    session: Session
    archive_root: Path | str
    code: CodeVersion
    trained_by: str
    tracker: Tracker | None
    config: Thresholds


def _context(
    session: Session,
    *,
    archive_root: Path | str,
    code: CodeVersion,
    trained_by: str,
    tracker: Tracker | None,
) -> _Context:
    return _Context(session, archive_root, code, trained_by, tracker, thresholds.load())


def _record[L](
    ctx: _Context,
    *,
    name: str,
    task: Task,
    dataset: LineDataset[L],
    split: _Split[L],
    model: Model,
    params: dict[str, Any],
    scored: dict[str, Any],
    rivals: dict[str, dict[str, Any]],
    lift: float,
) -> TrainingReport:
    """Judge, track and register one trained version (FR-ML-03)."""
    verdict, reason = _verdict(scored, rivals, lift=lift, task=task)
    # Read from the files behind the rows, never passed in (ADR-0026).
    data_source = provenance.data_source(
        ctx.session, claim_ids={r.claim_id for r in dataset.rows}, archive_root=ctx.archive_root
    )
    data_hash = dataset.data_hash()
    version = registry.next_version(ctx.session, name)
    baseline_name = next(iter(rivals))
    run_id = None
    if ctx.tracker is not None:
        run_id = ctx.tracker.record(
            experiment=name,
            params=params,
            metrics={
                **flat_metrics(scored, prefix="test."),
                **flat_metrics(rivals[baseline_name], prefix="baseline."),
            },
            tags={
                "model_name": name,
                "version": str(version),
                "verdict": str(verdict),
                "data_hash": data_hash,
                "code_commit": ctx.code.commit,
                "code_dirty": str(ctx.code.dirty),
                "data_source": str(data_source),
                "thresholds_version": str(ctx.config.version),
            },
            artefact=model.text(),
        )
    row = registry.register(
        ctx.session,
        model_name=name,
        model=model,
        verdict=verdict,
        reason=reason,
        data_source=data_source,
        data_hash=data_hash,
        code=ctx.code,
        thresholds_version=ctx.config.version,
        params=params,
        metrics={"test": scored, "rows": split.rows},
        baseline=rivals,
        windows=split.windows.as_dict(),
        trained_by=ctx.trained_by,
        mlflow_run_id=run_id,
    )
    key, _ = _METRIC[task]
    log.info(
        "ml.trained", model=name, version=row.version, verdict=str(verdict), **{key: scored[key]}
    )
    return TrainingReport(
        model_name=name,
        task=task,
        version=row.version,
        verdict=verdict,
        reason=reason,
        data_source=data_source,
        data_hash=data_hash,
        windows=split.windows,
        rows=split.rows,
        metrics=scored,
        baseline=rivals,
        mlflow_run_id=run_id,
    )


def _train_binary(
    ctx: _Context,
    *,
    name: str,
    dataset: LineDataset[bool],
    months: tuple[int, int, int],
    baseline: tuple[str, Callable[[Sequence[LineRow[bool]]], list[float]]],
) -> TrainingReport:
    split = _split(dataset, months)
    encoding = Encoding.fit(dataset.features, (r.values for r in split.train.rows))
    model = fit_binary(encoding, _xy(split.train.rows), _xy(split.valid.rows))

    test_x, test_y = _xy(split.test.rows)
    payers = [r.payer_id for r in split.test.rows]
    scored = binary_report(test_y, model.predict(test_x).tolist(), payers=payers)
    baseline_name, baseline_scores = baseline
    rivals = {baseline_name: binary_report(test_y, baseline_scores(split.test.rows), payers=payers)}
    previous: ModelVersion | None = registry.latest_accepted(ctx.session, name)
    if previous is not None:
        earlier = registry.load(previous).predict(test_x).tolist()
        rivals[f"v{previous.version}"] = binary_report(test_y, earlier, payers=payers)
    params = {**BINARY_PARAMS, "max_rounds": MAX_ROUNDS, "trees": model.booster.num_trees()}
    return _record(
        ctx,
        name=name,
        task=Task.BINARY,
        dataset=dataset,
        split=split,
        model=model,
        params=params,
        scored=scored,
        rivals=rivals,
        lift=ctx.config.min_pr_auc_lift,
    )


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
    ctx = _context(
        session, archive_root=archive_root, code=code, trained_by=trained_by, tracker=tracker
    )
    return _train_binary(
        ctx,
        name=DENIAL_RISK,
        dataset=denial_lines(session, features=DENIAL_RISK_FEATURES),
        months=months,
        baseline=("rules baseline", lambda rows: rules_flags(session, rows, today=today)),
    )


def train_recovery(
    session: Session,
    *,
    archive_root: Path | str,
    code: CodeVersion,
    trained_by: str,
    tracker: Tracker | None = None,
    months: tuple[int, int, int] = SPLIT_MONTHS,
) -> TrainingReport:
    """Train, judge and register one version of ML-3."""
    ctx = _context(
        session, archive_root=archive_root, code=code, trained_by=trained_by, tracker=tracker
    )
    return _train_binary(
        ctx,
        name=RECOVERY,
        dataset=recovery_lines(session, features=RECOVERY_FEATURES),
        months=months,
        baseline=("queue prior", recovery_prior),
    )


def train_denial_category(
    session: Session,
    *,
    archive_root: Path | str,
    code: CodeVersion,
    trained_by: str,
    tracker: Tracker | None = None,
    months: tuple[int, int, int] = SPLIT_MONTHS,
) -> TrainingReport:
    """Train, judge and register one version of ML-2.

    Its rival is the table the platform already classifies by. The mart's
    category is that table's answer for the line, so the table is very
    nearly right by construction, and a tie is not a win: expect ML-2 to be
    REJECTED on this corpus, which is the gate working (ADR-0027).
    """
    ctx = _context(
        session, archive_root=archive_root, code=code, trained_by=trained_by, tracker=tracker
    )
    dataset = category_lines(session, features=CATEGORY_FEATURES)
    split = _split(dataset, months)
    encoding = Encoding.fit(dataset.features, (r.values for r in split.train.rows))
    model = fit_multiclass(encoding, _xy(split.train.rows), _xy(split.valid.rows))

    test_x, test_y = _xy(split.test.rows)
    payers = [r.payer_id for r in split.test.rows]
    scored = multiclass_report(test_y, model.predict_class(test_x), payers=payers)
    rivals = {
        "taxonomy table": multiclass_report(
            test_y, taxonomy_answers(dataset, split.test.rows), payers=payers
        )
    }
    previous = registry.latest_accepted(session, DENIAL_CATEGORY)
    if previous is not None:
        earlier = registry.load(previous).predict_class(test_x)
        rivals[f"v{previous.version}"] = multiclass_report(test_y, earlier, payers=payers)
    params = {
        **MULTICLASS_PARAMS,
        "num_class": len(model.encoding.classes),
        "max_rounds": MAX_ROUNDS,
        "trees": model.booster.num_trees(),
    }
    return _record(
        ctx,
        name=DENIAL_CATEGORY,
        task=Task.MULTICLASS,
        dataset=dataset,
        split=split,
        model=model,
        params=params,
        scored=scored,
        rivals=rivals,
        lift=ctx.config.min_macro_f1_lift,
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
