"""
TruthScore WhatsApp Bot
========================
Handles incoming WhatsApp Business Cloud API (Meta) webhook events.

Setup:
1. Create a Meta Developer app with WhatsApp product
2. Set webhook URL to https://<your-host>/whatsapp/webhook
3. Set env vars: WHATSAPP_TOKEN, WHATSAPP_VERIFY_TOKEN, WHATSAPP_PHONE_ID
4. The verify token can be any string — set it in the Meta console and .env

Docs: https://developers.facebook.com/docs/whatsapp/cloud-api
"""
import os
import httpx

WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID", "")


async def send_whatsapp(to: str, text: str, wa_token: str) -> None:
    if not WHATSAPP_PHONE_ID or not wa_token:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://graph.facebook.com/v18.0/{WHATSAPP_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {wa_token}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text},
            },
            timeout=10,
        )


def verdict_emoji(verdict: str) -> str:
    return {"TRUE": "✅", "FALSE": "❌", "UNCERTAIN": "⚠️", "MIXED": "🔀"}.get(
        (verdict or "").upper(), "⚠️"
    )


def format_verdict(claim: str, d: dict) -> str:
    verdict = (d.get("verdict") or "UNCERTAIN").upper()
    score = d.get("score", 50)
    explanation = (d.get("explanation") or "")[:300]
    sup = len(d.get("supporting", []))
    con = len(d.get("contradicting", []))
    emoji = verdict_emoji(verdict)
    top_src = ""
    for src in (d.get("supporting") or d.get("neutral_sources") or [])[:2]:
        pub = (src.get("publisher") or src.get("title") or "")[:40]
        if pub:
            top_src += f"\n• {pub}"
    return (
        f"{emoji} *{verdict}* — {score}/100\n"
        f"_{claim[:200]}_\n\n"
        f"{explanation}\n"
        f"Sources: {sup} supporting · {con} contradicting"
        + (f"\n{top_src}" if top_src else "")
    )


async def handle_whatsapp_update(body: dict, wa_token: str) -> None:
    """Process a single WhatsApp Cloud API webhook payload."""
    try:
        entry = (body.get("entry") or [{}])[0]
        changes = (entry.get("changes") or [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return
        msg = messages[0]
        from_number = msg.get("from", "")
        msg_type = msg.get("type", "")
        if msg_type != "text":
            await send_whatsapp(from_number, "Send me a text claim to fact-check it. Example: _Vaccines cause autism._", wa_token)
            return
        text = (msg.get("text", {}).get("body") or "").strip()
        if not text:
            return
        if text.lower() in ("/start", "hi", "hello", "help"):
            await send_whatsapp(
                from_number,
                "🔍 *TruthScore Bot*\n\nSend me any claim and I'll fact-check it.\n\nExample:\n_The Great Wall is visible from space._",
                wa_token,
            )
            return
        # Indicate processing
        await send_whatsapp(from_number, "⏳ Checking...", wa_token)
        # Verify in-process via the pipeline — an HTTP self-call to /verify would
        # round-trip the public proxy AND be counted against the anonymous rate
        # limit (all bot users would share one server-IP bucket, ~5/day).
        from pipeline.verify import verify_claim
        from models import VerifyRequest
        result = await verify_claim(VerifyRequest(text=text[:3000]))
        d = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        subs = d.get("sub_claim_results") or []
        if subs:
            lines = []
            for i, res in enumerate(subs[:5]):
                v = (res.get("verdict") or "UNCERTAIN").upper()
                lines.append(f"{verdict_emoji(v)} #{i+1} {(res.get('claim') or '')[:80]} — {res.get('score', 50)}%")
            reply = (
                f"{verdict_emoji(d.get('verdict', 'UNCERTAIN'))} *Overall: {d.get('verdict', 'UNCERTAIN')}* ({d.get('score', 50)}/100)\n\n"
                + "\n".join(lines)
            )
        else:
            reply = format_verdict(text, d)
        await send_whatsapp(from_number, reply, wa_token)
    except Exception as e:
        print(f"[whatsapp-bot] error: {e}")
