"""
TruthScore Twitter/X Bot
=========================
Responds to @mentions with fact-check verdicts.

Setup:
1. Create a Twitter Developer App with OAuth 2.0 + v2 API access
2. Enable "Read and Write" permissions
3. Set up a webhook or use polling mode (polling is simpler, no server setup)
4. Env vars: TWITTER_BEARER_TOKEN, TWITTER_API_KEY, TWITTER_API_SECRET,
             TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_SECRET, TWITTER_BOT_USERNAME

Two modes:
  - Webhook: POST /twitter/webhook receives Account Activity API events
  - Polling: POST /twitter/poll-mentions checks recent mentions (call every 2min via cron)
"""
import os
import hmac
import hashlib
import base64
import httpx

TWITTER_BEARER = os.getenv("TWITTER_BEARER_TOKEN", "")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_SECRET = os.getenv("TWITTER_ACCESS_SECRET", "")
TWITTER_BOT_USERNAME = os.getenv("TWITTER_BOT_USERNAME", "TruthScoreBot")
TWITTER_BOT_USER_ID = os.getenv("TWITTER_BOT_USER_ID", "")

_last_mention_id: str = ""  # in-memory, reset on server restart


def _make_auth_header(method: str, url: str, params: dict) -> str:
    """Generate OAuth 1.0a Authorization header for Twitter API v1.1."""
    import time
    import urllib.parse
    import random
    nonce = base64.b64encode(random.randbytes(16)).decode().replace("=", "").replace("+", "").replace("/", "")
    ts = str(int(time.time()))
    oauth_params = {
        "oauth_consumer_key": TWITTER_API_KEY,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": ts,
        "oauth_token": TWITTER_ACCESS_TOKEN,
        "oauth_version": "1.0",
    }
    all_params = {**params, **oauth_params}
    sorted_params = "&".join(f"{urllib.parse.quote(k,'')}&{urllib.parse.quote(str(v),'')}" for k, v in sorted(all_params.items()))
    base = f"{method.upper()}&{urllib.parse.quote(url,'')}&{urllib.parse.quote(sorted_params,'')}"
    signing_key = f"{urllib.parse.quote(TWITTER_API_SECRET,'')}&{urllib.parse.quote(TWITTER_ACCESS_SECRET,'')}"
    sig = base64.b64encode(hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth_params["oauth_signature"] = sig
    header = "OAuth " + ", ".join(f'{urllib.parse.quote(k,"")}="{urllib.parse.quote(str(v),"")}"' for k, v in sorted(oauth_params.items()))
    return header


def verdict_emoji(v: str) -> str:
    return {"TRUE": "✅", "FALSE": "❌", "UNCERTAIN": "⚠️", "MIXED": "🔀"}.get((v or "").upper(), "⚠️")


async def post_reply(tweet_id: str, reply_to_user: str, text: str) -> bool:
    """Post a reply tweet using Twitter API v2."""
    if not TWITTER_API_KEY:
        return False
    url = "https://api.twitter.com/2/tweets"
    payload = {
        "text": f"@{reply_to_user} {text}"[:280],
        "reply": {"in_reply_to_tweet_id": tweet_id},
    }
    auth = _make_auth_header("POST", url, {})
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, json=payload, headers={"Authorization": auth, "Content-Type": "application/json"})
        return r.status_code in (200, 201)


async def get_recent_mentions(since_id: str = "") -> list[dict]:
    """Poll recent mentions using Twitter API v2 search."""
    if not TWITTER_BEARER:
        return []
    query = f"@{TWITTER_BOT_USERNAME} -is:retweet -from:{TWITTER_BOT_USERNAME}"
    params = {"query": query, "max_results": 10, "tweet.fields": "author_id,text,id"}
    if since_id:
        params["since_id"] = since_id
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params=params,
            headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data.get("data") or []


async def handle_mention(tweet: dict) -> None:
    """Process a single @mention tweet."""
    tweet_id = tweet.get("id", "")
    text = tweet.get("text", "")
    author_id = tweet.get("author_id", "")
    # Strip @mention
    claim = " ".join(w for w in text.split() if not w.startswith("@")).strip()
    if len(claim) < 10:
        return
    try:
        # Verify in-process — an HTTP self-call to /verify would round-trip the
        # public proxy AND count against the anonymous rate limit (all mentions
        # would share one server-IP bucket).
        from pipeline.verify import verify_claim
        from models import VerifyRequest
        from config import get_public_base_url
        result = await verify_claim(VerifyRequest(text=claim[:3000]))
        d = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        v = (d.get("verdict") or "UNCERTAIN").upper()
        score = d.get("score", 50)
        explanation = (d.get("explanation") or "")[:160]
        vid = d.get("_verdictId", "")
        link = f"{get_public_base_url().rstrip('/')}/v/{vid}" if vid else ""
        reply = f"{verdict_emoji(v)} {v} {score}/100\n{explanation}"
        if link:
            reply += f"\n🔗 {link}"
        # Get username from author_id
        username = tweet.get("author_username", "user")
        await post_reply(tweet_id, username, reply)
    except Exception as e:
        print(f"[twitter-bot] handle_mention error: {e}")


async def poll_and_reply() -> dict:
    """Poll recent mentions and reply. Called by POST /twitter/poll-mentions."""
    global _last_mention_id
    mentions = await get_recent_mentions(since_id=_last_mention_id)
    if mentions:
        _last_mention_id = mentions[0]["id"]
    for mention in mentions:
        await handle_mention(mention)
    return {"processed": len(mentions)}


def verify_crc(crc_token: str) -> str:
    """Return CRC response for Twitter webhook verification."""
    key = TWITTER_API_SECRET.encode()
    hash_val = hmac.new(key, crc_token.encode(), hashlib.sha256).digest()
    return base64.b64encode(hash_val).decode()
