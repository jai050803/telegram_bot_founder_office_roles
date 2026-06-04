import os
import re
import logging
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client

logger = logging.getLogger(__name__)

# Initialize Supabase client
url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key) if url and key else None

SYNONYM_MAP = {
    "founder's office": "founder_office",
    "founders office": "founder_office",
    "founder office": "founder_office",
    "fo role": "founder_office",
    "fo intern": "founder_office",
    "chief of staff": "chief_of_staff",
    "cos intern": "chief_of_staff",
    "ceo office": "ceo_office",
    "office of the ceo": "ceo_office",
    "shadow the ceo": "ceo_office",
    "work with ceo": "ceo_office",
    "work with founder": "founder_office",
    "report to founder": "founder_office",
    "report to ceo": "ceo_office",
    "founding team": "founding_team",
    "founding member": "founding_team",
    "right hand": "founder_office",
    "special projects": "special_projects",
    "strategy intern": "strategy_intern",
    "growth intern": "growth_intern",
    "startup intern": "startup_intern",
    "0 to 1": "zero_to_one",
    "zero to one": "zero_to_one",
    "0-1": "zero_to_one",
    "generalist": "generalist",
    "executive assistant": "exec_assistant",
    "ea intern": "exec_assistant",
}

STOPWORDS = {
    "find", "me", "show", "looking", "for", "i", "want", "need",
    "get", "search", "the", "a", "an", "in", "at", "to", "with",
    "some", "any", "please", "can", "you", "help", "roles",
    "opportunities", "openings", "jobs", "positions",
}

# In-memory pagination cache: user_id -> {"query": str, "jobs": list, "timestamp": datetime}
_pagination_cache: dict[int, dict] = {}


def normalize_query(raw_query: str, location: str = "") -> str:
    """Normalize a search query into a canonical cache key.

    Lowercases, strips punctuation, removes stopwords, replaces known synonym
    phrases with canonical tokens, sorts alphabetically, and appends location.
    """
    text = raw_query.lower()
    # Remove all punctuation except hyphens
    text = re.sub(r"[^\w\s-]", "", text)
    words = text.split()
    # Remove stopwords
    words = [w for w in words if w not in STOPWORDS]

    # Replace consecutive 2-word and 3-word synonym phrases
    result_tokens: list[str] = []
    i = 0
    while i < len(words):
        matched = False
        # Try 3-word phrase first
        if i + 2 < len(words):
            trigram = f"{words[i]} {words[i + 1]} {words[i + 2]}"
            if trigram in SYNONYM_MAP:
                result_tokens.append(SYNONYM_MAP[trigram])
                i += 3
                matched = True
        # Try 2-word phrase
        if not matched and i + 1 < len(words):
            bigram = f"{words[i]} {words[i + 1]}"
            if bigram in SYNONYM_MAP:
                result_tokens.append(SYNONYM_MAP[bigram])
                i += 2
                matched = True
        # Single word — check single-word synonyms too
        if not matched:
            word = words[i]
            if word in SYNONYM_MAP:
                result_tokens.append(SYNONYM_MAP[word])
            else:
                result_tokens.append(word)
            i += 1

    result_tokens.sort()
    cache_key = "|".join(result_tokens)

    if location and location.strip():
        cache_key += f"|{location.strip().lower()}"

    return cache_key


def store_jobs(jobs: list[dict], query_used: str, normalized_key: str = "") -> None:
    """Insert job listings into Supabase `job_listings` table, ignoring duplicates.

    Uses upsert on `content_hash`. Each call is individually wrapped so a single
    bad record never prevents the rest from being stored.
    """
    if supabase is None:
        return

    for job in jobs:
        try:
            row = {
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "source": job.get("source", ""),
                "url": job.get("url", ""),
                "description": job.get("description", ""),
                "stipend": job.get("stipend", ""),
                "duration": job.get("duration", ""),
                "fps": job.get("fps", 0),
                "fps_reasoning": job.get("fps_reasoning", ""),
                "is_genuine_fo_role": job.get("is_genuine_fo_role", False),
                "role_summary": job.get("role_summary", ""),
                "red_flags": job.get("red_flags", []),
                "skills_needed": job.get("skills_needed", []),
                "content_hash": job.get("content_hash", ""),
                "query_used": query_used,
                "normalized_query": normalized_key,
                "confidence": float(job.get("confidence", 0.0)),
                "searched_at": datetime.now(timezone.utc).isoformat(),
            }
            supabase.table("job_listings").upsert(
                row, on_conflict="content_hash"
            ).execute()
        except Exception:
            logger.exception("Failed to upsert job: %s", job.get("title", "unknown"))


def get_cached_jobs(query: str, location: str = "", hours: int = 3) -> list[dict]:
    """Return cached jobs for a normalized query from the last *hours* hours.

    Results are ordered by FPS descending, capped at 50.
    """
    if supabase is None:
        return []

    try:
        cache_key = normalize_query(query, location)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        resp = (
            supabase.table("job_listings")
            .select("*")
            .eq("normalized_query", cache_key)
            .gte("searched_at", cutoff)
            .order("fps", desc=True)
            .limit(50)
            .execute()
        )
        return resp.data if resp.data else []
    except Exception:
        logger.exception("Failed to fetch cached jobs for query: %s", query)
        return []


def store_scraper_health(source_name: str, is_healthy: bool, listing_count: int) -> None:
    """Insert a health-check row into the `scraper_health` table."""
    if supabase is None:
        logger.debug("Supabase not configured — skipping scraper health insert")
        return

    try:
        supabase.table("scraper_health").insert({
            "source_name": source_name,
            "is_healthy": is_healthy,
            "listing_count": listing_count,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        logger.exception("Failed to store scraper health for %s", source_name)


def get_scraper_consecutive_failures(source_name: str) -> int:
    """Return the number of consecutive recent failures (0-3) for a scraper source."""
    if supabase is None:
        return 0

    try:
        resp = (
            supabase.table("scraper_health")
            .select("is_healthy")
            .eq("source_name", source_name)
            .order("checked_at", desc=True)
            .limit(3)
            .execute()
        )
        if not resp.data:
            return 0

        consecutive = 0
        for row in resp.data:
            if not row.get("is_healthy", True):
                consecutive += 1
            else:
                break
        return consecutive
    except Exception:
        logger.exception(
            "Failed to get consecutive failures for %s", source_name
        )
        return 0


def get_ai_call_count_today() -> int:
    """Return the number of AI calls recorded for today (UTC)."""
    if supabase is None:
        return 0

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        resp = (
            supabase.table("ai_quota")
            .select("call_count")
            .eq("date", today)
            .execute()
        )
        if resp.data and len(resp.data) > 0:
            return int(resp.data[0].get("call_count", 0))
        return 0
    except Exception:
        logger.exception("Failed to get AI call count for today")
        return 0


def increment_ai_call_count(count: int = 1) -> None:
    """Increment today's AI call count by *count* (non-atomic read-then-write)."""
    if supabase is None:
        return

    try:
        current = get_ai_call_count_today()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        supabase.table("ai_quota").upsert(
            {"date": today, "call_count": current + count},
            on_conflict="date",
        ).execute()
    except Exception:
        logger.exception("Failed to increment AI call count")


def set_user_location(user_id: int, location: str) -> None:
    """Upsert the user's preferred location into `user_preferences`."""
    if supabase is None:
        return

    try:
        supabase.table("user_preferences").upsert(
            {"user_id": user_id, "location": location},
            on_conflict="user_id",
        ).execute()
    except Exception:
        logger.exception("Failed to set location for user %s", user_id)


def get_user_location(user_id: int) -> str:
    """Return the user's stored location preference, defaulting to 'India'."""
    if supabase is None:
        return "India"

    try:
        resp = (
            supabase.table("user_preferences")
            .select("location")
            .eq("user_id", user_id)
            .execute()
        )
        if resp.data and len(resp.data) > 0:
            return resp.data[0].get("location", "India")
        return "India"
    except Exception:
        logger.exception("Failed to get location for user %s", user_id)
        return "India"


def store_user_page_data(user_id: int, query: str, all_jobs: list[dict]) -> None:
    """Store a full result set in the in-memory pagination cache.

    Entries older than 30 minutes are evicted on every write.
    """
    # Evict stale entries
    cutoff = datetime.now() - timedelta(minutes=30)
    stale_keys = [
        uid for uid, entry in _pagination_cache.items()
        if entry.get("timestamp", datetime.min) < cutoff
    ]
    for uid in stale_keys:
        del _pagination_cache[uid]

    _pagination_cache[user_id] = {
        "query": query,
        "jobs": all_jobs,
        "timestamp": datetime.now(),
    }


def get_user_page_data(user_id: int) -> dict | None:
    """Return the pagination cache entry for a user, or None if expired/missing."""
    entry = _pagination_cache.get(user_id)
    if entry is None:
        return None

    if datetime.now() - entry.get("timestamp", datetime.min) > timedelta(minutes=30):
        del _pagination_cache[user_id]
        return None

    return entry