"""
founders_hunt.py
────────────────────────────────────────────────────────────────────
Founder's Office Internship Intelligence Engine
────────────────────────────────────────────────────────────────────
Modified to be callable from a Telegram bot – returns JSON-serializable
list of job listings instead of printing.
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

# ── optional: load .env if present ──────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.WARNING)

# ════════════════════════════════════════════════════════════════════
# DATA MODEL
# ════════════════════════════════════════════════════════════════════

@dataclass
class JobListing:
    title: str
    company: str
    location: str
    source: str
    url: str
    description: str = ""
    posted_date: str = ""
    stipend: str = ""
    duration: str = ""
    # AI-generated fields
    fps: float = 0.0
    fps_reasoning: str = ""
    is_genuine_fo_role: bool = False
    role_summary: str = ""
    red_flags: list = field(default_factory=list)
    skills_needed: list = field(default_factory=list)
    # Internal
    content_hash: str = ""

    def __post_init__(self):
        raw = f"{self.company}-{self.title}-{self.location}".lower()
        self.content_hash = hashlib.md5(raw.encode()).hexdigest()[:8]

    def to_dict(self) -> dict:
        return asdict(self)

# ════════════════════════════════════════════════════════════════════
# SEARCH QUERY VARIANTS
# ════════════════════════════════════════════════════════════════════

ROLE_QUERY_VARIANTS = [
    "founder's office intern",
    "founders office internship",
    "chief of staff intern startup",
    "CEO office intern",
    "founder associate intern",
    "strategy intern startup founder",
    "special projects intern startup",
    "growth intern report to founder",
    "executive intern startup",
    "office of the CEO internship",
]

FOUNDER_PROXIMITY_SIGNALS = {
    "high": [
        "report to the founder", "report to ceo", "directly to founder",
        "work closely with the founding team", "shadow the ceo",
        "founder's office", "office of the ceo", "chief of staff",
        "founding team", "work with the ceo",
    ],
    "medium": [
        "special projects", "cross-functional", "0-1", "zero to one",
        "strategy", "operations", "early stage", "seed", "series a",
        "growth hacking", "startup", "stealth",
    ],
    "low": [
        "intern", "internship", "trainee", "fellowship",
    ],
    "negative": [
        "large enterprise", "fortune 500", "mnc", "process-driven",
        "well-established company", "series d", "series e",
    ],
}

# ════════════════════════════════════════════════════════════════════
# HTTP UTILITY
# ════════════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def safe_get(url: str, timeout: int = 10, retries: int = 2) -> Optional[requests.Response]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp
            time.sleep(1.5)
        except Exception as e:
            logging.debug(f"Request failed ({url}): {e}")
            time.sleep(2)
    return None

# ════════════════════════════════════════════════════════════════════
# SOURCE SCRAPERS (unchanged, but included for completeness)
# ════════════════════════════════════════════════════════════════════

def scrape_yc_workatastartup(query: str) -> list[JobListing]:
    listings = []
    encoded = quote_plus(query)
    url = f"https://www.workatastartup.com/jobs?q={encoded}&role=intern"
    resp = safe_get(url)
    if not resp:
        return listings
    soup = BeautifulSoup(resp.text, "html.parser")
    job_cards = soup.find_all("div", class_=re.compile(r"job.*card|listing", re.I))[:20]
    for card in job_cards:
        try:
            title_el = card.find(["h2", "h3", "a"], class_=re.compile(r"title|role", re.I))
            company_el = card.find(["span", "div", "a"], class_=re.compile(r"company|startup", re.I))
            desc_el = card.find(["p", "div"], class_=re.compile(r"desc|summary", re.I))
            link_el = card.find("a", href=True)
            title = title_el.get_text(strip=True) if title_el else "Unknown Role"
            company = company_el.get_text(strip=True) if company_el else "Unknown Company"
            description = desc_el.get_text(strip=True) if desc_el else ""
            href = link_el["href"] if link_el else ""
            full_url = urljoin("https://www.workatastartup.com", href) if href else url
            if title and company and title != "Unknown Role":
                listings.append(JobListing(
                    title=title, company=company, location="Remote/US",
                    source="YC WorkAtAStartup", url=full_url, description=description,
                ))
        except Exception:
            continue
    return listings

def scrape_internshala(query: str, location: str = "") -> list[JobListing]:
    listings = []
    encoded_q = quote_plus(query.replace(" ", "-"))
    url = f"https://internshala.com/internships/{encoded_q}-internship"
    resp = safe_get(url)
    if not resp:
        return listings
    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.find_all("div", class_=re.compile(r"internship_meta|individual_internship", re.I))[:15]
    for c in containers:
        try:
            title_el = c.find("h3", class_=re.compile(r"heading|title", re.I)) or c.find("a", class_=re.compile(r"title", re.I))
            company_el = c.find("p", class_=re.compile(r"company", re.I))
            loc_el = c.find(id=re.compile(r"location_names", re.I))
            stipend_el = c.find("span", class_=re.compile(r"stipend", re.I))
            dur_el = c.find(id=re.compile(r"duration", re.I))
            link_el = c.find("a", href=True)
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            loc = loc_el.get_text(strip=True) if loc_el else location
            stipend = stipend_el.get_text(strip=True) if stipend_el else ""
            duration = dur_el.get_text(strip=True) if dur_el else ""
            href = link_el["href"] if link_el else ""
            full_url = urljoin("https://internshala.com", href) if href else url
            if title and company:
                listings.append(JobListing(
                    title=title, company=company, location=loc or "India",
                    source="Internshala", url=full_url, stipend=stipend, duration=duration,
                ))
        except Exception:
            continue
    return listings

def scrape_linkedin_public(query: str, location: str = "India") -> list[JobListing]:
    listings = []
    encoded_q = quote_plus(query)
    encoded_l = quote_plus(location)
    url = f"https://www.linkedin.com/jobs/search/?keywords={encoded_q}&location={encoded_l}&f_E=1&f_JT=I"
    resp = safe_get(url)
    if not resp:
        return listings
    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.find_all("div", class_=re.compile(r"job-search-card|base-card", re.I))[:20]
    for card in cards:
        try:
            title_el = card.find(["h3", "a"], class_=re.compile(r"title|base-card__full-link", re.I))
            company_el = card.find(["h4", "a"], class_=re.compile(r"company", re.I))
            loc_el = card.find("span", class_=re.compile(r"location", re.I))
            date_el = card.find("time")
            link_el = card.find("a", href=True)
            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            loc = loc_el.get_text(strip=True) if loc_el else location
            date = date_el.get("datetime", "") if date_el else ""
            href = link_el["href"] if link_el else ""
            if title and company:
                listings.append(JobListing(
                    title=title, company=company, location=loc,
                    source="LinkedIn", url=href, posted_date=date,
                ))
        except Exception:
            continue
    return listings

GOOGLE_DORK_TEMPLATES = [
    'site:linkedin.com/jobs "founder\'s office" intern',
    'site:wellfound.com "founder" "office" internship',
    '"founder\'s office" OR "office of the CEO" internship India 2024 2025',
    'site:instahyre.com "founder" intern',
    '"chief of staff" intern startup India apply',
    'site:cutshort.io "founder" "intern"',
    '"work directly with the founder" internship apply now',
]

def google_dork_search(query: str) -> list[JobListing]:
    listings = []
    encoded = quote_plus(query)
    url = f"https://www.google.com/search?q={encoded}&num=10"
    headers = {**HEADERS, "Accept": "text/html"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return listings
        soup = BeautifulSoup(resp.text, "html.parser")
        results = soup.find_all("div", class_=re.compile(r"^g$|tF2Cxc|yuRUbf", re.I))[:8]
        for r in results:
            try:
                link = r.find("a", href=True)
                title_el = r.find(["h3", "h2"])
                snippet_el = r.find("span", class_=re.compile(r"aCOpRe|VwiC3b", re.I))
                if not link or not title_el:
                    continue
                href = link["href"]
                if href.startswith("/url?q="):
                    href = href.split("/url?q=")[1].split("&")[0]
                title = title_el.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                domain = re.sub(r"https?://(www\.)?", "", href).split("/")[0]
                company = domain.replace(".com", "").replace(".in", "").title()
                listings.append(JobListing(
                    title=title, company=company, location="India",
                    source=f"Google Dork → {domain}", url=href, description=snippet,
                ))
            except Exception:
                continue
    except Exception as e:
        logging.debug(f"Google dork failed: {e}")
    return listings

def run_google_dorks(base_query: str) -> list[JobListing]:
    all_results = []
    for template in GOOGLE_DORK_TEMPLATES[:4]:
        all_results.extend(google_dork_search(template))
        time.sleep(2)
    return all_results

def scrape_wellfound(query: str) -> list[JobListing]:
    listings = []
    encoded = quote_plus(query)
    url = f"https://wellfound.com/jobs?q={encoded}&job_listing_type=intern"
    resp = safe_get(url)
    if not resp:
        return listings
    soup = BeautifulSoup(resp.text, "html.parser")
    scripts = soup.find_all("script", type="application/json")
    for script in scripts:
        try:
            data = json.loads(script.string or "{}")
            jobs = data.get("jobs", data.get("listings", []))
            for job in jobs[:10]:
                if isinstance(job, dict):
                    listings.append(JobListing(
                        title=job.get("title", ""),
                        company=job.get("company", {}).get("name", "") if isinstance(job.get("company"), dict) else job.get("company", ""),
                        location=job.get("location", "Remote"),
                        source="Wellfound",
                        url=job.get("url", url),
                        description=job.get("description", ""),
                    ))
        except Exception:
            continue
    return listings

# ════════════════════════════════════════════════════════════════════
# DEDUPLICATION
# ════════════════════════════════════════════════════════════════════

def deduplicate(listings: list[JobListing]) -> list[JobListing]:
    seen = set()
    unique = []
    for job in listings:
        if job.content_hash not in seen:
            seen.add(job.content_hash)
            unique.append(job)
    return unique

# ════════════════════════════════════════════════════════════════════
# AI SCORING (FPS)
# ════════════════════════════════════════════════════════════════════

FPS_SYSTEM_PROMPT = """You are an expert analyst of startup internship opportunities, specializing in identifying "Founder's Office" and high-founder-proximity roles.

Your task: Analyze a job listing and compute a FOUNDER PROXIMITY SCORE (FPS).

FPS Definition:
FPS measures how closely an intern will work with the actual founder(s) of the company.
- 9-10: Intern reports directly to founder, shadow the CEO, founder explicitly mentioned
- 7-8: Clear cross-functional role, small team (<20 people), early stage startup
- 5-6: Strategy/ops role at startup but founder proximity is implied, not explicit
- 3-4: Generic startup intern role, medium-sized company
- 1-2: MNC, large company, or process-heavy role with no founder access

Respond ONLY in this exact JSON format (no markdown, no explanation outside JSON):
{
  "fps": <float 0-10>,
  "fps_reasoning": "<1-2 sentence explanation>",
  "is_genuine_fo_role": <true/false>,
  "role_summary": "<what the intern will actually do in 1 sentence>",
  "red_flags": ["<flag1>", "<flag2>"],
  "skills_needed": ["<skill1>", "<skill2>", "<skill3>"]
}"""

def score_with_gemini(listings: list[JobListing], api_key: str) -> list[JobListing]:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    for job in listings:
        try:
            prompt = f"""
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description[:800] if job.description else 'Not available'}
Stipend: {job.stipend}
Duration: {job.duration}
"""
            response = model.generate_content(
                [{"role": "user", "parts": [FPS_SYSTEM_PROMPT + "\n\nJob Listing:\n" + prompt]}]
            )
            text = response.text.strip()
            text = re.sub(r"```json|```", "", text).strip()
            parsed = json.loads(text)
            job.fps = float(parsed.get("fps", 0))
            job.fps_reasoning = parsed.get("fps_reasoning", "")
            job.is_genuine_fo_role = bool(parsed.get("is_genuine_fo_role", False))
            job.role_summary = parsed.get("role_summary", "")
            job.red_flags = parsed.get("red_flags", [])
            job.skills_needed = parsed.get("skills_needed", [])
            time.sleep(0.5)
        except Exception as e:
            logging.debug(f"Gemini scoring failed for {job.title}: {e}")
    return listings

def score_with_groq(listings: list[JobListing], api_key: str) -> list[JobListing]:
    from groq import Groq
    client = Groq(api_key=api_key)
    for job in listings:
        try:
            prompt = f"""
Title: {job.title}
Company: {job.company}
Location: {job.location}
Description: {job.description[:800] if job.description else 'Not available'}
Stipend: {job.stipend}
Duration: {job.duration}
"""
            response = client.chat.completions.create(
                model="llama3-70b-8192",
                messages=[
                    {"role": "system", "content": FPS_SYSTEM_PROMPT},
                    {"role": "user", "content": "Job Listing:\n" + prompt},
                ],
                temperature=0.1,
                max_tokens=400,
            )
            text = response.choices[0].message.content.strip()
            text = re.sub(r"```json|```", "", text).strip()
            parsed = json.loads(text)
            job.fps = float(parsed.get("fps", 0))
            job.fps_reasoning = parsed.get("fps_reasoning", "")
            job.is_genuine_fo_role = bool(parsed.get("is_genuine_fo_role", False))
            job.role_summary = parsed.get("role_summary", "")
            job.red_flags = parsed.get("red_flags", [])
            job.skills_needed = parsed.get("skills_needed", [])
            time.sleep(0.3)
        except Exception as e:
            logging.debug(f"Groq scoring failed for {job.title}: {e}")
    return listings

def heuristic_fps_fallback(listings: list[JobListing]) -> list[JobListing]:
    for job in listings:
        score = 3.0
        text = (job.title + " " + job.description + " " + job.company).lower()
        for signal in FOUNDER_PROXIMITY_SIGNALS["high"]:
            if signal in text:
                score += 2.5
                break
        for signal in FOUNDER_PROXIMITY_SIGNALS["medium"]:
            if signal in text:
                score += 1.0
                break
        for signal in FOUNDER_PROXIMITY_SIGNALS["negative"]:
            if signal in text:
                score -= 2.0
                break
        job.fps = min(10.0, max(0.0, score))
        job.is_genuine_fo_role = score >= 6.0
        job.fps_reasoning = "Heuristic scoring (no AI key provided)"
    return listings

# ════════════════════════════════════════════════════════════════════
# MAIN EXPOSED FUNCTION
# ════════════════════════════════════════════════════════════════════

def run_search(
    query: str = "founder's office intern",
    location: str = "India",
    ai_provider: str = "auto",
    max_results: int = 50,
    skip_ai: bool = False,
) -> List[Dict[str, Any]]:
    """
    Execute the full search, scoring, and return list of job dicts.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    all_listings: list[JobListing] = []

    # Query variants – use first 3 to keep runtime reasonable
    for variant in ROLE_QUERY_VARIANTS[:3]:
        all_listings.extend(scrape_yc_workatastartup(variant))
        time.sleep(1)
    for variant in ROLE_QUERY_VARIANTS[:4]:
        all_listings.extend(scrape_internshala(variant, location))
        time.sleep(1.5)
    all_listings.extend(scrape_linkedin_public(query, location))
    all_listings.extend(run_google_dorks(query))
    all_listings.extend(scrape_wellfound(query))

    # Deduplicate and limit
    all_listings = deduplicate(all_listings)[:max_results]

    # AI Scoring
    if not skip_ai:
        if (ai_provider == "gemini" or ai_provider == "auto") and gemini_key:
            all_listings = score_with_gemini(all_listings, gemini_key)
        elif (ai_provider == "groq" or ai_provider == "auto") and groq_key:
            all_listings = score_with_groq(all_listings, groq_key)
        else:
            all_listings = heuristic_fps_fallback(all_listings)
    else:
        all_listings = heuristic_fps_fallback(all_listings)

    # Convert to dicts for JSON serialization
    return [job.to_dict() for job in all_listings]

if __name__ == "__main__":
    # For CLI testing (unchanged)
    import sys
    results = run_search(query=" ".join(sys.argv[1:]) if len(sys.argv) > 1 else "founder's office intern")
    print(json.dumps(results, indent=2))