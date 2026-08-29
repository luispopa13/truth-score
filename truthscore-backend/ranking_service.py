"""
TruthScore — Ranking Sidecar Service
Standalone FastAPI app: loads sentence-transformer models ONCE and serves
/rerank (cross-encoder) and /embed (cosine similarity) to all main-app workers.

Run:  uvicorn ranking_service:app --host 0.0.0.0 --port 8001
      (from the truthscore-backend/ directory)

In docker-compose the main backend sets RANKING_SERVICE_URL=http://ranking:8001
and this sidecar is the `ranking` service. Falls back to in-process silently.
"""
import asyncio
import math
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="TruthScore Ranking Sidecar", version="1.0")

_MODELS_DIR = Path(__file__).parent / "models"

# ── Lazy singletons (loaded once at first request, then reused) ──────────────
_cross_encoder = None
_embed_en      = None
_embed_multi   = None


def _load_cross_encoder():
    global _cross_encoder
    if _cross_encoder is None:
        try:
            from sentence_transformers import CrossEncoder
            _cross_encoder = CrossEncoder(
                str(_MODELS_DIR / "crossencoder"), max_length=512
            )
            print("[RANKING] Cross-encoder loaded")
        except Exception as e:
            print(f"[RANKING] Cross-encoder failed: {e}")
    return _cross_encoder


def _load_embed(multilingual: bool = False):
    global _embed_en, _embed_multi
    key = "embed_multi" if multilingual else "embed_en"
    ref = _embed_multi if multilingual else _embed_en
    if ref is None:
        try:
            from sentence_transformers import SentenceTransformer
            ref = SentenceTransformer(str(_MODELS_DIR / key))
            if multilingual:
                _embed_multi = ref
            else:
                _embed_en = ref
            print(f"[RANKING] {key} loaded")
        except Exception as e:
            print(f"[RANKING] {key} failed: {e}")
    return _embed_multi if multilingual else _embed_en


# Pre-warm models at startup so first request isn't slow
@app.on_event("startup")
async def _warm():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _load_cross_encoder)
    await loop.run_in_executor(None, lambda: _load_embed(False))
    await loop.run_in_executor(None, lambda: _load_embed(True))
    print("[RANKING] Models warmed up")


# ── Pydantic models ─────────────────────────────────────────────────────────

class SourceIn(BaseModel):
    title:        Optional[str] = ""
    snippet:      Optional[str] = ""
    url:          Optional[str] = ""
    publisher:    Optional[str] = ""
    published_at: Optional[str] = ""
    type:         Optional[str] = "web"
    relevance:    Optional[float] = 0.0

    class Config:
        extra = "allow"  # pass through any extra fields unchanged


class RerankRequest(BaseModel):
    claim:   str
    sources: list[SourceIn]
    top_k:   int = 12


class EmbedRequest(BaseModel):
    claim:   str
    sources: list[SourceIn]
    lang:    str = "en"


class RankResponse(BaseModel):
    sources: list[dict]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _recency_weight(src: SourceIn) -> float:
    """Slight boost for recent sources — mirrors pipeline/helpers.py logic."""
    pub = src.published_at or ""
    if not pub:
        return 1.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - dt).days
        if age_days < 30:
            return 1.25
        if age_days < 180:
            return 1.10
        if age_days < 365:
            return 1.05
    except Exception:
        pass
    return 1.0


def _cosine(a, b) -> float:
    try:
        dot    = sum(float(x) * float(y) for x, y in zip(a, b))
        norm_a = math.sqrt(sum(float(x) ** 2 for x in a))
        norm_b = math.sqrt(sum(float(x) ** 2 for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
    except Exception:
        return 0.0


# ── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "models": {
            "cross_encoder": _cross_encoder is not None,
            "embed_en":      _embed_en is not None,
            "embed_multi":   _embed_multi is not None,
        },
    }


@app.post("/rerank", response_model=RankResponse)
async def rerank(req: RerankRequest):
    """Cross-encoder reranking: re-scores (claim, evidence) pairs jointly."""
    if not req.sources:
        return {"sources": []}

    encoder = _load_cross_encoder()
    if encoder is None:
        return {"sources": [s.model_dump() for s in req.sources[: req.top_k]]}

    pairs = [
        (req.claim, f"{s.title or ''}. {s.snippet or ''}"[:512])
        for s in req.sources
    ]
    loop = asyncio.get_event_loop()
    scores = await loop.run_in_executor(
        None, lambda: encoder.predict(pairs, show_progress_bar=False)
    )

    ranked = sorted(
        zip(scores, req.sources),
        key=lambda x: float(x[0]) * _recency_weight(x[1]),
        reverse=True,
    )
    result = []
    for score, src in ranked[: req.top_k]:
        d = src.model_dump()
        d["relevance"] = round(float(score), 4)
        result.append(d)

    print(f"[RANKING] rerank: {len(req.sources)} -> top {len(result)}"
          f" | best={ranked[0][0]:.3f}" if ranked else "")
    return {"sources": result}


@app.post("/embed", response_model=RankResponse)
async def embed(req: EmbedRequest):
    """Embedding-based relevance ranking (cosine similarity)."""
    if not req.sources:
        return {"sources": []}

    model = _load_embed(multilingual=(req.lang == "ro"))
    if model is None:
        return {"sources": [s.model_dump() for s in req.sources]}

    texts = [req.claim] + [
        f"{s.title or ''} {s.snippet or ''}"[:400] for s in req.sources
    ]
    loop = asyncio.get_event_loop()
    embeddings = await loop.run_in_executor(
        None, lambda: model.encode(texts, show_progress_bar=False)
    )

    claim_vec = embeddings[0]
    result = []
    for i, src in enumerate(req.sources):
        d = src.model_dump()
        d["relevance"] = round(_cosine(claim_vec, embeddings[i + 1]), 4)
        result.append(d)

    result.sort(key=lambda x: x["relevance"], reverse=True)
    return {"sources": result}
