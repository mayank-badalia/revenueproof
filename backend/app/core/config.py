"""Central configuration.

Every credential is read from the environment (`.env` locally, never committed).
Provider settings are optional by design: RevenueProof must boot and run its
deterministic engine even when a founder has not connected a given source yet.
`Settings.provider_status()` reports what is actually live so the UI and the
Step 2a integration reality-check can distinguish a real call from a missing key.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application ----------------------------------------------------
    app_name: str = "RevenueProof"
    environment: Literal["local", "test", "production"] = "local"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    frontend_origin: str = "http://localhost:3000"
    #: Extra browser origins allowed to call this API, comma-separated. The
    #: deployed frontend lives on a different host from the backend — Vercel serves
    #: the pages, this process serves the data — so the Vercel URL has to be named
    #: here or every request from it fails preflight.
    extra_allowed_origins: str = ""
    log_level: str = "INFO"
    # Colourised, human-readable terminal output. Disable for machine-parsed logs.
    log_pretty: bool = True

    # ---- Security -------------------------------------------------------
    # Overridden in production; a static default keeps local restarts from
    # invalidating every reviewer session mid-build.
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiry_minutes: int = 720
    # Fernet key for encrypting stored provider tokens at rest.
    token_encryption_key: str | None = None

    # ---- Data stores ----------------------------------------------------
    database_url: str = "postgresql+psycopg://revenueproof:revenueproof@localhost:55432/revenueproof"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, value: str) -> str:
        """Accept the URL a managed provider hands you, unchanged.

        Render, Neon and Supabase all publish `postgres://` or `postgresql://`.
        SQLAlchemy needs the driver named, and pasting the provider's own string
        into the environment is what an operator will actually do — so the driver
        is added here rather than left as a deployment footgun that surfaces as
        "Can't load plugin: sqlalchemy.dialects:postgres".
        """
        if value.startswith("postgres://"):
            value = "postgresql://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value
    redis_url: str = "redis://localhost:56379/0"
    neo4j_uri: str = "neo4j://localhost:57687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "revenueproof"
    neo4j_database: str = "neo4j"
    chroma_host: str = "localhost"
    chroma_port: int = 58000

    # Local object storage root for immutable raw evidence (S3 in production).
    evidence_storage_path: str = "./data/evidence_vault"

    # ---- LLM (Groq preferred per build instructions Step 5a) ------------
    groq_api_key: str | None = None
    #: Cerebras runs the same OpenAI-compatible wire format on wafer-scale hardware.
    #: Measured on this workspace: contract extraction 0.78s against Groq's minutes,
    #: and a 60,000 token/minute free budget against Groq's 8,000 — which was the
    #: actual cause of the long runs, not model speed.
    cerebras_api_key: str | None = None
    # Proposer and critic deliberately use different model families: core_resources.md
    # rejects same-model agreement as independent verification.
    groq_model_proposer: str = "llama-3.3-70b-versatile"
    groq_model_critic: str = "openai/gpt-oss-120b"

    #: Which provider serves each role. Cerebras is preferred when its key is
    #: present; Groq stays configured so a provider outage is a setting change
    #: rather than a rewrite.
    llm_provider: str = "auto"  # auto | cerebras | groq
    #: Extraction and narration: high volume, and the job is to read what is on the
    #: page rather than to reason about it. Measured over the fourteen real contract
    #: PDFs: 15.1s and 108 of 108 citations verified, against gpt-oss-120b's 62.9s
    #: for one extra contract. Four times faster for the same evidence quality,
    #: because it does not reason past its completion budget and retry.
    cerebras_model_proposer: str = "gemma-4-31b"
    #: Criticism is the reasoning job, so the larger model takes it — and it is a
    #: *different family* from the proposer, which is the property Feature 7 rests
    #: on. Measured on the same prompt it returned BANK_MISMATCH and
    #: CASH_RETENTION_INCONSISTENCY where the smaller model saw only a generic
    #: DATA_INCONSISTENCY.
    cerebras_model_critic: str = "gpt-oss-120b"

    #: Cerebras free tier: 30 requests/minute is the binding limit, not tokens.
    #: Twelve simultaneous calls returned ten 429s, so concurrency is bounded and
    #: requests are paced.
    #: Start conservative and let the provider's own headers raise it. The
    #: published figure for the free tier was 30 requests/minute; the account
    #: actually grants 5, and starting high meant ten of twelve calls were
    #: rejected before anything had been learned.
    llm_max_concurrency: int = 4
    llm_requests_per_minute: int = 5
    llm_temperature: float = 0.0
    llm_max_retries: int = 3
    # Batch work (reading a folder of contracts) is worth waiting for; an
    # interactive call is not. Three 60-second waits then a failure is the worst of
    # both — it burns three minutes AND gives up.
    llm_max_retries_batch: int = 6
    llm_timeout_seconds: float = 60.0

    # ---- Providers (all optional until the founder connects them) -------
    razorpay_key_id: str | None = None
    razorpay_key_secret: str | None = None
    razorpay_webhook_secret: str | None = None

    zoho_client_id: str | None = None
    zoho_client_secret: str | None = None
    zoho_organization_id: str | None = None
    zoho_region: str = "in"  # in | com | eu | au — determines API host
    # Long-lived; exchanged for an access token per run. A self-client grant issues
    # this without any redirect URI, which is why it is the supported path here.
    zoho_refresh_token: str | None = None

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str = "http://localhost:8000/api/v1/connections/google/callback"
    # Path to a service-account JSON key. Preferred over the browser consent flow:
    # a service account sees only what has been explicitly shared with it.
    google_service_account_file: str | None = None

    hubspot_client_id: str | None = None
    hubspot_client_secret: str | None = None
    # HubSpot service key — a static bearer token, no exchange required.
    hubspot_access_token: str | None = None

    resend_api_key: str | None = None

    # ---- Financial policy defaults --------------------------------------
    base_currency: str = "INR"
    # Fraction of claimed revenue above which an item is "material" and therefore
    # requires critic agreement before it can be published as verified.
    default_materiality_pct: float = 1.0
    # Isolation Forest stays disabled below this many records — core_resources.md
    # requires a data-volume gate rather than an unstable score.
    ml_minimum_records: int = 50
    # Maximum independent-critic calls per verification run. The Groq free tier
    # allows ~8,000 tokens/minute, which is roughly 9 critic calls; challenging
    # every contestable link in a large workspace would take many minutes. The
    # budget is spent on the riskiest links first and anything left unchallenged is
    # reported, never silently treated as approved.
    critic_call_budget: int = 15

    @computed_field  # type: ignore[prop-decorator]
    @property
    def llm_enabled(self) -> bool:
        return bool(self.groq_api_key)

    def allowed_origins(self) -> list[str]:
        """Every browser origin permitted to call this API."""
        origins = [self.frontend_origin]
        origins += [
            origin.strip()
            for origin in self.extra_allowed_origins.split(",")
            if origin.strip()
        ]
        # De-duplicated, order preserved, so a repeated entry in the env var does
        # not turn into a duplicated Access-Control-Allow-Origin.
        seen: set[str] = set()
        return [o for o in origins if not (o in seen or seen.add(o))]

    def assert_production_ready(self) -> list[str]:
        """Problems that must not ship. Returned rather than raised, so start-up
        can report all of them at once instead of one per restart."""
        problems: list[str] = []
        if self.environment != "local":
            if self.jwt_secret == "dev-only-insecure-secret-change-me":
                problems.append(
                    "JWT_SECRET is still the development default — every session "
                    "token this deployment issues can be forged by anyone who has "
                    "read the source"
                )
            if not self.token_encryption_key:
                problems.append(
                    "TOKEN_ENCRYPTION_KEY is unset — provider tokens would be "
                    "stored without encryption at rest"
                )
        return problems

    def provider_status(self) -> dict[str, bool]:
        """Which integrations hold real credentials right now."""
        return {
            "groq": bool(self.groq_api_key),
            "razorpay": bool(self.razorpay_key_id and self.razorpay_key_secret),
            # What matters is whether a source can actually be *reached*, not whether
            # an app was registered. Zoho with a client id but no refresh token
            # authorises nothing, and reporting it as connected would be a lie the
            # rest of the UI then repeats.
            "zoho_books": bool(
                self.zoho_client_id
                and self.zoho_client_secret
                and self.zoho_refresh_token
                and self.zoho_organization_id
            ),
            "google_drive": bool(self.google_service_account_file) or bool(
                self.google_client_id and self.google_client_secret
            ),
            "hubspot": bool(self.hubspot_access_token) or bool(
                self.hubspot_client_id and self.hubspot_client_secret
            ),
            "resend": bool(self.resend_api_key),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
