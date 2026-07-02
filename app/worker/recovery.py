"""
Crash recovery monitor.

Runs in a background thread inside the worker process.
Its job: detect jobs that are stuck in PROCESSING because the worker
that claimed them crashed before finishing, and re-queue them.

YOUR IMPLEMENTATION goes in recover_stuck_jobs().
This is a "Should Have" feature — implement it after the core loop works.
"""
import logging
import threading

from app.core.config import settings

logger = logging.getLogger(__name__)


def recover_stuck_jobs() -> None:
    """Find PROCESSING jobs stuck longer than worker_job_timeout and re-queue or fail them."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.db.session import SessionLocal
    from app.models.job import Job, JobStatus
    from app.queue.redis_client import enqueue_job

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.worker_job_timeout)
    db = SessionLocal()
    try:
        stuck_jobs = db.scalars(
            select(Job).where(
                Job.status == JobStatus.PROCESSING,
                Job.started_at < cutoff,
            )
        ).all()

        if stuck_jobs:
            logger.warning("Found %s stuck job(s) — recovering", len(stuck_jobs))

        to_enqueue = []
        for job in stuck_jobs:
            job.attempt_count += 1  # count the timed-out attempt before deciding
            if job.attempt_count < job.max_attempts:
                job.status = JobStatus.PENDING
                to_enqueue.append((job.id, job.priority))
                logger.info("Re-queuing stuck job_id=%s (attempt %s/%s)",
                            job.id, job.attempt_count, job.max_attempts)
            else:
                job.status = JobStatus.FAILED
                job.error_message = "Job timed out after max attempts"
                logger.warning("Permanently failed stuck job_id=%s", job.id)

        # Commit BEFORE enqueueing — same ordering constraint as the scheduler:
        # a live worker can dequeue the ID before an uncommitted PENDING is visible.
        db.commit()

        for job_id, priority in to_enqueue:
            enqueue_job(job_id, priority)
    finally:
        db.close()


def run_recovery(shutdown: threading.Event) -> None:
    """Loop until shutdown, running recovery every worker_crash_recovery_interval seconds."""
    logger.info("Recovery monitor started.")
    while not shutdown.is_set():
        try:
            recover_stuck_jobs()
        except Exception:
            logger.exception("Error in recovery monitor — will retry")
        shutdown.wait(timeout=settings.worker_crash_recovery_interval)
    logger.info("Recovery monitor stopped.")
