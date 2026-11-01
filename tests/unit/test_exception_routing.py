"""DOC-02 section 10.1's routing table is complete (M3.5, BR-EXC-02)."""

from __future__ import annotations

import pytest

from rcm.platform.config import Settings
from rcm.work.exceptions import ROUTES
from rcm.work.models import ExceptionCategory, WorkItemKind


@pytest.mark.parametrize("kind", list(WorkItemKind))
def test_every_kind_has_a_route(kind: WorkItemKind) -> None:
    """A kind with no route would be an exception with no owner."""
    assert kind in ROUTES
    assert ROUTES[kind].owner_role


@pytest.mark.parametrize("category", list(ExceptionCategory))
def test_every_category_has_a_default_sla(category: ExceptionCategory) -> None:
    """BR-EXC-02: every category has an SLA out of the box."""
    assert Settings().exception_sla_hours[str(category)] > 0


def test_compliance_goes_to_immediate_escalation() -> None:
    """DOC-02 10.1: compliance concerns escalate immediately, with the
    shortest SLA of any category."""
    hours = Settings().exception_sla_hours
    assert ROUTES[WorkItemKind.COMPLIANCE_CONCERN].category is ExceptionCategory.COMPLIANCE
    assert hours["COMPLIANCE"] == min(hours.values())
