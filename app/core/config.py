from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Database
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/job_queue"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Worker
    worker_poll_timeout: int = 5          # seconds to block on BZPOPMIN
    worker_crash_recovery_interval: int = 60  # seconds between stale-job scans
    worker_job_timeout: int = 300         # seconds before a PROCESSING job is considered stuck

    # App
    debug: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",  # ignore .env vars not declared in this class (e.g. POSTGRES_DB used by Docker)
    )


settings = Settings()
