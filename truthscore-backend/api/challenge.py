"""
TruthScore -- Daily Challenge
"""
import uuid
import random
from datetime import datetime, timezone, date

HARDCODED_CHALLENGES = [
    {
        "claim": "Shakespeare wrote Hamlet",
        "verdict": "TRUE",
        "score": 98,
        "explanation": "Hamlet is universally attributed to William Shakespeare, written around 1600-1601.",
        "topic": "Literature",
    },
    {
        "claim": "Vaccines cause autism",
        "verdict": "FALSE",
        "score": 2,
        "explanation": "The original 1998 Wakefield study claiming this link was fraudulent and retracted. Extensive research across millions of children has found no connection.",
        "topic": "Health",
    },
    {
        "claim": "The Great Wall of China is visible from space",
        "verdict": "FALSE",
        "score": 8,
        "explanation": "The Great Wall is too narrow (~5–8 m wide) to be seen by the naked eye from low Earth orbit. Astronauts including Chinese astronaut Yang Liwei have confirmed this.",
        "topic": "Geography",
    },
    {
        "claim": "Water boils at 100 degrees Celsius at sea level",
        "verdict": "TRUE",
        "score": 97,
        "explanation": "At standard atmospheric pressure (1 atm / 101.325 kPa), pure water boils at exactly 100°C (212°F).",
        "topic": "Science",
    },
    {
        "claim": "Napoleon Bonaparte was unusually short",
        "verdict": "FALSE",
        "score": 20,
        "explanation": "Napoleon stood around 5 ft 7 in (170 cm), average for a Frenchman of his era. The 'short Napoleon' myth likely arose from a misunderstanding of French vs English inch measurements and British caricatures.",
        "topic": "History",
    },
]


async def seed_challenges(db):
    """Insert the 5 hardcoded challenges if the collection is empty. Called at startup."""
    try:
        count = await db.challenges.count_documents({})
        if count > 0:
            return
        now = datetime.now(timezone.utc).isoformat()
        docs = []
        for ch in HARDCODED_CHALLENGES:
            docs.append({
                "_id": str(uuid.uuid4()),
                "claim": ch["claim"],
                "verdict": ch["verdict"],
                "score": ch["score"],
                "explanation": ch["explanation"],
                "topic": ch["topic"],
                "created_at": now,
            })
        await db.challenges.insert_many(docs)
        print(f"[CHALLENGE] Seeded {len(docs)} hardcoded challenges.")
    except Exception as e:
        print(f"[CHALLENGE] seed_challenges error: {e}")


def _fallback_daily_challenge() -> dict:
    """In-memory daily pick when the DB is unavailable. Stable per calendar day.

    Uses the challenge's list index as a synthetic id (prefixed) so answer_challenge
    can resolve it without a DB round-trip.
    """
    today_seed = date.today().isoformat()
    rng = random.Random(today_seed)
    idx = rng.randrange(len(HARDCODED_CHALLENGES))
    chosen = HARDCODED_CHALLENGES[idx]
    return {
        "id": f"builtin:{idx}",
        "claim": chosen["claim"],
        "topic": chosen.get("topic", ""),
    }


async def get_daily_challenge(db) -> dict:
    """Return one challenge per day (deterministic for all users via date seed).

    The same challenge is served to everyone on the same calendar day.
    Only {id, claim, topic} is returned — verdict is withheld until answer_challenge.
    Degrades to an in-memory challenge if the DB is unavailable.
    """
    try:
        docs = await db.challenges.find({}).to_list(length=500)
        if not docs:
            await seed_challenges(db)
            docs = await db.challenges.find({}).to_list(length=500)
        if not docs:
            return _fallback_daily_challenge()
        # Use today's ISO date string as a stable seed so every user gets the same
        # challenge on the same day, but it rotates daily.
        today_seed = date.today().isoformat()
        rng = random.Random(today_seed)
        chosen = rng.choice(docs)
        return {
            "id": chosen["_id"],
            "claim": chosen["claim"],
            "topic": chosen.get("topic", ""),
        }
    except Exception as e:
        print(f"[CHALLENGE] get_daily_challenge error: {e}")
        return _fallback_daily_challenge()


async def answer_challenge(db, challenge_id: str, guess: str) -> dict:
    """Compare the user's guess to the stored verdict.

    Returns {correct, verdict, score, explanation}.
    Handles the ``builtin:N`` fallback ids used when the DB is unavailable.
    """
    def _grade(doc: dict) -> dict:
        correct = guess.upper().strip() == doc["verdict"].upper().strip()
        return {
            "correct": correct,
            "verdict": doc["verdict"],
            "score": doc["score"],
            "explanation": doc["explanation"],
        }

    if challenge_id.startswith("builtin:"):
        try:
            idx = int(challenge_id.split(":", 1)[1])
            return _grade(HARDCODED_CHALLENGES[idx])
        except (ValueError, IndexError):
            raise ValueError(f"Challenge not found: {challenge_id}")

    try:
        doc = await db.challenges.find_one({"_id": challenge_id})
        if not doc:
            raise ValueError(f"Challenge not found: {challenge_id}")
        return _grade(doc)
    except ValueError:
        raise
    except Exception as e:
        print(f"[CHALLENGE] answer_challenge error: {e}")
        raise
