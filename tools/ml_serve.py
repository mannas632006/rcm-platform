"""Put a model version into service, and take it out (M6.6, ADR-0028).

    python -m tools.ml_serve status
    python -m tools.ml_serve shadow   ML-1 1 --by user:ops.omar --reason "accepted 2026-11-11"
    python -m tools.ml_serve score    ML-1 --since 2025-10-01 --until 2025-12-01
    python -m tools.ml_serve compare  ML-1 1
    python -m tools.ml_serve promote  ML-1 1 --by user:ops.omar --reason "shadow report reviewed"
    python -m tools.ml_serve revert   ML-1 1 --by user:ops.omar --reason "payer rules changed"
    python -m tools.ml_serve monitor

Each change is a row in `ml_deployment`, with who and why; `--by` and
`--reason` are required, because "someone turned it on" is not an answer.
`promote` refuses a version that has not served its shadow period
(FR-ML-04). `monitor` is the daily job Celery beat runs; running it by hand
does exactly the same, including reverting on critical drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime

from rcm.decision.ml import deployment, monitoring, shadow
from rcm.platform.db import session_scope
from tools.load_corpus import DEFAULT_CLIENT

MODELS = ("ML-1", "ML-3")


def _say(text: str = "") -> None:
    sys.stdout.write(f"{text}\n")


def _change(args: argparse.Namespace) -> None:
    change = {
        "shadow": deployment.start_shadow,
        "promote": deployment.promote,
        "revert": deployment.revert,
    }[args.command]
    with session_scope() as session:
        row = change(
            session,
            model_name=args.model,
            version=args.version,
            changed_by=args.by,
            reason=args.reason,
            now=datetime.now(UTC),
        )
        _say(f"{row.model_name} v{row.version}: {row.status} ({row.changed_by}: {row.reason})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("monitor")
    for action in ("shadow", "promote", "revert"):
        change = commands.add_parser(action)
        change.add_argument("model", choices=MODELS)
        change.add_argument("version", type=int)
        change.add_argument("--by", required=True)
        change.add_argument("--reason", required=True)
    score = commands.add_parser("score")
    score.add_argument("model", choices=MODELS)
    score.add_argument("--since", type=date.fromisoformat, required=True)
    score.add_argument("--until", type=date.fromisoformat, required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("model", choices=MODELS)
    compare.add_argument("version", type=int)
    args = parser.parse_args(argv)

    if args.command in ("shadow", "promote", "revert"):
        _change(args)
        return 0
    now = datetime.now(UTC)
    with session_scope() as session:
        if args.command == "status":
            for model in MODELS:
                states = deployment.states(session, model)
                if not states:
                    _say(f"{model}: not deployed (rules only)")
                for version, row in sorted(states.items()):
                    _say(
                        f"{model} v{version}: {row.status} since {row.changed_at:%Y-%m-%d} "
                        f"by {row.changed_by}: {row.reason}"
                    )
        elif args.command == "score":
            claims = shadow.run(
                session,
                model_name=args.model,
                client_id=DEFAULT_CLIENT,
                since=args.since,
                until=args.until,
                now=now,
            )
            _say(f"{args.model}: {claims} claims scored by every serving version")
        elif args.command == "compare":
            report = shadow.compare(
                session, model_name=args.model, version=args.version, today=now.date()
            )
            _say(json.dumps(report, indent=2, default=str))
        else:
            for run in monitoring.run_all(session, now=now):
                _say(f"{run.model_name} v{run.version}: {run.severity}, {run.action}")
                for breach in run.breaches:
                    _say(f"  {breach['severity']:<8} {breach['metric']} {breach['value']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
