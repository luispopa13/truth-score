"""
TruthScore -- Redis client (async, connection-pooled).
Used for distributed caching / rate-limit / queue backing store.
Falls back to local-only mode if Redis is unreachable OR not installed.

Reliability model:
  * SHORT timeouts (default 1.5s) so a down Redis never adds a multi-second
    stall to the request hot path.
  * A CIRCUIT BREAKER: once a health probe fails, the circuit trips for a short
    cooldown (default 30s) during which get_async_redis() returns None
    immediately (fast-fail, zero network wait). After the cooldown a lightweight
    ping re-probes; on success the circuit closes and Redis is used again. This
    replaces the old "permanently disabled" flag that never re-detected recovery.
"""
import os, socket, asyncio, logging, time
from typing import Optional

logger = logging.getLogger("truthscore.redis")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "true").lower() in ("1", "true", "yes")

# Short, hot-path-friendly timeouts (seconds). NOT 3s+ — a down Redis must
# fast-fail, not stall every request.
REDIS_TIMEOUT = float(os.getenv("REDIS_TIMEOUT", "1.5"))
# How long the circuit stays open after a failure before we re-probe.
REDIS_CIRCUIT_COOLDOWN = float(os.getenv("REDIS_CIRCUIT_COOLDOWN", "30"))

# Circuit-breaker state: monotonic timestamp until which the circuit is OPEN
# (Redis treated as down, calls fast-fail). 0 == closed / healthy.
_circuit_open_until = 0.0

# Stable identity for leader election (host + pid).
_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}"

# Async redis client (used inside FastAPI coroutines)
_aredis_client = None
# Sync redis client (used inside RQ workers / threadpool)
_sync_redis = None

# Import redis lazily -- if not installed, we degrade to local-only.
_redis_available = True
try:
    import redis.asyncio as redis_async
    import redis as redis_sync_pkg
except Exception:
    _redis_available = False
    redis_async = None
    redis_sync_pkg = None
    logger.warning("redis-py not installed — running in local-only mode. "
                   "Install with: pip install redis")


def get_redis_url(db: int = 0) -> str:
    url = REDIS_URL
    if db:
        url = url.rsplit("/", 1)[0] + f"/{db}"
    return url


# ── Circuit breaker ─────────────────────────────────────────────

def circuit_open() -> bool:
    """True while the breaker is tripped (Redis assumed down; fast-fail)."""
    return time.monotonic() < _circuit_open_until


def trip_circuit(reason: str = "") -> None:
    """Mark Redis unavailable for the cooldown window. Callers that catch a
    Redis error on the hot path can call this so subsequent calls fast-fail
    instead of each paying the connect timeout."""
    global _circuit_open_until
    _circuit_open_until = time.monotonic() + REDIS_CIRCUIT_COOLDOWN
    logger.warning("Redis circuit OPEN for %.0fs%s",
                   REDIS_CIRCUIT_COOLDOWN, f" ({reason})" if reason else "")


def reset_circuit() -> None:
    """Close the breaker — Redis is healthy again."""
    global _circuit_open_until
    if _circuit_open_until:
        logger.info("Redis circuit CLOSED — healthy again")
    _circuit_open_until = 0.0


def get_async_redis():
    """Return the pooled async client, or None if Redis is disabled/absent or the
    circuit is currently open. Returning None is the clean 'no Redis' signal —
    callers fall back to local-only behavior."""
    global _aredis_client
    if not REDIS_ENABLED or not _redis_available or circuit_open():
        return None
    if _aredis_client is not None:
        return _aredis_client
    try:
        _aredis_client = redis_async.from_url(
            get_redis_url(0),
            decode_responses=True,
            max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "50")),
            socket_connect_timeout=REDIS_TIMEOUT,
            socket_timeout=REDIS_TIMEOUT,
            retry_on_timeout=False,   # don't multiply the wait on the hot path
            health_check_interval=30,
        )
        logger.info("Redis (async) connected: %s", get_redis_url(0))
    except Exception as e:
        logger.warning("Redis connection failed — local-only mode: %s", e)
        _aredis_client = None
        trip_circuit("connect failed")
    return _aredis_client


def get_sync_redis():
    global _sync_redis
    if not REDIS_ENABLED or not _redis_available:
        return None
    if _sync_redis is not None:
        return _sync_redis
    try:
        _sync_redis = redis_sync_pkg.from_url(
            get_redis_url(0),
            decode_responses=True,
            max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "50")),
            socket_connect_timeout=REDIS_TIMEOUT,
            socket_timeout=REDIS_TIMEOUT,
            retry_on_timeout=False,
            health_check_interval=30,
        )
        _sync_redis.ping()
        logger.info("Redis (sync) connected: %s", get_redis_url(0))
    except Exception as e:
        logger.warning("Redis sync connection failed: %s", e)
        _sync_redis = None
    return _sync_redis


async def ping_redis() -> bool:
    """Lightweight liveness probe that also drives the circuit breaker: on
    success the circuit closes, on failure it (re)trips. Safe to call on a
    schedule (e.g. every ~15s) to auto-detect Redis going down AND recovering.
    Returns True if Redis answered."""
    global _aredis_client
    if not REDIS_ENABLED or not _redis_available:
        return False
    # If the circuit is open, we still probe here (this IS the re-detection
    # path) — but bypass get_async_redis()'s open-circuit short-circuit by
    # building/using the client directly.
    client = _aredis_client
    if client is None:
        try:
            client = redis_async.from_url(
                get_redis_url(0),
                decode_responses=True,
                max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "50")),
                socket_connect_timeout=REDIS_TIMEOUT,
                socket_timeout=REDIS_TIMEOUT,
                retry_on_timeout=False,
                health_check_interval=30,
            )
            _aredis_client = client
        except Exception as e:
            trip_circuit(f"build failed: {e}")
            return False
    try:
        await client.ping()
        reset_circuit()
        return True
    except Exception as e:
        trip_circuit(f"ping failed: {e}")
        return False


async def verify_async_redis() -> bool:
    """Open a connection and PING once at startup. `from_url` only builds a lazy
    client, so without this a down/absent Redis is never detected. Delegates to
    ping_redis() so the circuit breaker state is set consistently. On failure the
    circuit is tripped (NOT permanently disabled) so recovery is re-detected
    after the cooldown."""
    if get_async_redis() is None and not circuit_open():
        # Redis disabled or redis-py missing — nothing to verify.
        return False
    ok = await ping_redis()
    if ok:
        logger.info("Redis (async) verified: %s", get_redis_url(0))
    else:
        logger.warning("Redis unreachable at startup — local-only mode "
                       "(will re-probe every %.0fs).", REDIS_CIRCUIT_COOLDOWN)
    return ok


async def close_redis():
    global _aredis_client
    if _aredis_client:
        try:
            await _aredis_client.close()
        except Exception:
            pass
        _aredis_client = None


def redis_available() -> bool:
    return bool(get_async_redis())


# ── Scheduler leader election ───────────────────────────────────
# Background schedulers (email digests, news scanner) must run in exactly ONE
# process, else every gunicorn worker fires them → N duplicate sends. Prefer a
# Redis SETNX lock (works across workers AND across hosts); fall back to the
# gunicorn worker id when Redis is down.

def _scheduler_lock_key(name: str) -> str:
    return f"ts:scheduler:leader:{name}"


async def acquire_scheduler_lock(name: str = "global", ttl: Optional[int] = None) -> bool:
    """Try to become the scheduler leader via SET NX EX. Returns True if THIS
    instance now holds the lock. The lock auto-expires after `ttl` seconds, so a
    dead leader is automatically replaced; the holder must renew_scheduler_lock()
    within the TTL to keep leadership."""
    ttl = int(ttl if ttl is not None else os.getenv("SCHEDULER_LOCK_TTL", "300"))
    redis = get_async_redis()
    if redis is None:
        return False
    try:
        got = await redis.set(_scheduler_lock_key(name), _INSTANCE_ID, nx=True, ex=ttl)
        return bool(got)
    except Exception as e:
        trip_circuit(f"scheduler lock failed: {e}")
        return False


async def renew_scheduler_lock(name: str = "global", ttl: Optional[int] = None) -> bool:
    """Extend our leadership TTL. Returns True while we still own the lock; False
    means we lost it (Redis flushed/expired it, or another instance took over) —
    the caller should then STOP its scheduler loop so leadership stays unique."""
    ttl = int(ttl if ttl is not None else os.getenv("SCHEDULER_LOCK_TTL", "300"))
    redis = get_async_redis()
    if redis is None:
        return False
    try:
        current = await redis.get(_scheduler_lock_key(name))
        if current == _INSTANCE_ID:
            await redis.expire(_scheduler_lock_key(name), ttl)
            return True
        if current is None:
            # Lock lapsed — try to re-acquire it.
            got = await redis.set(_scheduler_lock_key(name), _INSTANCE_ID, nx=True, ex=ttl)
            return bool(got)
        return False
    except Exception as e:
        trip_circuit(f"scheduler renew failed: {e}")
        return False


async def should_run_scheduler(name: str = "global") -> bool:
    """Decide whether THIS process should run background schedulers.

    Priority:
      1. RUN_SCHEDULER env override — "0"/"false" forces OFF, "1"/"true" forces ON.
      2. Redis SETNX leader lock (best: unique across workers and hosts).
      3. Fallback when Redis is down: run only in gunicorn worker 0
         (GUNICORN_WORKER_ID, stamped by gunicorn.conf.py post_fork). Single
         non-gunicorn process (no id set) counts as worker 0.

    Callers holding the lock (case 2) should periodically renew_scheduler_lock()
    and stop scheduling if it returns False.
    """
    override = os.getenv("RUN_SCHEDULER", "").strip().lower()
    if override in ("0", "false", "no"):
        return False
    if override in ("1", "true", "yes"):
        return True

    if await acquire_scheduler_lock(name):
        return True
    if get_async_redis() is not None:
        # Redis is up but someone else holds the lock → not us.
        return False
    # Redis unavailable — fall back to worker-id heuristic.
    return os.getenv("GUNICORN_WORKER_ID", "0") == "0"
