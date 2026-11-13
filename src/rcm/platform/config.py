"""Application configuration.

Settings come from the environment. Secrets come from the environment too, which
in any deployed environment means a secret manager injects them (S-8) -- there is
no code path that reads a credential from a file in the repository, and
`.env` is git-ignored.

Values the specifications have not yet decided are declared in
`rcm.platform.placeholders` and reported by `unresolved_placeholders()`. See
CLAUDE.md section 12.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from rcm.platform.placeholders import REGISTRY, Placeholder

Environment = Literal["local", "ci", "dev", "staging", "production"]


class Settings(BaseSettings):
    """Runtime configuration, immutable once loaded."""

    model_config = SettingsConfigDict(
        env_prefix="RCM_",
        env_file=".env",
        env_file_encoding="utf-8",
        frozen=True,
        extra="forbid",
    )

    environment: Environment = "local"
    debug: bool = False

    # --- database ----------------------------------------------------------
    # Two roles, always. `database_url` is the restricted runtime role; it holds
    # no UPDATE or DELETE on the audit tables (DR-06). `migration_database_url`
    # is the owner, used only by Alembic. Running the app as the owner would
    # make the append-only guarantee unenforceable -- see migration 0002.
    database_url: SecretStr = SecretStr("postgresql+psycopg://rcm_app:rcm_app@localhost:5432/rcm")
    migration_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://rcm_migrator:rcm_migrator@localhost:5432/rcm"
    )
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- redis -------------------------------------------------------------
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    celery_broker_url: SecretStr = SecretStr("redis://localhost:6379/1")
    celery_result_backend: SecretStr = SecretStr("redis://localhost:6379/2")

    # --- object storage (RAW layer, PD-9 / DR-01) --------------------------
    raw_archive_root: str = "data/raw"
    raw_archive_immutable: bool = True

    # --- auth --------------------------------------------------------------
    oidc_issuer: str = "http://localhost:8080/realms/rcm"
    oidc_audience: str = "rcm-platform"
    jwt_algorithms: tuple[str, ...] = ("RS256",)
    # Dev-only symmetric signing for the OIDC stub. Refused outside local/ci by
    # the validator below, so a stub issuer cannot reach a real environment.
    dev_jwt_secret: SecretStr | None = None

    # --- api ---------------------------------------------------------------
    api_rate_limit_per_minute: int = 120  # S-13
    idempotency_key_ttl_seconds: int = 86_400  # PD-14
    max_request_body_bytes: int = 10 * 1024 * 1024

    # --- x12 parsing limits (S-12: untrusted input) ------------------------
    x12_max_file_bytes: int = 200 * 1024 * 1024
    x12_max_segments: int = 5_000_000
    x12_max_loop_depth: int = 32

    # --- ml (M6, ADR-0026) ------------------------------------------------
    # Where training runs are tracked. Read by the training tool only; the
    # API never imports MLflow.
    ml_tracking_uri: str = "sqlite:///data/mlflow/mlflow.db"

    # --- logging -----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- values the specifications left open (see placeholders.py) --------
    # Most were decided by the owner on 2026-11-12 (ADR-0031, `DECIDED`);
    # the rest are still placeholders and are reported at startup.
    raw_file_retention_days: int = Field(default=2557)
    session_idle_timeout_minutes: int = Field(default=15)
    work_item_lock_expiry_minutes: int = Field(default=30)
    payer_registry_staleness_days: int = Field(default=180)
    # TBD-45, decided by the owner on 2026-11-03: 10%. A rule whose measured
    # false-positive rate is above this may not be promoted to BLOCKING
    # (FR-RULES-03, M4.6). Decimal, so it compares exactly with a backtest's
    # four-place rate.
    blocking_rule_max_false_positive_rate: Decimal = Field(default=Decimal("0.10"))
    outcome_maturity_days: int = Field(default=180)
    timely_filing_escalation_days: int = Field(default=14)
    preventable_denial_defect_threshold: int = Field(default=3)
    #: TBD-48 (BR-M11-05), decided by the owner on 2026-11-12: 0.00, so every
    #: specialist write-off waits for a named approver.
    write_off_approval_threshold: Decimal = Field(default=Decimal("0.00"), ge=0)
    #: Days after an appeal is sent before it is followed up (ADR-0017).
    #: Decided by the owner on 2026-11-12. Payers typically answer in 30-60.
    appeal_follow_up_days: int = Field(default=30, ge=1)
    # M3.5. No specification gives exception SLAs or a trend threshold;
    # these are labelled placeholders, keyed by ExceptionCategory.
    exception_sla_hours: dict[str, int] = Field(
        default_factory=lambda: {
            "COMPLIANCE": 4,
            "DEADLINE_RISK": 24,
            "EXTERNAL_SYSTEM": 8,
            "DATA_QUALITY": 72,
            "RULE_CONFLICT": 120,
            "MODEL_LOW_CONFIDENCE": 48,
            "OPERATIONAL": 72,
        }
    )
    exception_trend_ratio: float = Field(default=1.5)
    exception_trend_min_count: int = Field(default=10)
    bulk_handling_threshold: Decimal = Field(default=Decimal("25.00"))
    # M3.7. Clearinghouse resilience; no specification gives these numbers.
    clearinghouse_breaker_failures: int = Field(default=5)
    clearinghouse_breaker_open_seconds: int = Field(default=60)
    outbox_retry_seconds: int = Field(default=300)
    outbox_max_attempts: int = Field(default=12)
    submission_ack_followup_days: int = Field(default=2)
    # ADR-0033. Placeholders: no specification gives them.
    status_remittance_grace_days: int = Field(default=14, ge=0)
    status_pending_repeat_limit: int = Field(default=3, ge=1)
    # ADR-0006. Days until the next action, by claim state; "*" for any
    # state not listed. BR-STATE-04 needs a date; no specification gives one.
    next_action_days: dict[str, int] = Field(
        default_factory=lambda: {
            "*": 1,
            "Submitted": 2,
            "Rejected": 2,
            "InAdjudication": 30,
            "Paid": 7,
            "PartiallyPaid": 7,
            "Denied": 7,
            "Correcting": 3,
            "Appealing": 30,
            "SecondaryBilling": 3,
            "PatientBalance": 30,
            "Reopened": 2,
        }
    )

    def validate_for_environment(self) -> None:
        """Refuse configurations that are unsafe for the environment they name.

        PD-18: the system fails closed. A misconfiguration that would weaken a
        security control stops startup rather than degrading quietly.
        """
        deployed = self.environment in ("dev", "staging", "production")
        if deployed and self.dev_jwt_secret is not None:
            raise ValueError(
                "dev_jwt_secret is set but environment is "
                f"{self.environment!r}: the OIDC stub must never sign tokens "
                "outside local/ci."
            )
        # RFC 7518 section 3.2: an HMAC key shorter than the hash output
        # weakens the signature. PyJWT warns; here it is refused, because a
        # short dev secret makes local token forgery cheap and local is where
        # people paste tokens into shared scratch files.
        if self.dev_jwt_secret is not None and len(self.dev_jwt_secret.get_secret_value()) < 32:
            raise ValueError("dev_jwt_secret must be at least 32 bytes (RFC 7518 section 3.2)")
        if deployed and self.debug:
            raise ValueError(f"debug must be off in {self.environment!r}")
        if deployed and not self.log_json:
            raise ValueError(
                "structured JSON logging is required in deployed environments: "
                "the PHI scrubber is a log processor, and console rendering "
                "bypasses part of the chain."
            )
        if deployed and not self.raw_archive_immutable:
            raise ValueError("DR-01: the RAW layer is write-once. Immutability cannot be off.")

    def unresolved_placeholders(self) -> dict[str, Placeholder]:
        """Settings still running on a provisional value.

        A setting is resolved once it is set explicitly -- pydantic-settings
        records that in `model_fields_set`. Everything else in the registry is
        still an open question, and is logged at startup and reported at each
        milestone gate.
        """
        return {name: spec for name, spec in REGISTRY.items() if name not in self.model_fields_set}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached: configuration does not change at runtime."""
    settings = Settings()
    settings.validate_for_environment()
    return settings
