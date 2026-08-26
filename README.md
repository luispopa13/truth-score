# TruthScore — AI Fact-Checking Platform

Evidence-first AI fact-checking for a browser extension, embeddable widget,
and API.  This is the **production-optimized** version — rebuilt for **lowest
cost per verdict**, **highest verdict quality**, and **horizontal scalability
to 1000+ concurrent users**.

---

## 1. What changed (the money & scale optimizations)

### a) Cost: disabled Gemini "thinking mode" globally (saves ~5.8x)
`config.py` now builds every Gemini request through `make_gemini_config(...)`
with `thinking_budget=0`. Gemini 2.5 Flash enables thinking in the API you call.
With thinking **ON**, output costs **$3.50 / 1M tokens**; with it **OFF**, only
**$0.60 / 1M tokens**. For fact-checking (verify statements against evidence,
not solve proofs) thinking adds ~zero quality but multiplies the bill. **This
is the single biggest, zero-risk cost cut in the project.**

### Real cost per verdict (after the fix)
- Reasoning call (Gemini Flash, no thinking): ~ **$0.0006–0.001 / verdict**
- With semantic caching, 30–50% of claims never reach the LLM → **cost ~ $0**
- Even a Pro user burning all 300 checks/day only costs you **~$6.3/month**
  against a **9.99 €** subscription → healthy margin, even in the worst case.

### Why NOT self-hosting / fine-tuning for now
Self-hosting a 7–8B model = **$200–400/month in fixed GPU cost** you pay even
while idle, plus you need a large curated fine-tuning dataset (thousands of
labeled verdicts) you don't have yet. At low/medium volume the fixed cost
dominates. **Fine-tuning pays off only once you have real production traffic
and a feedback-labeled dataset.** Until then, use per-token APIs.

---

## 2. Architecture for 1000+ concurrent users

```
Browser Extension / Widget / API
        │  (Bearer JWT or stable API key `ts_...`)
        ▼
┌─────────────────────────────────────────────┐
│  FastAPI (stateless, horizontally scalable) │
│  • /verify  — auth + Redis rate-limited     │
│  • /batch-verify, /verify-pdf               │
│  • /api-keys, /metrics, /plans              │
└──────────────┬──────────────┬──────────────┘
               │              │
      Redis (shared)     MongoDB Atlas (pooled)
      • semantic cache   • users / plans
      • rate limiting    • api_keys
      • LLM queue        • case-study logs
               │              │
        LLM providers (Gemini → Groq → GPT-4o-mini)
```

### Key points
- **FastAPI stateless + horizontal scaling** — more instances behind a load
  balancer; Redis + MongoDB are the shared state.
- **Redis semantic cache** — exact *and* near-duplicate claims served free.
- **Redis rate limiter** — atomic per-plan daily quotas (free 15/day, pro 300,
  business 2000, enterprise 9999).
- **LLM concurrency cap** (`LLM_CONCURRENCY`, default 8) — a semaphore stops
  1000 users firing 1000 parallel provider calls and hitting 429s.
- **MongoDB pooling** — `maxPoolSize` tuned for many concurrent reads.

---

## 3. Model routing & quality vs cost

`pipeline/reasoning.py::call_llm_raw(..., model=...)` supports tiers:
- **`groq`** → `llama-3.3-70b` (very cheap) — good for easy claims
- **`gpt4o-mini`** — optional tier for paid plans
- **`gemini`** (default) — highest quality, thinking OFF

Calidad is preserved: verdict quality depends mostly on **evidence quality**
(retrieval + cross-encoder + NLI), not model size. The system already runs
Path A + Path B (mathematical evidence stance scoring) + FActScore + AVeriTeC
+ Wikidata, so even the "cheap" path is evidence-driven.

### Plan → allowed models
- **Free / Pro**: gemini
- **Business / Enterprise**: gemini + groq + gpt4o-mini
---

## 4. Monetization model

- **Free (15/day)** → conversion hook
- **Pro 9.99 €/mo** → 300 checks/day — core revenue
- **Business 39.99 €/mo** → 2000/day, batch + PDF + widget
- **Enterprise** → negotiated, 9999+/day, custom contracts

Monetization runs on **your own Stripe subscriptions** (not the store).
The browser extension is a thin client that authenticates with your API via a
JWT or a stable **API key** (`ts_...`). Extensions can't take subscriptions
themselves; the server handles it.

---

## 5. Quick start

```bash
# 1. Copy env and fill in your keys
cp truthscore-backend/.env.example truthscore-backend/.env

# 2. Install Python deps
cd truthscore-backend && pip install -r requirements.txt

# 3. Start Redis (optional but recommended for scaling)
docker compose up redis -d

# 4. Run the API
cd truthscore-backend && uvicorn main:app --reload

# 5. Tests
python run_tests.py   # 15 validation tests (no network)
```

Full local stack (backend + Redis):
```bash
docker compose up --build   # backend :8000, redis :6379
```

---

## 6. Endpoints quick reference

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/register`, `/auth/login` | – | accounts |
| POST | `/verify` | bearer | fact-check (rate-limited) |
| POST | `/detect-claims` | – | split text into claims |
| POST | `/feedback` | – | thumbs up/down |
| POST | `/batch-verify` | pro+ | verify many claims |
| GET | `/plans` | – | pricing |
| POST/GET/DELETE | `/api-keys` | bearer | widget/extension keys |
| GET | `/metrics/cost` | – | real cost per claim |
| GET | `/metrics/quota` | – | daily limits by plan |
| GET | `/widget.js?user_key=ts_...` | – | embed widget |

---

## 7. Monitoring & cost control

- **`/metrics/cost`** — live USD spend per model.
- **`/metrics/quota`** — daily limits snapshot.
- **`X-TruthScore-Interaction-Id`** header + `/case-study/export` gives a full
  CSV (verified claims + user feedback) for calibration.

`utils/metrics.py` makes margin math explicit:
```python
MODEL_COSTS = {
    "gemini-2.5-flash":        {"input": 0.075, "output": 0.60},  # thinking OFF
    "gemini-2.5-flash-think":  {"input": 0.075, "output": 3.50},  # avoid
}
```

---

## 8. Repository layout

```
truthscore-backend/
  main.py              # FastAPI app + routes (auth, billing, api-keys)
  config.py            # env, clients, thinking-disabled factory
  auth.py              # JWT + API keys + plan quotas + Redis rate-limit
  pipeline/            # retrieval, ranking, reasoning, verify
  api/                 # payments, batch/pdf, widget
  utils/               # redis_client, rate_limiter, semantic_cache,
                       # llm_queue, api_keys, metrics
truthscore-extension/  # Chrome/Edge MV3 extension (thin client)
  background.js        # auth header + verify/detect
  popup.js/.html/.css  # UI
  content.js/.css      # in-page selection + scan
docker-compose.yml     # backend + Redis
tests/                 # test_core.py, test_integration.py
```

---

## 9. Roadmap

- [x] Disable thinking (5.8x cost cut) on **every** Gemini call
- [x] Distributed semantic cache + rate limit on Redis
- [x] API keys for extension/widget (stable, not expiring JWT)
- [x] Real per-plan daily limits + Business tier
- [x] Cost / margin metrics endpoint
- [ ] Model routing per plan (cheap model for simple claims)
- [ ] Background worker for `/batch-verify` (Redis queue) for huge jobs
- [ ] k6 load-test script to prove 1000 concurrent users

---

# 🚀 Go-Live Checklist

## 1. Deploy backend (Render)
| Env var | Value |
|---|---|
| `MONGODB_URL` | Atlas SRV string |
| `REDIS_URL` | Upstash rediss:// URL |
| `GEMINI_API_KEY` / `GROQ_API_KEY` / `TAVILY_API_KEY` | your keys |
| `STRIPE_SECRET_KEY`, `STRIPE_PRO_PRICE_ID`, `STRIPE_BUSINESS_PRICE_ID` | Stripe dashboard |
| `ADSENSE_CLIENT` | **leave empty until AdSense approves the site** |
| `ADS_ENABLED` | true |

Build command: `pip install -r requirements.txt && python scripts/download_models.py`
Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`

> Model weights are NOT in git (~640 MB; GitHub caps files at 100 MB).
> `scripts/download_models.py` fetches them into `./models` on first build.

## 2. Domain (recommended: buy one)
AdSense requires a top-level domain you own — Render/Netlify/Vercel subdomains and
free TLDs (.tk/.ml) don't qualify for approval and look untrustworthy in the
Chrome Web Store listing. Cloudflare Registrar sells at cost (~$10/yr .com,
~$14 .app) and bundles DNS+CDN free. Point an A/CNAME record at your Render app;
HTTPS cert is automatic.

## 3. Google AdSense flow
1. Apply with your own domain, site live with real content.
2. After approval set `ADSENSE_CLIENT=ca-pub-XXXX` → redeploy.
   - `/ads.txt` is generated automatically from that env var.
   - Units render only for anonymous/free users on the dashboard (`X-TruthScore-Show-Ads`).
3. Extension popup/page overlays stay self-promo-only — AdSense policy forbids ads
   inside extensions or injected content; direct sponsorships are allowed there.

## 4. Chrome Web Store submission
1. `$5` developer registration one-time.
2. Zip `truthscore-extension/` (exclude any dev notes), upload, fill store listing.
3. Privacy tab: data usages = website content (claims you verify), provide URL:
   `https://your-domain/privacy`.
4. Justify permissions narrowly to speed review.