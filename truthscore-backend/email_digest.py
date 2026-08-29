"""
TruthScore Daily Email Digest
================================
Sends a daily digest of the top fact-checked claims to subscribed users.

Required env vars:
  SENDGRID_API_KEY  — SendGrid API key (free tier: 100 emails/day)
  FROM_EMAIL        — Verified sender (default: noreply@truthscore.app)
  PUBLIC_BASE_URL   — Your public URL (e.g. https://truthscore.app)
"""
import os
import httpx
from datetime import datetime, timezone, timedelta

FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@truthscore.app")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://truthscore.app")
SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def verdict_emoji(v: str) -> str:
    return {"TRUE": "✅", "FALSE": "❌", "UNCERTAIN": "⚠️", "MIXED": "🔀"}.get((v or "").upper(), "⚠️")


def verdict_color(v: str) -> str:
    return {"TRUE": "#10b981", "FALSE": "#ef4444", "UNCERTAIN": "#f59e0b", "MIXED": "#6c63ff"}.get(
        (v or "").upper(), "#9ca3af"
    )


def build_html(claims: list[dict]) -> str:
    rows = ""
    for i, c in enumerate(claims[:8]):
        v = (c.get("verdict") or "UNCERTAIN").upper()
        score = c.get("score", 50)
        claim_text = (c.get("claim") or "")[:160]
        explanation = (c.get("explanation") or "")[:200]
        vid = c.get("_verdictId") or c.get("id") or ""
        link = f"{PUBLIC_BASE_URL}/v/{vid}" if vid else PUBLIC_BASE_URL
        rows += f"""
        <tr>
          <td style="padding:16px 0;border-bottom:1px solid #2a2a3e">
            <div style="margin-bottom:6px">
              <span style="display:inline-block;padding:2px 10px;border-radius:20px;font-size:11px;font-weight:700;background:{verdict_color(v)}22;color:{verdict_color(v)};border:1px solid {verdict_color(v)}44">
                {verdict_emoji(v)} {v} · {score}/100
              </span>
            </div>
            <div style="font-size:14px;color:#e5e7eb;line-height:1.5;margin-bottom:6px">{claim_text}</div>
            <div style="font-size:12px;color:#9ca3af;line-height:1.5">{explanation}</div>
            <a href="{link}" style="display:inline-block;margin-top:8px;font-size:12px;color:#6c63ff;text-decoration:none">View full verdict →</a>
          </td>
        </tr>"""
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#0f0f17;font-family:Inter,-apple-system,sans-serif">
  <div style="max-width:580px;margin:0 auto;padding:32px 20px">
    <div style="text-align:center;margin-bottom:28px">
      <div style="font-size:28px;font-weight:800;color:#e5e7eb">🔍 TruthScore</div>
      <div style="font-size:13px;color:#6b7280;margin-top:4px">Daily Fact Check Digest · {datetime.now(timezone.utc).strftime('%B %d, %Y')}</div>
    </div>
    <div style="background:#1a1a2e;border-radius:16px;padding:24px;border:1px solid #2a2a3e">
      <div style="font-size:16px;font-weight:700;color:#e5e7eb;margin-bottom:16px">Today's top claims, verified:</div>
      <table style="width:100%;border-collapse:collapse">{rows}</table>
    </div>
    <div style="text-align:center;margin-top:24px">
      <a href="{PUBLIC_BASE_URL}" style="display:inline-block;padding:12px 28px;background:#6c63ff;color:#fff;border-radius:8px;font-size:14px;font-weight:600;text-decoration:none">
        Check your own claims →
      </a>
    </div>
    <div style="text-align:center;margin-top:20px;font-size:11px;color:#4b5563">
      <a href="{PUBLIC_BASE_URL}/unsubscribe?email={{{{email}}}}" style="color:#4b5563">Unsubscribe</a> · TruthScore · Evidence-first AI fact-checking
    </div>
  </div>
</body>
</html>"""


def build_text(claims: list[dict]) -> str:
    lines = [f"TruthScore Daily Digest — {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n"]
    for c in claims[:8]:
        v = (c.get("verdict") or "UNCERTAIN").upper()
        score = c.get("score", 50)
        claim_text = (c.get("claim") or "")[:120]
        vid = c.get("_verdictId") or c.get("id") or ""
        link = f"{PUBLIC_BASE_URL}/v/{vid}" if vid else PUBLIC_BASE_URL
        lines.append(f"{verdict_emoji(v)} {v} ({score}/100)\n{claim_text}\n{link}\n")
    lines.append(f"\nUnsubscribe: {PUBLIC_BASE_URL}/unsubscribe")
    return "\n".join(lines)


async def send_digest_to(email: str, claims: list[dict]) -> bool:
    api_key = os.getenv("SENDGRID_API_KEY", "")
    if not api_key:
        print("[digest] SENDGRID_API_KEY not set — skipping send")
        return False
    payload = {
        "personalizations": [{"to": [{"email": email}]}],
        "from": {"email": FROM_EMAIL, "name": "TruthScore"},
        "subject": f"🔍 TruthScore Digest — {datetime.now(timezone.utc).strftime('%b %d')}",
        "content": [
            {"type": "text/plain", "value": build_text(claims)},
            {"type": "text/html", "value": build_html(claims)},
        ],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            SENDGRID_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        return r.status_code in (200, 202)


async def get_trending_claims(db, limit: int = 8) -> list[dict]:
    """Fetch the most-checked claims from the last 48 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    col = db["verdicts"]
    try:
        docs = await col.find(
            {"created_at": {"$gte": cutoff}},
            {"claim": 1, "verdict": 1, "score": 1, "explanation": 1, "_id": 1}
        ).sort("check_count", -1).limit(limit).to_list(limit)
        for d in docs:
            d["id"] = str(d.pop("_id", ""))
        return docs
    except Exception:
        return []


async def run_digest(db) -> dict:
    """Send digest to all subscribers. Returns stats dict."""
    claims = await get_trending_claims(db)
    if not claims:
        return {"sent": 0, "skipped": 0, "reason": "no trending claims"}
    col = db["digest_subscribers"]
    subscribers = await col.find({"active": True}).to_list(10000)
    sent = 0
    failed = 0
    for sub in subscribers:
        email = sub.get("email", "")
        if not email:
            continue
        ok = await send_digest_to(email, claims)
        if ok:
            sent += 1
        else:
            failed += 1
    await col.update_many(
        {"active": True},
        {"$set": {"last_sent": datetime.now(timezone.utc).isoformat()}}
    )
    return {"sent": sent, "failed": failed, "claims_count": len(claims)}
