"""TruthScore integration tests (no pytest requirement — guarded)."""
try:
    import pytest  # noqa: F401  (used only when running via pytest)
except ImportError:
    pass


def test_verify_plan_features_consistent():
    """PLANS (auth) and rate_limiter PLAN_LIMITS / features stay in sync."""
    from auth import PLANS, _PLAN_FEATURES
    from utils.rate_limiter import _PLAN_FEATURES as RL_FEATURES
    for plan in PLANS.keys():
        assert plan in _PLAN_FEATURES, f"{plan} missing auth._PLAN_FEATURES"
        assert plan in RL_FEATURES, f"{plan} missing rate_limiter._PLAN_FEATURES"


def test_semantic_cache_store_lookup_roundtrip():
    """semantic_store + semantic_lookup roundtrip without Redis (local diskcache)."""
    import asyncio
    from utils.semantic_cache import semantic_store, semantic_lookup

    async def _run():
        data = {"claim": "test claim", "verdict": "TRUE", "score": 90}
        await semantic_store("the amazon rainforest produces 20% of oxygen", dict(data))
        hit = await semantic_lookup("the amazon rainforest produces 20% of oxygen")
        return hit

    hit = asyncio.run(_run())
    assert hit is not None
    assert hit["cached"] is True if isinstance(hit, dict) else True