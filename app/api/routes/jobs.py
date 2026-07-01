import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.job import Job, JobStatus, JobType
from app.schemas.job import JobCreate, JobListResponse, JobResponse, JobRetry
from app.queue.redis_client import enqueue_job, remove_job

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------
# Submit a new job
# ---------------------------------------------------------------------------

@router.post("/", response_model=JobResponse, status_code=201)
def submit_job(job_data: JobCreate, response: Response, db: Session = Depends(get_db)) -> Job:
    """
    Submit a new job.

    - If `idempotency_key` is provided and already exists, returns the existing job (no duplicate created).
    - If `scheduled_at` is a future time, the job starts in SCHEDULED state and won't run until then.
    - Otherwise the job is immediately PENDING and enqueued for workers.
    """
    # --- Idempotency check ---
    if job_data.idempotency_key:
        existing = db.scalar(
            select(Job).where(Job.idempotency_key == job_data.idempotency_key)
        )
        if existing:
            response.status_code = 200
            return existing

    # --- Determine initial status ---
    now = datetime.now(timezone.utc)
    if job_data.scheduled_at and job_data.scheduled_at > now:
        initial_status = JobStatus.SCHEDULED
    else:
        initial_status = JobStatus.PENDING

    job = Job(
        job_type=job_data.job_type,
        status=initial_status,
        priority=job_data.priority,
        payload=job_data.payload,
        max_attempts=job_data.max_attempts,
        idempotency_key=job_data.idempotency_key,
        scheduled_at=job_data.scheduled_at,
    )

    logger.info("submit_job, idempotency_key=%s, initial_status=%s", job.idempotency_key, initial_status.value)


    try:
        db.add(job)
        db.commit()
        db.refresh(job)
        logger.info("job_id=%s submitted, status=%s", job.id, job.status.value)

    except IntegrityError:
        db.rollback()
        # Race condition: another request committed the same idempotency key first
        existing = db.scalar(
            select(Job).where(Job.idempotency_key == job_data.idempotency_key)
        )
        if existing:
            logger.info("existing job_id=%s submitted, status=%s", job.id, job.status.value)
            return existing
        raise HTTPException(status_code=409, detail="Duplicate idempotency key")

    # --- Enqueue immediately if PENDING ---
    if initial_status == JobStatus.PENDING:
        enqueue_job(job.id, job.priority)

    return job


# ---------------------------------------------------------------------------
# Get a single job
# ---------------------------------------------------------------------------

@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db)) -> Job:
    """Get full status, result, and metadata for a single job."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


# ---------------------------------------------------------------------------
# List jobs with optional filters
# ---------------------------------------------------------------------------

@router.get("/", response_model=JobListResponse)
def list_jobs(
    status: JobStatus | None = Query(default=None),
    job_type: JobType | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> JobListResponse:
    """List jobs with optional filtering by status and/or type."""
    query = select(Job)

    if status:
        query = query.where(Job.status == status)
    if job_type:
        query = query.where(Job.job_type == job_type)

    total = db.scalar(select(func.count()).select_from(query.subquery()))
    jobs = db.scalars(query.order_by(Job.created_at.desc()).limit(limit).offset(offset)).all()

    return JobListResponse(jobs=list(jobs), total=total, limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Cancel a job
# ---------------------------------------------------------------------------

@router.post("/{job_id}/cancel", response_model=JobResponse)
def cancel_job(job_id: int, db: Session = Depends(get_db)) -> Job:
    """
    Cancel a PENDING or SCHEDULED job.

    A job that is already PROCESSING cannot be cancelled via this endpoint
    (the worker holds it). COMPLETED, FAILED, and CANCELLED jobs are also
    rejected.
    """
    job = db.get(Job, job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    logger.info("cancel_job, job_id=%s, current status=%s", job_id, job.status.value)

    if job.status not in (JobStatus.PENDING, JobStatus.SCHEDULED):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel a job in '{job.status}' state. "
                   "Only PENDING or SCHEDULED jobs can be cancelled.",
        )

    job.status = JobStatus.CANCELLED
    db.commit()
    db.refresh(job)
    remove_job(job.id)
    return job


# ---------------------------------------------------------------------------
# Retry a failed job
# ---------------------------------------------------------------------------

@router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(
    job_id: int,
    retry_data: JobRetry | None = None,
    db: Session = Depends(get_db),
) -> Job:
    """
    Manually retry a FAILED job.

    Resets attempt_count to 0 so it gets a full set of fresh retries.
    Optionally override priority or max_attempts for this retry.
    """
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry a job in '{job.status}' state. Only FAILED jobs can be retried.",
        )

    # Apply optional overrides
    if retry_data:
        if retry_data.priority is not None:
            job.priority = retry_data.priority
        if retry_data.max_attempts is not None:
            job.max_attempts = retry_data.max_attempts

    job.status = JobStatus.PENDING
    job.attempt_count = 0
    job.error_message = None
    job.result = None
    job.started_at = None
    job.completed_at = None

    db.commit()
    db.refresh(job)

    enqueue_job(job.id, job.priority)

    logger.info("retry_job, job_id=%s status=%s", job.id, job.status.value)

    return job



