"""
Worker entry point.

Run this as a separate process:
    python -m app.worker.main

Responsibilities:
  1. Pull the next job ID from Redis (via dequeue_job).
  2. Load the full job record from PostgreSQL.
  3. Mark it PROCESSING (and record started_at).
  4. Dispatch to the correct handler.
  5. On success  → mark COMPLETED, store result.
  6. On failure  → apply retry logic with exponential backoff,
                   or mark permanently FAILED after max_attempts.

Also starts two background threads:
  - scheduler_thread : promotes SCHEDULED jobs when their time arrives.
  - recovery_thread  : finds stuck PROCESSING jobs and re-queues them.

Retry backoff strategy (documented in DECISIONS.md):
  Failure after attempt 1 → SCHEDULED, scheduled_at = now + 30s
  Failure after attempt 2 → SCHEDULED, scheduled_at = now + 120s
  Failure after attempt N >= max_attempts → FAILED permanently
  The scheduler thread promotes SCHEDULED jobs back to PENDING when their time arrives.
"""
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.job import Job, JobStatus, JobType
from app.queue.redis_client import dequeue_job, enqueue_job
from app.worker.handlers.email_handler import EmailHandler
from app.worker.handlers.webhook_handler import WebhookHandler
from app.worker.handlers.report_handler import ReportHandler
from app.worker.handlers.batch_handler import BatchHandler
from app.worker.scheduler import run_scheduler
from app.worker.recovery import run_recovery

logger = logging.getLogger(__name__)

# Graceful shutdown flag — set to True by the SIGTERM/SIGINT handler
_shutdown = threading.Event()

# Maps job_type → handler class
HANDLER_REGISTRY = {
    JobType.EMAIL: EmailHandler,
    JobType.WEBHOOK: WebhookHandler,
    JobType.REPORT: ReportHandler,
    JobType.BATCH: BatchHandler,
}

# Backoff delays (seconds) indexed by attempt number (1-based).
# Attempt 1 fails → wait 30s; attempt 2 fails → wait 120s; beyond → last value.
BACKOFF_DELAYS = [30, 120]


def _handle_signal(signum, frame) -> None:
    logger.info("Shutdown signal received, finishing current job…")
    _shutdown.set()


def process_next_job() -> None:
    """Dequeue one job and execute it."""
    job_id = dequeue_job(timeout=settings.worker_poll_timeout)
    if job_id is None:
        return

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.warning("job_id=%s not found in DB — skipping", job_id)
            return

        # Cancel guard: another process may have cancelled the job after enqueue
        if job.status != JobStatus.PENDING:
            logger.info("job_id=%s has status=%s, skipping", job_id, job.status.value)
            return

        # Claim the job
        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        job.attempt_count += 1
        db.commit()

        logger.info("Processing job_id=%s type=%s attempt=%s/%s",
                    job_id, job.job_type.value, job.attempt_count, job.max_attempts)

        handler_class = HANDLER_REGISTRY.get(job.job_type)
        if handler_class is None:
            raise RuntimeError(f"No handler registered for job_type={job.job_type!r}")

        handler = handler_class()
        try:
            result = handler.execute(job, db=db)
        except Exception as exc:
            logger.warning("job_id=%s failed (attempt %s): %s", job_id, job.attempt_count, exc)
            _handle_failure(job, exc, db)
            db.commit()
            return

        job.status = JobStatus.COMPLETED
        job.result = result
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("job_id=%s completed successfully", job_id)

    finally:
        db.close()


def _handle_failure(job: Job, exc: Exception, db) -> None:
    """Apply retry backoff or permanently fail the job."""
    if job.attempt_count < job.max_attempts:
        delay_index = job.attempt_count - 1  # 0-based: first failure → index 0
        delay = BACKOFF_DELAYS[min(delay_index, len(BACKOFF_DELAYS) - 1)]
        job.status = JobStatus.SCHEDULED
        job.scheduled_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logger.info("job_id=%s scheduled for retry in %ss (attempt %s/%s)",
                    job.id, delay, job.attempt_count, job.max_attempts)
    else:
        job.status = JobStatus.FAILED
        job.error_message = str(exc)
        logger.warning("job_id=%s permanently failed after %s attempts: %s",
                       job.id, job.attempt_count, exc)


def run_worker() -> None:
    """Main worker loop. Runs until _shutdown is set."""
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Worker starting…")

    # Start background threads
    threading.Thread(target=run_scheduler, args=(_shutdown,), daemon=True, name="scheduler").start()
    threading.Thread(target=run_recovery, args=(_shutdown,), daemon=True, name="recovery").start()

    while not _shutdown.is_set():
        try:
            process_next_job()
        except Exception:
            logger.exception("Unhandled error in worker loop — continuing")

    logger.info("Worker stopped.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": "%(message)s"}',
    )
    run_worker()
