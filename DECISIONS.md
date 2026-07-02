# Design Decisions

## 1. Job Pickup Strategy

**Approach chosen:** Redis Sorted Set + `BZPOPMIN`

**Why:**
Redis Sorted Sets are an ordered collection where each member has a score.
We store job IDs as members, scored by `-priority * 1e9 + time.time()` so that:
- Higher priority → more negative score → popped first by `BZPOPMIN`
- Equal priority → older enqueue time → slightly more negative → FIFO ordering within same priority

`BZPOPMIN` is an atomic blocking operation — it pops exactly one element
and returns it to exactly one caller. Even with multiple concurrent workers,
no two workers can receive the same job ID. This solves duplicate pickup
without any application-level locking.

We store only the job ID in Redis, not the full payload. This means one extra
DB round-trip per job pickup, but keeps Redis as a pure queue and PostgreSQL
as the single source of truth. If Redis goes down, no state is lost — the
operator can re-populate Redis from PENDING jobs in PostgreSQL.

**Trade-offs:**
- Redis is a single point of failure for the queue. PostgreSQL still holds all
  state so nothing is lost, but new jobs won't be dequeued until Redis recovers.
- The extra DB round-trip per job adds latency. Acceptable for background jobs,
  but would be a concern for very high-throughput systems.

---

## 2. Worker Crash Recovery

**Approach chosen:** Timeout-based background monitor

**Why:**
A background thread (`recovery.py`) runs periodically. It queries for jobs in
`PROCESSING` state where `started_at` is older than `WORKER_JOB_TIMEOUT`
(default 300s). These are considered stuck — the worker that claimed them
likely crashed — and they are re-queued.

**What happens when a worker crashes mid-job:**
1. The job remains in `PROCESSING` state in PostgreSQL.
2. Its Redis entry was already removed by `BZPOPMIN` at pickup time.
3. After 300 seconds, the recovery monitor finds it.
4. If `attempt_count < max_attempts`: status → `PENDING`, re-enqueued in Redis.
5. If `attempt_count >= max_attempts`: status → `FAILED`.

**Trade-offs:**
- There is a recovery delay of up to 300 seconds. For most background jobs
  this is acceptable, but time-sensitive jobs would suffer.
- **Double-execution risk:** if the original worker recovers after the monitor
  re-queues the job, two workers could execute the same job simultaneously.
  The timeout was chosen conservatively (300s) to reduce this risk, but it
  cannot eliminate it entirely. The ideal solution is a heartbeat mechanism
  (see section 7).

---

## 3. Priority Queue Implementation

**Approach chosen:** Sorted Set score = `-priority * 1e9 + time.time()`

**Why:**
`BZPOPMIN` removes the member with the **lowest** score. To make higher
priority number = higher urgency, we negate: `-priority * 1e9`.

For jobs with equal priority, the `+ time.time()` term ensures older jobs
have a slightly more negative score and are dequeued first — FIFO within
the same priority level.

The `1e9` multiplier ensures the priority term always dominates the timestamp
term, so priority ordering is never broken by timing differences.

**Verified by tests:**
- `test_priority_ordering_real_queue`: three jobs with priorities 1, 5, 10
  enqueued in reverse order — processed high → mid → low, proven by `completed_at`
- `test_same_priority_fifo_ordering`: two jobs with equal priority — processed
  in insertion order, proven by `completed_at`

---

## 4. Retry Backoff Strategy

**Approach chosen:** SCHEDULED state + scheduler thread promotion

**Why:**
When a job fails and has remaining attempts, the worker does NOT immediately
re-enqueue it in Redis. Instead it:
1. Sets `status = SCHEDULED`
2. Sets `scheduled_at = now + delay` (30s after attempt 1, 120s after attempt 2)
3. Commits to PostgreSQL

A background scheduler thread then polls for SCHEDULED jobs where
`scheduled_at <= now`, sets them back to `PENDING`, and calls `enqueue_job()`.

**Why SCHEDULED state instead of re-enqueuing in Redis directly?**
PostgreSQL is the source of truth. If we re-enqueued in Redis immediately and
the worker crashed between the failure and the re-enqueue, the retry would be
lost forever. By writing `SCHEDULED` to PostgreSQL first, the retry survives
a worker crash: the job stays `SCHEDULED` until the promotion commit lands,
so the scheduler keeps finding it on every scan. (The scheduler and recovery
monitor commit status changes before calling `enqueue_job()`, not after —
same ordering as the API routes — so a live worker can never observe a
promotion mid-flight. The residual risk is a process crash between the
commit and the enqueue call, which is the same known gap described in the
race table below.)

**Backoff delays:**
- Attempt 1 fails → wait 30 seconds
- Attempt 2 fails → wait 120 seconds
- Attempt 3 fails (max_attempts=3) → permanently FAILED, error stored

---

## 5. Idempotency

**Approach chosen:** Two-layer protection — DB unique constraint + worker cancel guard

**Layer 1 — API level:**
The `jobs.idempotency_key` column has a `UNIQUE` constraint. Before inserting,
the API checks whether the key already exists and returns the existing job (HTTP 200)
if so. The unique constraint acts as a safety net for race conditions — if two
requests arrive simultaneously with the same key, one succeeds and the other
receives an `IntegrityError`, which we catch and handle by returning the existing job.

**Layer 2 — Worker level:**
Before executing any job, the worker checks `job.status == PENDING`. If the
status is anything else (CANCELLED, COMPLETED, etc.), it skips the job silently.
This protects against the cancel-then-dequeue race: a job can be cancelled via
the API while its ID still sits in the Redis queue. The worker dequeues it,
checks the status, and discards it without executing.

When a job is cancelled via the API, `ZREM` is also called to remove it from
Redis immediately — keeping the queue clean. The worker status check remains
as a safety net in case `ZREM` fails or races.

**Verified by test:** `test_worker_skips_cancelled_job` — job enqueued, then
cancelled in DB, worker dequeues and skips it, `attempt_count` stays 0.

---

## 6. Input Validation — Per-Job-Type Payload Schemas

**Approach chosen:** Pydantic schemas per job type, validated at the API boundary

**Why:**
Without validation, a malformed payload reaches the handler and raises an unhandled exception.
With `max_attempts=3`, a poison message (e.g. email job missing `to`) would fail, retry after 30s,
retry again after 120s, then permanently FAILED — wasting three worker slots on a job that could
never succeed.

Validation at the API boundary rejects bad payloads immediately with HTTP 422, before the job
is written to the DB or enqueued in Redis. The worker never sees it.

**Implementation:**
Each job type has a dedicated Pydantic schema (`EmailPayload`, `WebhookPayload`, `ReportPayload`,
`BatchPayload`). A `PAYLOAD_SCHEMAS` registry maps `JobType → schema`. The `JobCreate` validator
instantiates the correct schema against the submitted payload — Pydantic raises a `ValidationError`
with field-level error messages if required fields are missing or invalid.

**What is validated:**
- `email` — requires `to` (max 254 chars), `subject` (max 200), `body` (max 10,000)
- `webhook` — requires `url` (max 2048 chars)
- `report` — requires `report_type` (max 100 chars)
- `batch` — requires `items` as a non-empty list (max 1,000 items)

**Trade-offs:**
- Payload schemas are tightly coupled to handler expectations. If a handler changes its required
  fields, the schema must be updated in sync.
- The 10,000 char body limit and 1,000 item batch limit are conservative defaults — a production
  system would make these configurable.

---

## 7. What I Would Do Differently With More Time

**1. Heartbeat-based crash recovery instead of timeout**
The current recovery monitor waits 300 seconds before considering a job stuck.
This has two problems: long recovery delay, and double-execution risk if the
original worker recovers after the monitor re-queues the job.

A better approach: the worker writes a heartbeat timestamp to the DB every few
seconds while processing. The monitor considers a job stuck only if its
heartbeat is older than a short threshold (e.g. 30s). This gives faster
recovery and virtually eliminates double-execution.

**2. ZREM on cancel was initially missing**
The original `cancel_job` implementation only updated PostgreSQL and relied
entirely on the worker's status check to discard cancelled jobs. This meant
cancelled job IDs stayed in the Redis queue until the worker dequeued and
discarded them — wasting worker wakeups and keeping the queue artificially
large. We added `ZREM` during development after recognising this gap.
The lesson: Redis and PostgreSQL state must be kept in sync on every
state-changing operation, not just on the happy path.

**3. PENDING/Redis reconciliation sweep**
A job can end up `PENDING` in PostgreSQL but absent from Redis if a process
crashes between committing the status change and the following `enqueue_job`
call — a window shared by the API, the scheduler, and the recovery monitor —
or if a worker dies between `BZPOPMIN` and the `PROCESSING` commit. Such a
job is never picked up — no background process scans `PENDING` jobs. The fix is a periodic
reconciliation sweep (similar to the existing scheduler/recovery threads) that
re-enqueues `PENDING` jobs older than some threshold that are missing from the
queue. `ZADD` on an existing member is a no-op for membership, so the sweep
would be safe to run even against jobs that are already enqueued.

---

## Race Condition Analysis

| Scenario | What could go wrong | How it is handled | Status |
|---|---|---|---|
| Two requests submit with the same `idempotency_key` simultaneously | Both pass the pre-insert check before either commits — one gets an `IntegrityError` | `IntegrityError` is caught, DB is rolled back, the existing job is returned | ✅ Handled |
| Job is cancelled via API while its ID still sits in the Redis queue | Worker dequeues the job_id, executes the handler on a cancelled job | Worker checks `status == PENDING` before executing — skips if `CANCELLED` | ✅ Handled |
| Two workers call `BZPOPMIN` at the same instant | Both workers receive the same job_id and execute the handler twice | `BZPOPMIN` is atomic — only one caller receives each element, the other blocks | ✅ Handled (Redis guarantee) — verified by `test_concurrent_workers_no_duplicate_processing` |
| User calls `cancel` while worker is mid-execution | Cancel succeeds, handler continues running in parallel | Handled in normal timing: the worker commits `PROCESSING` before executing, so `cancel_job` sees it and returns 409. A narrow window remains — there is no row locking (`SELECT FOR UPDATE`), so if cancel reads `PENDING` just before the worker commits `PROCESSING`, the cancel commits `CANCELLED`, the job still runs, and the final `COMPLETED` commit overwrites it | ⚠️ Narrow unlocked window |
| Worker crashes after `BZPOPMIN` but before setting `PROCESSING` | Job is gone from Redis, DB still shows `PENDING` — nothing re-queues it. Workers only receive work from Redis, recovery only scans `PROCESSING`, the scheduler only scans `SCHEDULED` | Not automatically handled. The mitigation is operational: re-populate Redis from `PENDING` rows in PostgreSQL (see section 1). A periodic reconciliation sweep would close this gap (see section 7) | ⚠️ Known gap |
| API/scheduler/recovery crash (or Redis is unavailable) between committing a status change and the following `enqueue_job` call | Job committed as `PENDING` but never enqueued — same stranded state as above | Same gap in all three call sites — operational re-population from PostgreSQL; a reconciliation sweep would close it | ⚠️ Known gap |
| Worker crashes mid-execution, recovery re-queues the job, original worker recovers | Job executes twice | 300s timeout is conservative to reduce this window — not fully eliminated. Heartbeat would solve this (see section 7) | ⚠️ Accepted trade-off |
| `ZREM` fails during cancel, job_id stays in Redis | Worker dequeues a cancelled job and executes it | Worker cancel guard (status check) catches it regardless of whether `ZREM` succeeded | ✅ Handled |
