# Job Queue Service

A distributed background job processing system built with FastAPI, PostgreSQL, and Redis.

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

## How to Run Tests

Tests use a real PostgreSQL instance (port 5433) and real Redis (port 6379 DB index 1).
Docker must be running before running tests.

```bash
docker-compose up postgres redis -d
pip install -r requirements.txt
pytest tests/ -v
```

19 tests covering:
- Job submission, retrieval, listing
- Job completion, failure, retry, cancellation
- Idempotency (API layer + worker cancel guard)
- Priority ordering with real Redis (high → mid → low)
- FIFO ordering within equal priority
- Worker retry backoff (SCHEDULED state)
- Worker permanent failure after max attempts
- Cancel-then-dequeue race condition

## Example Requests

```bash
# Submit an email job (priority 7)
curl -X POST http://localhost:8000/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
    "job_type": "email",
    "payload": {"to": "hello@example.com", "subject": "Test", "body": "Hello"},
    "priority": 7
  }'

# Submit with idempotency key (safe to retry)
curl -X POST http://localhost:8000/jobs/ \
  -H "Content-Type: application/json" \
  -d '{
    "job_type": "webhook",
    "payload": {"url": "https://example.com/hook"},
    "idempotency_key": "my-unique-key-123"
  }'

# Check job status
curl http://localhost:8000/jobs/1

# List all pending jobs
curl "http://localhost:8000/jobs/?status=pending"

# Cancel a job
curl -X POST http://localhost:8000/jobs/1/cancel

# Retry a failed job
curl -X POST http://localhost:8000/jobs/1/retry

# Health check (includes Redis queue depth)
curl http://localhost:8000/health
```

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

See [DECISIONS.md](DECISIONS.md) for detailed design decisions and trade-offs.  
See [AI_USAGE.md](AI_USAGE.md) for how AI tooling was used during development.
