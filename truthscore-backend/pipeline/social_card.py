"""
TruthScore -- Open-Graph Social Card Renderer
==============================================
Generates a 1200×630 PNG for a verdict permalink so a shared /v/{id} link
unfurls into a branded card (verdict, score, claim) in chats and social feeds,
instead of a bare URL. Pure Pillow — no headless browser, no network.

Cards are deterministic per (verdict, score, claim) so we cache the rendered
PNG bytes in memory; crawlers refetch og:image aggressively and the draw is the
only CPU cost on the /v hot path.
"""
import os
from collections import OrderedDict

_CARD_CACHE_MAX = int(os.getenv("CARD_CACHE_MAX", "256"))
_card_cache: "OrderedDict[str, bytes]" = OrderedDict()

# Brand palette — mirrors the dashboard tokens.
_BG = (15, 16, 28)
_FG = (238, 238, 248)
_MUTED = (152, 152, 184)
_VERDICT_COLORS = {
    "TRUE": (46, 204, 113),
    "FALSE": (231, 76, 60),
    "UNCERTAIN": (241, 196, 15),
    "MISLEADING": (230, 126, 34),
    "UNVERIFIABLE": (149, 165, 166),
}

_FONT_CANDIDATES = [
    "DejaVuSans-Bold.ttf", "DejaVuSans.ttf",  # bundled with Pillow
    "arialbd.ttf", "arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _load_font(size: int, bold: bool = False):
    from PIL import ImageFont
    order = _FONT_CANDIDATES if bold else _FONT_CANDIDATES[::-1]
    for name in order:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int, max_lines: int) -> list:
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and (len(" ".join(lines)) < len(text or "")):
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _render(verdict: str, score: int, claim: str) -> bytes:
    from PIL import Image, ImageDraw
    import io

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), _BG)
    d = ImageDraw.Draw(img)

    accent = _VERDICT_COLORS.get((verdict or "").upper(), _MUTED)

    # Left accent bar keyed to the verdict color.
    d.rectangle([0, 0, 16, H], fill=accent)

    pad = 72
    # Brand row
    brand_font = _load_font(34, bold=True)
    d.text((pad, 54), "TruthScore", font=brand_font, fill=_FG)
    d.text((pad + brand_font.getlength("TruthScore") + 16, 60),
           "· verified", font=_load_font(24), fill=_MUTED)

    # Verdict headline
    verdict_font = _load_font(96, bold=True)
    vtext = (verdict or "UNCERTAIN").upper()
    d.text((pad, 150), vtext, font=verdict_font, fill=accent)

    # Score pill
    score_font = _load_font(40, bold=True)
    stext = f"{int(score)}/100"
    sx = pad
    sy = 280
    sw = score_font.getlength(stext)
    d.rounded_rectangle([sx - 4, sy - 8, sx + sw + 28, sy + 54],
                        radius=16, fill=(30, 32, 52))
    d.text((sx + 12, sy), stext, font=score_font, fill=_FG)

    # Claim (wrapped, up to 4 lines)
    claim_font = _load_font(44, bold=False)
    lines = _wrap(d, claim or "", claim_font, W - pad * 2, 4)
    y = 370
    for ln in lines:
        d.text((pad, y), ln, font=claim_font, fill=_FG)
        y += 56

    # Footer
    d.text((pad, H - 64),
           "Independent, source-backed fact check",
           font=_load_font(26), fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_card(vid: str, verdict: str, score: int, claim: str) -> bytes:
    """Return PNG bytes for this verdict's social card (memoized by id)."""
    cached = _card_cache.get(vid)
    if cached is not None:
        _card_cache.move_to_end(vid)
        return cached
    png = _render(verdict, score, claim)
    _card_cache[vid] = png
    _card_cache.move_to_end(vid)
    while len(_card_cache) > _CARD_CACHE_MAX:
        _card_cache.popitem(last=False)
    return png
