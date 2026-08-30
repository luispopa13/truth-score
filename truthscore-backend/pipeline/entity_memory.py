"""
TruthScore Entity Memory Graph
================================
Builds persistent profiles for named entities (people, companies, countries,
organisations) extracted from verified claims.

Each verified claim updates the entity profiles of all mentioned entities.
Over time, the system accumulates patterns:
  "Elon Musk: 47 claims · 19 FALSE · 8 misleading · pattern: exaggerates revenue figures 3-5x"

MongoDB collection: `entity_profiles`
Schema per document:
  {
    "_id": "elon musk",           # normalized lowercase name
    "name": "Elon Musk",          # display name
    "type": "person",             # person | company | country | org | other
    "claim_count": 47,
    "true_count": 20,
    "false_count": 19,
    "uncertain_count": 8,
    "misleading_count": 8,
    "accuracy_pct": 43,           # true / (true+false) * 100, ignoring uncertain
    "recent_verdicts": [          # last 10 verdicts, newest first
      {"claim": "...", "verdict": "FALSE", "score": 12, "date": "2026-01-15"}
    ],
    "pattern_notes": "...",       # LLM-generated pattern summary, updated every 10 claims
    "last_updated": "2026-01-15T12:00:00Z"
  }
"""
from __future__ import annotations
import re as _re
from datetime import datetime, timezone


# ── Entity extraction (lightweight, no NER model) ────────────────

_EXTRACT_PROMPT = """\
Extract named entities from this claim. Return ONLY a JSON list of objects.
Each object: {{"name": "<entity display name>", "type": "person|company|country|org|other"}}
Extract: people, companies, countries, organisations. Skip generic nouns.
Limit: max 5 entities.

CLAIM: {claim}

JSON only (no markdown, no explanation):"""


async def extract_entities(claim: str) -> list[dict]:
    """Extract named entities from a claim using LLM."""
    import json as _json
    try:
        from pipeline.reasoning import call_llm_raw
        raw = await call_llm_raw(
            _EXTRACT_PROMPT.format(claim=claim[:400]),
            max_tokens=200,
            model="groq",
        )
        # Find JSON array
        m = _re.search(r"\[.*\]", raw, _re.DOTALL)
        if m:
            entities = _json.loads(m.group(0))
            return [
                {"name": e["name"], "type": e.get("type", "other")}
                for e in entities
                if isinstance(e, dict) and e.get("name")
            ][:5]
    except Exception as e:
        print(f"[entity_memory] extract_entities error: {e}")
    return []


def _normalize_name(name: str) -> str:
    """Normalize entity name for use as MongoDB _id."""
    return _re.sub(r"\s+", " ", name.strip().lower())


# ── Profile update ────────────────────────────────────────────────

async def update_entity_profiles(
    db,
    claim: str,
    verdict: str,
    score: int,
    is_misleading: bool = False,
) -> list[str]:
    """
    Extract entities from claim and update their profiles in MongoDB.
    Returns list of entity names that were updated.
    """
    entities = await extract_entities(claim)
    if not entities:
        return []

    col = db["entity_profiles"]
    now_iso = datetime.now(timezone.utc).isoformat()
    verdict_upper = (verdict or "UNCERTAIN").upper()
    updated = []

    for ent in entities:
        name = ent["name"]
        ent_type = ent.get("type", "other")
        eid = _normalize_name(name)

        verdict_entry = {
            "claim": claim[:200],
            "verdict": verdict_upper,
            "score": score,
            "date": now_iso[:10],
        }

        # Increment counters
        inc = {"claim_count": 1}
        if verdict_upper == "TRUE":
            inc["true_count"] = 1
        elif verdict_upper == "FALSE":
            inc["false_count"] = 1
        else:
            inc["uncertain_count"] = 1
        if is_misleading:
            inc["misleading_count"] = 1

        try:
            await col.update_one(
                {"_id": eid},
                {
                    "$inc": inc,
                    "$set": {"name": name, "type": ent_type, "last_updated": now_iso},
                    "$push": {
                        "recent_verdicts": {
                            "$each": [verdict_entry],
                            "$position": 0,
                            "$slice": 10,
                        }
                    },
                    # NOTE: counters (claim_count/true_count/…) are intentionally
                    # NOT in $setOnInsert. MongoDB rejects any field that appears in
                    # BOTH $inc and $setOnInsert ("would create a conflict"), which
                    # made every write throw and left entity_profiles permanently
                    # empty. $inc treats a missing field as 0 and initializes it, so
                    # the counters seed correctly on insert without $setOnInsert.
                    "$setOnInsert": {
                        "accuracy_pct": 0, "pattern_notes": "",
                        "created_at": now_iso,
                    },
                },
                upsert=True,
            )

            # Recompute accuracy_pct
            doc = await col.find_one({"_id": eid})
            if doc:
                t = doc.get("true_count", 0)
                f = doc.get("false_count", 0)
                acc = int(t / (t + f) * 100) if (t + f) > 0 else 50
                update_fields: dict = {"accuracy_pct": acc}

                # Every 10 claims, regenerate pattern notes
                if doc.get("claim_count", 0) % 10 == 0 and doc.get("claim_count", 0) > 0:
                    notes = await _generate_pattern_notes(name, doc)
                    if notes:
                        update_fields["pattern_notes"] = notes

                await col.update_one({"_id": eid}, {"$set": update_fields})

            updated.append(name)
        except Exception as e:
            print(f"[entity_memory] update error for {name}: {e}")

    return updated


async def _generate_pattern_notes(entity_name: str, profile: dict) -> str:
    """Generate a 1-sentence pattern summary for an entity from its profile."""
    try:
        from pipeline.reasoning import call_llm_raw
        recent = profile.get("recent_verdicts", [])[:5]
        verdicts_str = "; ".join(
            f"{v['verdict']} ({v['claim'][:60]})" for v in recent
        )
        prompt = (
            f"Based on these fact-checking results for '{entity_name}':\n{verdicts_str}\n\n"
            f"Write ONE short sentence (max 20 words) describing any pattern in this entity's "
            f"claims (e.g. 'tends to exaggerate revenue figures' or 'frequently makes accurate "
            f"statements about X but misleads about Y'). If no clear pattern, say 'No consistent pattern detected.'"
        )
        raw = await call_llm_raw(prompt, max_tokens=60, model="groq")
        return raw.strip()[:200]
    except Exception:
        return ""


# ── Profile retrieval ─────────────────────────────────────────────

async def get_entity_profile(db, entity_name: str) -> dict | None:
    """Get profile for a named entity. Returns None if not found."""
    try:
        col = db["entity_profiles"]
        eid = _normalize_name(entity_name)
        return await col.find_one({"_id": eid})
    except Exception as e:
        print(f"[entity_memory] get_profile error: {e}")
        return None


async def get_claim_entity_profiles(db, claim: str) -> list[dict]:
    """
    Extract entities from a claim and return their profiles.
    Used to enrich verify results before returning to client.
    """
    entities = await extract_entities(claim)
    if not entities:
        return []
    profiles = []
    for ent in entities:
        profile = await get_entity_profile(db, ent["name"])
        if profile and profile.get("claim_count", 0) >= 3:
            profiles.append(_public_profile(profile))
    return profiles


def _public_profile(profile: dict) -> dict:
    """Shape a stored profile into the fields the client renders."""
    return {
        "name": profile["name"],
        "type": profile.get("type", "other"),
        "claim_count": profile.get("claim_count", 0),
        "accuracy_pct": profile.get("accuracy_pct", 50),
        "misleading_count": profile.get("misleading_count", 0),
        "pattern_notes": profile.get("pattern_notes", ""),
        "recent_verdicts": profile.get("recent_verdicts", [])[:3],
    }


# Candidate named-entity spans: runs of Capitalized words (incl. diacritics),
# e.g. "Elon Musk", "European Union". Cheap regex — NO LLM — for the hot-path
# READ enrichment, so surfacing an entity's history costs a Mongo lookup, not a
# model call. Names that never accumulated a profile simply don't match.
_CAP_SPAN = _re.compile(r"\b([A-ZĂÂÎȘȚ][\wăâîșț]+(?:\s+[A-ZĂÂÎȘȚ][\wăâîșț]+){0,3})")
_CAP_STOP = {"the", "a", "an", "this", "that", "is", "are", "was", "were"}


async def get_profiles_for_text_cheap(db, claim: str, min_claims: int = 3) -> list[dict]:
    """LLM-free profile surfacing: pull Capitalized-name candidates from the
    claim with a regex and look up any that already have an accumulated profile.
    Safe to call on every verify — a few indexed _id lookups, no model cost.
    """
    if db is None or not claim:
        return []
    try:
        seen: set[str] = set()
        candidates: list[str] = []
        for m in _CAP_SPAN.finditer(claim):
            span = m.group(1).strip()
            if span.lower() in _CAP_STOP:
                continue
            eid = _normalize_name(span)
            if eid and eid not in seen:
                seen.add(eid)
                candidates.append(eid)
        if not candidates:
            return []
        col = db["entity_profiles"]
        cursor = col.find({"_id": {"$in": candidates[:8]},
                           "claim_count": {"$gte": min_claims}})
        docs = await cursor.to_list(length=8)
        return [_public_profile(d) for d in docs]
    except Exception as e:
        print(f"[entity_memory] cheap lookup error: {e}")
        return []
