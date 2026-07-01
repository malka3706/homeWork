from sqlalchemy import text, select, func
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends

from app.api.deps import get_db
from app.models.job import Job, JobStatus
from app.schemas.job import HealthResponse
from app.queue.redis_client import get_queue_depth, get_redis_client

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)) -> HealthResponse:
    """
    Health check endpoint.

    Returns connectivity status for the database and Redis, current queue
    depth, and a breakdown of job counts by status.
    """
    # --- Database check ---
    db_status = "connected"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {e}"

    # --- Redis check + queue depth ---
    redis_status = "connected"
    queue_depth = 0
    try:
        redis = get_redis_client()
        redis.ping()
        queue_depth = get_queue_depth()
    except Exception as e:
        redis_status = f"error: {e}"

    # --- Job counts by status ---
    rows = db.execute(
        select(Job.status, func.count(Job.id)).group_by(Job.status)
    ).all()
    job_counts = {status.value: 0 for status in JobStatus}
    for status, count in rows:
        job_counts[status.value] = count

    overall = "ok" if db_status == "connected" and redis_status == "connected" else "degraded"

    return HealthResponse(
        status=overall,
        database=db_status,
        redis=redis_status,
        queue_depth=queue_depth,
        job_counts=job_counts,
    )
