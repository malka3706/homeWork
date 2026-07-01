# AI Tool Usage

## Tools Used

- Claude (Anthropic) — collaborative pair throughout the project

---

## How I Used It

I used Claude as a collaborative tool across the entire project — architecture, implementation, debugging, and documentation. The process was iterative: Claude proposed approaches, I questioned them, we refined them together, and I made the final calls.

I can explain every decision in this codebase. If something is in DECISIONS.md, I understood it before it was written.

---

## Where I Was Actively Involved

**Architecture and design**
I didn't just accept the first suggestion. For example, when Claude initially proposed re-enqueuing failed jobs directly in Redis, I pushed back — if the worker crashes between the failure and the re-enqueue, the retry is silently lost. We iterated until we arrived at the SCHEDULED state approach, where the retry intent is written to PostgreSQL first and survives any crash.

Similarly, the two-layer idempotency design came from a conversation where I asked what happens if a job is cancelled while it's still in the Redis queue. That question led to the worker cancel guard — not something that was obvious from the start.

**Test case design**
The API tests were largely straightforward. The edge cases were mine:

- `test_worker_skips_cancelled_job` — I identified that the realistic race isn't duplicate enqueue, it's cancel-then-dequeue. Claude's initial suggestion tested the wrong scenario. I defined the correct one: enqueue, cancel in DB, worker must skip.
- `test_same_priority_fifo_ordering` — I asked whether equal-priority jobs respect insertion order. Claude confirmed the timestamp tiebreaker in the score formula handles it. I insisted on a test that proves it rather than trusting the math.
- `test_concurrent_workers_no_duplicate_processing` — I asked how we prove BZPOPMIN atomicity under real load. We designed the test together: 3 threads × 10 jobs, `assert attempt_count == 1` for every job. The assertion is the proof, not the logs.
- `test_priority_ordering_real_queue` — I asked for a test that proves priority ordering with real Redis, not a mock.

**Trade-off awareness**
I understood and accepted the trade-offs explicitly — double-execution risk in crash recovery, at-least-once delivery semantics, the ZREM gap that existed before I asked why cancelled jobs weren't removed from Redis immediately. These are documented in DECISIONS.md because I understood them, not just because Claude wrote them.

---

## What Claude Contributed

- Translating design decisions into working Python code
- Catching implementation bugs (enum `.value` in logging, wrong type passed to `ZREM`, null-check ordering in cancel endpoint)
- Suggesting the recovery monitor pattern and score formula, which I then questioned and validated
- Writing documentation structure once the decisions were finalized
