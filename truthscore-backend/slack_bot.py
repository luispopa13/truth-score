"""
TruthScore Slack Bot
======================
Handles Slack slash commands and app mentions.

Setup:
1. Create a Slack App at api.slack.com/apps
2. Add slash command: /truthcheck → https://<host>/slack/command
3. Add Events API: https://<host>/slack/events (subscribe to app_mention)
4. Bot Token Scopes: chat:write, commands, app_mentions:read
5. Env vars: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET

Usage in Slack:
  /truthcheck The Earth is flat.
  @TruthScore The moon landing was faked.
"""
import os
import hmac
import hashlib
import time
import httpx

SLACK_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_API = "https://slack.com/api"


def verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Verify the request comes from Slack."""
    if not SLACK_SIGNING_SECRET:
        return True  # dev mode
    if abs(time.time() - int(timestamp)) > 300:
        return False
    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verdict_emoji(v: str) -> str:
    return {"TRUE": "✅", "FALSE": "❌", "UNCERTAIN": "⚠️", "MIXED": "🔀"}.get((v or "").upper(), "⚠️")


async def post_message(channel: str, text: str, blocks: list | None = None) -> bool:
    if not SLACK_TOKEN:
        return False
    payload: dict = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{SLACK_API}/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
            json=payload,
        )
        return r.json().get("ok", False)


def build_slack_blocks(claim: str, d: dict) -> list:
    v = (d.get("verdict") or "UNCERTAIN").upper()
    score = d.get("score", 50)
    explanation = (d.get("explanation") or "")[:300]
    vid = d.get("_verdictId", "")
    public_url = os.getenv("PUBLIC_BASE_URL", "https://truthscore.app")
    link = f"{public_url}/v/{vid}" if vid else public_url
    sup = len(d.get("supporting", []))
    con = len(d.get("contradicting", []))
    color = {"TRUE": "#10b981", "FALSE": "#ef4444", "UNCERTAIN": "#f59e0b"}.get(v, "#6c63ff")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{verdict_emoji(v)} {v}* — {score}/100\n_{claim[:200]}_"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": explanation}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"✓ {sup} supporting · ✗ {con} contradicting · <{link}|Full verdict>"}
        ]},
    ]


async def handle_slash_command(claim: str, channel: str, backend_url: str) -> None:
    """Handle /truthcheck command."""
    if not claim.strip():
        await post_message(channel, "Usage: `/truthcheck The Earth is flat.`")
        return
    await post_message(channel, f"⏳ Checking: _{claim[:100]}_…")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{backend_url}/verify", json={"text": claim})
            r.raise_for_status()
            d = r.json()
        blocks = build_slack_blocks(claim, d)
        v = (d.get("verdict") or "UNCERTAIN").upper()
        await post_message(channel, f"{verdict_emoji(v)} {v} for: {claim[:80]}", blocks=blocks)
    except Exception as e:
        await post_message(channel, f"⚠️ Error: {str(e)[:200]}")


async def handle_app_mention(event: dict, backend_url: str) -> None:
    """Handle @TruthScore mention in channels."""
    text = event.get("text", "")
    channel = event.get("channel", "")
    # Strip the mention
    claim = " ".join(w for w in text.split() if not w.startswith("<@")).strip()
    if len(claim) < 8:
        await post_message(channel, "Hi! Mention me with a claim to fact-check it.\nExample: `@TruthScore The moon landing was faked.`")
        return
    await handle_slash_command(claim, channel, backend_url)
