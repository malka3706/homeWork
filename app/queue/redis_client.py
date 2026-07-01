"""
Redis queue client.

Data structure: a single Redis Sorted Set named QUEUE_KEY.
  - Member: job ID (as a string)
  - Score:  a float that encodes priority + insertion order so that
            higher-priority jobs always sort before lower-priority ones.

YOU implement the queue operations below.
The rest of the codebase (API routes, worker) calls these functions —
you only need to fill in the bodies.
"""
import logging
import time
import redis

logger = logging.getLogger(__name__)

from app.core.config import settings

# The sorted-set key that holds all pending job IDs in Redis
QUEUE_KEY = "job_queue"

# Module-level client (lazily created)
_redis_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    """Return a shared Redis client, creating it on first call."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


# ---------------------------------------------------------------------------
# YOUR IMPLEMENTATION — fill in the three functions below
# ---------------------------------------------------------------------------

def enqueue_job(job_id: int, priority: int) -> None:
    """
    Push a job onto the priority queue.

    Called by the API after creating a PENDING job, and by the retry endpoint.

    Hints:
    - Use ZADD on QUEUE_KEY.
    - The score must encode priority so that BZPOPMIN (which pops the LOWEST
      score) picks the most urgent job.  A simple approach: score = -priority.
    - To break ties between jobs of equal priority, you can incorporate
      a timestamp: score = -priority * 1e9 + time.time()  (still negative,
      but slightly higher for newer jobs → older jobs of same priority win).

    Args:
        job_id:   The DB primary key of the job.
        priority: Integer 1–10 (10 = most urgent).
    """

    score = -priority * 1e9 + time.time()
    get_redis_client().zadd(QUEUE_KEY, {str(job_id): score})
    logger.info("enqueue_job ZADDED to redis queue, jobId= %s score=%s", job_id, score)


def dequeue_job(timeout: int = 5) -> int | None:
    """
    Atomically pop the next job from the queue (blocking).

    Called by the worker loop on every iteration.

    Hints:
    - Use BZPOPMIN with the configured timeout.
    - BZPOPMIN returns (key, member, score) or None on timeout.
    - Cast member to int before returning — Redis stores strings.

    Args:
        timeout: Seconds to block waiting for a job.

    Returns:
        The job_id (int) of the next job, or None if the queue was empty
        for the full timeout duration.
    """
    result = get_redis_client().bzpopmin(QUEUE_KEY, timeout)
    if result is None:
        return None
    key, member, score = result
    return int(member)


def remove_job(job_id: int) -> None:
    """
    Remove a specific job from the queue (used when cancelling a PENDING job).

    Hint: ZREM QUEUE_KEY <job_id>

    Args:
        job_id: The DB primary key of the job to remove.
    """

    removed = get_redis_client().zrem(QUEUE_KEY, str(job_id))
    if removed:
        logger.info("remove_job, job_id=%s removed from queue", job_id)
    else:
        logger.info("remove_job, job_id=%s not in queue (already dequeued)", job_id)


def get_queue_depth() -> int:
    """
    Return the number of jobs currently waiting in the queue.

    Used by the /health endpoint.

    Hint: ZCARD QUEUE_KEY
    """
    queue_size = get_redis_client().zcard(QUEUE_KEY)
    logger.info("get_queue_depth, queue size %s",queue_size)
    return  queue_size

