import os
from supabase import create_client, Client

url = os.environ.get("SUPABASE_URL")
key = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key) if url and key else None

def init_tables():
    """Run this once to create tables via Supabase dashboard or migrations.
    We provide SQL for manual creation."""
    pass

# Helper functions
def store_jobs(jobs: list[dict], query_used: str):
    """Insert job listings into Supabase, ignoring duplicates."""
    if not supabase:
        return
    for job in jobs:
        data = {
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "source": job.get("source"),
            "url": job.get("url"),
            "description": job.get("description", ""),
            "stipend": job.get("stipend", ""),
            "duration": job.get("duration", ""),
            "fps": job.get("fps", 0.0),
            "fps_reasoning": job.get("fps_reasoning", ""),
            "is_genuine_fo_role": job.get("is_genuine_fo_role", False),
            "role_summary": job.get("role_summary", ""),
            "red_flags": job.get("red_flags", []),
            "skills_needed": job.get("skills_needed", []),
            "content_hash": job.get("content_hash", ""),
            "query_used": query_used,
            "searched_at": "now()",
        }
        # Use upsert on content_hash to avoid duplicates
        try:
            supabase.table("job_listings").upsert(data, on_conflict="content_hash").execute()
        except Exception as e:
            print(f"Supabase insert error: {e}")

def get_cached_jobs(query: str, hours: int = 1):
    """Return jobs from last N hours for this exact query."""
    if not supabase:
        return []
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    resp = supabase.table("job_listings")\
        .select("*")\
        .eq("query_used", query)\
        .gte("searched_at", cutoff)\
        .order("fps", desc=True)\
        .limit(30)\
        .execute()
    return resp.data

def set_user_location(user_id: int, location: str):
    if not supabase:
        return
    supabase.table("user_preferences").upsert(
        {"user_id": user_id, "location": location},
        on_conflict="user_id"
    ).execute()

def get_user_location(user_id: int) -> str:
    if not supabase:
        return "India"
    resp = supabase.table("user_preferences").select("location").eq("user_id", user_id).execute()
    if resp.data and len(resp.data) > 0:
        return resp.data[0]["location"]
    return "India"