"""
TruthScore — Config
All imports, env vars, constants.
"""

"""
TruthScore v4 -- Evidence-Based Fact-Checking Pipeline
======================================================
MSc Thesis: AI Real-Time Fact-Checking System

ARCHITECTURE (evidence-first + GPT-4o mini reasoning):
  1. RETRIEVE  -- domain-routed evidence from curated authoritative sources
  2. FILTER    -- sentence embeddings rank evidence by semantic relevance
  3. REASON    -- GPT-4o mini reads top evidence and produces verdict + explanation
  4. CITE      -- sources cited in explanation are marked as supporting/contradicting

REASONING MODEL: GPT-4o mini (OpenAI)
FALLBACK: HuggingFace NLI (if no OpenAI key)

CURATED SOURCES BY DOMAIN:
  Medicine    -> PubMed, OpenFDA, WHO, CDC
  Science     -> arXiv, NASA, NOAA
  CS/Tech     -> Semantic Scholar, arXiv
  Economics   -> World Bank, IMF, OECD
  Law/EU      -> EUR-Lex
  Biology     -> NCBI, GBIF
  Climate     -> NOAA, Climate Data Store
  News        -> Reuters, BBC, AP, The Guardian
  Fact-check  -> Snopes, FactCheck.org, PolitiFact
  Art         -> Europeana
  Math        -> arXiv, CrossRef
"""

from pathlib import Path
from dotenv import load_dotenv
# ── Console encoding hardening (must run before any print) ──────
# On Windows stdout defaults to legacy cp1252; any print() containing
# Romanian diacritics (ă/ș/ț…) raised UnicodeEncodeError and killed an
# in-flight verification. Force UTF-8 with lossy replacement so logging
# can never crash a request, whatever the language.
import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Load .env from the same directory as config.py
import pathlib as _pathlib
_env_path = _pathlib.Path(__file__).parent / ".env"
load_dotenv(dotenv_path=str(_env_path), override=True)
print(f"[ENV] Loading from: {_env_path} (exists: {_env_path.exists()})")  # Must be first -- loads .env before any os.getenv
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import asyncio, httpx, os, re, math

# ── New feature imports ────────────────────────────────────────
try:
    from auth import (register_user, login_user, get_current_user, require_user,
                      check_rate_limit, get_user_out, upgrade_user_plan,
                      UserRegister, UserLogin, UserOut, PLANS)
    AUTH_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Auth not available: {e}")
    AUTH_AVAILABLE = False
    # Fallback stubs so endpoints don't crash at load time
    class UserRegister(BaseModel):
        email: str
        password: str
        name: str = ""
    class UserLogin(BaseModel):
        email: str
        password: str
    class UserOut(BaseModel):
        id: str = ""
        email: str = ""
        name: str = ""
        plan: str = "free"
        daily_limit: int = 10
        used_today: int = 0
    PLANS = {"free":{"daily_limit":10,"batch_limit":0,"pdf":False,"widget":False}}
    async def register_user(d): raise Exception("Auth not configured")
    async def login_user(d): raise Exception("Auth not configured")
    async def get_current_user(**k): return None
    async def require_user(**k): raise Exception("Auth not configured")
    async def check_rate_limit(u, c): return {"allowed":True,"used":0,"limit":10,"plan":"free"}
    async def get_user_out(u): return UserOut()
    async def upgrade_user_plan(*a, **k): pass

try:
    import stripe
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    def _init_stripe():
        key = os.getenv("STRIPE_SECRET_KEY", "")
        if key:
            stripe.api_key = key
        return bool(key)
    STRIPE_AVAILABLE = _init_stripe()
    if not STRIPE_AVAILABLE:
        print("[WARN] Stripe: STRIPE_SECRET_KEY not set in .env")
except ImportError:
    STRIPE_AVAILABLE = False
    print("[WARN] Stripe not installed")

try:
    from pdf_report import generate_pdf_report
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    print("[WARN] ReportLab not installed")

from google import genai as genai_new
from google.genai import types as genai_types


# ── API Keys ──────────────────────────────────────────────────
HF_TOKEN        = os.getenv("HF_TOKEN", os.getenv("HF", ""))
GOOGLE_API_KEY  = os.getenv("GOOGLE_API_TOKEN", os.getenv("GOOGLE_API_KEY", ""))
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", os.getenv("GEMINI", ""))
NEWS_API_KEY     = os.getenv("NEWS_API_KEY", os.getenv("NEWS", ""))
GUARDIAN_API_KEY = os.getenv("GUARDIAN_API_KEY", os.getenv("GUARDIAN", ""))
NASA_API_KEY     = os.getenv("NASA_API_KEY", os.getenv("NASA", ""))
OPENFDA_API_KEY  = os.getenv("OPENFDA_API_KEY", os.getenv("OPENFDA", ""))
NOAA_TOKEN       = os.getenv("NOAA_TOKEN", os.getenv("NOAA", ""))
GEONAMES_USER       = os.getenv("GEONAMES_USER", "demo")
# Smithsonian works without key (optional for higher rate limits)
SMITHSONIAN_API_KEY = os.getenv("SMITHSONIAN_API_KEY", "")
HARVARD_API_KEY     = os.getenv("HARVARD_API_KEY", os.getenv("HARVARD", ""))
NPS_API_KEY          = os.getenv("NPS_API_KEY", os.getenv("NPS", ""))
WOLFRAM_API_KEY      = os.getenv("WOLFRAM_API_KEY", os.getenv("WOLFRAM", ""))
FOOTBALL_DATA_KEY    = os.getenv("FOOTBALL_DATA_KEY", os.getenv("FOOTBALLDATA", ""))
OPENSTATES_API_KEY   = os.getenv("OPENSTATES_API_KEY", os.getenv("OPENSTATES", ""))
NASA_ADS_KEY         = os.getenv("NASA_ADS_KEY", "")
STRIPE_SECRET_KEY    = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRO_PRICE_ID  = os.getenv("STRIPE_PRO_PRICE_ID", "")
STRIPE_ENT_PRICE_ID  = os.getenv("STRIPE_ENT_PRICE_ID", "")
MONGODB_URL          = os.getenv("MONGODB_URL", "mongodb://localhost:27017")
USDA_API_KEY         = os.getenv("USDA_API_KEY", "DEMO_KEY")
TAVILY_API_KEY       = os.getenv("TAVILY_API_KEY", "")

# Public base URL for building externally-visible links (Stripe return/success/
# cancel). Defaults to localhost for dev; set PUBLIC_BASE_URL in prod.
PUBLIC_BASE_URL      = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")

# ── OpenAI client ─────────────────────────────────────────────
# OpenAI removed -- using Gemini
GEMINI_MODEL = "gemini-2.5-flash"

# ── LLM CASCADE (config-driven) ───────────────────────────────
# The verification vision: try the best FREE model first, fall back to the
# next free model, and only spend on a paid model as a last resort.
#   Gemini 2.5 Flash (free tier)  →  Groq GPT-OSS (free tier)  →  paid
# Override the whole chain via LLM_CASCADE (comma-separated model names).
# LLM_PAID_FALLBACK names the paid model tried only when every free tier
# fails; leave PAID empty (default) to never spend automatically.
LLM_CASCADE = [m.strip() for m in
               os.getenv("LLM_CASCADE", "gemini,groq").split(",") if m.strip()]
LLM_PAID_FALLBACK = os.getenv("LLM_PAID_FALLBACK", "").strip()  # e.g. "gpt4o-mini"

# IMPORTANT: Use ONLY GEMINI_API_KEY -- never GOOGLE_API_KEY (that's for Fact Check API)
if GEMINI_API_KEY:
    gemini_client = genai_new.Client(api_key=GEMINI_API_KEY)
else:
    gemini_client = None
    print("WARNING: GEMINI_API_KEY not set -- AI reasoning disabled")

# ── Gemini Search Grounding config ────────────────────────────
# Enables Gemini to search Google in real-time before reasoning.
# This is the single biggest accuracy improvement for real-world claims.
# Gemini 2.5 Flash supports this natively -- no extra API key needed.
_SEARCH_TOOL = None
try:
    _SEARCH_TOOL = genai_types.Tool(
        google_search=genai_types.GoogleSearch()
    )
    print("[INFO] Gemini Search Grounding enabled (real-time Google search)")
except Exception as _e:
    print(f"[WARN] Search Grounding not available: {_e}")

# ── COST OPTIMIZATION: DISABLE THINKING MODE ───────────────────
# Gemini 2.5 Flash has "thinking mode" ON by default, which costs 5.8x
# more on output tokens ($3.50 vs $0.60 per 1M).  For fact-checking
# (verification against evidence, not mathematical reasoning), thinking
# adds zero quality but 5.8x cost.  We disable it globally.
import google.genai.types as _genai_types  # noqa: E402
THINKING_CONFIG = None
try:
    THINKING_CONFIG = genai_types.ThinkingConfig(thinking_budget=0)
    print("[INFO] Thinking mode DISABLED (saves ~5.8x on output tokens)")
except Exception:
    # Fallback: construct manually if the above fails
    try:
        THINKING_CONFIG = genai_types.ThinkingConfig(thinking_budget=0)
    except Exception:
        THINKING_CONFIG = None


def make_gemini_config(max_tokens=1000, use_search=False, temperature=0.1,
                       thinking_budget=0):
    """
    Factory: always produces a GenerateContentConfig with thinking
    *explicitly* disabled (thinking_budget=0).

    Pass thinking_budget=-1 to allow dynamic thinking when the claim
    genuinely requires deep reasoning (rare).
    """
    cfg_kwargs = {"temperature": temperature, "max_output_tokens": max_tokens}

    if thinking_budget == -1:
        # Allow thinking for genuinely complex claims
        pass  # default thinking (enabled)
    else:
        cfg_kwargs["thinking_config"] = THINKING_CONFIG

    if use_search and _SEARCH_TOOL:
        cfg_kwargs["tools"] = [_SEARCH_TOOL]

    return genai_types.GenerateContentConfig(**cfg_kwargs)

# ── Redis (distributed cache + rate-limiting + queue) ──────────
# REQUIRED for horizontal scaling.  Set REDIS_URL in .env.
# Falls back to local-only if Redis is unreachable.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

try:
    from utils.redis_client import get_async_redis, get_sync_redis, close_redis
    _redis = get_async_redis()
    if _redis:
        print("[INFO] Redis connected — distributed cache + rate-limit enabled")
    else:
        print("[WARN] Redis not available — running in single-instance mode")
except Exception as e:
    print(f"[WARN] Redis import failed: {e}")
    _redis = None

# Evidence ranking keeps top-K after cross-encoder rerank
EMBED_TOP_K       = 12

# ── Config ────────────────────────────────────────────────────
GOOGLE_FC_URL = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
RO_CHARS      = set("ăâîșțĂÂÎȘȚ")

RSS_FEEDS = [
    ("Reuters",        "https://feeds.reuters.com/reuters/topNews"),
    ("BBC News",       "https://feeds.bbci.co.uk/news/rss.xml"),
    ("FactCheck.org",  "https://www.factcheck.org/feed/"),
    ("Snopes",         "https://www.snopes.com/feed/"),
    ("AP News",        "https://apnews.com/rss"),
    ("The Guardian",   "https://www.theguardian.com/world/rss"),
]

# NOTE: the FastAPI app lives ONLY in main.py. Config exposes shared
# constants + clients; no second ASGI app is created here.