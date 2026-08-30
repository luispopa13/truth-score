"""
TruthScore — Email utility
Sends transactional emails via SendGrid if SENDGRID_API_KEY is set,
otherwise prints to console (safe for local dev with no config).
"""
import os


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send an HTML email. Returns True on success (or dry-run), False on failure."""
    import httpx

    api_key = os.getenv("SENDGRID_API_KEY", "")
    from_email = os.getenv("FROM_EMAIL", "noreply@truthscore.app")

    if not api_key:
        print(f"[MAILER] No SENDGRID_API_KEY — would send to {to}: {subject}")
        return True

    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": from_email},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        if resp.status_code in (200, 202):
            return True
        print(f"[MAILER] SendGrid returned {resp.status_code}: {resp.text[:200]}")
        return False
    except Exception as e:
        print(f"[MAILER] Failed to send email to {to}: {e}")
        return False
