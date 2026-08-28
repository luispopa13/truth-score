"""
TruthScore -- API Key management for browser extensions, widgets,
and programmatic access.

Each API key is a stable 32-byte hex token tied to a user account
and inherits that user's plan limits.  Keys are stored hashed
(sha256) in MongoDB; the plaintext is returned to the user only
once, at creation time.
"""
import os, time, hashlib, secrets, logging
from typing import Optional, List

logger = logging.getLogger("truthscore.api_keys")

KEY_PREFIX = "ts_"  # keys always start with this prefix for identification


def generate_api_key() -> str:
    """Generate a cryptographically-secure API key (plaintext)."""
    raw = secrets.token_urlsafe(32)
    return KEY_PREFIX + raw


def hash_api_key(api_key: str) -> str:
    """Hash an API key for storage (never store plaintext)."""
    return "sha256:" + hashlib.sha256(api_key.encode()).hexdigest()


def get_db():
    from auth import get_db as _gdb
    return _gdb()


async def create_api_key(user_id: str, name: str = "", plan: str = "free") -> dict:
    """
    Create a new API key for *user_id*.  Returns the plaintext key
    (shown only once) plus metadata.
    """
    plaintext = generate_api_key()
    hashed = hash_api_key(plaintext)
    key_id = hashlib.sha256(plaintext.encode()).hexdigest()[:16]

    key_doc = {
        "key_id": key_id,
        "hashed_key": hashed,
        "name": name or f"Key {time.strftime('%Y-%m-%d')}",
        "user_id": user_id,
        "plan": plan,
        "created_at": int(time.time()),
        "last_used": 0,
        "revoked": False,
    }

    db = get_db()
    try:
        await db.api_keys.insert_one(key_doc)
        logger.info("API key created: key_id=%s user=%s plan=%s", key_id, user_id, plan)
    except Exception as e:
        logger.error("Failed to create API key: %s", e)
        raise

    # Return plaintext only once
    return {
        "api_key": plaintext,
        "key_id": key_id,
        "name": key_doc["name"],
        "plan": plan,
        "created_at": key_doc["created_at"],
    }


async def validate_api_key(api_key: str) -> Optional[dict]:
    """
    Validate an API key.  Returns the key document (without the hash)
    if valid, None otherwise.
    """
    if not api_key or not api_key.startswith(KEY_PREFIX):
        return None

    hashed = hash_api_key(api_key)
    db = get_db()
    try:
        doc = await db.api_keys.find_one({"hashed_key": hashed, "revoked": False})
        if doc:
            # Update last_used
            await db.api_keys.update_one(
                {"key_id": doc["key_id"]},
                {"$set": {"last_used": int(time.time())}},
            )
            # Build a pseudo-user dict compatible with the rate-limiter.
            # Use the user's LIVE plan from the users collection — not the plan
            # frozen into the key at creation time — so an upgrade/downgrade
            # takes effect immediately for existing keys instead of stranding
            # the user on their old quota.
            user_doc = await db.users.find_one({"_id": __import__("bson").ObjectId(doc["user_id"])})
            live_plan = (user_doc.get("plan") if user_doc else None) or doc.get("plan", "free")
            return {
                "id": str(user_doc["_id"]) if user_doc else doc["user_id"],
                "email": user_doc.get("email", "") if user_doc else "",
                "name": user_doc.get("name", "") if user_doc else "",
                "plan": live_plan,
                "source": "api_key",
                "key_id": doc["key_id"],
            }
    except Exception as e:
        logger.debug("API key validation error: %s", e)
    return None


async def list_api_keys(user_id: str) -> List[dict]:
    """Return all non-revoked API keys for a user (without hashes)."""
    db = get_db()
    docs = await db.api_keys.find(
        {"user_id": user_id, "revoked": False}
    ).to_list(None)
    return [
        {
            "key_id": d["key_id"],
            "name": d["name"],
            "plan": d["plan"],
            "created_at": d["created_at"],
            "last_used": d.get("last_used", 0),
        }
        for d in docs
    ]


async def revoke_api_key(key_id: str, user_id: str) -> bool:
    """Revoke an API key.  Returns True if a key was actually updated."""
    db = get_db()
    result = await db.api_keys.update_one(
        {"key_id": key_id, "user_id": user_id, "revoked": False},
        {"$set": {"revoked": True}},
    )
    return result.modified_count > 0
