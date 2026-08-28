"""
TruthScore -- Redis client (async, connection-pooled).
Used for distributed caching / rate-limit / queue backing store.
Falls back to local-only mode if Redis is unreachable OR not installed.
"""
import os, asyncio, logging
from typing import Optional

logger = logging.getLogger("truthscore.redis")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "true").lower() in ("1", "true", "yes")

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


def get_async_redis():
    global _aredis_client
    if not REDIS_ENABLED or not _redis_available:
        return None
    if _aredis_client is not None:
        return _aredis_client
    try:
        _aredis_client = redis_async.from_url(
            get_redis_url(0),
            decode_responses=True,
            max_connections=int(os.getenv("REDIS_MAX_CONNECTIONS", "50")),
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        logger.info("Redis (async) connected: %s", get_redis_url(0))
    except Exception as e:
        logger.warning("Redis connection failed — running in local-only mode: %s", e)
        _aredis_client = None
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
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
            health_check_interval=30,
        )
        _sync_redis.ping()
        logger.info("Redis (sync) connected: %s", get_redis_url(0))
    except Exception as e:
        logger.warning("Redis sync connection failed: %s", e)
        _sync_redis = None
    return _sync_redis


async def verify_async_redis():
    """
    Actually open a connection and PING. `from_url` only builds a lazy client,
    so without this a down/absent Redis is never detected and callers waste a
    connect-timeout on every command. Call once at app startup: on failure the
    async client is nulled so `redis_available()` and every `if redis:` guard
    correctly fall back to local-only mode.
    """
    global _aredis_client
    client = get_async_redis()
    if client is None:
        return False
    try:
        await client.ping()
        logger.info("Redis (async) verified: %s", get_redis_url(0))
        return True
    except Exception as e:
        logger.warning("Redis unreachable — local-only mode: %s", e)
        try:
            await client.close()
        except Exception:
            pass
        _aredis_client = None
        return False


async def close_redis():
    global _aredis_client
    if _aredis_client:
        try:
            await _aredis_client.close()
        except Exception:
            pass
        _aredis_client = None


def redis_available():
    return bool(get_async_redis())
