# AI Tool Usage

## Tools I Used

- Claude (Anthropic) — used throughout the project: design discussions, implementation, debugging, and documentation. The process was iterative: I described the requirement, Claude proposed an approach or code, I reviewed and questioned it, and we refined it until I was satisfied. Final decisions were mine.

---

## What Helped Most

**1. Translating design decisions into working code**
Once a decision was made (Redis sorted set as the queue, PostgreSQL as the source of truth, SCHEDULED state for retries), Claude produced the FastAPI routes, SQLAlchemy model, and worker loop quickly and mostly correctly. The priority score formula (`-priority * 1e9 + time.time()`) came from Claude; rather than trusting the math, I added tests that prove both priority ordering and FIFO ordering within the same priority (`test_priority_ordering_real_queue`, `test_same_priority_fifo_ordering`).

**2. Catching small implementation bugs**
Claude caught several bugs during review passes: an enum `.value` mistake in logging, the wrong type passed to `ZREM`, and null-check ordering in the cancel endpoint.

---

## What I Had to Fix

**1. Retry re-enqueue was not crash-safe (concurrency)**
Claude's initial retry design had the worker re-enqueue a failed job directly into Redis. I asked what happens if the worker crashes between the failure and the re-enqueue — the answer is that the retry is silently lost. We changed the design: on failure the worker writes `SCHEDULED` + `scheduled_at` to PostgreSQL first, and a scheduler thread promotes due jobs back to the queue. The retry intent now survives a worker crash.

**2. The cancellation test targeted the wrong race**
Claude's first version of the cancellation test checked for duplicate enqueue. The realistic production race is different: a job is cancelled via the API while its ID is still sitting in the Redis queue, and a worker dequeues it afterwards. I redefined the test (`test_worker_skips_cancelled_job`): enqueue, cancel in the DB, run the worker, assert the handler never ran (`attempt_count == 0`). This also led to adding the worker-side status check as a guard.

---

## What AI Struggled With

- **Crash-window analysis.** Claude was good at implementing a chosen recovery mechanism, but did not proactively surface crash windows (e.g., what happens if a process dies between two specific operations). Each of those scenarios only got analyzed when I asked about it directly, and its first answer sometimes presented a case as fully handled when a smaller residual window remained. I treated every "this is handled" claim as something to verify, not accept — the race condition table in DECISIONS.md is the result, including the trade-offs we knowingly accepted (e.g., possible double execution after timeout-based recovery).
- **Knowing when to stop.** Left unchecked, Claude tended to add abstractions and options beyond what the assignment needed. Keeping the scope tight was my job, not the tool's.
