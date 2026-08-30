# TruthScore — Ghid de Lansare (pas cu pas, tot ce ai TU de făcut)

> Codul e gata și verificat (23/23 teste trec). Ce urmează sunt **conturi, chei și configurări externe** — lucruri pe care doar tu le poți face. Le-am pus în ordinea în care ar trebui făcute, cu efort/cost estimat.
>
> **Regula de aur:** aplicația citește TOTUL din variabile de mediu (`.env` local / dashboard-ul de hosting în prod). Nu trebuie să modifici cod. Când vezi `NUME_CHEIE=`, aia e o variabilă pe care o completezi.

---

## 0. Harta rapidă — de ce ai nevoie ca să lansezi

| Nivel | Ce-ți trebuie | Cost |
|---|---|---|
| **MINIM (pornește + verifică)** | Gemini key, Groq key, MongoDB, JWT_SECRET | **0 €** |
| **PUBLIC (site live pe domeniu)** | + Domeniu, Hosting, PUBLIC_BASE_URL, ENV=production | ~10–15 €/an domeniu + hosting free/ieftin |
| **LOGIN social** | + Google OAuth (client id/secret) | 0 € |
| **BANI (abonamente)** | + Stripe (produse + price IDs + webhook) | 0 € (Stripe ia % pe tranzacție) |
| **SCALARE 1000 useri** | + Redis (Upstash free) | 0 € pe free tier |
| **EMAIL (digest, reset parolă)** | + SendGrid / Brevo / SMTP | 0 € (free tier) |
| **ANTI-BOT** | + Cloudflare Turnstile | 0 € |
| **MONETIZARE ADS** | + Google AdSense | 0 € (câștigi tu) |
| **OPȚIONAL** | API-uri de date (surse mai bune), boți, push | 0 € (majoritatea free) |

Poți lansa cu MINIM + PUBLIC + LOGIN + BANI. Restul le adaugi treptat.

---

## 1. MongoDB (baza de date) — **OBLIGATORIU**

Fără MongoDB nu se salvează useri, verdicte, feedback. Recomand **MongoDB Atlas** (cloud, free tier M0 — 512 MB, suficient pentru start).

**Pas cu pas:**
1. Intră pe [mongodb.com/cloud/atlas](https://www.mongodb.com/cloud/atlas) → **Sign up** (gratis).
2. **Create a Cluster** → alege **M0 Free** → regiune apropiată (ex. Frankfurt/Ireland pentru Europa).
3. **Database Access** (meniu stânga) → **Add New Database User**:
   - Username: `truthscore` · Password: generează unul lung, salvează-l.
   - Role: **Read and write to any database**.
4. **Network Access** → **Add IP Address**:
   - Pentru început, `0.0.0.0/0` (acces de oriunde — simplu, dar mai puțin sigur).
   - Ideal în prod: adaugă doar IP-ul serverului de hosting (îl afli după ce alegi hosting-ul).
5. **Database** → **Connect** → **Drivers** → copiază connection string-ul, arată așa:
   ```
   mongodb+srv://truthscore:<PAROLA>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   ```
6. Înlocuiește `<PAROLA>` cu parola reală. Asta pui în:
   ```
   MONGODB_URL=mongodb+srv://truthscore:PAROLA@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority
   MONGODB_DB=truthscore
   ```

> **Indexuri:** aplicația le creează automat la pornire. Nu trebuie să faci nimic manual.

---

## 2. Chei LLM (creierul AI) — **OBLIGATORIU (cel puțin una)**

Viziunea e „cele mai bune modele GRATUITE": Gemini 2.5 Flash (free) → Groq (free) → plătit doar ca ultimă soluție. Ia-le pe amândouă gratuite.

### 2a. Gemini (principal)
1. [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey) → **Create API key**.
2. Copiază → `GEMINI_API_KEY=...`
3. Free tier: ~1500 requests/zi, suficient pentru start.

> ⚠️ **Atenție (memoria ta):** rețeaua SAP de la laptopul de muncă blochează Gemini/Groq (`User location not supported`). **Testează pe laptopul personal.** Cheile sunt valide; e blocaj de rețea, nu de cont.

### 2b. Groq (fallback ieftin/rapid)
1. [console.groq.com/keys](https://console.groq.com/keys) → **Create API Key**.
2. `GROQ_API_KEY=...`
3. Gratis, foarte rapid, folosit automat pentru claim-uri simple (eco-mode) → îți taie costul.

### 2c. OpenAI (OPȚIONAL)
Doar dacă vrei un al treilea tier. Lasă gol dacă nu: `OPENAI_API_KEY=`

---

## 3. JWT_SECRET (securitatea login-ului) — **OBLIGATORIU**

Un șir lung random care semnează token-urile de login. Generează-l:
```bash
openssl rand -hex 32
```
(pe Windows Git Bash merge). Rezultatul îl pui:
```
JWT_SECRET=rezultatul_de_64_caractere_de_mai_sus
```
> În producție aplicația **refuză să pornească** cu un secret placeholder — asta e intenționat.

---

## 4. Domeniu — **OBLIGATORIU pentru public**

1. Cumpără de la **Namecheap**, **Cloudflare Registrar** (cel mai ieftin, la preț de cost) sau **Porkbun**. Ex: `truthscore.app` (~15 €/an) sau `.ro`/`.com`.
2. Recomand să treci DNS-ul prin **Cloudflare** (gratis): cont Cloudflare → Add site → schimbă nameserver-ele la registrar → Cloudflare îți dă SSL gratuit + protecție DDoS + e necesar oricum pentru Turnstile (anti-bot).
3. După ce ai hosting-ul (pasul 5), adaugi un record DNS (A sau CNAME) care pointează domeniul spre server.
4. Setezi în env:
   ```
   PUBLIC_BASE_URL=https://truthscore.app
   ENV=production
   CORS_ORIGINS=https://truthscore.app
   ```

> `PUBLIC_BASE_URL` e critic: link-urile Stripe, emailurile, verdictele publice, boții — toate îl folosesc. În prod, dacă e nesetat, aplicația dă 503 intenționat (fail-closed) ca să nu trimită link-uri către localhost.

---

## 5. Hosting / Deploy — **OBLIGATORIU pentru public**

Aplicația e Python (FastAPI + gunicorn). Ai `Dockerfile`, `Procfile` și `gunicorn.conf.py` gata.

**Opțiuni (de la simplu la avansat):**

| Platformă | De ce | Cost |
|---|---|---|
| **Render.com** | Cel mai simplu; conectezi GitHub, detectează Dockerfile, deploy automat | Free tier (adoarme după inactivitate) / ~7 $/lună always-on |
| **Railway.app** | Similar, UI foarte bun, bază de date + Redis într-un loc | ~5 $/lună credit |
| **Fly.io** | Rapid global, generos free tier | Free / pay-as-you-go |
| **VPS (Hetzner/DigitalOcean)** | Control total, cel mai ieftin la scală | ~4–6 €/lună |

**Pas cu pas (exemplu Render):**
1. Pune codul pe GitHub (repo privat e ok).
2. Render → **New → Web Service** → conectează repo-ul.
3. Environment: **Docker** (folosește Dockerfile-ul existent). Root directory: `truthscore-backend`.
4. **Environment Variables** → adaugi TOATE cheile din `.env` (vezi checklist-ul de la secțiunea 14).
5. Deploy. Îți dă un URL `xxxx.onrender.com` — testează pe el întâi, apoi legi domeniul.
6. **Custom domain** → adaugi `truthscore.app` → Render îți dă un CNAME → îl pui în Cloudflare DNS.
7. Setează `PUBLIC_BASE_URL=https://truthscore.app` și `ENV=production`.

> **Workers:** `gunicorn.conf.py` gestionează numărul de workeri. Pentru M0/free tier lasă 1–2. Pentru 1000 useri simultani ai nevoie de Redis (secțiunea 6) + mai mulți workeri + eventual sidecar de ranking (avansat, mai târziu).

---

## 6. Redis — **RECOMANDAT (obligatoriu pentru scalare/1000 useri)**

Fără Redis, aplicația merge (degradează grațios), dar: rate-limiting-ul e per-worker, cache-ul nu e partajat, scheduler-ul poate rula de mai multe ori. Cu Redis: cache distribuit, rate-limit global, leader-election pentru joburi, cozi LLM.

**Upstash (free, serverless, perfect pentru început):**
1. [upstash.com](https://upstash.com) → Sign up → **Create Database** (Redis) → regiune apropiată.
2. Copiază **Redis URL** (format `rediss://...`).
3. Setează:
   ```
   REDIS_URL=rediss://default:PAROLA@xxxx.upstash.io:6379
   REDIS_ENABLED=true
   ```

---

## 7. Google OAuth (login cu Google) — pentru login social

1. [console.cloud.google.com](https://console.cloud.google.com) → creează un proiect (ex. „TruthScore").
2. **APIs & Services → OAuth consent screen** → External → completează nume app, email, logo → publică (la început poate rămâne „Testing" cu tine ca test user).
3. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**.
   - **Authorized JavaScript origins:** `https://truthscore.app` (și `http://localhost:8000` pentru dev).
   - **Authorized redirect URIs:** `https://truthscore.app` (aplicația folosește flow authorization-code + PKCE din dashboard).
4. Copiază **Client ID** și **Client Secret**:
   ```
   GOOGLE_CLIENT_ID=xxxxx.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=xxxxx
   ```
> Frontend-ul ia client_id-ul automat din `/site-config` (server = sursa de adevăr). **Nu trebuie să editezi niciun fișier** — doar setezi env-ul.

---

## 8. Stripe (abonamente/bani) — pentru monetizare

1. [stripe.com](https://stripe.com) → cont → activează-l (date firmă/PFA + cont bancar pentru payout).
2. Lucrează întâi în **Test mode** (comutatorul din dreapta sus).
3. **Products** → creează câte un produs cu preț recurent pentru fiecare plan pe care-l vinzi. Aplicația suportă aceste price IDs:

   | Plan | Variabilă lunară | Variabilă anuală |
   |---|---|---|
   | Pro | `STRIPE_PRO_PRICE_ID` | `STRIPE_PRO_ANNUAL_PRICE_ID` |
   | Business (29.99) | `STRIPE_BUSINESS_PRICE_ID` | `STRIPE_BUSINESS_ANNUAL_PRICE_ID` |
   | Monitor | `STRIPE_MONITOR_PRICE_ID` | `STRIPE_MONITOR_ANNUAL_PRICE_ID` |
   | Enterprise | `STRIPE_ENT_PRICE_ID` | — |

   Fiecare produs → **Add price** → recurring monthly/yearly → copiază `price_xxx`.
4. **Developers → API keys** → copiază **Secret key**:
   ```
   STRIPE_SECRET_KEY=sk_test_... (apoi sk_live_... în producție)
   ```
5. **Webhook:** Developers → **Webhooks → Add endpoint**:
   - URL: `https://truthscore.app/stripe/webhook` (verifică numele exact al rutei în cod dacă diferă — caută `stripe/webhook` în `api/payments.py`/`main.py`).
   - Events: `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.paid`, `invoice.payment_failed`.
   - Copiază **Signing secret**:
     ```
     STRIPE_WEBHOOK_SECRET=whsec_...
     ```
6. Testează cu cardul `4242 4242 4242 4242` (orice dată viitoare + CVC). Când merge, treci pe **Live mode** și repeți (chei `live` + price IDs `live`).

---

## 9. Email (digest zilnic + reset parolă) — recomandat

Codul suportă **SendGrid**, **Brevo** sau **SMTP** generic. Alege UNA:

- **SendGrid** (100 emailuri/zi gratis): [sendgrid.com](https://sendgrid.com) → API Key → verifică un sender/domeniu →
  ```
  SENDGRID_API_KEY=SG.xxxx
  FROM_EMAIL=noreply@truthscore.app
  FROM_NAME=TruthScore
  ```
- **Brevo** (fost Sendinblue, 300/zi gratis): `BREVO_API_KEY=...`
- **SMTP** (Gmail/alt): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`.

> Verifică domeniul expeditor (SPF/DKIM) în panoul providerului ca să nu ajungi în spam. Cloudflare DNS + instrucțiunile SendGrid rezolvă asta în 10 min.

---

## 10. Cloudflare Turnstile (anti-bot la înregistrare/login) — recomandat

1. Cloudflare dashboard → **Turnstile** → **Add site** → domeniul tău.
2. Copiază **Secret key**:
   ```
   TURNSTILE_SECRET=0x4AAA...
   ```
> Fără el, în DEV protecția e OFF (fail-open). În PROD, dacă e setat, se aplică. Pune-l înainte de lansare ca să nu-ți umple boții baza cu conturi fake.

---

## 11. Google AdSense (monetizare din reclame, doar free tier) — opțional dar profitabil

1. [adsense.google.com](https://adsense.google.com) → cont → adaugă site-ul → așteaptă aprobarea (poate dura zile; ai nevoie de trafic real + conținut).
2. După aprobare, copiază **Publisher ID** (`ca-pub-xxxxxxxx`):
   ```
   ADSENSE_CLIENT=ca-pub-xxxxxxxxxxxxxxxx
   ADS_ENABLED=true
   ```
> Lasă `ADSENSE_CLIENT` gol până la aprobare — atunci **niciun script de ad nu se încarcă** nicăieri. Reclamele apar DOAR pe dashboard, DOAR la userii free. Extensia rămâne fără reclame (doar self-promo). Asta e deja implementat în cod.

---

## 12. API-uri de date pentru surse mai bune — OPȚIONAL (toate gratuite)

Aplicația funcționează fără ele (folosește surse free: Wikipedia, DuckDuckGo, PubMed, arXiv). Dar cu ele, verificările pe domenii specifice devin mult mai puternice. Adaugă-le pe cele care contează pentru publicul tău:

| Cheie | Pentru ce | De unde (gratis) |
|---|---|---|
| `TAVILY_API_KEY` | Căutare web premium (fallback când sursele free-s subțiri) | tavily.com — 1000 credite/lună free |
| `NEWS_API_KEY` | Știri | newsapi.org |
| `GUARDIAN_API_KEY` | Articole Guardian | open-platform.theguardian.com |
| `GOOGLE_API_KEY` | Google Fact Check Tools (fact-check-uri existente) | console.cloud.google.com → Fact Check Tools API |
| `OPENFDA_API_KEY` | Medicamente/sănătate (FDA) | open.fda.gov |
| `NOAA_TOKEN` | Vreme/climă | ncdc.noaa.gov |
| `NASA_API_KEY` | Spațiu/astronomie | api.nasa.gov |
| `USDA_API_KEY` | Nutriție/alimente | fdc.nal.usda.gov |
| `HF_TOKEN` | Hugging Face (modele) | huggingface.co/settings/tokens |
| `WOLFRAM_API_KEY` | Calcule/fapte științifice | developer.wolframalpha.com |

> **Recomandare pentru start:** pune doar `TAVILY_API_KEY` + `GOOGLE_API_KEY` (Fact Check). Restul le adaugi când vezi ce întreabă userii. `TAVILY_MODE=fallback_only` (deja default) îți ține costul mic — plătește Tavily doar când sursele gratuite nu ajung.

---

## 13. Boți & extras — OPȚIONAL (adaugă după lansare)

- **Extensie browser:** e în `truthscore-extension/`. Se publică separat pe Chrome Web Store (~5 $ taxă unică dev) și Firefox Add-ons (gratis). O faci după ce site-ul e stabil.
- **Telegram bot:** vorbește cu [@BotFather](https://t.me/BotFather) → `/newbot` → `TELEGRAM_BOT_TOKEN=...` → setezi webhook la `https://truthscore.app/telegram/webhook`.
- **Twitter/X, Slack, WhatsApp:** au variabile dedicate (`TWITTER_*`, `SLACK_*`, `WHATSAPP_*`). Sunt nișă — lasă-le pe mai târziu.
- **Push notifications (VAPID):** generează chei cu `npx web-push generate-vapid-keys` → `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_EMAIL=mailto:tu@truthscore.app`.

---

## 14. Checklist `.env` — copiază și completează

### MINIM ca să pornească local:
```
GEMINI_API_KEY=
GROQ_API_KEY=
MONGODB_URL=
MONGODB_DB=truthscore
JWT_SECRET=
ENV=dev
```

### COMPLET pentru lansare publică:
```
# --- Core AI ---
GEMINI_API_KEY=
GROQ_API_KEY=
OPENAI_API_KEY=

# --- Infra ---
MONGODB_URL=
MONGODB_DB=truthscore
REDIS_URL=
REDIS_ENABLED=true

# --- Public / deploy ---
PUBLIC_BASE_URL=https://truthscore.app
ENV=production
CORS_ORIGINS=https://truthscore.app

# --- Auth ---
JWT_SECRET=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# --- Stripe ---
STRIPE_SECRET_KEY=
STRIPE_WEBHOOK_SECRET=
STRIPE_PRO_PRICE_ID=
STRIPE_PRO_ANNUAL_PRICE_ID=
STRIPE_BUSINESS_PRICE_ID=
STRIPE_BUSINESS_ANNUAL_PRICE_ID=
STRIPE_MONITOR_PRICE_ID=
STRIPE_MONITOR_ANNUAL_PRICE_ID=
STRIPE_ENT_PRICE_ID=

# --- Email ---
SENDGRID_API_KEY=
FROM_EMAIL=noreply@truthscore.app
FROM_NAME=TruthScore

# --- Anti-abuse ---
TURNSTILE_SECRET=

# --- Ads (după aprobare AdSense) ---
ADSENSE_CLIENT=
ADS_ENABLED=true

# --- Surse (opțional, recomandate) ---
TAVILY_API_KEY=
GOOGLE_API_KEY=
```

---

## 15. Verificare finală înainte de lansare (smoke test)

Pe laptopul personal (unde LLM-ul nu e blocat):
1. `cd truthscore-backend && uvicorn main:app --port 8000`
2. Deschide `http://localhost:8000` → verifică o afirmație compusă:
   *„Shakespeare wrote Hamlet and Paris is the capital of Italy"* → trebuie să vezi sub-claims cu scoruri/verdicte + surse.
3. **Login** cu email + cu Google.
4. **Stripe test:** cumpără un plan cu `4242 4242 4242 4242` → verifică că planul se activează în cont.
5. **Feedback** (like/dislike) → verifică în MongoDB (colecția de feedback) că se salvează.
6. `python run_tests.py` din root → **23/23 passed**.
7. Verifică `/health` întoarce 200 și `/site-config` întoarce `google_client_id`.
8. Deploy pe hosting → repetă 2–5 pe domeniul real → apoi treci Stripe pe **Live**.

---

# 📣 STRATEGIE DE MARKETING / PROMOVARE

Produsul tău are un avantaj clar de comunicat: **„fact-checking cu surse reale și scor per-afirmație, nu doar o părere de la un chatbot"**. Radical transparency — arăți sursele, contradicțiile și de ce. Construiește totul în jurul acestui mesaj.

## Faza 0 — Pregătire (înainte de lansare)
- **Poziționare într-o frază:** „TruthScore verifică orice afirmație pe surse reale și îți dă un scor de adevăr, cu dovezi." Pune-o în hero-ul site-ului, în bio-uri, peste tot.
- **Landing page clar:** demo live vizibil imediat (fără login), 3 exemple pre-completate pe care le poate încerca oricine în 1 click.
- **Track record public** (pagina există deja) — dovada că nu ascunzi greșelile = încredere.
- **Analytics gratuit:** pune **Plausible** (privacy-friendly) sau Google Analytics ca să știi de unde vin userii și ce afirmații caută.
- **Pregătește 20–30 de „verificări virale"**: mituri populare, știri false recente, afirmații politicieni/vedete. Fiecare devine o postare cu screenshot + link.

## Faza 1 — Lansare (primele 2 săptămâni, target: primii 100–1000 useri)
1. **Product Hunt** — lansează într-o marți/miercuri. Pregătește: GIF demo, 3–5 comentarii de la susținători în prima oră, răspunde la TOATE comentariile. Un loc în top 5 pe zi = mii de vizitatori.
2. **Reddit** (unde e publicul tău, dar respectă regulile fiecărui sub):
   - r/InternetIsBeautiful, r/coolgithubprojects, r/artificial, r/technology
   - Nișă: r/skeptic, r/politics (cu verificări de actualitate), r/COVID19 etc.
   - **NU spama** — postează o verificare interesantă ca și conținut, linkul vine natural.
3. **Hacker News** — „Show HN: TruthScore – fact-checking with real sources and per-claim scoring". Fii prezent în comentarii, tehnic și onest.
4. **X/Twitter + Threads + Bluesky:** postează zilnic o verificare virală. Formatul care merge: *„Cineva a zis X. Am verificat. Iată verdictul + sursele 🧵"*. Screenshot + link.
5. **TikTok / Reels / Shorts:** cel mai mare potențial viral pentru fact-checking. Video de 20–40s: „Am dat prin TruthScore afirmația asta pe care o vezi peste tot..." → reveal verdict. Debunking-ul de mituri e conținut care se distribuie singur.

## Faza 2 — Creștere (lunile 1–3)
- **SEO — motorul pe termen lung:** fiecare verdict public e o pagină indexabilă. Lasă Google să le indexeze (sitemap). Oamenii caută „is X true" → aterizează pe verdictul tău. Asta poate deveni sursa #1 de trafic gratuit.
- **Conținut programatic:** generează pagini pentru afirmații populare pe categorii (sănătate, politică, știință). Fiecare = intrare SEO.
- **Newsletter (digest-ul zilnic e deja construit!):** „Top 5 minciuni ale săptămânii" — conținut perfect de share, aduce oamenii înapoi. Promovează-l agresiv.
- **Extensia de browser:** odată publicată, e un canal de retenție puternic — verifici direct pe orice pagină. Cere review-uri pe store (crește ranking-ul).
- **Colaborări:** contactează creatori de conținut din zona „debunking"/skepticism/educație. Oferă-le acces gratuit Pro/Business în schimbul unui mention. Un YouTuber de nișă potrivit = mii de useri.
- **Comunități:** Discord/Telegram unde se discută știri și dezinformare — fii de ajutor, nu vânzător.

## Faza 3 — Monetizare & scalare (luna 3+)
- **Free → Pro conversie:** limita zilnică free (10 verificări) e cârligul. Când o ating, arată clar valoarea Pro (mai multe verificări, fără reclame, monitorizare). Nu fi agresiv — lasă valoarea să vândă.
- **B2B (unde-s banii serioși):** planul Business/Monitor pentru:
  - Redacții / jurnaliști (verificare rapidă înainte de publicare).
  - Branduri (monitorizare afirmații despre ele).
  - Educație (profesori, biblioteci — media literacy).
  - Contactează direct pe LinkedIn/email. Un singur client B2B > sute de useri free.
- **API ca produs:** vinde acces API (deja ai chei API + planuri). Developerii care vor fact-checking în app-urile lor = venit recurent.
- **PR:** când ai un caz bun (ai prins o dezinformare virală înaintea altora), trimite-l la jurnaliști care scriu despre AI/dezinformare. O apariție în presă = credibilitate + trafic.

## Principii care contează mai mult decât orice tactică
- **Fii tu însuți exemplul de rigoare:** dacă TruthScore greșește un verdict și taci, pierzi tot. Recunoaște, corectează public (ai deja temporal drift + track record). Onestitatea E marketingul.
- **Viteza pe actualitate:** cea mai bună promovare gratuită = să fii primul care verifică o afirmație virală DE AZI. Automatizează scannerul de știri (deja există) + postează rapid.
- **Un canal făcut bine > cinci pe jumătate.** Alege 1–2 (ex. TikTok + SEO) și fii consecvent 90 de zile înainte să judeci.
- **Măsoară:** CAC (cât te costă un user), conversie free→paid, retenție la 7/30 zile. Dublează ce merge, taie ce nu.

---

## Rezumat: ordinea recomandată de execuție
1. MongoDB (§1) + chei LLM (§2) + JWT (§3) → **pornește local, testează pe laptop personal**
2. Domeniu (§4) + Hosting (§5) + Redis (§6) → **site live**
3. Google OAuth (§7) + Email (§9) + Turnstile (§10) → **login + securitate**
4. Stripe (§8) → **încasezi bani**
5. Smoke test complet (§15) → **treci Stripe pe Live**
6. AdSense (§11) + surse opționale (§12) → **monetizare + calitate**
7. Lansare + marketing (Faza 1) → **primii useri**
8. Extensie + boți (§13) + SEO/conținut → **creștere**

Succes! Codul e pregătit — de aici e despre conturi, chei și oameni. 🚀
