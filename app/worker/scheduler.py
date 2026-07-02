"""
Scheduled-job promoter.

Runs in a background thread inside the worker process.
Its only job: find SCHEDULED jobs whose scheduled_at has passed,
move them to PENDING, and push them onto the Redis queue.

YOUR IMPLEMENTATION goes in promote_due_jobs().
"""
import logging
import threading
import time
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

# How often (seconds) to check for due scheduled jobs
SCHEDULER_INTERVAL = 10


def promote_due_jobs() -> None:
    """Find all SCHEDULED jobs where scheduled_at <= now() and promote them to PENDING."""
    from sqlalchemy import select
    from app.db.session import SessionLocal
    from app.models.job import Job, JobStatus
    from app.queue.redis_client import enqueue_job

    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        due_jobs = db.scalars(
            select(Job).where(
                Job.status == JobStatus.SCHEDULED,
                Job.scheduled_at <= now,
            )
        ).all()

        if due_jobs:
            logger.info("Promoting %s due scheduled job(s)", len(due_jobs))

        to_enqueue = []
        for job in due_jobs:
            job.status = JobStatus.PENDING
            to_enqueue.append((job.id, job.priority))

        # Commit BEFORE enqueueing: a worker blocked on BZPOPMIN reacts to ZADD
        # within microseconds — if the PENDING commit hasn't landed yet it reads
        # SCHEDULED, skips, and the job is stranded (PENDING in DB, gone from Redis).
        db.commit()

        for job_id, priority in to_enqueue:
            enqueue_job(job_id, priority)
    finally:
        db.close()


def run_scheduler(shutdown: threading.Event) -> None:
    """Loop until shutdown, calling promote_due_jobs every SCHEDULER_INTERVAL seconds."""
    logger.info("Scheduler thread started.")
    while not shutdown.is_set():
        try:
            promote_due_jobs()
        except Exception:
            logger.exception("Error in scheduler — will retry")
        shutdown.wait(timeout=SCHEDULER_INTERVAL)
    logger.info("Scheduler thread stopped.")
