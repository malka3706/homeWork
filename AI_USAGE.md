# AI Tool Usage

## Tools Used

- Claude (Anthropic) — pair programming, code generation, debugging

---

## How I Used AI

I used Claude as a technical collaborator, not as an answer machine.
My role was to drive the architecture and validate every decision —
Claude handled the implementation speed.

---

## Architectural Decisions I Made (AI Assisted With Implementation)

**1. SCHEDULED state instead of immediate re-enqueue on failure**
Claude's first suggestion was to re-enqueue the job in Redis directly after
failure with a delayed score. I questioned this: what happens if the worker
crashes between the failure and the re-enqueue? The retry would be lost.
I pushed for writing SCHEDULED to PostgreSQL first so the retry survives
any crash. Claude then implemented the scheduler thread based on that decision.

**2. Two-layer idempotency**
Claude implemented the DB unique constraint for idempotency at the API level.
I identified the second layer: the worker's cancel guard. A job can be cancelled
via the API while its ID still sits in Redis. Without the status check in the
worker, a cancelled job would still execute. I drove the decision to keep the
status check as a safety net even after adding ZREM.

**3. ZREM on cancel**
The original cancel implementation only updated PostgreSQL and relied on the
worker's status check. I identified that cancelled job IDs would stay in Redis
unnecessarily, wasting worker wakeups. I decided to add ZREM to keep Redis and
PostgreSQL in sync on every state change.

**4. Worker crash recovery timeout**
Claude suggested a generic heartbeat approach. I questioned the trade-off:
the 300s timeout was chosen conservatively to reduce double-execution risk,
accepting a slower recovery in exchange for safety. I understood and documented
why heartbeat would be better but is more complex to implement correctly.

---

## Test Cases I Designed

Claude wrote the initial test scaffolding. I drove the test scenarios:

- **Cancel-then-dequeue race** (`test_worker_skips_cancelled_job`): I identified
  that the realistic race condition is not "same job enqueued twice" but rather
  "job cancelled while already in Redis queue." Claude had initially suggested
  a less realistic duplicate-enqueue scenario. I corrected the scenario.

- **FIFO within same priority** (`test_same_priority_fifo_ordering`): I asked
  whether equal-priority jobs are processed in insertion order. Claude confirmed
  the timestamp tiebreaker handles this. I insisted on a test that proves it
  with real Redis rather than trusting the theory.

- **Priority ordering with real Redis** (`test_priority_ordering_real_queue`):
  I wanted to see the scores in the logs to verify the formula, not just assert
  the outcome. This led to understanding exactly how `-priority * 1e9 + time.time()`
  produces the ordering.

---

## What AI Got Wrong / I Had to Correct

- Suggested double-enqueue as the idempotency test scenario — I identified the
  realistic cancel-race scenario instead.
- Initially described recovery without thinking through the double-execution risk.
  I pushed on "what if the original worker recovers?" which led to the honest
  trade-off documented in DECISIONS.md.
- Left placeholders in DECISIONS.md that I had to identify and fill.
