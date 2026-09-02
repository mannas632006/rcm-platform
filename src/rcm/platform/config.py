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

    # --- logging -----------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- values nobody has decided yet (see placeholders.py) ---------------
    raw_file_retention_days: int = Field(default=2557)
    session_idle_timeout_minutes: int = Field(default=15)
    work_item_lock_expiry_minutes: int = Field(default=30)
    payer_registry_staleness_days: int = Field(default=180)
    blocking_rule_max_false_positive_rate: float = Field(default=0.05)
    outcome_maturity_days: int = Field(default=180)
    timely_filing_escalation_days: int = Field(default=14)
    preventable_denial_defect_threshold: int = Field(default=3)

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
