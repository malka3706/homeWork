# Job Queue Service

A distributed background job processing system built with FastAPI, PostgreSQL, and Redis.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI (Python) |
| Queue | Redis Sorted Set + `BZPOPMIN` |
| Database | PostgreSQL + SQLAlchemy + Alembic |
| Worker | Python background process |
| Validation | Pydantic v2 (per-job-type payload schemas) |
| Testing | pytest + real PostgreSQL + real Redis (no mocks) |
| Containers | Docker + Docker Compose |

---

## Project Structure

```
app/
  api/routes/        FastAPI endpoints (jobs, health)
  worker/
    main.py          Worker loop — dequeue, dispatch, retry logic
    handlers/        One handler per job type (email, webhook, report, batch)
    scheduler.py     Promotes SCHEDULED jobs back to PENDING when delay elapses
    recovery.py      Re-queues PROCESSING jobs stuck longer than 300s
  queue/
    redis_client.py  enqueue_job / dequeue_job / remove_job (BZPOPMIN)
  models/            SQLAlchemy Job model
  schemas/           Pydantic request/response + per-job-type payload validation
  db/                Session, Base, Alembic migrations
tests/
  conftest.py        Real DB + real Redis fixtures, table truncation between tests
  test_jobs.py       19 tests covering API, worker, concurrency, edge cases
```

---

## How to Run

**Prerequisites:** Docker and Docker Compose installed.

```bash
docker-compose up --build
```

This starts:
- PostgreSQL on port 5433
- Redis on port 6379
- FastAPI API on port 8000
- Worker process (separate container)
- Alembic migrations (runs once on startup)

The API will be available at http://localhost:8000  
Interactive docs (Swagger UI): http://localhost:8000/docs

---

## How to Run Tests

Tests use a real PostgreSQL instance (port 5433) and real Redis (port 6379 DB index 1).  
Docker must be running before running tests.

```bash
docker-compose up postgres redis -d
pip install -r requirements.txt
pytest tests/ -v
```

---

## End-to-End Flow — Observed Behavior

The log lines below are real output captured from the test suite (`pytest -s`).  
They show the system's actual behavior at each stage.

### Step 1 — Submit a job (API enqueues into Redis)

```
{"level": "INFO", "logger": "app.api.routes.jobs", "message": "submit_job, idempotency_key=None, initial_status=pending"}
{"level": "INFO", "logger": "app.api.routes.jobs", "message": "job_id=1 submitted, status=pending"}
{"level": "INFO", "logger": "app.queue.redis_client", "message": "enqueue_job ZADDED to redis queue, jobId= 1 score=-3217100931.707"}
```

The score `-3217100931` encodes priority: `score = -priority * 1e9 + time.time()`.  
Higher priority → more negative score → popped first by `BZPOPMIN`.

### Step 2 — Worker picks up and completes the job

```
{"level": "INFO", "logger": "app.worker.main", "message": "Processing job_id=1 type=email attempt=1/3"}
{"level": "INFO", "logger": "app.worker.main", "message": "job_id=1 completed successfully"}
```

`BZPOPMIN` atomically removes the job from Redis and returns it to exactly one worker.  
The worker marks it `PROCESSING` in PostgreSQL before executing the handler.

### Step 3 — Job fails, retry is scheduled (backoff)

```
{"level": "INFO",    "logger": "app.worker.main", "message": "Processing job_id=1 type=webhook attempt=1/3"}
{"level": "WARNING", "logger": "app.worker.main", "message": "job_id=1 failed (attempt 1): Simulated webhook failure"}
{"level": "INFO",    "logger": "app.worker.main", "message": "job_id=1 scheduled for retry in 30s (attempt 1/3)"}
```

On failure, status → `SCHEDULED`, `scheduled_at = now + 30s`.  
The scheduler thread promotes it back to `PENDING` when the delay elapses.  
This survives worker crashes — the retry is written to PostgreSQL, not Redis.

### Step 4 — Job exhausts all attempts → permanently FAILED

```
{"level": "INFO",    "logger": "app.worker.main", "message": "Processing job_id=1 type=webhook attempt=1/1"}
{"level": "WARNING", "logger": "app.worker.main", "message": "job_id=1 failed (attempt 1): Simulated webhook failure"}
{"level": "WARNING", "logger": "app.worker.main", "message": "job_id=1 permanently failed after 1 attempts: Simulated webhook failure"}
```

### Step 5 — Cancel-then-dequeue race condition (worker cancel guard)

```
{"level": "INFO", "logger": "app.queue.redis_client", "message": "enqueue_job ZADDED to redis queue, jobId= 1 score=-3217100562.491"}
{"level": "INFO", "logger": "app.worker.main",        "message": "job_id=1 has status=cancelled, skipping"}
```

Job was enqueued in Redis, then cancelled via API before the worker polled.  
Worker dequeues the job_id, checks `status == PENDING` in PostgreSQL — sees `CANCELLED` — skips.  
`attempt_count` stays 0. The handler never ran.

### Step 6 — Three concurrent workers, 10 jobs, no duplicate processing

```
{"level": "INFO", "logger": "app.worker.main", "message": "Processing job_id=1 type=email attempt=1/3"}
{"level": "INFO", "logger": "app.worker.main", "message": "Processing job_id=3 type=email attempt=1/3"}
{"level": "INFO", "logger": "app.worker.main", "message": "Processing job_id=2 type=email attempt=1/3"}
{"level": "INFO", "logger": "app.worker.main", "message": "job_id=2 completed successfully"}
{"level": "INFO", "logger": "app.worker.main", "message": "Processing job_id=4 type=email attempt=1/3"}
{"level": "INFO", "logger": "app.worker.main", "message": "job_id=3 completed successfully"}
{"level": "INFO", "logger": "app.worker.main", "message": "Processing job_id=5 type=email attempt=1/3"}
{"level": "INFO", "logger": "app.worker.main", "message": "job_id=1 completed successfully"}
```

Three `Processing` lines appear before any `completed` — workers are running in parallel.  
Each job_id appears exactly once. Verified by `assert job.attempt_count == 1` for all 10 jobs.

---

## Test Coverage

| Test | What it proves |
|---|---|
| `test_submit_job_returns_201` | Valid submission returns 201 and PENDING status |
| `test_get_job_by_id` | Submitted job retrievable by ID |
| `test_get_nonexistent_job_returns_404` | Missing job returns 404 |
| `test_job_completion_flow` | Job transitions correctly through PROCESSING → COMPLETED |
| `test_job_failure_increments_attempts` | Failed job increments attempt_count, reaches FAILED after max |
| `test_retry_failed_job` | FAILED job resets to PENDING with attempt_count=0 via retry endpoint |
| `test_cannot_retry_non_failed_job` | Retrying a PENDING job returns 409 |
| `test_cancel_pending_job` | PENDING job cancels successfully, removed from Redis queue |
| `test_cannot_cancel_completed_job` | COMPLETED job cannot be cancelled — returns 409 |
| `test_idempotency_key_prevents_duplicate` | Same key → first call 201, second call 200, same job ID |
| `test_different_idempotency_keys_create_separate_jobs` | Different keys → two distinct jobs |
| `test_list_jobs_filtered_by_status` | List endpoint correctly filters by status |
| `test_worker_processes_email_job` | Worker completes a real email job end-to-end using real Redis + DB |
| `test_worker_retries_on_failure` | Failed job → SCHEDULED state with future scheduled_at (backoff) |
| `test_worker_permanent_failure_after_max_attempts` | Exhausted retries → FAILED with error_message stored |
| `test_worker_skips_cancelled_job` | Cancel-then-dequeue race: worker dequeues but skips cancelled job |
| `test_same_priority_fifo_ordering` | Equal-priority jobs processed in insertion order (timestamp tiebreaker) |
| `test_priority_ordering_real_queue` | High-priority job dequeued before low-priority — proven by completed_at order |
| `test_concurrent_workers_no_duplicate_processing` | 3 workers × 10 jobs: every job has attempt_count=1 — BZPOPMIN atomicity proven |

---

## Architecture

```
Client → FastAPI API → PostgreSQL (source of truth)
                    ↘ Redis sorted set (priority queue)
                              ↓  BZPOPMIN (atomic)
                        Worker Process
                              ↓
                    Job Handlers (email / webhook / report / batch)
                              ↓
                        PostgreSQL (write result)
```

- **PostgreSQL** stores all job state, payloads, results, and history.
- **Redis** holds only job IDs in a sorted set, scored by `-priority * 1e9 + timestamp`.
  Workers use `BZPOPMIN` for atomic, contention-free pickup — no application-level locking needed.
- **Worker** is a separate process (`python -m app.worker.main`).
  It runs two background threads:
  - **Scheduler** — promotes SCHEDULED jobs back to PENDING when their retry delay elapses
  - **Recovery monitor** — re-queues PROCESSING jobs older than 300s (stuck worker detection)

---

## Job Lifecycle

```
                    ┌─────────┐
          ┌────────▶│ PENDING │◀──────────────────────────┐
          │         └────┬────┘                           │
          │              │ worker picks up                │
          │              ▼                                │
          │        ┌────────────┐                        │
          │        │ PROCESSING │                        │
          │        └─────┬──────┘                        │
          │              │                               │
          │      ┌───────┴────────┐                      │
          │      │                │                      │
          │   success           failure                  │
          │      │                │                      │
          │      ▼                ▼                      │
          │  ┌──────────┐   attempts        ┌───────────┐│
          │  │COMPLETED │   remaining?  yes │ SCHEDULED ││
          │  └──────────┘       │      ────▶└─────┬─────┘│
          │                     │                 │      │
          │                     │ no    scheduler │      │
          │                     ▼ (exhausted)     └──────┘
          │               ┌────────┐
          │  manual retry │ FAILED │
          └───────────────└────────┘

PENDING ──cancel──▶ CANCELLED (terminal)
```

---

See [DECISIONS.md](DECISIONS.md) for detailed design decisions and trade-offs.  
See [AI_USAGE.md](AI_USAGE.md) for how AI tooling was used during development.
