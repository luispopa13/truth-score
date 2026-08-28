"""
TruthScore -- Cost & usage metrics tracking.

Tracks per-verification cost in real time so you always know:
  - cost per claim
  - cost per plan
  - daily / monthly spend
  - cost vs revenue (margin)

All counters are kept in Redis for cross-instance aggregation,
with a local in-process dict fallback when Redis is offline.
"""
import os, json, time, logging
from typing import Optional
from datetime import datetime, timezone

logger = logging.getLogger("truthscore.metrics")

# --- Per-model / per-service costs ---
# LLM prices: USD per 1M tokens.  VERIFIED Aug 2026:
#   - Gemini Flash w/o thinking ≈ $0.30 in / $0.60 out  (thinking ON = ~5x output)
#   - Groq GPT-OSS-120B: $0.15 in / $0.60 out (500 tok/s) — VERIFIED groq.com/docs
#   - Groq GPT-OSS-20B : $0.075 in / $0.30 out (1000 tok/s) — cheapest quality tier
MODEL_COSTS = {
    "gemini-2.5-flash":        {"input": 0.30,   "output": 0.60},
    "gemini-2.5-flash-think":  {"input": 0.30,   "output": 3.50},  # thinking ON — avoid
    "groq-gpt-oss-120b":       {"input": 0.15,   "output": 0.60},
    "groq-gpt-oss-20b":        {"input": 0.075,  "output": 0.30},
    "gpt-4o-mini":             {"input": 0.15,   "output": 0.60},
    "llama-3.3-70b-versatile": {"input": 0.59,   "output": 0.79},  # legacy fallback
}

# Search-layer costs (USD per call). Tavily: $8/1000 credits pay-as-you-go.
SEARCH_COSTS = {
    "tavily_basic":     0.008,   # search_depth=basic (1 credit) — current default
    "tavily_advanced":  0.016,   # search_depth=advanced (2 credits) — avoid by default
    "ddg_wiki":         0.0,     # free (rate-limited only)
    "wikipedia_api":    0.0,
    "pubmed_arxiv_cr":  0.0,     # free academic APIs
}

# Pricing plans (monthly) — these set your MARGIN.
# Prices come from the single source of truth in auth.py (_PLAN_PRICES) so the
# margin math here can never disagree with what customers are actually charged.
from auth import _PLAN_PRICES as _AUTH_PLAN_PRICES
PLAN_PRICES = {
    plan: {"price": price, "currency": "EUR"}
    for plan, price in _AUTH_PLAN_PRICES.items()
}


class CostTracker:
    """In-process accumulator; flushes to Redis hourly."""

    def __init__(self):
        self._local = {}          # {model: {"input_tokens": N, "output_tokens": N, "calls": N}}
        self._last_flush = time.time()

    def _ensure(self, model: str) -> dict:
        if model not in self._local:
            self._local[model] = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        return self._local[model]

    def record_call(self, model: str, input_tokens: int, output_tokens: int):
        entry = self._ensure(model)
        entry["input_tokens"] += input_tokens
        entry["output_tokens"] += output_tokens
        entry["calls"] += 1

    def cost_usd(self) -> float:
        total = 0.0
        for model, counts in self._local.items():
            rates = MODEL_COSTS.get(model)
            if not rates:
                continue
            total += counts["input_tokens"] * rates["input"] / 1_000_000
            total += counts["output_tokens"] * rates["output"] / 1_000_000
        return total

    def summary(self) -> dict:
        return {
            "models": dict(self._local),
            "cost_usd": round(self.cost_usd(), 4),
            "calls": sum(v["calls"] for v in self._local.values()),
        }

    async def _flush_to_redis(self):
        """Best-effort flush to Redis for cross-instance aggregation."""
        try:
            from utils.redis_client import get_async_redis
            redis = get_async_redis()
            if not redis:
                return
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            for model, counts in self._local.items():
                key = f"ts:metrics:{day}:model:{model}"
                await redis.hincrby(key, "input_tokens", counts["input_tokens"])
                await redis.hincrby(key, "output_tokens", counts["output_tokens"])
                await redis.hincrby(key, "calls", counts["calls"])
                await redis.expire(key, 90 * 86400)
            self._local.clear()
            self._last_flush = time.time()
        except Exception as e:
            logger.debug("metrics flush to Redis failed: %s", e)

    async def flush_if_needed(self):
        if time.time() - self._last_flush > 3600:
            await self._flush_to_redis()


_tracker = CostTracker()


def record_llm_call(model: str, input_tokens: int, output_tokens: int):
    """Call this after every LLM invocation."""
    model = model or "unknown"
    # Detect if thinking was on (heuristic: check env)
    _tracker.record_call(model, input_tokens, output_tokens)


def get_cost_summary() -> dict:
    return _tracker.summary()


def estimate_cost_per_claim(plan: str = "free") -> dict:
    """
    Estimate the marginal cost of verifying one claim for a given plan.

    Cost structure (verified Aug 2026):
      - Search layer is now the DOMINANT cost, not the LLM:
          Tavily basic = $0.008/call; pipeline fires ~1-3 searches/claim
      - LLM (Gemini Flash, thinking OFF): ~$0.001-0.002/claim
      - Evidence cache + verdict cache cut both dramatically on repeats.
    """
    rates = MODEL_COSTS["gemini-2.5-flash"]
    base_input  = 4000   # avg prompt tokens after condensation
    base_output = 600    # avg output tokens
    llm_cost = (base_input * rates["input"] + base_output * rates["output"]) / 1_000_000

    # Search: assume 1.5 Tavily calls average (cache hit rate reduces this)
    search_cost_gross   = 1.5 * SEARCH_COSTS["tavily_basic"]
    search_cost_cached  = search_cost_gross * 0.35   # 65% evidence-cache hit rate

    llm_cost_final   = llm_cost * 0.70              # 30% verdict-cache hits
    expected_cost    = llm_cost_final + search_cost_cached

    price = PLAN_PRICES.get(plan, {}).get("price", 0)
    return {
        "llm_cost_usd": round(llm_cost_final, 5),
        "search_cost_usd": round(search_cost_cached, 5),
        "estimated_cost_per_claim_usd": round(expected_cost, 5),
        "worst_case_per_claim_usd": round(llm_cost + search_cost_gross, 5),
        "price_usd": price,
        "margin_usd": round(price - expected_cost, 4) if price else None,
        "margin_ratio": round((price - expected_cost) / price, 3) if price else None,
        "notes": "Search layer dominates cost — evidence cache & budget cap are critical.",
    }
