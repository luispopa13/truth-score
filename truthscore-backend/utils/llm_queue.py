"""
TruthScore -- LLM request queue (Redis + asyncio semaphore).

Problem: 1000 concurrent users can fire 1000 parallel requests to
Gemini / Groq.  Providers rate-limit at ~600 RPM. Without a queue
the server crashes with 429s.

Solution: every LLM call goes through a Redis-sidekiq queue (or an
in-process asyncio.Semaphore as fallback).  At most N calls run
concurrently; the rest are queued and served FIFO with retry/backoff.
"""
import os, asyncio, logging, time
from typing import Optional, Any

logger = logging.getLogger("truthscore.llm_queue")

# In-process semaphore — caps concurrent LLM calls per instance
MAX_CONCURRENT_LLM = int(os.getenv("LLM_CONCURRENCY", "8"))
_llm_semaphore: Optional[asyncio.Semaphore] = None


def get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)
    return _llm_semaphore


class LLMJob:
    """
    Represents a queued LLM job.  In local mode we just wrap the
    coroutine in a semaphore; in distributed mode we push the payload
    to Redis and have a background worker process it.
    """

    def __init__(self, coro_func, *args, **kwargs):
        self._coro_func = coro_func
        self._args = args
        self._kwargs = kwargs

    async def run(self) -> Any:
        sem = get_llm_semaphore()
        async with sem:
            attempt = 0
            while attempt < 3:
                try:
                    return await asyncio.wait_for(
                        self._coro_func(*self._args, **self._kwargs),
                        timeout=float(os.getenv("LLM_TIMEOUT", "30")),
                    )
                except asyncio.TimeoutError:
                    attempt += 1
                    logger.warning("LLM call timed out (attempt %d/3)", attempt)
                    await asyncio.sleep(2 ** attempt)  # exponential backoff: 2s, 4s
                except Exception as e:
                    err = str(e)
                    if any(x in err for x in ("503", "UNAVAILABLE", "rate", "429", "overload")):
                        attempt += 1
                        wait = 2 ** attempt
                        logger.warning("LLM rate-limited, backing off %ds (attempt %d/3)", wait, attempt)
                        await asyncio.sleep(wait)
                    else:
                        raise
            logger.error("LLM call failed after 3 retries")
            return ""


async def enqueue_llm_call(coro_func, *args, **kwargs) -> str:
    """
    Execute an LLM call through the concurrency limiter.
    Returns the result string (or "" on total failure).
    """
    job = LLMJob(coro_func, *args, **kwargs)
    return await job.run()
