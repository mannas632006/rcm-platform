"""Measure GATE M6 on the training database.

    GATE M6: "Demonstrate auto-revert firing under simulated drift, without
    human intervention."

Against `rcm_ml` (`make ml-db`, `make ml-train`), in one transaction that is
rolled back at the end, so the training database is left as it was:

1. The latest ACCEPTED ML-1 version enters shadow, serves its shadow period
   (time is passed in, not waited for), and is promoted (FR-ML-04).
2. **Control.** It scores the lines it was validated and tested on, and the
   daily monitor runs: nothing may be reverted. If this fires, the alarm is
   noise, and the gate fails.
3. **Simulated drift.** It scores lines from another world: the training
   months' lines, with every payer replaced by one it has never seen and
   every charge multiplied twenty-fold.
4. **The daily monitor runs as Celery beat runs it** (`monitoring.run_all`):
   no version is named and nobody is asked. It must find the drift CRITICAL
   and revert the version itself.
5. The next request for a score must get none ("withdrawn"), and score
   nothing: the work is on the rules alone.

    python -m tools.m6_gate          # via `make m6-gate`, against rcm_ml
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from rcm.analytics.dataset import DENIAL_RISK_FEATURES, LineRow, denial_lines, serving_lines
from rcm.analytics.features import Stage
from rcm.decision.ml import deployment, monitoring, registry, serving, thresholds
from rcm.decision.ml.monitoring import reference_window
from rcm.decision.ml.serving_models import DENIAL_RISK, MonitorRun, Prediction
from rcm.platform.config import get_settings
from rcm.platform.db import session_scope
from tools.load_corpus import DEFAULT_CLIENT

ML_DATABASE: Final = "rcm_ml"
OPERATOR: Final = "gate:m6"


def _say(text: str = "") -> None:
    sys.stdout.write(f"{text}\n")


def _lines(session: Session, rows: list[LineRow[bool]]) -> list[LineRow[None]]:
    claim_ids = list(dict.fromkeys(r.claim_id for r in rows))
    return list(
        serving_lines(
            session,
            features=DENIAL_RISK_FEATURES,
            stage=Stage.SUBMISSION,
            claim_ids=claim_ids,
            denied_only=False,
        ).rows
    )


def drifted(rows: list[LineRow[None]]) -> list[LineRow[None]]:
    """The same lines from another world: an unknown payer, twenty-fold charges."""
    names = [f.name for f in DENIAL_RISK_FEATURES]
    payer = names.index("payer_id")
    charges = [names.index("line_charge"), names.index("claim_total_charge")]

    def shift(row: LineRow[None]) -> LineRow[None]:
        values = list(row.values)
        values[payer] = "SIMULATED_NEW_PAYER"
        for index in charges:
            charged = values[index]
            values[index] = charged * 20 if isinstance(charged, float) else charged
        return replace(row, values=tuple(values))

    return [shift(r) for r in rows]


def _report(run: MonitorRun) -> None:
    metrics = run.metrics
    _say(
        f"  monitor: severity {run.severity}, action {run.action}; "
        f"{metrics.get('requests')} requests, coverage {metrics.get('coverage')}, "
        f"output PSI {metrics.get('output_psi')}, ECE {metrics.get('ece')}"
    )
    for breach in run.breaches:
        _say(
            f"    {breach['severity']:<8} {breach['metric']:<40} "
            f"{breach['value']:.4f} (limit {breach['limit']:.4f})"
        )


def main() -> int:
    settings = get_settings()
    database = make_url(settings.database_url.get_secret_value()).database
    if database != ML_DATABASE:
        sys.stderr.write(f"refusing: run against {ML_DATABASE} (`make m6-gate`), not {database}\n")
        return 2

    config = thresholds.load()
    now = datetime.now(UTC)
    passed = True
    with session_scope() as session:
        version = registry.latest_accepted(session, DENIAL_RISK)
        if version is None:
            sys.stderr.write("no ACCEPTED ML-1 version: run `make ml-train` first\n")
            return 2
        _say(f"ML-1 v{version.version}, thresholds v{config.version} (decided: {config.decided})")

        start = now - timedelta(days=config.shadow_min_days + 2)
        promoted = now - timedelta(days=1)
        deployment.start_shadow(
            session,
            model_name=DENIAL_RISK,
            version=version.version,
            changed_by=OPERATOR,
            reason="GATE M6: accepted; enters shadow",
            now=start,
        )
        deployment.promote(
            session,
            model_name=DENIAL_RISK,
            version=version.version,
            changed_by=OPERATOR,
            reason=f"GATE M6: {config.shadow_min_days}-day shadow period served",
            now=promoted,
        )
        _say(f"shadow from {start.date()}, promoted to ACTIVE on {promoted.date()}")

        dataset = denial_lines(session)
        state = deployment.states(session, DENIAL_RISK)[version.version]

        # 2. Control: the population the model was validated and tested on.
        control = _lines(session, list(reference_window(dataset, version.windows).rows))
        quiet = now - timedelta(hours=12)
        serving.score_rows(session, deployed=state, rows=control, now=quiet)
        runs = [r for r in monitoring.run_all(session, now=quiet) if r.model_name == DENIAL_RISK]
        _say(f"\nCONTROL: {len(control)} lines the model was validated and tested on")
        for run in runs:
            _report(run)
        if any(r.action == "REVERTED" for r in runs):
            _say("  FAIL: the monitor reverted a model on the population it was judged on")
            passed = False

        # 3. Simulated drift, past the control's monitoring window.
        later = now + timedelta(days=config.drift.window_days + 1)
        train_end = str(version.windows["valid_start"])
        world = drifted(
            _lines(session, [r for r in dataset.rows if r.submission_date.isoformat() < train_end])
        )
        serving.score_rows(session, deployed=state, rows=world, now=later)
        _say(
            f"\nSIMULATED DRIFT: {len(world)} training-window lines, payer replaced by "
            "one never seen, charges x20"
        )

        # 4. The daily monitor, as beat runs it. Nobody names the version.
        runs = [r for r in monitoring.run_all(session, now=later) if r.model_name == DENIAL_RISK]
        for run in runs:
            _report(run)
        last = deployment.latest(session, DENIAL_RISK)
        reverted = (
            last is not None and last.status == "REVERTED" and last.changed_by == monitoring.MONITOR
        )
        if last is not None:
            _say(f"  ml_deployment: v{last.version} {last.status} by {last.changed_by}")
            _say(f"    reason: {last.reason}")
        passed = passed and reverted

        # 5. The model path is closed.
        before = session.scalar(select(func.count()).select_from(Prediction))
        served = serving.score(
            session,
            DENIAL_RISK,
            client_id=DEFAULT_CLIENT,
            claim_ids=list(dict.fromkeys(r.claim_id for r in control))[:50],
            now=later,
        )
        after = session.scalar(select(func.count()).select_from(Prediction))
        _say(
            f"\nNEXT REQUEST: unavailable = {served.unavailable!r}; "
            f"{len(served.scores)} scores returned; {(after or 0) - (before or 0)} new predictions"
        )
        passed = passed and served.unavailable == serving.REVERTED and not served.scores
        passed = passed and after == before

        # Nothing here is kept: the gate demonstrates, it does not deploy.
        session.rollback()

    _say("\n(rolled back: rcm_ml is unchanged)")
    _say(f"\nGATE M6: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
