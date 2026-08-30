"""
TruthScore — Email utility
Provider priority (first configured wins):
  1. Brevo (brevo.com) — 300 emails/day FREE, no credit card
  2. SendGrid           — 100 emails/day free
  3. SMTP               — any provider (Mailgun, Postmark, Gmail SMTP, etc.)
  4. Console fallback   — prints to stdout, never crashes (local dev)

Set ONE of:
  BREVO_API_KEY      → Brevo transactional (recommended free tier)
  SENDGRID_API_KEY   → SendGrid
  SMTP_HOST + SMTP_PORT + SMTP_USER + SMTP_PASS  → any SMTP server
"""
import os


_FROM = os.getenv("FROM_EMAIL", "noreply@truthscore.app")
_FROM_NAME = os.getenv("FROM_NAME", "TruthScore")


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send an HTML email. Returns True on success (or dry-run), False on failure."""
    brevo_key = os.getenv("BREVO_API_KEY", "")
    sg_key    = os.getenv("SENDGRID_API_KEY", "")
    smtp_host = os.getenv("SMTP_HOST", "")

    if brevo_key:
        return await _send_brevo(to, subject, html, brevo_key)
    if sg_key:
        return await _send_sendgrid(to, subject, html, sg_key)
    if smtp_host:
        return _send_smtp(to, subject, html)

    print(f"[MAILER] No email provider configured — would send to {to}: {subject}")
    return True


async def _send_brevo(to: str, subject: str, html: str, api_key: str) -> bool:
    import httpx
    payload = {
        "sender":   {"name": _FROM_NAME, "email": _FROM},
        "to":       [{"email": to}],
        "subject":  subject,
        "htmlContent": html,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={"api-key": api_key, "Content-Type": "application/json"},
            )
        if r.status_code in (200, 201):
            return True
        print(f"[MAILER] Brevo {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[MAILER] Brevo error: {e}")
        return False


async def _send_sendgrid(to: str, subject: str, html: str, api_key: str) -> bool:
    import httpx
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": _FROM, "name": _FROM_NAME},
        "subject": subject,
        "content": [{"type": "text/html", "value": html}],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.sendgrid.com/v3/mail/send",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        if r.status_code in (200, 202):
            return True
        print(f"[MAILER] SendGrid {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[MAILER] SendGrid error: {e}")
        return False


def _send_smtp(to: str, subject: str, html: str) -> bool:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    pwd  = os.getenv("SMTP_PASS", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{_FROM_NAME} <{_FROM}>"
    msg["To"]      = to
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            if user:
                s.login(user, pwd)
            s.sendmail(_FROM, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"[MAILER] SMTP error: {e}")
        return False
