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

A value its owner has decided is recorded in `DECIDED`, with who decided it and
when (ADR-0031). It leaves `REGISTRY`, so it is no longer reported, and
`tests/unit/test_placeholders.py` holds the setting's default to the decision: a
decided value changes only by another decision.

Registered here, consumed by `rcm.platform.config`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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


@dataclass(frozen=True, slots=True)
class Decision:
    """A placeholder its owner has settled."""

    value: object
    """What was decided. The setting's default in `rcm.platform.config`."""

    decided_by: str
    decided_on: date


#: Every value the specifications left open, decided or not.
DECLARED: Final[dict[str, Placeholder]] = {
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
    "outbox_max_attempts": Placeholder(
        tbd_id="UNREGISTERED",
        description="Failed delivery attempts after which an unsent submission stops "
        "retrying and goes to a person (M3.7).",
        owner="Eng 1",
        provisional=12,
        source="none -- raised at M3.7",
    ),
    "next_action_days": Placeholder(
        tbd_id="UNREGISTERED",
        description="Days until a claim's next action, by state (BR-STATE-04, BR-M10-01; "
        "BR-M10-04 wants payer-specific cadence later). Pre-submission states 1, "
        "Submitted 2, InAdjudication 30, Paid/PartiallyPaid/Denied 7.",
        owner="Ops",
        provisional="see description",
        source="none -- raised at GATE M3",
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
    "write_off_approval_threshold": Placeholder(
        tbd_id="TBD-48",
        description="Write-off amount above which a named approver must sign off "
        '(BR-M11-05). Applies to roles whose limit is "<= threshold" in SRS 11.5; '
        'an Ops Manager\'s limit is "Any".',
        owner="Muhammad Anas",
        # 0.00: every specialist write-off waits for approval.
        provisional="0.00",
        source="DOC-02 Appendix A",
    ),
    "appeal_follow_up_days": Placeholder(
        tbd_id="UNREGISTERED",
        description="Days after an appeal is sent before it is followed up "
        "(BR-STATE-04: every claim needs a next action). Payers typically answer "
        "in 30-60 days; Medicare redeterminations in 60.",
        owner="Muhammad Anas",
        # The early end of the range: a follow-up too soon costs a call; too
        # late can miss the next appeal level's window.
        provisional=30,
        source="none -- raised at M5.7 (ADR-0017)",
    ),
    "status_remittance_grace_days": Placeholder(
        tbd_id="UNREGISTERED",
        description="Days after a payer's status answer says a claim is decided "
        "(paid, denied, finalised) before a missing 835 is chased (EC-54).",
        owner="Muhammad Anas",
        provisional=14,
        source="none -- raised by ADR-0033",
    ),
    "status_pending_repeat_limit": Placeholder(
        tbd_id="UNREGISTERED",
        description="Consecutive pending answers with no reason before a person "
        "asks the payer why (EC-56).",
        owner="Muhammad Anas",
        provisional=3,
        source="none -- raised by ADR-0033",
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

_OWNER: Final = "Muhammad Anas"
_ON: Final = date(2026, 11, 12)


def _decided(value: object) -> Decision:
    return Decision(value=value, decided_by=_OWNER, decided_on=_ON)


#: The owner's decisions of 2026-11-12 (ADR-0031): each provisional value
#: adopted as it stood. TBD-45 was decided on 2026-11-03 and was never
#: registered here; it is a plain setting in `rcm.platform.config`.
DECIDED: Final[dict[str, Decision]] = {
    "raw_file_retention_days": _decided(2557),
    "session_idle_timeout_minutes": _decided(15),
    "work_item_lock_expiry_minutes": _decided(30),
    "payer_registry_staleness_days": _decided(180),
    "outcome_maturity_days": _decided(180),
    "timely_filing_escalation_days": _decided(14),
    "preventable_denial_defect_threshold": _decided(3),
    "bulk_handling_threshold": _decided("25.00"),
    # 0.00: every specialist write-off waits for a named approver. Decided,
    # not a default left in place: the owner chose the closed side.
    "write_off_approval_threshold": _decided("0.00"),
    "appeal_follow_up_days": _decided(30),
    "submission_ack_followup_days": _decided(2),
    "outbox_retry_seconds": _decided(300),
    "outbox_max_attempts": _decided(12),
    "clearinghouse_breaker_failures": _decided(5),
    "clearinghouse_breaker_open_seconds": _decided(60),
}

#: What nobody has decided yet: reported at startup and at each gate.
REGISTRY: Final[dict[str, Placeholder]] = {
    name: spec for name, spec in DECLARED.items() if name not in DECIDED
}
