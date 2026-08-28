"""
TruthScore -- Embedding Filter + Cross-encoder Reranking
Two-stage ranking: cosine similarity (broad) -> cross-encoder (precise).
Uses LOCAL sentence-transformers models (no HF API calls needed).
"""
from pathlib import Path
from config import *
from models import *
from pipeline.helpers import get_source_recency_weight

# Absolute path to models dir — works regardless of working directory
_MODELS_DIR = Path(__file__).parent.parent / "models"

_cross_encoder     = None
_embed_model       = None
_embed_model_multi = None


def _get_cross_encoder():
    """Lazy-load cross-encoder to avoid startup delay."""
    global _cross_encoder
    if _cross_encoder is None:
        try:
            from sentence_transformers import CrossEncoder
            _cross_encoder = CrossEncoder(
                str(_MODELS_DIR / "crossencoder"),
                max_length=512,
            )
            print("[INFO] Cross-encoder loaded: ms-marco-MiniLM-L-6-v2")
        except ImportError:
            print("[WARN] sentence-transformers not installed")
        except Exception as e:
            print(f"[WARN] Cross-encoder load failed: {e}")
    return _cross_encoder


def _get_embed_model(multilingual: bool = False):
    """Lazy-load sentence embedding model."""
    global _embed_model, _embed_model_multi
    if multilingual:
        if _embed_model_multi is None:
            try:
                from sentence_transformers import SentenceTransformer
                _embed_model_multi = SentenceTransformer(
                    str(_MODELS_DIR / "embed_multi")
                )
            except Exception as e:
                print(f"[WARN] Multilingual embed model failed: {e}")
        return _embed_model_multi
    else:
        if _embed_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                _embed_model = SentenceTransformer(
                    str(_MODELS_DIR / "embed_en")
                )
            except Exception as e:
                print(f"[WARN] Embed model failed: {e}")
        return _embed_model


async def rerank_with_crossencoder(
    claim: str,
    sources: list,
    top_k: int = 12,
) -> list:
    """
    Cross-encoder reranking: scores each (claim, evidence) pair jointly.
    Falls back to original order if model unavailable.
    """
    if not sources:
        return sources

    encoder = _get_cross_encoder()
    if encoder is None:
        print("  [RERANK] Cross-encoder unavailable -- using embedding ranking")
        return sources[:top_k]

    try:
        import asyncio as _aio

        pairs = []
        for src in sources:
            evidence_text = f"{src.title or ''}. {src.snippet or ''}"[:512]
            pairs.append((claim, evidence_text))

        loop = _aio.get_event_loop()
        scores = await loop.run_in_executor(
            None,
            lambda: encoder.predict(pairs, show_progress_bar=False)
        )

        # Sort by cross-encoder score × recency weight
        ranked = sorted(
            zip(scores, sources),
            key=lambda x: float(x[0]) * get_source_recency_weight(x[1]),
            reverse=True,
        )

        for score, src in ranked:
            src.relevance = round(float(score), 4)

        result = [src for _, src in ranked[:top_k]]
        if len(ranked) >= top_k:
            print(f"  [RERANK] Cross-encoder: {len(sources)} -> top {len(result)}"
                  f" | best={ranked[0][0]:.3f}")
        return result

    except Exception as e:
        print(f"  [RERANK] Error: {e} -- falling back to original order")
        return sources[:top_k]


async def rank_by_relevance(claim: str, evidence: list) -> list:
    """
    Rank evidence by semantic similarity using LOCAL sentence-transformers.
    No HuggingFace API calls — uses downloaded models directly.
    Falls back to keyword scoring if models unavailable.
    """
    if not evidence:
        return evidence

    lang  = "ro" if any(c in RO_CHARS for c in claim) else "en"
    model = _get_embed_model(multilingual=(lang == "ro"))

    if model is not None:
        try:
            import asyncio as _aio

            texts = [claim] + [
                f"{s.title or ''} {s.snippet or ''}"[:400]
                for s in evidence
            ]

            loop = _aio.get_event_loop()
            embeddings = await loop.run_in_executor(
                None,
                lambda: model.encode(texts, show_progress_bar=False)
            )

            claim_vec = embeddings[0]
            scored = []
            for i, src in enumerate(evidence):
                ev_vec = embeddings[i + 1]
                sim = _cosine(claim_vec, ev_vec)
                src.relevance = round(float(sim), 4)
                scored.append(src)

            return sorted(scored, key=lambda s: s.relevance, reverse=True)

        except Exception as e:
            print(f"  [EMBED] Local model error: {e} -- keyword fallback")

    # Keyword fallback (fast, no models needed)
    print("  [EMBED] Using keyword fallback scoring")
    c = claim.lower()
    named     = set(w.lower() for w in re.findall(r"[A-Z][a-zA-Z]{2,}", claim))
    stop      = {"the","and","for","with","that","this","from","are","was",
                 "were","has","have","been","not","can","but","its"}
    all_words = set(re.findall(r"[a-zA-Zăâîșț]{3,}", c))
    claim_words = all_words - stop

    trusted = {"pubmed","who","cdc","nasa","fda","nature","ncbi","arxiv",
               "wikipedia","wolfram","clinicaltrials","crossref","scopus",
               "noaa","epa","britannica","reuters","bbc","ap ","apnews"}

    scored = []
    for src in evidence[:50]:
        text      = f"{src.title or ''} {src.snippet or ''} {src.publisher or ''}".lower()
        txt_words = set(re.findall(r"[a-zA-Zăâîșț]{3,}", text))
        ne   = len(named & txt_words) / (len(named) + 0.1)
        gen  = len(claim_words & txt_words) / (len(claim_words) + 0.1)
        auth = 0.1 if any(d in (src.publisher or "").lower() for d in trusted) else 0.0
        src.relevance = round(ne * 0.6 + gen * 0.3 + auth, 4)
        scored.append(src)

    return sorted(scored, key=lambda s: s.relevance, reverse=True)


def _cosine(a, b) -> float:
    """Cosine similarity between two vectors."""
    try:
        import math as _math
        dot    = sum(float(x) * float(y) for x, y in zip(a, b))
        norm_a = _math.sqrt(sum(float(x) ** 2 for x in a))
        norm_b = _math.sqrt(sum(float(x) ** 2 for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)
    except Exception:
        return 0.0