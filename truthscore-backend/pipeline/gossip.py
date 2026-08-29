"""
TruthScore -- Gossip / Tabloid Vertical
========================================
Celebrity & entertainment claims (breakups, pregnancies, feuds, "sources say…")
are the one topic where the mainstream fact-check pipeline is weakest: the
loudest sources are tabloids that publish rumor as fact, and the reliable
signal is thin and recency-bound. This module makes TruthScore *honest and
useful* on exactly that terrain — the request was to work with pages like
cancan.ro from every country.

Three jobs:
  1. Detect gossip/celebrity claims (multilingual) so the pipeline routes to
     live web (recency) and applies the rules below.
  2. Classify a source's domain as a low-trust `tabloid` or a reliable outlet,
     per country, so authority weighting stops treating a tabloid scoop like a
     Reuters wire.
  3. Translate the numeric verdict into gossip-native labels and — critically —
     refuse to call a rumor TRUE when the ONLY evidence is tabloids. No reliable
     corroboration ⇒ "UNCONFIRMED RUMOR", not TRUE.

All pure functions + constant sets — deterministic and unit-testable without a
live LLM.
"""
import re

# ── Per-country tabloid / gossip domains (LOW trust) ─────────────
# These publish celebrity rumor as headline fact; treated as weak evidence.
_TABLOID_DOMAINS = {
    # Romania (the motivating example: cancan.ro & co.)
    "cancan.ro", "spynews.ro", "wowbiz.ro", "viva.ro", "click.ro",
    "libertatea.ro", "okmagazine.ro", "fanatik.ro", "playtech.ro",
    "ego.ro", "a1.ro", "bzi.ro", "impact.ro", "ciao.ro",
    # United States
    "tmz.com", "pagesix.com", "radaronline.com", "perezhilton.com",
    "usmagazine.com", "intouchweekly.com", "lifeandstylemag.com",
    "okmagazine.com", "hollywoodlife.com", "justjared.com", "dlisted.com",
    "thishollywood.com", "mediatakeout.com",
    # United Kingdom
    "thesun.co.uk", "mirror.co.uk", "dailystar.co.uk", "dailymail.co.uk",
    "metro.co.uk", "ok.co.uk", "closeronline.co.uk", "heatworld.com",
    # Global / other
    "hola.com", "gala.fr", "voici.fr", "public.fr", "closermag.fr",
    "bild.de", "gente.it", "chi.it", "novella2000.it",
    "tvguia.es", "lecturas.com", "diezminutos.es",
    "elnacional.com", "tvnotas.com.mx", "quien.com",
}

# ── Reliable outlets that ALSO cover entertainment credibly ──────
# When one of these corroborates, a gossip claim can graduate past "rumor".
_RELIABLE_ENTERTAINMENT_DOMAINS = {
    # Trade / entertainment press with editorial standards
    "variety.com", "hollywoodreporter.com", "deadline.com", "ew.com",
    "billboard.com", "rollingstone.com", "thewrap.com", "vulture.com",
    # Wire services & tier-1 news
    "apnews.com", "reuters.com", "bbc.com", "bbc.co.uk", "npr.org",
    "theguardian.com", "nytimes.com", "washingtonpost.com",
    # Romania — reliable outlets per the source catalog
    "agerpres.ro", "digi24.ro", "g4media.ro", "hotnews.ro",
    "news.ro", "mediafax.ro", "europafm.ro",
}

# ── Gossip / celebrity claim keywords (multilingual) ─────────────
_GOSSIP_KEYWORDS = (
    # English relationship / celebrity signals
    "celebrity", "celeb", "actor", "actress", "singer", "rapper", "pop star",
    "influencer", "reality star", "red carpet", "paparazzi", "gossip",
    "dating", "breakup", "broke up", "split", "divorce", "divorcing",
    "cheating", "affair", "engaged", "engagement", "wedding", "married",
    "pregnant", "pregnancy", "baby bump", "expecting", "feud", "drama",
    "scandal", "rumor", "rumour", "rumored", "sources say", "insider claims",
    "spotted with", "new boyfriend", "new girlfriend", "secret relationship",
    "plastic surgery", "diss track", "clapback", "unfollowed",
    # Romanian
    "vedeta", "vedetă", "vedete", "celebritate", "celebrități", "artista",
    "artistă", "manelist", "bârfe", "barfe", "barfa", "s-au despărțit",
    "s-au despartit", "divorț", "divort", "divorțeaza", "împăcare",
    "impacare", "însărcinată", "insarcinata", "gravidă", "gravida",
    "logodnă", "logodna", "logodit", "iubit", "iubită", "iubita",
    "amant", "amantă", "amanta", "înșelat", "inselat", "scandal",
    "s-au căsătorit", "s-au casatorit", "relație secretă", "relatie secreta",
    "operații estetice", "operatii estetice",
)


def _domain_of(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return (m.group(1) if m else (url or "")).lower()


def classify_gossip_domain(url: str) -> str | None:
    """Return 'tabloid' for a known gossip outlet, 'news' for a reliable one
    that covers entertainment, or None if the domain isn't in either catalog
    (so the caller falls back to its normal classification)."""
    d = _domain_of(url)
    if not d:
        return None
    if any(t == d or d.endswith("." + t) for t in _TABLOID_DOMAINS):
        return "tabloid"
    if any(r == d or d.endswith("." + r) for r in _RELIABLE_ENTERTAINMENT_DOMAINS):
        return "news"
    return None


def is_tabloid_url(url: str) -> bool:
    return classify_gossip_domain(url) == "tabloid"


def is_gossip_claim(claim: str) -> bool:
    """Heuristic: does this claim look like celebrity/tabloid gossip?

    Fires on gossip vocabulary OR on the presence of a known tabloid domain in
    the text (someone pasting a cancan.ro headline). Deliberately broad on the
    entertainment side and conservative elsewhere — a false positive only routes
    to live web + applies the honest-rumor gate, which is safe."""
    if not claim:
        return False
    c = claim.lower()
    if any(t in c for t in _TABLOID_DOMAINS):
        return True
    return any(kw in c for kw in _GOSSIP_KEYWORDS)


# ── Verdict presentation for gossip ──────────────────────────────
# Canonical verdict (TRUE/FALSE/UNCERTAIN) stays intact for chip coloring; these
# labels are surfaced in the human-readable explanation so the semantics match
# how people talk about gossip.
_GOSSIP_LABELS = {
    "TRUE": "CONFIRMED",
    "FALSE": "DEBUNKED",
    "UNCERTAIN": "UNCONFIRMED RUMOR",
    "MISLEADING": "MISLEADING",
}


def gossip_label(verdict: str, official: bool = False) -> str:
    """Map an internal verdict to its gossip-native label."""
    v = (verdict or "UNCERTAIN").upper()
    if official and v == "TRUE":
        return "OFFICIALLY CONFIRMED"
    return _GOSSIP_LABELS.get(v, "UNCONFIRMED RUMOR")


def _has_reliable(sources) -> bool:
    for s in sources or []:
        url = getattr(s, "url", "") or ""
        # A reliable outlet, OR a genuinely authoritative source type the normal
        # classifier already trusts (factcheck/academic/news that isn't tabloid).
        if classify_gossip_domain(url) == "news":
            return True
        st = (getattr(s, "type", "") or "").lower()
        if st in ("factcheck", "academic", "news") and not is_tabloid_url(url):
            return True
    return False


def apply_gossip_gate(verdict: str, score: int, supporting, contradicting,
                      explanation: str, true_at: int, false_at: int):
    """Honest-rumor gate for gossip claims.

    Returns (verdict, score, explanation, label). If a claim would read as TRUE
    but every supporting source is a tabloid (no reliable corroboration), we
    refuse the TRUE: downgrade to UNCERTAIN, cap the score just below the TRUE
    threshold, and label it an UNCONFIRMED RUMOR. Debunks by reliable outlets are
    left intact (a credible outlet CAN kill a rumor). The gossip label is always
    prepended to the explanation so the semantics are explicit.
    """
    v = (verdict or "UNCERTAIN").upper()
    note = ""

    if v == "TRUE" and not _has_reliable(supporting):
        v = "UNCERTAIN"
        score = min(int(score), true_at - 1)
        note = ("Only tabloid/gossip outlets report this and no reliable outlet "
                "or official statement corroborates it — treated as an unconfirmed "
                "rumor rather than confirmed. ")

    label = gossip_label(v, official=(v == "TRUE" and _has_reliable(supporting)))
    prefix = f"[{label}] "
    new_expl = prefix + note + (explanation or "")
    return v, int(score), new_expl, label
