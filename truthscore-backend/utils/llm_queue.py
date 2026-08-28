"""
TruthScore -- LLM request queue + distributed rate limiting + circuit breaker.

Problem: 1000 concurrent users can fire 1000 parallel requests to
Gemini / Groq.  Providers rate-limit at ~600 RPM. Without coordination
the server crashes with 429s, and when a provider actually goes down the
retry/backoff on every in-flight request MULTIPLIES the load.

Three layers, each degrading gracefully to the next when infra is absent:

  1. In-process semaphore  — caps concurrent LLM calls *per instance*.
  2. Redis token bucket    — caps LLM calls/min *across every instance*
                             (atomic Lua; no-ops to local-only if Redis down).
  3. Circuit breaker       — after N consecutive failures the LLM subsystem is
                             marked DOWN and calls fail fast (return "") for a
                             cooldown window instead of each one grinding through
                             retries+backoff and piling more load on a sick
                             provider. Auto half-opens with a single probe.

Every layer is best-effort: any error in the limiter or breaker falls through
to just running the call. The queue must NEVER be the reason a request fails.
"""
import os, asyncio, logging, time, random
from typing import Optional, Any

logger = logging.getLogger("truthscore.llm_queue")

# ── Layer 1: in-process concurrency cap ──────────────────────────────
MAX_CONCURRENT_LLM = int(os.getenv("LLM_CONCURRENCY", "8"))
_llm_semaphore: Optional[asyncio.Semaphore] = None


def get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM)
    return _llm_semaphore


# ── Layer 2: distributed token bucket (Redis, cross-instance) ────────
# Refill-on-read token bucket. Capacity = short burst allowance; rate =
# steady-state tokens/sec derived from the provider RPM ceiling. Atomic Lua so
# concurrent instances can't oversubscribe. All state lives under ONE Redis key.
_GLOBAL_RPM   = float(os.getenv("LLM_GLOBAL_RPM", "600"))       # provider ceiling
_BUCKET_RATE  = max(_GLOBAL_RPM / 60.0, 0.1)                    # tokens per second
_BUCKET_CAP   = float(os.getenv("LLM_BURST", str(max(10.0, _BUCKET_RATE * 2))))
_BUCKET_KEY   = os.getenv("LLM_BUCKET_KEY", "truthscore:llm:bucket")
# Never block a user forever waiting for a token; past this we proceed anyway
# (priority: the request works > perfect rate adherence). The semaphore + the
# provider's own 429 handling remain as backstops.
_BUCKET_WAIT_MAX = float(os.getenv("LLM_BUCKET_WAIT_MAX", "5"))

# Token-bucket Lua: KEYS[1]=bucket; ARGV=[rate, capacity, now, requested].
# Returns {allowed(1/0), tokens_left_or_wait_seconds}.
_BUCKET_LUA = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
local allowed = 0
local ret = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
  ret = tokens
else
  ret = (requested - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / rate) + 10)
return {allowed, tostring(ret)}
"""

_bucket_sha: Optional[str] = None
_bucket_disabled = False   # set True after a hard Redis/script failure


async def _acquire_global_token() -> None:
    """Best-effort: wait until the cross-instance bucket grants a token.

    No-ops (returns immediately) when Redis is unavailable or the bucket has
    been disabled after a failure — the in-process semaphore still caps load.
    Waits at most _BUCKET_WAIT_MAX total, then proceeds regardless.
    """
    global _bucket_sha, _bucket_disabled
    if _bucket_disabled:
        return
    try:
        from utils.redis_client import get_async_redis
        redis = get_async_redis()
    except Exception:
        redis = None
    if redis is None:
        return

    waited = 0.0
    while True:
        try:
            if _bucket_sha is None:
                _bucket_sha = await redis.script_load(_BUCKET_LUA)
            now = time.time()
            res = await redis.evalsha(
                _bucket_sha, 1, _BUCKET_KEY,
                _BUCKET_RATE, _BUCKET_CAP, now, 1,
            )
            allowed = int(res[0]) == 1
            val = float(res[1])
        except Exception as e:
            # NOSCRIPT after a Redis restart → reload once; any other error →
            # disable the bucket for the process rather than failing the call.
            if "NOSCRIPT" in str(e):
                _bucket_sha = None
                continue
            logger.warning("LLM token bucket disabled (%s) — local-only limiting", e)
            _bucket_disabled = True
            return
        if allowed:
            return
        # Not enough tokens: sleep the refill wait (+ small jitter to avoid a
        # synchronized thundering herd across instances), capped by the budget.
        wait = min(val, _BUCKET_WAIT_MAX - waited) + random.uniform(0, 0.05)
        if waited >= _BUCKET_WAIT_MAX or wait <= 0:
            logger.info("LLM bucket wait budget exhausted — proceeding uncapped")
            return
        await asyncio.sleep(wait)
        waited += wait


# ── Layer 3: circuit breaker (per-process) ───────────────────────────
_CB_THRESHOLD = int(os.getenv("LLM_CB_THRESHOLD", "8"))     # consecutive fails to trip
_CB_COOLDOWN  = float(os.getenv("LLM_CB_COOLDOWN", "20"))   # seconds to stay open
_cb_fails = 0
_cb_opened_at = 0.0
_cb_half_open = False


def _cb_is_open() -> bool:
    """True → fail fast. Auto half-opens after the cooldown to probe recovery."""
    global _cb_half_open
    if _cb_opened_at == 0.0:
        return False
    if (time.time() - _cb_opened_at) >= _CB_COOLDOWN:
        # Cooldown elapsed: admit EXACTLY ONE probe (half-open). The first caller
        # flips the flag and is let through; every other caller in the window
        # keeps failing fast until the probe resolves (_cb_record clears/re-opens).
        if not _cb_half_open:
            _cb_half_open = True
            return False
        return True
    return not _cb_half_open


def _cb_record(success: bool) -> None:
    global _cb_fails, _cb_opened_at, _cb_half_open
    if success:
        if _cb_fails or _cb_opened_at:
            logger.info("LLM circuit breaker reset (subsystem healthy)")
        _cb_fails = 0
        _cb_opened_at = 0.0
        _cb_half_open = False
        return
    _cb_fails += 1
    if _cb_half_open:
        # Probe failed → re-open the circuit for another cooldown window.
        _cb_opened_at = time.time()
        _cb_half_open = False
        logger.warning("LLM circuit breaker re-opened (probe failed)")
    elif _cb_fails >= _CB_THRESHOLD and _cb_opened_at == 0.0:
        _cb_opened_at = time.time()
        logger.warning("LLM circuit breaker OPEN after %d consecutive failures "
                       "— failing fast for %.0fs", _cb_fails, _CB_COOLDOWN)


class LLMJob:
    """
    Runs one LLM call through: circuit breaker → in-process semaphore →
    distributed token bucket → the wrapped coroutine, with jittered
    exponential backoff on transient (429/503/rate/overload/timeout) errors.
    A "" result is treated as a failure signal for the breaker (the underlying
    impl swallows provider errors and returns "").
    """

    def __init__(self, coro_func, *args, **kwargs):
        self._coro_func = coro_func
        self._args = args
        self._kwargs = kwargs

    async def run(self) -> Any:
        # Fail fast when the LLM subsystem is known-down.
        if _cb_is_open():
            logger.warning("LLM circuit open — short-circuiting call (returning '')")
            return ""

        sem = get_llm_semaphore()
        max_attempts = int(os.getenv("LLM_MAX_ATTEMPTS", "3"))
        base = float(os.getenv("LLM_BACKOFF_BASE", "1.0"))
        timeout = float(os.getenv("LLM_TIMEOUT", "30"))
        async with sem:
            attempt = 0
            while attempt < max_attempts:
                await _acquire_global_token()
                try:
                    result = await asyncio.wait_for(
                        self._coro_func(*self._args, **self._kwargs),
                        timeout=timeout,
                    )
                    # Empty string == underlying total failure (impl swallows
                    # provider errors) → feed the breaker but return as-is.
                    _cb_record(success=bool(result))
                    return result
                except asyncio.TimeoutError:
                    attempt += 1
                    _cb_record(success=False)
                    logger.warning("LLM call timed out (attempt %d/%d)", attempt, max_attempts)
                    if _cb_is_open():
                        return ""
                    await asyncio.sleep(self._backoff(base, attempt))
                except Exception as e:
                    err = str(e)
                    transient = any(x in err for x in
                                    ("503", "UNAVAILABLE", "rate", "429", "overload", "timeout"))
                    if transient:
                        attempt += 1
                        _cb_record(success=False)
                        wait = self._backoff(base, attempt)
                        logger.warning("LLM transient error, backing off %.1fs (attempt %d/%d): %s",
                                       wait, attempt, max_attempts, err[:120])
                        if _cb_is_open():
                            return ""
                        await asyncio.sleep(wait)
                    else:
                        # Non-transient (bug, bad request) → don't retry, don't
                        # trip the breaker on it, surface to the caller's guard.
                        raise
            logger.error("LLM call failed after %d retries", max_attempts)
            return ""

    @staticmethod
    def _backoff(base: float, attempt: int) -> float:
        """Full-jitter exponential backoff: random in [0, base * 2**attempt].

        Full jitter (vs fixed 2**attempt) de-synchronizes retries across the
        1000-user fleet so they don't re-hit the provider in lockstep waves.
        Capped so a user never waits absurdly long.
        """
        ceiling = min(base * (2 ** attempt), 30.0)
        return random.uniform(0, ceiling)


async def enqueue_llm_call(coro_func, *args, **kwargs) -> str:
    """
    Execute an LLM call through the concurrency limiter, distributed rate
    limiter, and circuit breaker. Returns the result string (or "" on total
    failure / open circuit).
    """
    job = LLMJob(coro_func, *args, **kwargs)
    return await job.run()
