"""
TruthScore -- Startup capability / health report.

Surfaces, at boot and via /health, exactly which optional integrations are
configured so operators aren't guessing why a subsystem is degraded. Every
check is a cheap presence test (is the key/URL set?) — the actual reachability
of Redis is verified separately in redis_client.verify_async_redis(). Nothing
here raises; a missing key is reported, never fatal.
"""
import os


def _has(*names: str) -> bool:
    """True if any of the given env vars is set to a non-empty value."""
    return any((os.getenv(n) or "").strip() for n in names)


def capability_report() -> dict:
    """Presence map of every optional integration, grouped by concern.

    The three LLM providers form the reasoning cascade (Gemini → Groq →
    GPT-4o-mini); at least one must be present for verdicts to work. Search
    keys are all optional — the pipeline has keyless fallbacks (Wikipedia,
    OpenAlex, CrossRef, Semantic Scholar, DDG) so retrieval degrades rather
    than fails when they're absent.
    """
    jwt_default = "CHANGE_THIS_SECRET_IN_PRODUCTION_32chars"
    jwt_set = bool((os.getenv("JWT_SECRET") or "").strip())
    jwt_is_placeholder = (os.getenv("JWT_SECRET") or jwt_default) == jwt_default

    llm = {
        "gemini":      _has("GEMINI_API_KEY", "GEMINI"),
        "groq":        _has("GROQ_API_KEY"),
        "gpt4o_mini":  _has("OPENAI_API_KEY"),
    }
    search = {
        "tavily":      _has("TAVILY_API_KEY"),
        "newsapi":     _has("NEWS_API_KEY", "NEWS"),
        "guardian":    _has("GUARDIAN_API_KEY", "GUARDIAN"),
        "scopus":      _has("SCOPUS_API_KEY"),
        "openfda":     _has("OPENFDA_API_KEY", "OPENFDA"),
        "noaa":        _has("NOAA_TOKEN", "NOAA"),
        "factcheck":   _has("GOOGLE_FACTCHECK_API_KEY", "FACTCHECK_API_KEY"),
    }
    infra = {
        "redis":       _has("REDIS_URL"),
        "mongodb":     _has("MONGODB_URL") and "localhost" not in (os.getenv("MONGODB_URL") or ""),
        "stripe":      _has("STRIPE_SECRET_KEY"),
        "stripe_webhook": _has("STRIPE_WEBHOOK_SECRET"),
        "google_oauth": _has("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_IDS"),
        "turnstile":   _has("TURNSTILE_SECRET_KEY", "TURNSTILE_SECRET"),
        "jwt_secret":  jwt_set and not jwt_is_placeholder,
    }
    return {
        "llm": llm,
        "llm_ok": any(llm.values()),
        "search": search,
        "search_keys_present": sum(1 for v in search.values() if v),
        "infra": infra,
        "warnings": _warnings(llm, infra),
    }


def _warnings(llm: dict, infra: dict) -> list:
    w = []
    if not any(llm.values()):
        w.append("NO LLM PROVIDER configured (GEMINI/GROQ/OPENAI) — verdicts will be UNCERTAIN.")
    if not infra["jwt_secret"]:
        w.append("JWT_SECRET is unset or the default placeholder — set a real 32+ char secret before production.")
    if not infra["redis"]:
        w.append("REDIS_URL unset — running single-instance (no cross-instance cache/limit/queue).")
    if infra["stripe"] and not infra["stripe_webhook"]:
        w.append("Stripe key set but STRIPE_WEBHOOK_SECRET missing — subscription lifecycle events won't be verified.")
    return w


def log_startup_health() -> dict:
    """Print a concise capability summary at boot; return the full report."""
    rep = capability_report()
    llm_on  = [k for k, v in rep["llm"].items() if v] or ["NONE"]
    infra_on = [k for k, v in rep["infra"].items() if v] or ["none"]
    print(f"[HEALTH] LLM providers: {', '.join(llm_on)}")
    print(f"[HEALTH] Search keys present: {rep['search_keys_present']}/{len(rep['search'])} "
          f"(keyless fallbacks always active)")
    print(f"[HEALTH] Infra: {', '.join(infra_on)}")
    for warn in rep["warnings"]:
        print(f"[HEALTH][WARN] {warn}")
    return rep
