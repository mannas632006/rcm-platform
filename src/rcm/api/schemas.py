"""Response schemas for the operator UI's API (M5.1, SRS 7.2).

The frontend's types are generated from the OpenAPI schema these produce
(FR-API-05), so a field here is a field the UI is held to. Money is a decimal
string, never a float: the UI formats it with `MoneyDisplay` (UR-21) and a
float would have lost cents before it got there.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rcm.platform.auth import Permission, Role


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class Problem(_Out):
    """RFC 7807 Problem Details (FR-API-04): every error the API returns.

    Declared on each route so the generated client types the error body too.
    """

    type: str
    title: str
    status: int
    detail: str
    code: str = Field(description="Machine-readable error code, stable across releases.")
    instance: str | None = None
    request_id: str | None = Field(default=None, description="Correlates to logs (FR-API-03).")


#: The errors every authenticated route can return.
AUTH_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": Problem, "description": "No valid bearer token."},
    403: {"model": Problem, "description": "The token lacks the permission this route needs."},
}


class Me(_Out):
    """Who is signed in, and what they may do. No PHI: a staff identity."""

    subject: str
    roles: list[Role]
    permissions: list[Permission]
    client_ids: list[uuid.UUID]


class QueueSummaryOut(_Out):
    key: str = Field(description="Stable queue identifier, used in URLs.")
    label: str
    open: int
    unassigned: int
    mine: int = Field(description="Open items held by the signed-in operator.")
    pinned: int = Field(description="Deadline-pinned items (PD-15), always shown first.")
    overdue: int = Field(description="Open items past their SLA.")


class WorkQueues(_Out):
    queues: list[QueueSummaryOut]


class DeadlineRiskOut(_Out):
    """UR-26: the alert bar that cannot be dismissed while claims are at risk."""

    claims: int
    charges: Decimal = Field(
        description="Billed charges of the claims at risk, as an exact decimal string to the cent."
    )
