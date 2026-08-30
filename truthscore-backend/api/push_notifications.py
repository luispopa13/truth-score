"""
TruthScore — Web Push Notifications (VAPID)
"""
import os, json, base64
from datetime import datetime, timezone


def get_vapid_public_key() -> str:
    """Return the VAPID public key from env, or empty string if not set."""
    return os.getenv("VAPID_PUBLIC_KEY", "")


async def subscribe(db, user_id: str, subscription: dict) -> bool:
    """Upsert a push subscription for the given user."""
    try:
        endpoint = subscription.get("endpoint", "")
        if not endpoint:
            return False
        doc = {
            "_id": endpoint,
            "user_id": user_id,
            "subscription": subscription,
            "created_at": datetime.now(timezone.utc),
        }
        await db["push_subscriptions"].replace_one(
            {"_id": endpoint}, doc, upsert=True
        )
        return True
    except Exception as e:
        print(f"[PUSH] subscribe error: {e}")
        return False


async def unsubscribe(db, endpoint: str, user_id: str) -> bool:
    """Delete a push subscription by endpoint + user_id."""
    try:
        result = await db["push_subscriptions"].delete_one(
            {"_id": endpoint, "user_id": user_id}
        )
        return result.deleted_count > 0
    except Exception as e:
        print(f"[PUSH] unsubscribe error: {e}")
        return False


def send_push(subscription: dict, title: str, body: str, url: str = "/") -> bool:
    """Send a Web Push notification using pywebpush (optional dependency).

    Returns False gracefully if pywebpush is not installed or VAPID keys are missing.
    """
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return False

    private_key = os.getenv("VAPID_PRIVATE_KEY", "")
    public_key = os.getenv("VAPID_PUBLIC_KEY", "")
    email = os.getenv("VAPID_EMAIL", "mailto:hello@truthscore.app")

    if not private_key or not public_key:
        return False

    payload = json.dumps({"title": title, "body": body, "url": url})

    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=private_key,
            vapid_claims={"sub": email},
        )
        return True
    except Exception as e:
        print(f"[PUSH] send error: {e}")
        return False


async def notify_user(db, user_id: str, title: str, body: str, url: str = "/") -> int:
    """Send push notifications to all subscriptions for a user.

    Returns count of successful sends.
    """
    try:
        count = 0
        async for doc in db["push_subscriptions"].find({"user_id": user_id}):
            try:
                ok = send_push(doc["subscription"], title, body, url)
                if ok:
                    count += 1
            except Exception as e:
                print(f"[PUSH] notify_user send error: {e}")
        return count
    except Exception as e:
        print(f"[PUSH] notify_user error: {e}")
        return 0


async def notify_claim_watchers(db, claim_text: str, verdict: str, slug: str = "") -> int:
    """Notify all users watching a claim about a new verdict.

    Returns total count of successful push sends.
    """
    try:
        url = f"/claim/{slug}" if slug else "/#result"
        title = "TruthScore: Claim Updated"
        body = f"Verdict: {verdict}"

        total = 0
        async for watch_doc in db["watched_claims"].find({"claim": claim_text}):
            try:
                uid = watch_doc.get("user_id", "")
                if uid:
                    sent = await notify_user(db, uid, title, body, url)
                    total += sent
            except Exception as e:
                print(f"[PUSH] notify_claim_watchers watcher error: {e}")
        return total
    except Exception as e:
        print(f"[PUSH] notify_claim_watchers error: {e}")
        return 0
