"""
TruthScore -- Webhooks
"""
import uuid
import httpx
from datetime import datetime, timezone

VALID_EVENTS = ["verdict_change", "new_check"]
MAX_WEBHOOKS_PER_USER = 5


async def create_webhook(db, user_id: str, url: str, events: list) -> dict:
    """Validate URL, enforce per-user cap, insert and return the new webhook doc."""
    if not url.startswith("https://"):
        raise ValueError("Webhook URL must start with https://")
    unknown = [e for e in events if e not in VALID_EVENTS]
    if unknown:
        raise ValueError(f"Unknown event(s): {unknown}. Valid: {VALID_EVENTS}")
    try:
        count = await db.webhooks.count_documents({"user_id": user_id, "active": True})
        if count >= MAX_WEBHOOKS_PER_USER:
            raise ValueError(f"Maximum {MAX_WEBHOOKS_PER_USER} webhooks per user")
        doc = {
            "_id": str(uuid.uuid4()),
            "user_id": user_id,
            "url": url,
            "events": list(events),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "active": True,
        }
        await db.webhooks.insert_one(doc)
        return doc
    except ValueError:
        raise
    except Exception as e:
        print(f"[WEBHOOKS] create_webhook error: {e}")
        raise


async def list_webhooks(db, user_id: str) -> list:
    """Return all webhooks for a user (strips _id)."""
    try:
        cursor = db.webhooks.find({"user_id": user_id})
        docs = await cursor.to_list(length=100)
        result = []
        for doc in docs:
            d = dict(doc)
            wh_id = d.pop("_id", None)
            d["id"] = wh_id
            result.append(d)
        return result
    except Exception as e:
        print(f"[WEBHOOKS] list_webhooks error: {e}")
        return []


async def delete_webhook(db, user_id: str, webhook_id: str) -> bool:
    """Delete a webhook owned by user_id. Returns True if deleted, False otherwise."""
    try:
        result = await db.webhooks.delete_one({"_id": webhook_id, "user_id": user_id})
        return result.deleted_count > 0
    except Exception as e:
        print(f"[WEBHOOKS] delete_webhook error: {e}")
        return False


async def deliver_webhook(url: str, event: str, payload: dict):
    """POST event payload to webhook URL. Best-effort — errors are silently ignored."""
    body = {
        "event": event,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=body)
    except Exception as e:
        print(f"[WEBHOOKS] Delivery failed url={url} event={event}: {e}")


async def notify_verdict_change(
    db,
    claim: str,
    old_verdict: str,
    new_verdict: str,
    score: int,
    verdict_url: str,
):
    """Fan out a verdict_change event to all subscribed webhooks."""
    try:
        cursor = db.webhooks.find({"events": "verdict_change", "active": True})
        hooks = await cursor.to_list(length=500)
    except Exception as e:
        print(f"[WEBHOOKS] notify_verdict_change fetch error: {e}")
        return

    payload = {
        "claim": claim,
        "old_verdict": old_verdict,
        "new_verdict": new_verdict,
        "score": score,
        "verdict_url": verdict_url,
    }
    for hook in hooks:
        await deliver_webhook(hook["url"], "verdict_change", payload)
