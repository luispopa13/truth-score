"""
TruthScore -- Cache Setup
Shared diskcache instance used across all pipeline modules.
"""
import diskcache
import os
from pathlib import Path

# Cache directory — relative to the backend root
# Use absolute path to avoid issues with working directory
_BACKEND_ROOT = Path(__file__).parent.parent
CACHE_DIR     = os.getenv("CACHE_DIR", str(_BACKEND_ROOT / ".truthscore_cache"))

cache = diskcache.Cache(CACHE_DIR)


def clear_all_caches() -> dict:
    """
    Clear all caches completely and return stats.
    Deletes all cached verify results.
    """
    count = len(cache)
    cache.clear()
    # Also evict expired entries
    try:
        cache.expire()
    except Exception:
        pass
    return {"cleared": count, "cache_dir": CACHE_DIR}