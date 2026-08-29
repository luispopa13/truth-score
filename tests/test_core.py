"""
TruthScore -- core tests (no network, no API keys required).
Run:  pytest tests/ -v   (or: python -m pytest tests/)
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "truthscore-backend"))

# ── Unit tests (pure logic, cheap) ─────────────────────────────

def test_normalize_cache_key():
    from pipeline.helpers import normalize_claim
    assert normalize_claim("Vaccines cause autism?") == normalize_claim("vaccines cause autism")
    assert normalize_claim("  Space  is silent ! ") == normalize_claim("space is silent")


def test_claim_signature_strips_diacritics():
    from pipeline.helpers import normalize_claim
    assert normalize_claim("România") == normalize_claim("Romania")


def test_rate_limiter_limits_align_with_auth():
    from utils.rate_limiter import PLAN_LIMITS
    from auth import _PLAN_DAILY
    for plan in ("free", "pro", "business", "enterprise"):
        assert PLAN_LIMITS[plan] == _PLAN_DAILY[plan], f"plan {plan} out of sync"


def test_all_plans_present():
    from auth import PLANS
    assert set(PLANS.keys()) == {"free", "pro", "business", "enterprise"}
    assert PLANS["free"]["daily_limit"] >= 1
    assert PLANS["pro"]["daily_limit"] > PLANS["free"]["daily_limit"]
    assert PLANS["business"]["price"] == 29.99


def test_eco_thresholds_protect_margin():
    """Eco-mode kicks in below the daily cap — margin armor for heavy days."""
    from auth import _ECO_AFTER, _PLAN_DAILY
    assert _ECO_AFTER["pro"] < _PLAN_DAILY["pro"]
    assert _ECO_AFTER["business"] < _PLAN_DAILY["business"]
    # Free users never hit eco (they're capped way below anyway)
    assert _ECO_AFTER["free"] > _PLAN_DAILY["free"]


def test_disposable_email_blocking():
    from utils.abuse import is_disposable_email
    assert is_disposable_email("scammer@mailinator.com") is True
    assert is_disposable_email("user@gmail.com") is False
    assert is_disposable_email("not-an-email") is False


def test_model_routing_hard_signals():
    """Nuance/strict-domain claims ALWAYS go to Gemini, never cheap."""
    from pipeline.reasoning import pick_model
    assert pick_model("Space is completely silent", "physics") == "gemini"
    assert pick_model("Coffee cures cancer says new study", "medical") == "gemini"


def test_model_routing_easy_claims():
    """Short simple claims route to the cheap model (env-overridable)."""
    from pipeline.reasoning import pick_model
    m = pick_model("The Eiffel Tower is in Paris", "geography")
    assert m in ("groq-gpt-oss-120b", "gemini")   # env-dependent but valid alias/gemini


def test_verdict_cache_l1_roundtrip():
    """L1 in-process cache serves repeats with zero I/O."""
    import asyncio
    from utils.semantic_cache import semantic_store, semantic_lookup

    async def _run():
        await semantic_store("unique l1 probe claim xyz", {"verdict": "FALSE", "score": 12})
        return await semantic_lookup("unique l1 probe claim xyz")

    hit = asyncio.run(_run())
    assert hit and hit["cached"] is True and hit["verdict"] == "FALSE"


def test_thinking_disabled():
    import config
    assert config.THINKING_CONFIG is not None
    assert config.THINKING_CONFIG.thinking_budget == 0


def test_semantic_cache_signature():
    from utils.semantic_cache import _claim_signature
    assert _claim_signature("vaccines cause autism") == _claim_signature("Vaccines cause autism?")
    assert _claim_signature("România") == _claim_signature("Romania")


def test_api_key_generation():
    from utils.api_keys import generate_api_key, hash_api_key
    k1 = generate_api_key()
    k2 = generate_api_key()
    assert k1.startswith("ts_") and k2.startswith("ts_")
    assert k1 != k2  # unique
    # hash is stable and differs per key
    assert hash_api_key(k1) != hash_api_key(k2)
    assert hash_api_key(k1) == hash_api_key(k1)


def test_cost_estimate_positive_margin():
    from utils.metrics import estimate_cost_per_claim
    pro = estimate_cost_per_claim("pro")
    assert pro["estimated_cost_per_claim_usd"] > 0
    assert pro["margin_usd"] > 0  # pro plan is profitable


# ── FastAPI endpoint tests ──────────────────────────────────────

def _make_client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_health_ok():
    c = _make_client()
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_plans_include_business():
    c = _make_client()
    plans = c.get("/plans").json()
    assert "business" in plans
    assert plans["pro"]["daily_limit"] == 200


def test_metrics_cost_protected():
    """Cost/margin metrics are admin-only — must not be public."""
    c = _make_client()
    r = c.get("/metrics/cost")
    assert r.status_code in (401, 403)


def test_anonymous_verify_limited():
    """Anonymous visitors: 3/day via Redis when present; dev-no-Redis allows."""
    c = _make_client()
    r = c.post("/verify", json={"text": "the moon is made of green cheese"})
    assert r.status_code in (200, 429)
    if r.status_code == 200:
        assert "verdict" in r.json()


def test_detect_claims_without_auth():
    """detect-claims requires no account, but it DOES invoke an LLM so it's
    rate-limited (anon per-IP cap). Assert it's reachable without auth and, when
    the quota allows the call, returns the claim list — tolerating 429 so a
    shared/exhausted anon counter (or a fail-closed no-Redis prod posture)
    doesn't spuriously fail the suite."""
    c = _make_client()
    r = c.post("/detect-claims", json={"text": "Vaccines cause autism. The earth is flat."})
    assert r.status_code in (200, 429)
    if r.status_code == 200:
        assert "claims" in r.json()


def test_related_verdict_similarity():
    """Knowledge-base matching: near-duplicate claims score high, unrelated
    claims score ~0, and empty input is safe. Guards the compounding
    'previously checked' moat against silent regressions."""
    from pipeline.verdict_store import _tokenize, _jaccard
    a = _tokenize("Vaccines cause autism in children")
    b = _tokenize("Do vaccines cause autism?")
    c = _tokenize("Paris is the capital of France")
    assert _jaccard(a, b) >= 0.5          # same claim, reworded -> matches
    assert _jaccard(a, c) < 0.2           # unrelated -> no false match
    assert _jaccard(_tokenize(""), a) == 0.0   # empty is safe, never raises
    # Stopwords must not create spurious overlap between unrelated claims.
    assert _jaccard(_tokenize("the is of and"), _tokenize("a an to for")) == 0.0


def test_verdict_integrity_hash():
    """Tamper-evidence: the content hash is stable when the `integrity` field
    is added back, and any change to a committed field (verdict/score/claim/
    source URL) changes the hash. Guards the verifiable-citation moat."""
    from pipeline.verdict_store import verdict_content_hash
    rec = {"id": "abc123", "created_at": "2026-08-29T10:00:00+00:00",
           "claim": "The Earth is round", "verdict": "TRUE", "score": 95,
           "payload": {"supporting": [{"url": "https://nasa.gov/x"}],
                       "contradicting": []}}
    h1 = verdict_content_hash(rec)
    rec["integrity"] = h1
    assert verdict_content_hash(rec) == h1           # self-field excluded
    assert len(h1) == 64 and int(h1, 16) >= 0        # valid sha256 hex
    tampered = dict(rec); tampered["verdict"] = "FALSE"
    assert verdict_content_hash(tampered) != h1      # tamper detected
    src = {"id": "abc123", "created_at": "2026-08-29T10:00:00+00:00",
           "claim": "The Earth is round", "verdict": "TRUE", "score": 95,
           "payload": {"supporting": [{"url": "https://evil.example/x"}],
                       "contradicting": []}}
    assert verdict_content_hash(src) != h1           # source-URL swap detected


def test_image_validation_guards():
    """The screenshot on-ramp's security boundary: bytes are sniffed by magic
    number (never trusting the client's Content-Type), oversized/empty/non-image
    uploads are rejected before the vision model is ever touched. Deterministic —
    validates the guard, not the OCR (OCR needs a live multimodal model)."""
    from pipeline.vision import sniff_image_mime, validate_image, MAX_IMAGE_BYTES
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    gif = b"GIF89a" + b"\x00" * 32
    webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8
    assert sniff_image_mime(png) == "image/png"
    assert sniff_image_mime(jpg) == "image/jpeg"
    assert sniff_image_mime(gif) == "image/gif"
    assert sniff_image_mime(webp) == "image/webp"
    # A spoofed content-type can't smuggle a non-image through — sniff wins.
    ok, mime, err = validate_image(b"<html>not an image</html>", "image/png")
    assert ok is False and mime == "" and err
    # Empty and oversized are rejected.
    assert validate_image(b"", "image/png")[0] is False
    assert validate_image(b"\xff\xd8\xff" + b"\x00" * (MAX_IMAGE_BYTES + 10),
                          "image/jpeg")[0] is False
    # A real PNG passes and returns the sniffed (trustworthy) mime.
    ok2, mime2, err2 = validate_image(png, "application/octet-stream")
    assert ok2 is True and mime2 == "image/png" and err2 == ""


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-x"]))