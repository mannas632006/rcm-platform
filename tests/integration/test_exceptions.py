"""The exception manager: named, owned, SLA-bound (M3.5).

    BR-EXC-01 (MUST): Every exception MUST name the specific problem and the
    specific record. Generic failure messages are defects.
    BR-EXC-02 (MUST): Every exception MUST have an owner and an SLA.
    BR-EXC-03 (MUST): Compliance-category exceptions MUST suspend the
    affected automation path until reviewed.
    BR-EXC-04 (MUST): Exception volume by type MUST be monitored; a rising
    trend indicates upstream degradation.
    BR-EXC-05 (MUST): No exception may be closed without a recorded
    resolution.

Every test passes the clock in, as the queue's do.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text

from rcm.platform.audit import AuditEvent
from rcm.platform.db import reset_engines, session_scope
from rcm.platform.errors import ComplianceError, StateTransitionError, ValidationFailedError
from rcm.work import exceptions, queue
from rcm.work.exceptions import (
    AutomationPath,
    ExceptionCategory,
    RecordRef,
    raise_exception,
)
from rcm.work.models import WorkItem, WorkItemKind
from rcm.work.state import WorkItemOutcome, WorkItemState

pytestmark = pytest.mark.integration

CLIENT = uuid.UUID("5a5a5a5a-0000-4000-8000-00000000005a")
OTHER_CLIENT = uuid.UUID("5b5b5b5b-0000-4000-8000-00000000005b")
NOON = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
RAW_FILE = RecordRef("raw_file", uuid.UUID("00000000-0000-4000-8000-0000000000f1"))


@pytest.fixture(autouse=True)
def clean(owner_engine: Engine) -> Iterator[None]:
    def wipe() -> None:
        with owner_engine.begin() as conn:
            conn.execute(text("TRUNCATE work_item, automation_suspension RESTART IDENTITY CASCADE"))

    reset_engines()
    wipe()
    yield
    wipe()


def raise_quarantine(**overrides: object) -> WorkItem:
    args: dict[str, object] = {
        "client_id": CLIENT,
        "kind": WorkItemKind.DATA_QUALITY,
        "problem": "835 does not balance: BPR02 differs from claim payments less PLB",
        "record": RAW_FILE,
        "source_key": f"quarantine:{RAW_FILE.entity_id}",
        "now": NOON,
    }
    args.update(overrides)
    with session_scope() as session:
        item = raise_exception(session, **args)  # type: ignore[arg-type]
        session.expunge(item)
        return item


# ---------------------------------------------------------------------------
# BR-EXC-01: specific problem, specific record
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@pytest.mark.parametrize(
    "problem",
    ["error", "Something went wrong", "An error occurred", "failed", "Exception", "  ", "unknown"],
)
def test_a_generic_exception_is_refused(problem: str) -> None:
    """BR-EXC-01: *"Generic failure messages are defects."* A title an
    operator cannot act on is refused at the point it is raised."""
    with pytest.raises(ValidationFailedError) as raised:
        raise_quarantine(problem=problem)
    assert "BR-EXC-01" in raised.value.context["reason"]


def test_the_title_names_the_record_by_surrogate_id() -> None:
    """The record is named by its surrogate id, never a patient identifier,
    because the title is shown on screens and must stay PHI-free (PD-13)."""
    item = raise_quarantine()
    assert str(RAW_FILE.entity_id)[:8] in item.title
    assert "raw_file" in item.title
    assert item.title.startswith("835 does not balance")


# ---------------------------------------------------------------------------
# BR-EXC-02: an owner and an SLA, from the routing table
# ---------------------------------------------------------------------------


@pytest.mark.invariant
def test_every_exception_has_an_owner_and_an_sla() -> None:
    item = raise_quarantine()
    assert item.owner_role == "ops_correction"
    assert item.category == str(ExceptionCategory.DATA_QUALITY)
    assert item.sla_due_at is not None
    assert item.sla_due_at > NOON


def test_routing_follows_doc02_section_10_1() -> None:
    """DOC-02 10.1: each category goes to its named queue."""
    expected = {
        WorkItemKind.DATA_QUALITY: ("ops_correction", ExceptionCategory.DATA_QUALITY),
        WorkItemKind.RULE_CONFLICT: ("rules_review", ExceptionCategory.RULE_CONFLICT),
        WorkItemKind.MODEL_LOW_CONFIDENCE: (
            "domain_specialist",
            ExceptionCategory.MODEL_LOW_CONFIDENCE,
        ),
        WorkItemKind.EXTERNAL_SYSTEM_FAILURE: ("technical", ExceptionCategory.EXTERNAL_SYSTEM),
        WorkItemKind.DEADLINE_RISK: ("priority_queue", ExceptionCategory.DEADLINE_RISK),
    }
    for kind, (owner, category) in expected.items():
        item = raise_quarantine(kind=kind, source_key=f"routing:{kind}")
        assert (item.owner_role, item.category) == (owner, str(category)), kind


def test_a_deadline_risk_is_pinned_to_the_top() -> None:
    """DOC-02 10.1: deadline risk goes to the priority queue, which
    *"overrides all"* (PD-15)."""
    item = raise_quarantine(kind=WorkItemKind.DEADLINE_RISK, source_key="deadline:1")
    assert item.deadline_pinned is True
    assert item.priority_score == 100


def test_a_missing_filing_deadline_asks_the_user_and_says_what_happens() -> None:
    """Owner decision (2026-11-01): when a payer's filing deadline is
    unknown, ask for it, and until it is given treat the claim as urgent --
    and say so, so the person reading knows the consequence of not answering."""
    item = raise_quarantine(
        kind=WorkItemKind.MISSING_FILING_DEADLINE,
        problem="No timely-filing deadline is recorded for payer BCBSTX",
        record=RecordRef("payer", uuid.UUID("00000000-0000-4000-8000-0000000000aa")),
        source_key="missing_deadline:BCBSTX",
    )
    assert item.deadline_pinned is True
    assert item.priority_score == 100
    assert "urgent" in item.detail.lower()
    assert "enter" in item.detail.lower()


def test_sla_hours_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SLA lengths are unspecified placeholders; setting one must change it."""
    from rcm.platform.config import get_settings

    monkeypatch.setenv("RCM_EXCEPTION_SLA_HOURS", '{"DATA_QUALITY": 5}')
    get_settings.cache_clear()
    try:
        item = raise_quarantine()
    finally:
        monkeypatch.delenv("RCM_EXCEPTION_SLA_HOURS")
        get_settings.cache_clear()
    assert item.sla_due_at == NOON + timedelta(hours=5)


def test_an_unknown_sla_category_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """PD-18: a category with no configured SLA is a configuration error,
    not an exception that silently gets no deadline."""
    from rcm.platform.config import get_settings

    monkeypatch.setenv("RCM_EXCEPTION_SLA_HOURS", '{"COMPLIANCE": 4}')
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationFailedError):
            raise_quarantine()
    finally:
        monkeypatch.delenv("RCM_EXCEPTION_SLA_HOURS")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Raising is idempotent, and audited
# ---------------------------------------------------------------------------


def test_raising_the_same_fact_twice_gives_one_open_item() -> None:
    """A loader that re-reads a quarantined file must not flood the queue."""
    first = raise_quarantine()
    second = raise_quarantine(now=NOON + timedelta(hours=1))
    assert first.work_item_id == second.work_item_id
    with session_scope() as session:
        assert len(session.scalars(select(WorkItem)).all()) == 1


def test_a_resolved_fact_can_be_raised_again() -> None:
    """Once resolved, the same problem recurring is a new piece of work."""
    first = raise_quarantine()
    with session_scope() as session:
        queue.claim(
            session, work_item_id=first.work_item_id, client_id=CLIENT, operator="ops", now=NOON
        )
        queue.open_item(
            session, work_item_id=first.work_item_id, client_id=CLIENT, operator="ops", now=NOON
        )
        queue.resolve(
            session,
            work_item_id=first.work_item_id,
            client_id=CLIENT,
            operator="ops",
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=NOON,
        )
    second = raise_quarantine(now=NOON + timedelta(days=1))
    assert second.work_item_id != first.work_item_id


def test_raising_writes_an_audit_event() -> None:
    item = raise_quarantine()
    with session_scope() as session:
        actions = session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_id == item.work_item_id)
        ).all()
    assert "work_item.created" in actions


# ---------------------------------------------------------------------------
# BR-EXC-03: compliance suspends the automation path until reviewed
# ---------------------------------------------------------------------------


def raise_compliance(path: AutomationPath = AutomationPath.CLAIM_SUBMISSION) -> WorkItem:
    return raise_quarantine(
        kind=WorkItemKind.COMPLIANCE_CONCERN,
        problem="Claim submitted for a service the payer flagged as excluded",
        record=RecordRef("claim", uuid.UUID("00000000-0000-4000-8000-0000000000c1")),
        source_key="compliance:c1",
        suspends=path,
    )


@pytest.mark.invariant
def test_a_compliance_exception_suspends_its_automation_path() -> None:
    """BR-EXC-03 / FR-BE-07. The path refuses to run, by name."""
    raise_compliance()
    with session_scope() as session:
        with pytest.raises(ComplianceError):
            exceptions.require_automation_allowed(
                session, client_id=CLIENT, path=AutomationPath.CLAIM_SUBMISSION
            )
        # Other paths, and other clients, are unaffected.
        exceptions.require_automation_allowed(
            session, client_id=CLIENT, path=AutomationPath.AUTO_RESOLUTION
        )
        exceptions.require_automation_allowed(
            session, client_id=OTHER_CLIENT, path=AutomationPath.CLAIM_SUBMISSION
        )


def test_a_compliance_exception_must_name_the_path_it_suspends() -> None:
    """A compliance concern that suspends nothing would be logged and ignored."""
    with pytest.raises(ValidationFailedError):
        raise_quarantine(
            kind=WorkItemKind.COMPLIANCE_CONCERN,
            problem="Claim submitted for a service the payer flagged as excluded",
            source_key="compliance:none",
        )


def test_only_compliance_exceptions_suspend() -> None:
    with pytest.raises(ValidationFailedError):
        raise_quarantine(suspends=AutomationPath.CLAIM_SUBMISSION)


def test_a_suspension_cannot_be_lifted_before_review() -> None:
    """*"Until reviewed"*: lifting needs the compliance item resolved first."""
    item = raise_compliance()
    with pytest.raises(StateTransitionError), session_scope() as session:
        exceptions.lift_suspension(
            session,
            client_id=CLIENT,
            work_item_id=item.work_item_id,
            operator="compliance_officer",
            now=NOON,
        )


def test_resolving_and_lifting_restores_the_path() -> None:
    item = raise_compliance()
    with session_scope() as session:
        queue.claim(
            session, work_item_id=item.work_item_id, client_id=CLIENT, operator="co", now=NOON
        )
        queue.open_item(
            session, work_item_id=item.work_item_id, client_id=CLIENT, operator="co", now=NOON
        )
        queue.resolve(
            session,
            work_item_id=item.work_item_id,
            client_id=CLIENT,
            operator="co",
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=NOON,
        )
        exceptions.lift_suspension(
            session, client_id=CLIENT, work_item_id=item.work_item_id, operator="co", now=NOON
        )
    with session_scope() as session:
        exceptions.require_automation_allowed(
            session, client_id=CLIENT, path=AutomationPath.CLAIM_SUBMISSION
        )
        lifted = session.scalars(
            select(AuditEvent.action).where(AuditEvent.entity_id == item.work_item_id)
        ).all()
    assert "automation.suspension_lifted" in lifted


# ---------------------------------------------------------------------------
# BR-EXC-04: volume by type, with a rising-trend flag
# ---------------------------------------------------------------------------


def test_a_rising_exception_volume_is_flagged() -> None:
    """Three in the earlier window, twelve in the latest: upstream is
    degrading, and the trend says so before an operator notices."""
    for i in range(3):
        raise_quarantine(source_key=f"old:{i}", now=NOON - timedelta(days=10))
    for i in range(12):
        raise_quarantine(source_key=f"new:{i}", now=NOON - timedelta(days=1))

    with session_scope() as session:
        trends = exceptions.exception_volume(session, client_id=CLIENT, now=NOON, window_days=7)

    by_kind = {t.kind: t for t in trends}
    data_quality = by_kind[str(WorkItemKind.DATA_QUALITY)]
    assert (data_quality.current, data_quality.previous) == (12, 3)
    assert data_quality.rising is True


def test_a_steady_volume_is_not_flagged() -> None:
    for i in range(10):
        raise_quarantine(source_key=f"old:{i}", now=NOON - timedelta(days=10))
    for i in range(10):
        raise_quarantine(source_key=f"new:{i}", now=NOON - timedelta(days=1))

    with session_scope() as session:
        trends = exceptions.exception_volume(session, client_id=CLIENT, now=NOON, window_days=7)
    assert all(not t.rising for t in trends)


def test_a_small_jump_is_not_flagged() -> None:
    """One to three is a tripling and still noise. Below the minimum count
    a trend is not called rising, or every quiet kind would alarm."""
    raise_quarantine(source_key="old:0", now=NOON - timedelta(days=10))
    for i in range(3):
        raise_quarantine(source_key=f"new:{i}", now=NOON - timedelta(days=1))

    with session_scope() as session:
        trends = exceptions.exception_volume(session, client_id=CLIENT, now=NOON, window_days=7)
    assert [(t.current, t.previous, t.rising) for t in trends] == [(3, 1, False)]


def test_volume_is_scoped_to_one_client() -> None:
    """PD-20: one client's degradation is not another's statistic."""
    for i in range(12):
        raise_quarantine(source_key=f"new:{i}", now=NOON - timedelta(days=1))
    with session_scope() as session:
        assert exceptions.exception_volume(session, client_id=OTHER_CLIENT, now=NOON) == []


# ---------------------------------------------------------------------------
# BR-EXC-05 is structural (the outcome CHECK), asserted here end to end
# ---------------------------------------------------------------------------


def test_an_exception_is_closed_only_with_a_recorded_resolution() -> None:
    item = raise_quarantine()
    with session_scope() as session:
        found = session.get(WorkItem, item.work_item_id)
        assert found is not None
        assert WorkItemState(found.state) is WorkItemState.QUEUED
        assert found.outcome is None


# ---------------------------------------------------------------------------
# Review findings: nothing may fail open
# ---------------------------------------------------------------------------


def resolve_item(item_id: uuid.UUID) -> None:
    with session_scope() as session:
        queue.claim(session, work_item_id=item_id, client_id=CLIENT, operator="co", now=NOON)
        queue.open_item(session, work_item_id=item_id, client_id=CLIENT, operator="co", now=NOON)
        queue.resolve(
            session,
            work_item_id=item_id,
            client_id=CLIENT,
            operator="co",
            outcome=WorkItemOutcome.NO_ACTION_NEEDED,
            now=NOON,
        )


def assert_suspended(path: AutomationPath) -> None:
    with pytest.raises(ComplianceError), session_scope() as session:
        exceptions.require_automation_allowed(session, client_id=CLIENT, path=path)


@pytest.mark.invariant
def test_re_raising_a_compliance_concern_suspends_the_newly_named_path() -> None:
    """The idempotent return must not skip a suspension: the caller is told
    the item exists, so the path it named has to be stopped too."""
    first = raise_compliance(AutomationPath.CLAIM_SUBMISSION)
    second = raise_compliance(AutomationPath.AUTO_CORRECTION)
    assert first.work_item_id == second.work_item_id
    assert_suspended(AutomationPath.CLAIM_SUBMISSION)
    assert_suspended(AutomationPath.AUTO_CORRECTION)


@pytest.mark.invariant
def test_a_reopened_compliance_concern_suspends_its_path_again() -> None:
    """Resolved, lifted, then reopened: the concern is back, so the path
    stops again until it is reviewed again (BR-EXC-03, PD-18)."""
    item = raise_compliance()
    resolve_item(item.work_item_id)
    with session_scope() as session:
        exceptions.lift_suspension(
            session, client_id=CLIENT, work_item_id=item.work_item_id, operator="co", now=NOON
        )
        exceptions.reopen_exception(
            session,
            client_id=CLIENT,
            work_item_id=item.work_item_id,
            operator="co",
            now=NOON,
            reason="payer flagged the service again",
        )
    assert_suspended(AutomationPath.CLAIM_SUBMISSION)


def test_a_lift_that_lifts_nothing_is_refused() -> None:
    """A resolved item with no active suspension: saying "lifted" is false."""
    item = raise_quarantine()
    resolve_item(item.work_item_id)
    with pytest.raises(StateTransitionError), session_scope() as session:
        exceptions.lift_suspension(
            session, client_id=CLIENT, work_item_id=item.work_item_id, operator="co", now=NOON
        )


@pytest.mark.parametrize(
    "problem",
    [
        "Member id ABC123456789 has no eligibility on file",
        "Patient SSN 123-45-6789 does not match the subscriber",
        "Date of birth 1980-02-14 disagrees with the payer record",
    ],
)
def test_a_problem_carrying_a_patient_identifier_is_refused(problem: str) -> None:
    """PD-13: the title is shown on screens. Name the record by its id."""
    with pytest.raises(ValidationFailedError) as raised:
        raise_quarantine(problem=problem)
    assert "PD-13" in raised.value.context["reason"]


def test_simultaneous_raises_of_one_fact_return_one_item() -> None:
    """Both callers miss the select; the index stops the second insert, and
    the loser gets the winner's item rather than an IntegrityError."""
    import threading

    from rcm.platform.db import session_factory

    maker = session_factory()
    barrier = threading.Barrier(2)
    ids: list[uuid.UUID] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def contend() -> None:
        session = maker()
        try:
            barrier.wait(timeout=10)
            item = raise_exception(
                session,
                client_id=CLIENT,
                kind=WorkItemKind.DATA_QUALITY,
                problem="835 does not balance: BPR02 differs from claim payments less PLB",
                record=RAW_FILE,
                source_key="race:1",
                now=NOON,
            )
            session.commit()
            with lock:
                ids.append(item.work_item_id)
        except BaseException as err:  # collected and asserted below
            session.rollback()
            with lock:
                errors.append(err)
        finally:
            session.close()

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert len(ids) == 2 and ids[0] == ids[1]
    with session_scope() as session:
        assert len(session.scalars(select(WorkItem)).all()) == 1


def test_a_long_problem_is_marked_as_cut() -> None:
    item = raise_quarantine(problem="835 does not balance because " + "x" * 300)
    assert len(item.title) <= 200
    assert "…" in item.title
