"""Celery application (M0.2 / SRS section 7.3).

Task modules are registered as milestones add them. The scheduled sweeps in SRS
section 7.3 -- clearinghouse polling, eligibility, filing-deadline checks -- arrive with
M3; the app object exists now so `docker compose up` brings a worker and a beat
alongside the API rather than bolting them on later.
"""

from __future__ import annotations

from celery import Celery

from rcm.platform.config import get_settings
from rcm.platform.logging import configure_logging


def create_celery() -> Celery:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    celery = Celery(
        "rcm",
        broker=settings.celery_broker_url.get_secret_value(),
        backend=settings.celery_result_backend.get_secret_value(),
    )
    celery.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # A task that vanishes on worker restart is a claim that silently stops
        # moving. Acknowledge only after the work is done (FR-BE-11: no claim
        # rests without a next action).
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
    )
    return celery


app = create_celery()
