"""Unresolved specification values, tracked rather than guessed.

CLAUDE.md section 12: where a `[TBD-nn]` blocks progress, make it a configurable value
with a clearly-labelled placeholder default, list it in the report, and continue.
Never hard-code a guess as if it were decided.

The mechanism: every placeholder-backed setting is declared here with its TBD id,
description, owner and provisional value. At startup the application logs every
setting still running on its provisional value, so "we never decided this" is
visible in operations rather than buried in a diff. Supplying the corresponding
environment variable resolves it -- `Settings.unresolved_placeholders()` then stops
reporting it.

Registered here, consumed by `rcm.platform.config`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Placeholder:
    """A specification value nobody has decided yet."""

    tbd_id: str
    """Identifier as written in the specifications, e.g. "TBD-56"."""

    description: str
    """What the value means, copied from the placeholder register."""

    owner: str
    """Who decides. From DOC-02 Appendix A / SRS Appendix B."""

    provisional: object
    """A value that lets the system run. It is not a recommendation."""

    source: str
    """Which document's register declares it."""


#: Every placeholder this milestone needs. Extended as later milestones reach
#: the settings that depend on them.
REGISTRY: Final[dict[str, Placeholder]] = {
    "raw_file_retention_days": Placeholder(
        tbd_id="TBD-55",
        description="Raw X12 file retention period. Minimum 7 years per SRS section 5.8, "
        "but the contractual figure is undecided.",
        owner="Muhammad Anas",
        provisional=2557,  # 7 years, the regulatory floor -- not a chosen policy
        source="SRS Appendix B",
    ),
    "session_idle_timeout_minutes": Placeholder(
        tbd_id="TBD-56",
        description="Session idle timeout before re-authentication (SR-14).",
        owner="Eng 2",
        provisional=15,
        source="SRS Appendix B",
    ),
    "work_item_lock_expiry_minutes": Placeholder(
        tbd_id="TBD-57",
        description="How long an operator's exclusive lock on a work item survives "
        "before the item returns to the queue (M3.4).",
        owner="Ops",
        provisional=30,
        source="SRS Appendix B",
    ),
    "payer_registry_staleness_days": Placeholder(
        tbd_id="TBD-42",
        description="Age at which a payer registry entry is stale and may no longer "
        "drive a BLOCKING decision (BR-REG-02, M4.10).",
        owner="Muhammad Anas",
        provisional=180,
        source="DOC-02 Appendix A",
    ),
    "blocking_rule_max_false_positive_rate": Placeholder(
        tbd_id="TBD-45",
        description="Maximum measured false-positive rate for a rule to be promoted "
        "to BLOCKING (M4.6).",
        owner="Eng 2",
        provisional=0.05,
        source="DOC-02 Appendix A",
    ),
    "outcome_maturity_days": Placeholder(
        tbd_id="TBD-50",
        description="Days post-DOS before a row's outcome is final enough to train on. "
        "DR-09 recommends 180; reversals and recoupments change outcomes retroactively.",
        owner="Eng 2",
        provisional=180,
        source="DOC-02 Appendix A",
    ),
    "timely_filing_escalation_days": Placeholder(
        tbd_id="TBD-37",
        description="Days before the filing deadline at which a claim escalates "
        "(BR-GLOBAL-04, PD-15).",
        owner="Muhammad Anas",
        provisional=14,
        source="DOC-02 Appendix A",
    ),
    "preventable_denial_defect_threshold": Placeholder(
        tbd_id="TBD-44",
        description="Recurrence count at which an unexplained preventable denial is "
        "treated as a defect (BR-M7-01).",
        owner="Eng 2",
        provisional=3,
        source="DOC-02 Appendix A",
    ),
    "bulk_handling_threshold": Placeholder(
        tbd_id="TBD-46",
        description="Expected recovery below which a work item is routed to bulk "
        "handling rather than individual work (BR-M10-06, M3.6).",
        owner="Muhammad Anas",
        provisional="25.00",
        source="DOC-02 Appendix A",
    ),
    "clearinghouse_breaker_failures": Placeholder(
        tbd_id="UNREGISTERED",
        description="Consecutive clearinghouse failures that open its circuit breaker "
        "and fail over to the secondary (IR-09, M3.7).",
        owner="Eng 1",
        provisional=5,
        source="none -- raised at M3.7",
    ),
    "clearinghouse_breaker_open_seconds": Placeholder(
        tbd_id="UNREGISTERED",
        description="Seconds a tripped breaker stays open before one probe is allowed.",
        owner="Eng 1",
        provisional=60,
        source="none -- raised at M3.7",
    ),
    "outbox_retry_seconds": Placeholder(
        tbd_id="UNREGISTERED",
        description="Seconds before an unsent submission is retried when no "
        "clearinghouse was available (IR-06).",
        owner="Eng 1",
        provisional=300,
        source="none -- raised at M3.7",
    ),
    "submission_ack_followup_days": Placeholder(
        tbd_id="UNREGISTERED",
        description="Days after submission before a claim with no 999/277CA "
        "acknowledgement needs a person (BR-STATE-04).",
        owner="Ops",
        provisional=2,
        source="none -- raised at M3.7",
    ),
    # No register declares these. They were raised at M3.5, where BR-EXC-02
    # and BR-EXC-04 need numbers the specifications never give.
    "exception_sla_hours": Placeholder(
        tbd_id="UNREGISTERED",
        description="Hours until an exception breaches its SLA, per category "
        "(BR-EXC-02). Compliance 4, deadline risk 24, external system 8, data "
        "quality 72, rule conflict 120, model low confidence 48, operational 72.",
        owner="Muhammad Anas",
        provisional="see description",
        source="none -- raised at M3.5",
    ),
    "exception_trend_ratio": Placeholder(
        tbd_id="UNREGISTERED",
        description="Exception volume in the latest window this many times the "
        "previous window is a rising trend (BR-EXC-04).",
        owner="Muhammad Anas",
        provisional=1.5,
        source="none -- raised at M3.5",
    ),
    "exception_trend_min_count": Placeholder(
        tbd_id="UNREGISTERED",
        description="Minimum exceptions in the latest window before a trend can "
        "be called rising, so three-versus-one is not an alarm (BR-EXC-04).",
        owner="Muhammad Anas",
        provisional=10,
        source="none -- raised at M3.5",
    ),
}
