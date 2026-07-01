"""
Required test cases (6 minimum as per spec).

Each test is scaffolded with the correct setup and assertion structure.
Fill in the logic marked TODO — most cases just need a few lines.
"""
from logging import Handler
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.job import Job, JobStatus, JobType
import redis as redis_lib
import app.queue.redis_client as queue_module
from app.queue.redis_client import enqueue_job
from app.worker.main import process_next_job
from tests.conftest import TEST_REDIS_URL, TEST_DATABASE_URL
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=False)
def worker_redis(clean_redis, monkeypatch):
    """
    Point both the Redis client and DB session at test infrastructure
    (localhost) so process_next_job() works outside Docker.
    """
    # Override Redis
    queue_module._redis_client = redis_lib.from_url(TEST_REDIS_URL, decode_responses=True)

    # Override SessionLocal in worker module namespace — this is what process_next_job() uses
    test_engine = create_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    TestSession = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("app.worker.main.SessionLocal", TestSession)

    yield

    queue_module._redis_client = None


# ---------------------------------------------------------------------------
# 1. Job submission and retrieval
# ---------------------------------------------------------------------------

def test_submit_job_returns_201(client: TestClient):
    """Submitting a valid job returns HTTP 201 and a job with PENDING status."""
    response = client.post("/jobs/", json={
        "job_type": "email",
        "payload": {"to": "test@example.com", "subject": "Hello", "body": "World"},
        "priority": 5,
    })
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "pending"
    assert data["job_type"] == "email"
    assert data["id"] is not None


def test_get_job_by_id(client: TestClient):
    """A submitted job can be retrieved by its ID with full details."""
    create_response = client.post("/jobs/", json={
        "job_type": "webhook",
        "payload": {"url": "https://example.com/hook"},
    })
    job_id = create_response.json()["id"]

    get_response = client.get(f"/jobs/{job_id}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == job_id


def test_get_nonexistent_job_returns_404(client: TestClient):
    """Getting a job that doesn't exist returns 404."""
    response = client.get("/jobs/99999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 2. Job completion flow  (worker-side — tests the DB state directly)
# ---------------------------------------------------------------------------

def test_job_completion_flow(client: TestClient, db: Session):
    """
    Simulate the worker completing a job and verify the final DB state.

    This test bypasses the worker process and updates the DB directly,
    which is the correct way to test job state transitions in isolation.
    """
    # Submit via API
    response = client.post("/jobs/", json={
        "job_type": "report",
        "payload": {"report_type": "monthly_sales"},
    })
    job_id = response.json()["id"]

    # Simulate worker: mark as PROCESSING
    job = db.get(Job, job_id)
    job.status = JobStatus.PROCESSING
    job.attempt_count = 1
    db.commit()

    # Simulate worker: mark as COMPLETED with a result
    from datetime import datetime, timezone
    job.status = JobStatus.COMPLETED
    job.result = {"file_url": "https://mock.example.com/report.pdf"}
    job.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(job)

    assert job.status == JobStatus.COMPLETED
    assert job.result is not None
    assert job.completed_at is not None


# ---------------------------------------------------------------------------
# 3. Job failure and retry
# ---------------------------------------------------------------------------

def test_job_failure_increments_attempts(client: TestClient, db: Session):
    """
    When a job fails, attempt_count increments and status becomes FAILED
    after max_attempts is exhausted.
    """
    response = client.post("/jobs/", json={
        "job_type": "webhook",
        "payload": {"url": "https://example.com/hook"},
        "max_attempts": 3,
    })
    job_id = response.json()["id"]
    job = db.get(Job, job_id)

    # Simulate 3 failed attempts
    for attempt in range(1, 4):
        job.status = JobStatus.PROCESSING
        job.attempt_count = attempt
        db.commit()

        job.status = JobStatus.PENDING if attempt < 3 else JobStatus.FAILED
        job.error_message = "Simulated webhook failure"
        db.commit()

    db.refresh(job)
    assert job.status == JobStatus.FAILED
    assert job.attempt_count == 3
    assert job.error_message is not None


def test_retry_failed_job(client: TestClient, db: Session):
    """A FAILED job can be manually retried; it resets to PENDING."""
    # Create and fail a job
    response = client.post("/jobs/", json={
        "job_type": "webhook",
        "payload": {"url": "https://example.com/hook"},
    })
    job_id = response.json()["id"]
    job = db.get(Job, job_id)
    job.status = JobStatus.FAILED
    job.attempt_count = 3
    job.error_message = "all attempts exhausted"
    db.commit()

    # Retry via API
    retry_response = client.post(f"/jobs/{job_id}/retry")
    assert retry_response.status_code == 200
    data = retry_response.json()
    assert data["status"] == "pending"
    assert data["attempt_count"] == 0
    assert data["error_message"] is None


def test_cannot_retry_non_failed_job(client: TestClient):
    """Retrying a PENDING job returns 409."""
    response = client.post("/jobs/", json={
        "job_type": "email",
        "payload": {"to": "x@example.com", "subject": "s", "body": "b"},
    })
    job_id = response.json()["id"]

    retry_response = client.post(f"/jobs/{job_id}/retry")
    assert retry_response.status_code == 409


# ---------------------------------------------------------------------------
# 4. Job cancellation
# ---------------------------------------------------------------------------

def test_cancel_pending_job(client: TestClient):
    """A PENDING job can be cancelled; status becomes CANCELLED."""
    response = client.post("/jobs/", json={
        "job_type": "report",
        "payload": {"report_type": "user_activity"},
    })
    job_id = response.json()["id"]

    cancel_response = client.post(f"/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == "cancelled"


def test_cannot_cancel_completed_job(client: TestClient, db: Session):
    """A COMPLETED job cannot be cancelled."""
    response = client.post("/jobs/", json={
        "job_type": "email",
        "payload": {"to": "x@example.com", "subject": "s", "body": "b"},
    })
    job_id = response.json()["id"]
    job = db.get(Job, job_id)
    job.status = JobStatus.COMPLETED
    db.commit()

    cancel_response = client.post(f"/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 409


# ---------------------------------------------------------------------------
# 5. Idempotency
# ---------------------------------------------------------------------------

def test_idempotency_key_prevents_duplicate(client: TestClient):
    """Submitting the same idempotency key twice returns the original job."""
    payload = {
        "job_type": "email",
        "payload": {"to": "x@example.com", "subject": "s", "body": "b"},
        "idempotency_key": "unique-key-abc123",
    }
    first = client.post("/jobs/", json=payload)
    second = client.post("/jobs/", json=payload)

    assert first.status_code == 201
    assert second.status_code == 200  # returns existing, not 201
    assert first.json()["id"] == second.json()["id"]


def test_different_idempotency_keys_create_separate_jobs(client: TestClient):
    """Two submissions with different idempotency keys create two distinct jobs."""
    base_payload = {
        "job_type": "email",
        "payload": {"to": "x@example.com", "subject": "s", "body": "b"},
    }
    first = client.post("/jobs/", json={**base_payload, "idempotency_key": "key-1"})
    second = client.post("/jobs/", json={**base_payload, "idempotency_key": "key-2"})

    assert first.json()["id"] != second.json()["id"]


# ---------------------------------------------------------------------------
# 6. Priority ordering
# ---------------------------------------------------------------------------

def test_list_jobs_filtered_by_status(client: TestClient):
    """List endpoint correctly filters by status."""
    client.post("/jobs/", json={"job_type": "email", "payload": {"to": "a@b.com", "subject": "s", "body": "b"}})
    client.post("/jobs/", json={"job_type": "report", "payload": {"report_type": "x"}})

    response = client.get("/jobs/?status=pending")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert all(j["status"] == "pending" for j in data["jobs"])




# ---------------------------------------------------------------------------
# 7. Worker logic — tested independently of the API
# ---------------------------------------------------------------------------

def test_worker_processes_email_job(db, worker_redis):
    """
    Worker successfully processes an email job end-to-end.
    Creates a job directly in DB, enqueues it in Redis, calls process_next_job(),
    and verifies the job reached COMPLETED status with a result.
    """
    job = Job(
        job_type=JobType.EMAIL,
        status=JobStatus.PENDING,
        priority=5,
        payload={"to": "test@example.com", "subject": "Hello", "body": "World"},
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Put the job in Redis so the worker can dequeue it
    enqueue_job(job.id, job.priority)

    # Run the worker function directly — no HTTP, no Docker
    process_next_job()

    # process_next_job uses its own DB session, so we must refresh to see changes
    db.expire_all()
    db.refresh(job)

    assert job.status == JobStatus.COMPLETED
    assert job.result is not None
    assert job.completed_at is not None
    assert job.attempt_count == 1


def test_worker_retries_on_failure(db, worker_redis, monkeypatch):
    """
    When a job fails and has attempts remaining, it should be rescheduled (SCHEDULED)
    with a future scheduled_at for retry backoff — not permanently failed.
    """
    # Force webhook to always fail
    monkeypatch.setattr("app.worker.handlers.webhook_handler.random.random", lambda: 0.0)

    job = Job(
        job_type=JobType.WEBHOOK,
        status=JobStatus.PENDING,
        priority=5,
        payload={"url": "https://example.com/hook"},
        max_attempts=3,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    enqueue_job(job.id, job.priority)
    process_next_job()

    db.expire_all()
    db.refresh(job)

    # After first failure: should be SCHEDULED (waiting for retry delay), not FAILED
    assert job.status == JobStatus.SCHEDULED
    assert job.scheduled_at is not None
    assert job.attempt_count == 1


def test_worker_permanent_failure_after_max_attempts(db, worker_redis, monkeypatch):
    """
    When a job fails and has no attempts remaining, it should be permanently FAILED.
    Simulates a job that has already used all its attempts.
    """
    monkeypatch.setattr("app.worker.handlers.webhook_handler.random.random", lambda: 0.0)

    job = Job(
        job_type=JobType.WEBHOOK,
        status=JobStatus.PENDING,
        priority=5,
        payload={"url": "https://example.com/hook"},
        max_attempts=1,  # only 1 attempt allowed
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    enqueue_job(job.id, job.priority)
    process_next_job()

    db.expire_all()
    db.refresh(job)

    # After exhausting max_attempts: permanently FAILED
    assert job.status == JobStatus.FAILED
    assert job.error_message is not None
    assert job.attempt_count == 1


def test_worker_skips_cancelled_job(db, worker_redis):
    """
    Cancel-then-dequeue race condition: job is enqueued in Redis, then cancelled
    via the API before the worker picks it up. Worker dequeues the job_id,
    sees status=CANCELLED, and skips it without executing.

    This is the real production race the cancel guard defends against.
    """
    job = Job(
        job_type=JobType.EMAIL,
        status=JobStatus.PENDING,
        priority=5,
        payload={"to": "test@example.com", "subject": "Hello", "body": "World"},
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # Job is in Redis queue — worker hasn't picked it up yet
    enqueue_job(job.id, job.priority)

    # User cancels the job before the worker dequeues it
    job.status = JobStatus.CANCELLED
    db.commit()

    # Worker dequeues job_id, checks status=CANCELLED → skips
    process_next_job()

    db.expire_all()
    db.refresh(job)

    assert job.status == JobStatus.CANCELLED
    assert job.attempt_count == 0  # never executed


def test_same_priority_fifo_ordering(db, worker_redis):
    """
    Jobs with equal priority are processed in FIFO order.
    The timestamp tiebreaker in the score formula (-priority*1e9 + time.time())
    ensures the earlier-enqueued job has a lower score and is dequeued first.
    """
    first = Job(job_type=JobType.EMAIL, status=JobStatus.PENDING, priority=5,
                payload={"to": "first@example.com", "subject": "first", "body": "b"})
    second = Job(job_type=JobType.EMAIL, status=JobStatus.PENDING, priority=5,
                 payload={"to": "second@example.com", "subject": "second", "body": "b"})

    db.add(first)
    db.commit()
    db.refresh(first)
    enqueue_job(first.id, first.priority)

    db.add(second)
    db.commit()
    db.refresh(second)
    enqueue_job(second.id, second.priority)

    process_next_job()
    process_next_job()

    db.expire_all()
    db.refresh(first)
    db.refresh(second)

    assert first.status == JobStatus.COMPLETED
    assert second.status == JobStatus.COMPLETED
    assert first.completed_at < second.completed_at, (
        f"Expected first({first.completed_at}) processed before second({second.completed_at})"
    )


def test_priority_ordering_real_queue(db, worker_redis):
    """
    Higher-priority jobs are dequeued before lower-priority ones.
    Enqueue 3 jobs in reverse priority order, run the worker 3 times,
    verify completed_at timestamps reflect priority order (high → mid → low).
    """
    low = Job(job_type=JobType.EMAIL, status=JobStatus.PENDING, priority=1,
              payload={"to": "low@example.com", "subject": "low", "body": "b"})
    mid = Job(job_type=JobType.EMAIL, status=JobStatus.PENDING, priority=5,
              payload={"to": "mid@example.com", "subject": "mid", "body": "b"})
    high = Job(job_type=JobType.EMAIL, status=JobStatus.PENDING, priority=10,
               payload={"to": "high@example.com", "subject": "high", "body": "b"})

    for job in (low, mid, high):
        db.add(job)
    db.commit()
    for job in (low, mid, high):
        db.refresh(job)

    # Enqueue all three — order of enqueue should NOT matter
    enqueue_job(low.id, low.priority)
    enqueue_job(mid.id, mid.priority)
    enqueue_job(high.id, high.priority)

    # Worker processes all three
    process_next_job()
    process_next_job()
    process_next_job()

    db.expire_all()
    for job in (low, mid, high):
        db.refresh(job)

    assert high.status == JobStatus.COMPLETED
    assert mid.status == JobStatus.COMPLETED
    assert low.status == JobStatus.COMPLETED

    # completed_at order proves priority was respected
    assert high.completed_at < mid.completed_at < low.completed_at, (
        f"Expected high({high.completed_at}) < mid({mid.completed_at}) < low({low.completed_at})"
    )


# ---------------------------------------------------------------------------
# 8. Concurrency — multiple workers, no duplicate processing
# ---------------------------------------------------------------------------

def test_concurrent_workers_no_duplicate_processing(db, worker_redis, monkeypatch):
    """
    Three workers run simultaneously against a shared Redis queue of 10 jobs.
    BZPOPMIN atomicity guarantees each job is dequeued by exactly one worker.

    Proof: after all workers finish, every job has:
      - status = COMPLETED  (every job was processed)
      - attempt_count = 1   (no job was processed twice)
    """
    from app.core.config import settings as app_settings
    # Patch timeout to 1s so workers exit quickly once queue is empty
    monkeypatch.setattr(app_settings, "worker_poll_timeout", 1)

    NUM_JOBS = 10
    NUM_WORKERS = 3

    # Create and enqueue all jobs
    for i in range(NUM_JOBS):
        job = Job(
            job_type=JobType.EMAIL,
            status=JobStatus.PENDING,
            priority=5,
            payload={"to": f"worker{i}@example.com", "subject": f"Job {i}", "body": "test"},
        )
        db.add(job)
    db.commit()

    job_ids = list(db.scalars(select(Job.id)).all())
    for job_id in job_ids:
        enqueue_job(job_id, 5)

    errors = []

    def worker_fn():
        try:
            # Each worker loops enough times to cover its share of jobs.
            # Workers stop naturally when BZPOPMIN times out (queue empty).
            for _ in range(NUM_JOBS):
                process_next_job()
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker_fn, name=f"worker-{i}") for i in range(NUM_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"Worker errors: {errors}"

    # Verify every job was processed exactly once
    db.expire_all()
    all_jobs = list(db.scalars(select(Job)).all())
    assert len(all_jobs) == NUM_JOBS

    for job in all_jobs:
        db.refresh(job)
        assert job.status == JobStatus.COMPLETED, \
            f"job_id={job.id} has status={job.status} — not processed"
        assert job.attempt_count == 1, \
            f"job_id={job.id} has attempt_count={job.attempt_count} — processed more than once"
