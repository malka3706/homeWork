# AI Tool Usage

## Tools Used

- Claude (Anthropic) — implementation pair, code generation, debugging

---

## My Role

I drove the architecture and owned all design decisions. Claude handled implementation speed — translating decisions into code, catching syntax issues, and running iterations. Every architectural choice in DECISIONS.md reflects my reasoning, not AI output.

---

## Key Architectural Decisions I Made

**Retry via SCHEDULED state, not Redis re-enqueue**
The natural first approach is to re-enqueue a failed job directly in Redis with a delayed score. I rejected this: if the worker crashes between the failure and the re-enqueue, the retry is silently lost. I designed the SCHEDULED state approach — write the retry intent to PostgreSQL first, promote via scheduler thread — so retries survive any crash. PostgreSQL is the source of truth, not Redis.

**Two-layer idempotency**
I identified that a DB unique constraint alone is not sufficient. A job can be cancelled via the API while its ID still sits in the Redis queue — the worker would dequeue and execute a cancelled job. I designed the second layer: the worker's status check before execution, making cancel-then-dequeue safe regardless of timing. I also identified that `ZREM` should be called on cancel to keep Redis clean, and that the status check must remain as a safety net even after adding `ZREM`.

**Recovery timeout tradeoff**
I understood and documented the double-execution risk in crash recovery — the 300s timeout reduces the window but cannot eliminate it. I chose to accept this tradeoff honestly rather than over-engineer a heartbeat solution within the assignment scope, and documented it explicitly in DECISIONS.md.

---

## Test Cases I Designed

The core API tests were scaffolded. I owned the worker and edge-case tests — scenarios that required understanding the system's failure modes, not just its happy path:

**`test_worker_skips_cancelled_job`**
Claude's initial suggestion was to test duplicate enqueue as the idempotency scenario. I identified this was unrealistic — the actual race is a job cancelled via API while already sitting in Redis. I designed the correct scenario: enqueue, cancel in DB, worker dequeues and must skip. This test directly proves the cancel guard works against the real production race.

**`test_worker_retries_on_failure` / `test_worker_permanent_failure_after_max_attempts`**
I defined what needed to be asserted: not just that the job failed, but that `status=SCHEDULED` with a future `scheduled_at` proves the backoff mechanism, and that `status=FAILED` with `error_message` proves exhaustion. The distinction between these two tests proves the retry state machine works at both ends.

**`test_same_priority_fifo_ordering`**
I asked whether equal-priority jobs are processed in insertion order — the timestamp tiebreaker in the score formula. Claude confirmed the theory; I insisted on a test that proves it with real Redis rather than trusting the formula.

**`test_concurrent_workers_no_duplicate_processing`**
I drove the decision to implement a real concurrency test: 3 threads × 10 jobs, asserting `attempt_count == 1` for every job after all threads finish. The assertion — not the logs — is the proof. Logs show interleaving; `attempt_count == 1` proves BZPOPMIN atomicity held under concurrent load.

---

## What AI Handled

- Translating architectural decisions into working Python code
- Identifying bugs during implementation (enum `.value` in log formatting, wrong argument type in `ZREM` call, dead code in routes)
- Writing test scaffolding once the scenario was defined
- Structuring documentation once the decisions were made
