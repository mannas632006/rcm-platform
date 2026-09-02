"""Structured logging with a PHI scrubber (PD-13, S-9).

Every log line, metric dimension, trace attribute and error payload leaves the
process through this module. CLAUDE.md section 6.3 names those four paths; they share
one scrubber so a fix in one place fixes all four.

Two rules do the work:

1. **Named fields are redacted by key.** A value under `ssn` or `patient_name` is
   replaced wholesale, without inspecting it.
2. **Free text is redacted by pattern.** Field names cannot be trusted for prose,
   so strings not under a known-safe key are scanned for structured identifiers.

And one policy that is easy to miss and matters more than either:

3. **Exception messages are never logged.** An exception message is free text
   assembled from whatever the raising code had in scope, which in this system is
   routinely a patient. No pattern catches a name, so the only guarantee available
   is not to emit the message at all. The exception *type* and the source location
   are logged instead, and code that needs context raises a typed error from
   `rcm.platform.errors` carrying structured, already-classified fields.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from types import TracebackType
from typing import Any, Final

import structlog

REDACTED: Final = "[REDACTED]"

#: Values under these keys are replaced without inspection. Matched exactly on the
#: lowercased key -- substring matching would swallow `claim_id` along with
#: `member_id`, and an unusable log gets switched off.
PHI_KEYS: Final[frozenset[str]] = frozenset(
    {
        # names
        "name",
        "full_name",
        "first_name",
        "last_name",
        "middle_name",
        "patient_name",
        "subscriber_name",
        "guarantor",
        "guarantor_name",
        "patient",
        "insured_name",
        # government and payer identifiers
        "ssn",
        "social_security_number",
        "tax_id",
        "member_id",
        "subscriber_id",
        "policy_number",
        "insurance_id",
        "hicn",
        "mbi",
        # record identifiers
        "mrn",
        "medical_record_number",
        "patient_account_number",
        "account_number",
        # dates tied to an individual
        "dob",
        "date_of_birth",
        "birth_date",
        "death_date",
        "date_of_death",
        # contact and location
        "address",
        "address_line_1",
        "address_line_2",
        "street",
        "street_address",
        "city",
        "postal_code",
        "zip",
        "zip_code",
        "phone",
        "phone_number",
        "telephone",
        "fax",
        "email",
        "email_address",
        # free-text clinical narrative
        "clinical_note",
        "chart_note",
        "diagnosis_description",
    }
)

#: Values under these keys skip pattern scanning. These are the fields operations
#: runs on; redacting them would make the logs useless, which is its own failure
#: mode. None of them can carry a patient identifier.
SAFE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "event",
        "level",
        "timestamp",
        "logger",
        "request_id",
        "session_id",
        "claim_id",
        "service_line_id",
        "adjustment_id",
        "remittance_line_id",
        "work_item_id",
        "decision_id",
        "correlation_id",
        "trace_id",
        "span_id",
        "payer_id",
        "practice_id",
        "billing_npi",
        "rendering_npi",
        "taxonomy",
        "cpt_hcpcs",
        "cas_group_code",
        "carc_code",
        "rarc_codes",
        "state",
        "claim_status_code",
        "ruleset_version",
        "logic_version",
        "model_version",
        "error_type",
        "error_location",
        "status_code",
        "duration_ms",
        "count",
    }
)

#: Structured identifiers, for strings that are not under a safe key.
#: Ordered: the more specific patterns run first so a member id is not partly
#: consumed by a looser rule.
_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\bMRN-?\d{4,}\b", re.IGNORECASE),  # medical record number
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # email
    re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),  # US phone
    re.compile(r"\b[A-Z]{2,}\d{6,}\b"),  # payer member id
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO date (DOB in prose)
)

_MAX_DEPTH: Final = 12


def _scrub_text(value: str) -> str:
    for pattern in _PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def scrub(value: object, *, _key: str | None = None, _depth: int = 0) -> object:
    """Return `value` with PHI removed.

    Recurses through mappings and sequences. `_key` carries the field name the
    value was found under, which is what makes rules 1 and 2 above distinguishable.
    """
    if _depth > _MAX_DEPTH:
        # Depth-limited rather than unbounded: a hostile or cyclic payload must
        # not turn logging into a denial of service (S-12).
        return REDACTED

    key = _key.lower() if _key else None
    if key is not None and key in PHI_KEYS:
        return REDACTED

    if isinstance(value, Mapping):
        return {k: scrub(v, _key=str(k), _depth=_depth + 1) for k, v in value.items()}

    if isinstance(value, list | tuple | set | frozenset):
        scrubbed = [scrub(v, _key=_key, _depth=_depth + 1) for v in value]
        return type(value)(scrubbed) if isinstance(value, list | tuple) else scrubbed

    if isinstance(value, str):
        if key is not None and key in SAFE_KEYS:
            return value
        return _scrub_text(value)

    if isinstance(value, BaseException):
        # Never the message. See rule 3 in the module docstring.
        return f"{type(value).__name__}(<message withheld: PD-13>)"

    return value


# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------


def safe_exception_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace exception info with its type and location, dropping the message.

    Consuming `exc_info` here also stops it reaching the stdlib handler, which
    would otherwise format and emit the full traceback -- message included.
    """
    exc_info = event_dict.pop("exc_info", None)
    if not exc_info:
        return event_dict

    if exc_info is True:
        exc_info = sys.exc_info()
    if not (isinstance(exc_info, tuple) and len(exc_info) == 3):
        return event_dict

    exc_type, _exc_value, tb = exc_info
    if exc_type is None:
        return event_dict

    event_dict["error_type"] = exc_type.__name__
    event_dict["error_location"] = _innermost_location(tb)
    return event_dict


def _innermost_location(tb: TracebackType | None) -> str:
    """`file:line` of the deepest frame -- enough to find the raise site."""
    if tb is None:
        return "unknown"
    frame = tb
    while frame.tb_next is not None:
        frame = frame.tb_next
    code = frame.tb_frame.f_code
    return f"{code.co_filename}:{frame.tb_lineno}"


def phi_scrub_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Scrub every field in the event. Runs before timestamp and level are added."""
    scrubbed = scrub(dict(event_dict))
    if not isinstance(scrubbed, dict):  # pragma: no cover - scrub preserves mappings
        raise TypeError("scrub() must return a mapping for a mapping input")
    return scrubbed


def _install_record_factory() -> None:
    """Strip exception text from every `LogRecord` in the process.

    The structlog processor above only guards loggers that go through structlog.
    A stdlib handler formats `record.exc_info` itself, so any library in the
    process -- SQLAlchemy, celery, a payer SDK -- can render a traceback whose
    final line is the exception message. That message is exactly what rule 3
    says we cannot emit.

    Record creation is the one place every logger passes through, so the
    exception is reduced to its type and raise site here, once, for everyone.
    """
    base = logging.getLogRecordFactory()
    if getattr(base, "_rcm_guarded", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = base(*args, **kwargs)
        exc_info = record.exc_info
        if exc_info and exc_info[0] is not None:
            record.exc_text = (
                f"{exc_info[0].__name__} at {_innermost_location(exc_info[2])} "
                "(message withheld: PD-13)"
            )
            record.exc_info = None
        record.stack_info = None
        return record

    factory._rcm_guarded = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog and the stdlib root logger.

    Idempotent: safe to call from application startup, workers, and tests.
    """
    _install_record_factory()

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            safe_exception_processor,
            # Scrubbing happens before the framework's own fields are added, so
            # the ISO-date pattern cannot redact our own `timestamp`.
            phi_scrub_processor,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_rcm_handler", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._rcm_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A logger whose output has passed the scrubber."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
