"""
TruthScore -- Image ingestion ("check a screenshot")
====================================================
The frictionless on-ramp to the verification engine: a user pastes/uploads a
SCREENSHOT (a fake tweet, a "news" headline, a WhatsApp forward) and we OCR the
text, extract the factual claims, and run them through the exact same pipeline
as typed text. Screenshots are the #1 format misinformation travels in, so this
is where "I'll use TruthScore for this, not ChatGPT" actually happens: ChatGPT
can read an image too, but it can't give you a sourced, scored, permanently
shareable verdict to fire back into the group chat.

Two responsibilities, split by testability:
  • validate_image() / sniff_image_mime()  — pure byte inspection (size caps,
    magic-byte sniffing, MIME allow-list). Fully deterministic, unit-tested,
    and the security boundary: we never hand un-sniffed bytes to the model.
  • extract_text_from_image()  — the Gemini-vision OCR+claim-extraction call.
    Needs a live multimodal model (Gemini 2.5 Flash is multimodal and free), so
    it is validated on a machine where the LLM is reachable, not in CI.

This module never raises to its callers for model/transport problems: a failed
extraction returns "" so the endpoint can answer 422 cleanly.
"""
import asyncio

# 8 MB default — comfortably covers phone screenshots while capping abuse and
# per-request memory at 1000-user concurrency. Overridable via env.
import os
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))

# Formats Gemini vision reliably decodes. Sniffed from magic bytes, not trusted
# from the client-declared Content-Type (which is spoofable).
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}


def sniff_image_mime(data: bytes) -> str | None:
    """Return the real image MIME from magic bytes, or None if `data` is not a
    recognized raster image. Never trusts a client-declared type."""
    if not data or len(data) < 12:
        return None
    b = data
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:2] == b"BM":
        return "image/bmp"
    return None


def validate_image(data: bytes, declared_mime: str = "") -> tuple[bool, str, str]:
    """Validate raw upload bytes. Returns (ok, sniffed_mime, error_message).

    The security contract: the returned MIME comes from the bytes themselves, so
    a caller can pass it straight to the model without trusting the client. Size
    is enforced here as a hard ceiling even though the endpoint also bounds the
    read — belt and suspenders."""
    if not data:
        return False, "", "Empty image."
    if len(data) > MAX_IMAGE_BYTES:
        mb = MAX_IMAGE_BYTES // (1024 * 1024)
        return False, "", f"Image too large (max {mb} MB)."
    mime = sniff_image_mime(data)
    if mime is None:
        return False, "", "Unsupported or corrupt image (use JPEG, PNG, WEBP, GIF or BMP)."
    if mime not in _ALLOWED_MIME:
        return False, "", f"Unsupported image type: {mime}."
    return True, mime, ""


# Prompt kept model-agnostic and language-neutral: transcribe faithfully, keep
# only assertions of fact, drop UI chrome. Downstream split_claims() does the
# decomposition, so we only need clean claim text back — in the image's own
# language (the pipeline detects language and translates for retrieval).
_EXTRACT_PROMPT = (
    "You are extracting checkable claims from an image (often a screenshot of a "
    "social post, chat message, or news headline).\n"
    "1. Read ALL legible text in the image.\n"
    "2. Output ONLY the factual assertions being made — the statements that "
    "could be true or false. Preserve the original language and wording.\n"
    "3. Ignore interface chrome (usernames, timestamps, like/share counts, "
    "button labels, watermarks) unless it is part of the claim.\n"
    "4. If the image contains no checkable factual claim, output exactly: NONE\n"
    "Return plain text only — one claim per line, no numbering, no commentary."
)


async def extract_text_from_image(data: bytes, mime: str) -> str:
    """OCR + claim extraction via Gemini vision. Returns the extracted claim
    text (possibly multi-line), "" if nothing usable was found or the model is
    unavailable. Never raises — transport/model errors degrade to ""."""
    # Imported lazily so the module (and its pure validators/tests) load with no
    # API key or SDK configured.
    import config as _config
    gemini_client = getattr(_config, "gemini_client", None)
    genai_types   = getattr(_config, "genai_types", None)
    GEMINI_MODEL  = getattr(_config, "GEMINI_MODEL", "gemini-2.5-flash")
    if gemini_client is None or genai_types is None:
        print("[VISION] Gemini client unavailable -> image OCR skipped")
        return ""
    try:
        part = genai_types.Part.from_bytes(data=data, mime_type=mime)
        cfg = _config.make_gemini_config(max_tokens=1024, use_search=False,
                                         thinking_budget=0)
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[_EXTRACT_PROMPT, part],
                config=cfg,
            ),
        )
        text = (getattr(resp, "text", "") or "").strip()
        if not text or text.strip().upper() == "NONE":
            return ""
        print(f"[VISION] extracted {len(text)} chars from {mime}")
        return text
    except Exception as e:
        print(f"[VISION] extraction failed (non-fatal): {str(e)[:120]}")
        return ""
