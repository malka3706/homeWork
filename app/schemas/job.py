from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.job import JobStatus, JobType


# ---------------------------------------------------------------------------
# Per-job-type payload schemas
# Validated at API boundary — malformed payloads are rejected before enqueue.
# ---------------------------------------------------------------------------

class EmailPayload(BaseModel):
    to: str = Field(max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)


class WebhookPayload(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class ReportPayload(BaseModel):
    report_type: str = Field(min_length=1, max_length=100)


class BatchPayload(BaseModel):
    items: list[Any] = Field(min_length=1, max_length=1000)


PAYLOAD_SCHEMAS: dict[JobType, type[BaseModel]] = {
    JobType.EMAIL: EmailPayload,
    JobType.WEBHOOK: WebhookPayload,
    JobType.REPORT: ReportPayload,
    JobType.BATCH: BatchPayload,
}


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class JobCreate(BaseModel):
    job_type: JobType
    payload: dict[str, Any]
    priority: int = Field(default=5, ge=1, le=10, description="1 = lowest, 10 = highest")
    max_attempts: int = Field(default=3, ge=1, le=10)
    idempotency_key: str | None = Field(default=None, max_length=255)
    scheduled_at: datetime | None = Field(
        default=None,
        description="If set to a future time, the job will wait until then before running.",
    )

    @field_validator("payload")
    @classmethod
    def validate_payload(cls, payload: dict, info) -> dict:
        job_type = info.data.get("job_type")
        if job_type is None:
            return payload
        schema = PAYLOAD_SCHEMAS.get(job_type)
        if schema:
            schema(**payload)  # raises ValidationError with clear field-level errors
        return payload


class JobRetry(BaseModel):
    """Optional override when manually retrying a failed job."""
    priority: int | None = Field(default=None, ge=1, le=10)
    max_attempts: int | None = Field(default=None, ge=1, le=10)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_type: JobType
    status: JobStatus
    priority: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error_message: str | None
    attempt_count: int
    max_attempts: int
    progress: int
    idempotency_key: str | None
    scheduled_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Health schema
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str                      # "ok" | "degraded"
    database: str                    # "connected" | "error: ..."
    redis: str                       # "connected" | "error: ..."
    queue_depth: int
    job_counts: dict[str, int]       # e.g. {"pending": 3, "processing": 1, ...}
