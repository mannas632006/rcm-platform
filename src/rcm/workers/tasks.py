"""Scheduled work (SRS section 7.3).

    FR-ML-08 (MUST): Critical drift triggers automatic reversion to
    rules-only operation ... without waiting for a human.

`ml_monitor` is the daily monitor of SRS 9.4. Beat runs it; nobody has to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from rcm.decision.ml import monitoring
from rcm.platform.db import session_scope
from rcm.workers.app import app


@app.task(name="rcm.ml.monitor")  # type: ignore[untyped-decorator]
def ml_monitor() -> list[dict[str, Any]]:
    """Measure every serving model version; revert any in critical drift."""
    with session_scope() as session:
        runs = monitoring.run_all(session, now=datetime.now(UTC))
        return [
            {
                "model": run.model_name,
                "version": run.version,
                "severity": run.severity,
                "action": run.action,
            }
            for run in runs
        ]
