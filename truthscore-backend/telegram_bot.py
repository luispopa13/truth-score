"""
TruthScore Telegram Bot
========================
Webhook handler for the Telegram bot integration.
Set up the webhook via:
  curl https://api.telegram.org/bot<TOKEN>/setWebhook?url=<YOUR_PUBLIC_URL>/telegram/webhook

Required env var: TELEGRAM_BOT_TOKEN
"""
import os
import httpx

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


async def send_message(chat_id: int, text: str, parse_mode: str = "HTML") -> None:
    if not TELEGRAM_TOKEN:
        return
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )


def verdict_emoji(verdict: str) -> str:
    return {"TRUE": "✅", "FALSE": "❌", "UNCERTAIN": "⚠️", "MIXED": "🔀"}.get(
        (verdict or "").upper(), "⚠️"
    )


def format_verdict(claim: str, d: dict) -> str:
    verdict = (d.get("verdict") or "UNCERTAIN").upper()
    score = d.get("score", 50)
    explanation = d.get("explanation", "")
    confidence = d.get("confidence", "")
    emoji = verdict_emoji(verdict)
    sup_count = len(d.get("supporting", []))
    con_count = len(d.get("contradicting", []))

    top_src = ""
    for src in (d.get("supporting") or d.get("neutral_sources") or [])[:2]:
        title = (src.get("publisher") or src.get("title") or "")[:50]
        url = src.get("url", "")
        if url and title:
            top_src += f'\n  • <a href="{url}">{title}</a>'

    text = (
        f"{emoji} <b>{verdict}</b> — {score}/100\n"
        f"<i>{claim[:200]}</i>\n\n"
        f"{explanation[:300]}\n"
    )
    if confidence:
        text += f"Confidence: {confidence}\n"
    text += f"Sources: {sup_count} supporting · {con_count} contradicting"
    if top_src:
        text += f"\n{top_src}"
    return text


async def handle_update(update: dict) -> None:
    """Process a single Telegram update."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()

    if not text:
        return

    if text.startswith("/start"):
        await send_message(
            chat_id,
            "🔍 <b>TruthScore Bot</b>\n\nSend me any claim or statement and I'll fact-check it in real-time.\n\nExample:\n<i>Vaccines cause autism.</i>",
        )
        return

    if text.startswith("/help"):
        await send_message(
            chat_id,
            "📖 <b>How to use:</b>\nJust send any factual claim as a message.\n\n"
            "For paragraphs with multiple claims, send the full text — I'll verify each claim separately.",
        )
        return

    # Indicate processing
    await send_message(chat_id, "🔄 Checking...")

    try:
        # Verify in-process via the pipeline. An HTTP self-call to /verify or
        # /analyze-text would round-trip the public proxy AND be counted against
        # the anonymous rate limit (every Telegram user would share one
        # server-IP bucket, ~5/day). verify_claim already decomposes compound
        # text into sub_claim_results, so a single call covers both the
        # single-claim and the paragraph case.
        from pipeline.verify import verify_claim
        from models import VerifyRequest
        result = await verify_claim(VerifyRequest(text=text[:3000]))
        d = result.model_dump() if hasattr(result, "model_dump") else dict(result)

        subs = d.get("sub_claim_results") or []
        if subs:
            # Compound text: show a per-sub-claim breakdown
            lines = []
            for i, res in enumerate(subs[:5]):
                v = (res.get("verdict") or "UNCERTAIN").upper()
                lines.append(
                    f"{verdict_emoji(v)} #{i+1} {(res.get('claim') or '')[:80]} — {res.get('score',50)}%"
                )
            reply = (
                f"{verdict_emoji(d.get('verdict','UNCERTAIN'))} <b>Overall: {d.get('verdict','UNCERTAIN')}</b> ({d.get('score',50)}/100)\n\n"
                + "\n".join(lines)
            )
        else:
            reply = format_verdict(text, d)

        await send_message(chat_id, reply)

    except Exception as e:
        await send_message(chat_id, f"⚠️ Error: {str(e)[:200]}")
