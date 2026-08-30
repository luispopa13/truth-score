"""
TruthScore — Legal pages (Terms, Privacy, Refund/Withdrawal)
=============================================================
Bilingual (EN default, RO available via ?lang=ro) legal documents rendered
from a single shared shell so the CSS/nav/footer live in ONE place.

Everything company-specific (name, CUI, sediu, email) comes from
config.COMPANY, which reads env vars and falls back to visible
"[COMPLETEAZĂ …]" placeholders. After you register the SRL/PFA and set the
env vars, every page updates itself — no code change needed.

Romanian-legality building blocks included:
  • Company identity block (denumire, CUI, Reg. Com., sediu)
  • Data controller (operator de date) + GDPR legal basis (Art. 6) + ANSPDCP
  • EU 14-day right of withdrawal + digital-content waiver
  • ANPC SAL + EU ODR (SOL) dispute-resolution links
  • Governing law = România
"""
from __future__ import annotations

try:
    from config import COMPANY
except Exception:  # pragma: no cover — keep module importable standalone
    import os
    COMPANY = {
        "name": os.getenv("COMPANY_NAME", "[DENUMIRE SRL / PFA]"),
        "cui": os.getenv("COMPANY_CUI", "[CUI / CIF]"),
        "reg": os.getenv("COMPANY_REG", "[Nr. Reg. Com. J../..../20..]"),
        "address": os.getenv("COMPANY_ADDRESS", "[Sediu social, România]"),
        "email": os.getenv("COMPANY_EMAIL", "hello@truthscore.app"),
        "privacy_email": os.getenv("COMPANY_PRIVACY_EMAIL", "privacy@truthscore.app"),
        "country": os.getenv("COMPANY_COUNTRY", "România"),
        "registered": bool(os.getenv("COMPANY_CUI", "").strip()),
    }

_ANPC_SAL = "https://anpc.ro/ce-este-sal/"
_ANPC_SOL = "https://ec.europa.eu/consumers/odr"
_ANSPDCP = "https://www.dataprotection.ro"

_CSS = """
:root{--bg:#080810;--bg2:#0e0e1a;--bg3:#141424;--bg4:#1a1a2e;
--text:#f0f0fa;--text2:#7878a0;--text3:#3a3a60;
--accent:#5b4eff;--accent-h:#7060ff;--accent2:#8b5cf6;
--border:rgba(255,255,255,.05);--border2:rgba(255,255,255,.09);--border3:rgba(255,255,255,.14)}
*{margin:0;padding:0;box-sizing:border-box}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,sans-serif;
line-height:1.75;font-size:15px;-webkit-font-smoothing:antialiased}
body::before{content:'';position:fixed;inset:0;z-index:0;pointer-events:none;background:
radial-gradient(640px 320px at 12% -6%,rgba(91,78,255,.15),transparent 62%),
radial-gradient(720px 360px at 92% 8%,rgba(139,92,246,.10),transparent 62%)}
.wrap{position:relative;z-index:1;max-width:880px;margin:0 auto;padding:0 22px}
nav{position:sticky;top:0;z-index:20;background:rgba(8,8,16,.84);border-bottom:1px solid var(--border);
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
nav .wrap{display:flex;align-items:center;justify-content:space-between;height:64px;gap:12px}
.brand{display:flex;align-items:center;gap:11px;text-decoration:none;color:var(--text)}
.mark{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;color:#fff;
font-family:'Syne',sans-serif;font-weight:800;font-size:16px;
background:linear-gradient(135deg,var(--accent),var(--accent2));
box-shadow:0 4px 16px rgba(91,78,255,.35)}
.bname{font-family:'Syne',sans-serif;font-weight:700;font-size:17px;letter-spacing:-.3px}
.nav-right{display:flex;align-items:center;gap:10px}
.lang{display:flex;gap:2px;border:1px solid var(--border2);border-radius:99px;padding:3px;font-size:12px}
.lang a{padding:4px 11px;border-radius:99px;text-decoration:none;color:var(--text2);font-weight:600}
.lang a.on{background:var(--accent);color:#fff}
.pill{font-size:13px;font-weight:600;text-decoration:none;color:var(--text2);
border:1px solid var(--border2);padding:8px 16px;border-radius:99px;transition:.18s;white-space:nowrap}
.pill:hover{color:var(--text);border-color:var(--border3);background:rgba(255,255,255,.03)}
header{padding:56px 0 8px}
.badge{display:inline-flex;align-items:center;gap:7px;font-family:'JetBrains Mono',monospace;
font-size:11px;color:var(--accent-h);border:1px solid rgba(91,78,255,.35);
background:rgba(91,78,255,.08);padding:5px 12px;border-radius:99px;margin-bottom:18px}
.dot{width:6px;height:6px;border-radius:50%;background:var(--accent2)}
h1{font-family:'Syne',sans-serif;font-size:clamp(28px,5vw,40px);letter-spacing:-.8px;line-height:1.15}
.sub{color:var(--text2);max-width:680px;margin-top:12px;font-size:15.5px}
.warn{display:flex;gap:12px;align-items:flex-start;margin:26px 0 4px;padding:15px 18px;border-radius:14px;
background:rgba(234,179,8,.07);border:1px solid rgba(234,179,8,.30);color:#fde68a;font-size:13px;line-height:1.6}
.warn b{color:#fef3c7}
.idtable{margin:26px 0 4px;border:1px solid var(--border2);border-radius:16px;overflow:hidden;
background:linear-gradient(180deg,var(--bg2),var(--bg3))}
.idrow{display:flex;gap:14px;padding:12px 20px;border-bottom:1px solid var(--border);font-size:14px}
.idrow:last-child{border-bottom:none}
.idrow .k{flex:0 0 190px;color:var(--text2);font-weight:600}
.idrow .v{color:var(--text)}
.toc{display:flex;flex-wrap:wrap;gap:8px;margin:26px 0 36px}
.toc a{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text2);text-decoration:none;
border:1px solid var(--border2);border-radius:99px;padding:6px 13px;transition:.15s}
.toc a:hover{color:var(--text);border-color:var(--accent);background:rgba(91,78,255,.07)}
h2{font-family:'Syne',sans-serif;font-size:19px;letter-spacing:-.3px}
section{background:linear-gradient(180deg,var(--bg2),var(--bg3));border:1px solid var(--border2);
border-radius:18px;padding:30px 32px;margin:16px 0;scroll-margin-top:84px;transition:.18s}
section:hover{border-color:var(--border3)}
.sh{display:flex;align-items:center;gap:13px;margin-bottom:14px}
.num{width:30px;height:30px;flex-shrink:0;border-radius:9px;display:grid;place-items:center;
font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;color:var(--accent-h);
background:rgba(91,78,255,.10);border:1px solid rgba(91,78,255,.30)}
section p{color:#b8b8d4;margin-top:10px}
section p:first-of-type{margin-top:0}
ul{list-style:none;margin-top:10px}
li{position:relative;padding:5px 0 5px 24px;color:#b8b8d4}
li::before{content:'▸';position:absolute;left:2px;color:var(--accent);font-size:12px}
li b{color:var(--text);font-weight:600}
a.inline{color:var(--accent-h);text-decoration:none;border-bottom:1px solid rgba(91,78,255,.4)}
a.inline:hover{color:var(--text)}
.contact{border-radius:18px;padding:1px;margin:32px 0 26px;
background:linear-gradient(135deg,rgba(91,78,255,.55),rgba(139,92,246,.30))}
.contact-in{border-radius:17px;background:linear-gradient(180deg,var(--bg2),var(--bg3));
padding:30px 32px;display:flex;align-items:center;justify-content:space-between;gap:20px;flex-wrap:wrap}
.contact-in h3{font-family:'Syne',sans-serif;font-size:19px}
.contact-in p{color:var(--text2);font-size:13.5px;margin-top:4px}
.cta{white-space:nowrap;text-decoration:none;font-weight:600;font-size:14px;color:#fff;
background:linear-gradient(135deg,var(--accent),var(--accent2));
box-shadow:0 4px 18px rgba(91,78,255,.35);padding:12px 24px;border-radius:11px;transition:.18s}
.cta:hover{filter:brightness(1.12);transform:translateY(-1px)}
footer{border-top:1px solid var(--border);margin-top:10px}
footer .wrap{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
padding-top:26px;padding-bottom:36px;color:var(--text3);font-size:12.5px}
footer a{color:var(--text2);text-decoration:none}
footer a:hover{color:var(--text)}
@media(max-width:560px){section{padding:24px 20px}.idrow{flex-direction:column;gap:2px}.idrow .k{flex:none}}
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _shell(*, slug: str, lang: str, title: str, badge: str, subtitle: str,
           toc: str, body: str) -> str:
    """Wrap page-specific content in the shared nav / header / footer chrome."""
    other = "ro" if lang == "en" else "en"
    L = {
        "en": {"back": "← Back to app", "priv": "Privacy", "terms": "Terms",
               "refund": "Refunds", "dash": "Dashboard", "docs": "API docs"},
        "ro": {"back": "← Înapoi la aplicație", "priv": "Confidențialitate",
               "terms": "Termeni", "refund": "Retururi", "dash": "Panou",
               "docs": "Documentație API"},
    }[lang]
    en_on = "on" if lang == "en" else ""
    ro_on = "on" if lang == "ro" else ""
    warn = ""
    if not COMPANY.get("registered"):
        warn = ('<div class="warn"><span>⚠️</span><div>'
                + ("<b>Draft — company not yet registered.</b> The operator "
                   "identity below shows placeholders until the Romanian "
                   "company (SRL/PFA) is registered and its details "
                   "(name, CUI, registered office) are configured."
                   if lang == "en" else
                   "<b>Ciornă — firma nu este încă înregistrată.</b> Datele "
                   "operatorului de mai jos sunt provizorii până la "
                   "înființarea firmei (SRL/PFA) și completarea denumirii, "
                   "CUI-ului și a sediului social.")
                + "</div></div>")
    return f"""<!doctype html><html lang="{lang}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)} — TruthScore</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Syne:wght@700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>{_CSS}</style></head><body>
<nav><div class="wrap">
<a class="brand" href="/"><span class="mark">T</span><span class="bname">TruthScore</span></a>
<div class="nav-right">
<span class="lang"><a class="{en_on}" href="/{slug}?lang=en">EN</a><a class="{ro_on}" href="/{slug}?lang=ro">RO</a></span>
<a class="pill" href="/">{L['back']}</a>
</div>
</div></nav>
<div class="wrap">
<header>
<span class="badge"><span class="dot"></span>{_esc(badge)}</span>
<h1>{_esc(title)}</h1>
<p class="sub">{subtitle}</p>
</header>
{warn}
<div class="toc">{toc}</div>
{body}
</div>
<footer><div class="wrap">
<span>© 2026 {_esc(COMPANY['name'])} · TruthScore</span>
<span><a href="/?">{L['dash']}</a> · <a href="/privacy?lang={lang}">{L['priv']}</a> · <a href="/terms?lang={lang}">{L['terms']}</a> · <a href="/refund?lang={lang}">{L['refund']}</a> · <a href="/docs">{L['docs']}</a></span>
</div></footer>
</body></html>"""


def _sec(n: int, title: str, inner: str) -> str:
    return (f'<section id="s{n}"><div class="sh"><span class="num">{n:02d}</span>'
            f'<h2>{title}</h2></div>{inner}</section>')


def _id_table(lang: str) -> str:
    C = COMPANY
    k = {"en": ["Legal name", "Tax ID (CUI/CIF)", "Trade Register",
                "Registered office", "Country", "Contact"],
         "ro": ["Denumire", "Cod fiscal (CUI/CIF)", "Reg. Comerțului",
                "Sediu social", "Țară", "Contact"]}[lang]
    vals = [C["name"], C["cui"], C["reg"], C["address"], C["country"],
            f'<a class="inline" href="mailto:{C["email"]}">{C["email"]}</a>']
    rows = "".join(f'<div class="idrow"><span class="k">{kk}</span>'
                   f'<span class="v">{vv}</span></div>'
                   for kk, vv in zip(k, vals))
    return f'<div class="idtable">{rows}</div>'


# ───────────────────────── TERMS ─────────────────────────
def _terms(lang: str) -> str:
    C = COMPANY
    email = C["email"]
    if lang == "ro":
        badge = "ULTIMA ACTUALIZARE · AUGUST 2026"
        subtitle = ("Acești Termeni guvernează utilizarea site-ului, a panoului "
                    "și a extensiei de browser TruthScore (împreună, „Serviciul"
                    "”), operate de firma identificată mai jos.")
        toc = ("".join(f'<a href="#s{i}">{i} · {t}</a>' for i, t in enumerate([
            "Operator", "Acceptare", "Descrierea serviciului", "Disclaimer acuratețe",
            "Conturi & utilizare acceptabilă", "Nivel gratuit", "Abonamente & facturare",
            "Drept de retragere", "Proprietate intelectuală", "Încetare",
            "Limitarea răspunderii", "Lege aplicabilă & litigii", "Modificări", "Contact"], start=1)))
        body = (
            _sec(1, "Operatorul serviciului", _id_table("ro"))
            + _sec(2, "Acceptarea termenilor",
                   "<p>Prin utilizarea Serviciului declari că ai citit și accepți "
                   "acești Termeni și <a class='inline' href='/privacy?lang=ro'>Politica "
                   "de Confidențialitate</a>. Dacă nu ești de acord, nu utiliza Serviciul.</p>")
            + _sec(3, "Descrierea serviciului",
                   "<p>TruthScore este o platformă de verificare a faptelor asistată "
                   "de inteligență artificială. Analizează afirmații folosind surse "
                   "public disponibile și modele lingvistice, oferind un verdict "
                   "(ADEVĂRAT/FALS/INCERT) și un scor de încredere, cu citarea surselor.</p>"
                   "<p>Serviciul are caracter <b>informativ</b>.</p>")
            + _sec(4, "Disclaimer privind acuratețea",
                   "<ul>"
                   "<li>Verdictele sunt generate <b>automat</b> de AI și retrieval și "
                   "<b>pot fi greșite, incomplete sau depășite</b>.</li>"
                   "<li>Rezultatele <b>nu constituie</b> consultanță juridică, medicală, "
                   "financiară, fiscală sau profesională de niciun fel.</li>"
                   "<li>Verifică întotdeauna informațiile critice direct la sursele "
                   "autoritare. Nu garantăm acuratețea niciunui rezultat.</li>"
                   "<li>Adevărul unei afirmații se poate schimba în timp; un verdict "
                   "reflectă dovezile disponibile la momentul verificării.</li></ul>")
            + _sec(5, "Conturi și utilizare acceptabilă",
                   "<p>Ești responsabil de securitatea contului tău și de activitatea "
                   "desfășurată prin el. Îți este interzis să folosești Serviciul pentru:</p>"
                   "<ul><li>a răspândi dezinformare, a hărțui sau a defăima persoane;</li>"
                   "<li>a încălca legea aplicabilă sau drepturile terților;</li>"
                   "<li>a supraîncărca, a ocoli limitele de utilizare sau a extrage "
                   "automat date fără autorizare.</li></ul>")
            + _sec(6, "Nivelul gratuit",
                   "<p>Conturile gratuite primesc un număr limitat de verificări pe zi. "
                   "Limitele pot fi modificate cu notificare rezonabilă. Nivelul gratuit "
                   "poate afișa reclame (niciodată în extensie).</p>")
            + _sec(7, "Abonamente și facturare",
                   "<p>Abonamentele plătite sunt procesate prin <b>Stripe</b>; datele "
                   "cardului nu ajung pe serverele noastre. Prețurile includ sau exclud "
                   "TVA conform legislației aplicabile și sunt afișate la checkout. "
                   "Abonamentul se reînnoiește automat până la anulare; anularea "
                   "produce efecte la finalul perioadei de facturare curente.</p>"
                   "<p>Detalii despre anulare și rambursări: "
                   "<a class='inline' href='/refund?lang=ro'>Politica de retur</a>.</p>")
            + _sec(8, "Dreptul de retragere (consumatori UE)",
                   "<p>În calitate de consumator din UE ai, în principiu, un drept de "
                   "retragere de <b>14 zile</b> pentru serviciile achiziționate la "
                   "distanță (OUG 34/2014). Întrucât Serviciul este conținut digital "
                   "furnizat imediat, prin plasarea comenzii <b>soliciti expres</b> "
                   "începerea prestării înainte de expirarea termenului și "
                   "<b>confirmi că îți pierzi dreptul de retragere</b> odată ce "
                   "prestarea a început integral. Vezi "
                   "<a class='inline' href='/refund?lang=ro'>Politica de retur</a>.</p>")
            + _sec(9, "Proprietate intelectuală",
                   "<p>Toate drepturile asupra platformei aparțin operatorului. Păstrezi "
                   "drepturile asupra afirmațiilor trimise; prin trimitere acorzi o "
                   "licență neexclusivă de a le afișa pe paginile publice de verdict, "
                   "în scop de indexare.</p>")
            + _sec(10, "Încetare",
                   "<p>Putem suspenda sau închide conturi care încalcă acești Termeni, "
                   "abuzează de API sau desfășoară activități frauduloase.</p>")
            + _sec(11, "Limitarea răspunderii",
                   "<p>Serviciul este oferit „ca atare”. În limita maximă permisă de "
                   "lege, nu răspundem pentru daune rezultate din utilizarea Serviciului "
                   "sau din bazarea pe verdicte. Nimic din acești Termeni nu limitează "
                   "drepturile imperative ale consumatorului.</p>")
            + _sec(12, "Legea aplicabilă și soluționarea litigiilor",
                   f"<p>Acești Termeni sunt guvernați de legea din {C['country']}. "
                   "Litigiile se soluționează de instanțele competente de la sediul "
                   "operatorului, fără a afecta drepturile imperative ale consumatorului.</p>"
                   "<ul>"
                   f"<li><b>ANPC — SAL:</b> soluționare alternativă a litigiilor: "
                   f"<a class='inline' href='{_ANPC_SAL}' target='_blank' rel='noopener'>anpc.ro/ce-este-sal</a></li>"
                   f"<li><b>Platforma SOL (UE):</b> soluționare online a litigiilor: "
                   f"<a class='inline' href='{_ANPC_SOL}' target='_blank' rel='noopener'>ec.europa.eu/consumers/odr</a></li>"
                   "</ul>")
            + _sec(13, "Modificări",
                   "<p>Putem actualiza acești Termeni. Continuarea utilizării după "
                   "modificări reprezintă acceptarea lor.</p>")
            + _sec(14, "Contact",
                   f"<p>Întrebări: <a class='inline' href='mailto:{email}'>{email}</a></p>")
        )
    else:
        badge = "LAST UPDATED · AUGUST 2026"
        subtitle = ("These Terms govern your use of the TruthScore website, "
                    "dashboard and browser extension (together, the “Service”), "
                    "operated by the company identified below.")
        toc = ("".join(f'<a href="#s{i}">{i} · {t}</a>' for i, t in enumerate([
            "Operator", "Acceptance", "Service", "Accuracy disclaimer",
            "Accounts & use", "Free tier", "Subscriptions & billing",
            "Right of withdrawal", "IP", "Termination", "Liability",
            "Governing law & disputes", "Changes", "Contact"], start=1)))
        body = (
            _sec(1, "Service operator", _id_table("en"))
            + _sec(2, "Acceptance",
                   "<p>By using the Service you confirm you have read and accept these "
                   "Terms and the <a class='inline' href='/privacy?lang=en'>Privacy "
                   "Policy</a>. If you disagree, do not use the Service.</p>")
            + _sec(3, "Description of service",
                   "<p>TruthScore is an AI-assisted fact-checking platform. It analyzes "
                   "claims using publicly available sources and large language models, "
                   "returning a verdict (TRUE/FALSE/UNCERTAIN) and a confidence score "
                   "with cited sources.</p><p>The Service is <b>informational</b>.</p>")
            + _sec(4, "Accuracy disclaimer",
                   "<ul>"
                   "<li>Verdicts are generated <b>automatically</b> by AI and retrieval "
                   "and <b>may be wrong, incomplete or out of date</b>.</li>"
                   "<li>Results are <b>not</b> legal, medical, financial, tax or any "
                   "other professional advice.</li>"
                   "<li>Always verify critical information with authoritative sources. "
                   "We do not guarantee the accuracy of any result.</li>"
                   "<li>The truth of a claim can change over time; a verdict reflects "
                   "the evidence available at the moment of checking.</li></ul>")
            + _sec(5, "Accounts and acceptable use",
                   "<p>You are responsible for your account's security and activity. "
                   "You must not use the Service to:</p>"
                   "<ul><li>spread misinformation, harass or defame people;</li>"
                   "<li>break applicable law or infringe third-party rights;</li>"
                   "<li>overload it, evade usage limits or scrape data without "
                   "authorization.</li></ul>")
            + _sec(6, "Free tier",
                   "<p>Free accounts receive a limited number of checks per day. Limits "
                   "may change with reasonable notice. The free tier may show ads "
                   "(never inside the extension).</p>")
            + _sec(7, "Subscriptions and billing",
                   "<p>Paid subscriptions are processed by <b>Stripe</b>; card details "
                   "never reach our servers. Prices are shown at checkout inclusive or "
                   "exclusive of VAT as applicable. Subscriptions auto-renew until "
                   "cancelled; cancellation takes effect at the end of the current "
                   "billing period.</p><p>Cancellation and refund details: "
                   "<a class='inline' href='/refund?lang=en'>Refund Policy</a>.</p>")
            + _sec(8, "Right of withdrawal (EU consumers)",
                   "<p>As an EU consumer you generally have a <b>14-day</b> right of "
                   "withdrawal for distance purchases. Because the Service is digital "
                   "content supplied immediately, by placing an order you <b>expressly "
                   "request</b> that performance begin before the period ends and "
                   "<b>acknowledge you lose the right of withdrawal</b> once performance "
                   "has fully started. See the "
                   "<a class='inline' href='/refund?lang=en'>Refund Policy</a>.</p>")
            + _sec(9, "Intellectual property",
                   "<p>All rights in the platform belong to the operator. You keep "
                   "rights to the claims you submit; by submitting one you grant a "
                   "non-exclusive licence to display it on public verdict pages for "
                   "indexing.</p>")
            + _sec(10, "Termination",
                   "<p>We may suspend or close accounts that violate these Terms, abuse "
                   "the API or engage in fraud.</p>")
            + _sec(11, "Limitation of liability",
                   "<p>The Service is provided “as is”. To the maximum extent permitted "
                   "by law, we are not liable for damages arising from use of the "
                   "Service or reliance on verdicts. Nothing here limits your mandatory "
                   "consumer rights.</p>")
            + _sec(12, "Governing law and dispute resolution",
                   f"<p>These Terms are governed by the law of {C['country']}. Disputes "
                   "fall to the competent courts at the operator's registered office, "
                   "without prejudice to your mandatory consumer rights.</p>"
                   "<ul>"
                   f"<li><b>ANPC — SAL</b> (alternative dispute resolution): "
                   f"<a class='inline' href='{_ANPC_SAL}' target='_blank' rel='noopener'>anpc.ro/ce-este-sal</a></li>"
                   f"<li><b>EU ODR platform (SOL):</b> "
                   f"<a class='inline' href='{_ANPC_SOL}' target='_blank' rel='noopener'>ec.europa.eu/consumers/odr</a></li>"
                   "</ul>")
            + _sec(13, "Changes",
                   "<p>We may update these Terms. Continued use after changes means "
                   "acceptance.</p>")
            + _sec(14, "Contact",
                   f"<p>Questions: <a class='inline' href='mailto:{email}'>{email}</a></p>")
        )
    return _shell(slug="terms", lang=lang, title=("Termeni și Condiții" if lang == "ro" else "Terms of Service"),
                  badge=badge, subtitle=subtitle, toc=toc, body=body)


# ───────────────────────── PRIVACY ─────────────────────────
def _privacy(lang: str) -> str:
    C = COMPANY
    pemail = C["privacy_email"]
    if lang == "ro":
        badge = "ULTIMA ACTUALIZARE · AUGUST 2026"
        subtitle = ("Această politică acoperă site-ul, panoul și extensia TruthScore. "
                    "Colectăm minimul de date necesare pentru a verifica afirmații, a "
                    "menține Serviciul fără abuzuri și a respecta legea. Fără vânzare "
                    "de date, fără pixeli de tracking în extensie.")
        toc = ("".join(f'<a href="#s{i}">{i} · {t}</a>' for i, t in enumerate([
            "Operator de date", "Ce colectăm", "Temeiul legal", "Împuterniciți & terți",
            "Transferuri internaționale", "Păstrare", "Ce nu facem niciodată",
            "Drepturile tale", "Contact"], start=1)))
        body = (
            _sec(1, "Operatorul de date", _id_table("ro")
                 + "<p>Operatorul răspunde de prelucrarea datelor tale conform "
                   "Regulamentului (UE) 2016/679 (GDPR) și Legii nr. 190/2018.</p>")
            + _sec(2, "Ce colectăm",
                   "<ul>"
                   "<li><b>Date de cont:</b> adresă de email, parolă (stocată doar ca "
                   "hash bcrypt), nume afișat (opțional).</li>"
                   "<li><b>Input de verificare:</b> afirmațiile/paragrafele trimise, "
                   "păstrate pentru calitatea verdictelor, statistici de calibrare și "
                   "prevenirea abuzului.</li>"
                   "<li><b>Feedback:</b> semnalele opționale de tip like/dislike.</li>"
                   "<li><b>Contoare de utilizare:</b> numărul de verificări zilnice și "
                   "identificatori de rate-limit (adresă IP).</li>"
                   "<li><b>Semnal anti-abuz:</b> o amprentă de browser (hash) folosită "
                   "exclusiv pentru limita zilnică a nivelului gratuit — nu pentru "
                   "publicitate sau tracking cross-site.</li></ul>")
            + _sec(3, "Temeiul legal al prelucrării (Art. 6 GDPR)",
                   "<ul>"
                   "<li><b>Executarea contractului</b> (art. 6(1)(b)): furnizarea "
                   "Serviciului și a contului.</li>"
                   "<li><b>Interes legitim</b> (art. 6(1)(f)): prevenirea abuzului, "
                   "securitate, îmbunătățirea calității verdictelor.</li>"
                   "<li><b>Consimțământ</b> (art. 6(1)(a)): cookie-uri non-esențiale și "
                   "publicitate, acordat prin bannerul de cookies.</li>"
                   "<li><b>Obligație legală</b> (art. 6(1)(c)): facturare și "
                   "raportări fiscale.</li></ul>")
            + _sec(4, "Împuterniciți și terți implicați",
                   "<ul>"
                   "<li>Furnizorii de modele lingvistice și de căutare primesc textul "
                   "afirmației <b>doar pentru a produce un verdict</b>.</li>"
                   "<li>MongoDB Atlas găzduiește baza de date; Upstash poate găzdui "
                   "cache-ul.</li>"
                   "<li>Stripe procesează plățile — <b>datele cardului nu ajung la "
                   "noi</b>.</li>"
                   "<li>Panoul poate afișa Google AdSense vizitatorilor gratuiti "
                   "(niciodată în extensie).</li></ul>")
            + _sec(5, "Transferuri internaționale",
                   "<p>Unii furnizori pot prelucra date în afara SEE. În aceste cazuri "
                   "ne bazăm pe decizii de adecvare sau pe Clauzele Contractuale "
                   "Standard ale Comisiei Europene.</p>")
            + _sec(6, "Perioada de păstrare",
                   "<ul>"
                   "<li><b>Cont:</b> pe durata contului, șters în 30 de zile de la cerere.</li>"
                   "<li><b>Input de verificare:</b> păstrat pentru calitate/abuz, apoi "
                   "anonimizat sau șters.</li>"
                   "<li><b>Date de facturare:</b> pe durata cerută de legea fiscală.</li></ul>")
            + _sec(7, "Ce nu facem niciodată",
                   "<ul>"
                   "<li>Nu <b>vindem și nu închiriem</b> date personale — nimănui.</li>"
                   "<li>Extensia nu injectează <b>reclame terțe</b> în paginile vizitate.</li>"
                   "<li>Parolele se stochează doar ca <b>hash bcrypt</b>, niciodată "
                   "recuperabile.</li></ul>")
            + _sec(8, "Drepturile tale (GDPR)",
                   "<p>Ai dreptul de acces, rectificare, ștergere, restricționare, "
                   "portabilitate și opoziție. Cererile sunt onorate în 30 de zile.</p>"
                   "<ul>"
                   f"<li>Trimite cererea la <a class='inline' href='mailto:{pemail}'>{pemail}</a>.</li>"
                   "<li>Poți renunța la publicitate trecând la orice plan plătit.</li>"
                   f"<li>Ai dreptul să depui plângere la <b>ANSPDCP</b>: "
                   f"<a class='inline' href='{_ANSPDCP}' target='_blank' rel='noopener'>dataprotection.ro</a>.</li>"
                   "</ul>")
        )
        contact_h, contact_p, updated = ("Întrebări despre confidențialitate?",
                                         "Răspundem fiecărei cereri în 30 de zile.",
                                         "")
    else:
        badge = "LAST UPDATED · AUGUST 2026"
        subtitle = ("This policy covers the TruthScore website, dashboard and browser "
                    "extension. We collect the minimum data needed to verify claims, "
                    "keep the Service abuse-free and comply with the law. No data "
                    "selling, no tracking pixels inside the extension.")
        toc = ("".join(f'<a href="#s{i}">{i} · {t}</a>' for i, t in enumerate([
            "Data controller", "What we collect", "Legal basis", "Processors & third parties",
            "International transfers", "Retention", "What we never do",
            "Your rights", "Contact"], start=1)))
        body = (
            _sec(1, "Data controller", _id_table("en")
                 + "<p>The operator is responsible for processing your data under "
                   "Regulation (EU) 2016/679 (GDPR) and Romanian Law 190/2018.</p>")
            + _sec(2, "What we collect",
                   "<ul>"
                   "<li><b>Account data:</b> email, password (stored only as a bcrypt "
                   "hash), display name (optional).</li>"
                   "<li><b>Verification inputs:</b> the claims/paragraphs you submit, "
                   "kept to improve verdict quality, build calibration statistics and "
                   "prevent abuse.</li>"
                   "<li><b>Feedback:</b> optional thumbs up/down signals.</li>"
                   "<li><b>Usage counters:</b> daily check counts and rate-limit "
                   "identifiers (IP address).</li>"
                   "<li><b>Anti-abuse signal:</b> a browser fingerprint (hash) used "
                   "solely to enforce the free-tier daily limit — not for advertising "
                   "or cross-site tracking.</li></ul>")
            + _sec(3, "Legal basis for processing (GDPR Art. 6)",
                   "<ul>"
                   "<li><b>Contract</b> (Art. 6(1)(b)): providing the Service and your "
                   "account.</li>"
                   "<li><b>Legitimate interest</b> (Art. 6(1)(f)): abuse prevention, "
                   "security, verdict-quality improvement.</li>"
                   "<li><b>Consent</b> (Art. 6(1)(a)): non-essential cookies and "
                   "advertising, given via the cookie banner.</li>"
                   "<li><b>Legal obligation</b> (Art. 6(1)(c)): billing and tax "
                   "records.</li></ul>")
            + _sec(4, "Processors and third parties",
                   "<ul>"
                   "<li>Language-model and search providers receive the claim text "
                   "<b>solely to produce a verdict</b>.</li>"
                   "<li>MongoDB Atlas hosts the database; Upstash may host the cache.</li>"
                   "<li>Stripe processes payments — <b>card details never reach our "
                   "servers</b>.</li>"
                   "<li>The dashboard may show Google AdSense to free-tier visitors "
                   "(never in the extension).</li></ul>")
            + _sec(5, "International transfers",
                   "<p>Some providers may process data outside the EEA. Where they do, "
                   "we rely on adequacy decisions or the European Commission's Standard "
                   "Contractual Clauses.</p>")
            + _sec(6, "Retention",
                   "<ul>"
                   "<li><b>Account:</b> for the life of the account, deleted within 30 "
                   "days of request.</li>"
                   "<li><b>Verification inputs:</b> kept for quality/abuse, then "
                   "anonymized or deleted.</li>"
                   "<li><b>Billing data:</b> for the period required by tax law.</li></ul>")
            + _sec(7, "What we never do",
                   "<ul>"
                   "<li>We never <b>sell or rent</b> personal data — to anyone.</li>"
                   "<li>The extension never injects <b>third-party ads</b> into pages "
                   "you visit.</li>"
                   "<li>Passwords are stored only as <b>bcrypt hashes</b> and are never "
                   "recoverable.</li></ul>")
            + _sec(8, "Your rights (GDPR)",
                   "<p>You have the rights of access, rectification, erasure, "
                   "restriction, portability and objection. Requests are honored within "
                   "30 days.</p>"
                   "<ul>"
                   f"<li>Send requests to <a class='inline' href='mailto:{pemail}'>{pemail}</a>.</li>"
                   "<li>Opt out of advertising by upgrading to any paid plan.</li>"
                   f"<li>You may lodge a complaint with the Romanian DPA (<b>ANSPDCP</b>): "
                   f"<a class='inline' href='{_ANSPDCP}' target='_blank' rel='noopener'>dataprotection.ro</a>.</li>"
                   "</ul>")
        )
        contact_h, contact_p = ("Privacy questions?", "We answer every request within 30 days.")

    contact = (f'<div class="contact" id="s9"><div class="contact-in"><div>'
               f'<h3>{contact_h}</h3><p>{contact_p}</p></div>'
               f'<a class="cta" href="mailto:{pemail}">{pemail}</a></div></div>')
    return _shell(slug="privacy", lang=lang,
                  title=("Politica de Confidențialitate" if lang == "ro" else "Privacy Policy"),
                  badge=badge, subtitle=subtitle, toc=toc, body=body + contact)


# ───────────────────────── REFUND ─────────────────────────
def _refund(lang: str) -> str:
    C = COMPANY
    email = C["email"]
    if lang == "ro":
        badge = "ULTIMA ACTUALIZARE · AUGUST 2026"
        subtitle = ("Cum funcționează anularea abonamentului, dreptul de retragere și "
                    "rambursările pentru TruthScore.")
        toc = ("".join(f'<a href="#s{i}">{i} · {t}</a>' for i, t in enumerate([
            "Drept de retragere", "Anularea abonamentului", "Eligibilitate rambursare",
            "Cum ceri o rambursare", "Litigii", "Contact"], start=1)))
        body = (
            _sec(1, "Dreptul de retragere (14 zile)",
                 "<p>Consumatorii UE au un drept de retragere de 14 zile pentru "
                 "achiziții la distanță (OUG 34/2014). TruthScore este <b>conținut "
                 "digital</b> furnizat imediat: la achiziție soliciti expres începerea "
                 "prestării și confirmi că <b>îți pierzi dreptul de retragere</b> odată "
                 "ce serviciul a fost prestat integral. Dacă nu ai folosit deloc "
                 "serviciul plătit, contactează-ne pentru rambursare integrală.</p>")
            + _sec(2, "Anularea abonamentului",
                   "<p>Poți anula oricând din <b>portalul de facturare</b> (butonul "
                   "„Manage billing” din cont) sau scriindu-ne. Anularea oprește "
                   "reînnoirile viitoare; păstrezi accesul plătit până la finalul "
                   "perioadei deja achitate.</p>")
            + _sec(3, "Eligibilitate pentru rambursare",
                   "<ul>"
                   "<li>Rambursare integrală dacă serviciul plătit <b>nu a fost "
                   "utilizat</b> și ceri în termenul legal.</li>"
                   "<li>Nu oferim rambursări pentru perioade parțiale deja consumate, "
                   "cu excepția cazurilor cerute de lege.</li>"
                   "<li>Erori de facturare (dublă debitare etc.) se rambursează integral.</li></ul>")
            + _sec(4, "Cum ceri o rambursare",
                   f"<p>Scrie la <a class='inline' href='mailto:{email}'>{email}</a> cu "
                   "emailul contului și motivul. Răspundem în 5 zile lucrătoare; "
                   "rambursările aprobate se fac prin Stripe, pe aceeași metodă de plată.</p>")
            + _sec(5, "Soluționarea litigiilor",
                   "<ul>"
                   f"<li><b>ANPC — SAL:</b> <a class='inline' href='{_ANPC_SAL}' target='_blank' rel='noopener'>anpc.ro/ce-este-sal</a></li>"
                   f"<li><b>Platforma SOL (UE):</b> <a class='inline' href='{_ANPC_SOL}' target='_blank' rel='noopener'>ec.europa.eu/consumers/odr</a></li>"
                   "</ul>")
            + _sec(6, "Contact",
                   f"<p><a class='inline' href='mailto:{email}'>{email}</a></p>")
        )
        title = "Politica de Retur"
    else:
        badge = "LAST UPDATED · AUGUST 2026"
        subtitle = ("How subscription cancellation, the right of withdrawal and refunds "
                    "work for TruthScore.")
        toc = ("".join(f'<a href="#s{i}">{i} · {t}</a>' for i, t in enumerate([
            "Right of withdrawal", "Cancelling", "Refund eligibility",
            "How to request", "Disputes", "Contact"], start=1)))
        body = (
            _sec(1, "Right of withdrawal (14 days)",
                 "<p>EU consumers have a 14-day right of withdrawal for distance "
                 "purchases. TruthScore is <b>digital content</b> supplied immediately: "
                 "at purchase you expressly request performance to begin and acknowledge "
                 "you <b>lose the right of withdrawal</b> once the service has been fully "
                 "performed. If you have not used the paid service at all, contact us for "
                 "a full refund.</p>")
            + _sec(2, "Cancelling your subscription",
                   "<p>You can cancel anytime from the <b>billing portal</b> (the "
                   "“Manage billing” button in your account) or by writing to us. "
                   "Cancellation stops future renewals; you keep paid access until the "
                   "end of the period already paid for.</p>")
            + _sec(3, "Refund eligibility",
                   "<ul>"
                   "<li>Full refund if the paid service was <b>not used</b> and you ask "
                   "within the legal window.</li>"
                   "<li>No refunds for partial periods already consumed, except where "
                   "required by law.</li>"
                   "<li>Billing errors (double charges, etc.) are refunded in full.</li></ul>")
            + _sec(4, "How to request a refund",
                   f"<p>Email <a class='inline' href='mailto:{email}'>{email}</a> with "
                   "your account email and the reason. We reply within 5 business days; "
                   "approved refunds are issued via Stripe to the original payment "
                   "method.</p>")
            + _sec(5, "Dispute resolution",
                   "<ul>"
                   f"<li><b>ANPC — SAL:</b> <a class='inline' href='{_ANPC_SAL}' target='_blank' rel='noopener'>anpc.ro/ce-este-sal</a></li>"
                   f"<li><b>EU ODR platform (SOL):</b> <a class='inline' href='{_ANPC_SOL}' target='_blank' rel='noopener'>ec.europa.eu/consumers/odr</a></li>"
                   "</ul>")
            + _sec(6, "Contact",
                   f"<p><a class='inline' href='mailto:{email}'>{email}</a></p>")
        )
        title = "Refund & Cancellation Policy"
    return _shell(slug="refund", lang=lang, title=title, badge=badge,
                  subtitle=subtitle, toc=toc, body=body)


def _norm_lang(lang: str | None) -> str:
    return "ro" if (lang or "").strip().lower().startswith("ro") else "en"


def render_legal(slug: str, lang: str | None = None) -> str:
    """Render a legal page. slug ∈ {terms, privacy, refund}; lang ∈ {en, ro}."""
    lang = _norm_lang(lang)
    return {"terms": _terms, "privacy": _privacy, "refund": _refund}[slug](lang)
