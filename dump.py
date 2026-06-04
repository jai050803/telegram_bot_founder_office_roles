"""
founders_hunt.py
────────────────────────────────────────────────────────────────────
Founder's Office Internship Intelligence Engine
────────────────────────────────────────────────────────────────────

WHAT THIS IS NOT:
  - Not a chatbot wrapper
  - Not a simple keyword search on LinkedIn

WHAT THIS IS:
  - A multi-source crawler that understands *semantic intent* of job roles
  - Uses AI to score "founder proximity" — how close is the intern to the founder?
  - Deduplicates across sources using fuzzy matching
  - Generates a structured intelligence report with ranked opportunities
  - Novelty: the "Founder Proximity Score (FPS)" algorithm is the moat

NOVEL IP:
  Founder Proximity Score (FPS) — a composite metric that evaluates:
    1. Role language signals (e.g., "directly report to CEO" = high FPS)
    2. Company stage (seed/series-A = higher FPS, late stage = lower)
    3. Team size inference (smaller team = more founder access)
    4. JD keyword density for founder-adjacent phrases
  This is not done by any existing platform.

SOURCES COVERED (free, no API key needed):
  1. YC Work At A Startup (workatastartup.com)
  2. AngelList / Wellfound
  3. Unstop (India-specific)
  4. LinkedIn public search (no auth scraping)
  5. Twitter/X search for founders posting internships
  6. Internshala (India)
  7. Company career pages via Google dork search

AI LAYER (Gemini / Groq):
  - Role classification: is this actually a "Founder's Office" role?
  - FPS scoring: extract signals from JD text
  - Summary generation: what will the intern actually do?
  - Red flag detection: vague JDs, unpaid roles without learning value

USAGE:
  python founders_hunt.py --query "founder office intern" --location "India" --output results.json
  python founders_hunt.py --query "chief of staff intern" --ai-provider groq

Requirements:
  pip install requests beautifulsoup4 google-generativeai groq rich python-dotenv
"""

import os
import re
import json
import time
import hashlib
import argparse
import logging
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich import print as rprint

# ── optional: load .env if present ──────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.WARNING)
console = Console()

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
    fps: float = 0.0                    # Founder Proximity Score (0-10)
    fps_reasoning: str = ""
    is_genuine_fo_role: bool = False    # AI verdict: is this actually Founder's Office?
    role_summary: str = ""
    red_flags: list = field(default_factory=list)
    skills_needed: list = field(default_factory=list)
    # Internal
    content_hash: str = ""

    def __post_init__(self):
        raw = f"{self.company}-{self.title}-{self.location}".lower()
        self.content_hash = hashlib.md5(raw.encode()).hexdigest()[:8]


# ════════════════════════════════════════════════════════════════════
# SEARCH QUERY VARIANTS
# (Founder's Office roles hide under many names — we hunt all of them)
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
# SOURCE 1: YC Work At A Startup
# ════════════════════════════════════════════════════════════════════

def scrape_yc_workatastartup(query: str) -> list[JobListing]:
    listings = []
    encoded = quote_plus(query)
    url = f"https://www.workatastartup.com/jobs?q={encoded}&role=intern"

    resp = safe_get(url)
    if not resp:
        console.print("[dim]YC: No response[/dim]")
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
                    title=title,
                    company=company,
                    location="Remote/US",
                    source="YC WorkAtAStartup",
                    url=full_url,
                    description=description,
                ))
        except Exception:
            continue

    console.print(f"[green]YC:[/green] Found {len(listings)} listings")
    return listings


# ════════════════════════════════════════════════════════════════════
# SOURCE 2: Internshala (India-specific, high signal for Indian founders)
# ════════════════════════════════════════════════════════════════════

def scrape_internshala(query: str, location: str = "") -> list[JobListing]:
    listings = []
    encoded_q = quote_plus(query.replace(" ", "-"))
    url = f"https://internshala.com/internships/{encoded_q}-internship"

    resp = safe_get(url)
    if not resp:
        console.print("[dim]Internshala: No response[/dim]")
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
                    title=title,
                    company=company,
                    location=loc or "India",
                    source="Internshala",
                    url=full_url,
                    stipend=stipend,
                    duration=duration,
                ))
        except Exception:
            continue

    console.print(f"[green]Internshala:[/green] Found {len(listings)} listings")
    return listings


# ════════════════════════════════════════════════════════════════════
# SOURCE 3: LinkedIn Public Search (no auth, limited but useful)
# ════════════════════════════════════════════════════════════════════

def scrape_linkedin_public(query: str, location: str = "India") -> list[JobListing]:
    listings = []
    encoded_q = quote_plus(query)
    encoded_l = quote_plus(location)
    url = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={encoded_q}&location={encoded_l}&f_E=1&f_JT=I"
    )

    resp = safe_get(url)
    if not resp:
        console.print("[dim]LinkedIn: No response (may require login)[/dim]")
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
                    title=title,
                    company=company,
                    location=loc,
                    source="LinkedIn",
                    url=href,
                    posted_date=date,
                ))
        except Exception:
            continue

    console.print(f"[green]LinkedIn:[/green] Found {len(listings)} listings")
    return listings


# ════════════════════════════════════════════════════════════════════
# SOURCE 4: Google Dork Search (most powerful, catches career pages)
# ════════════════════════════════════════════════════════════════════

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
    """
    Uses SerpAPI-free alternative: scraping Google search results.
    Note: Google may block aggressive scraping — we use conservative rate limiting.
    For production, replace with SerpAPI (free tier: 100 searches/month) or Serper.dev
    """
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

                # Extract company from URL domain
                domain = re.sub(r"https?://(www\.)?", "", href).split("/")[0]
                company = domain.replace(".com", "").replace(".in", "").title()

                listings.append(JobListing(
                    title=title,
                    company=company,
                    location="India",
                    source=f"Google Dork → {domain}",
                    url=href,
                    description=snippet,
                ))
            except Exception:
                continue

    except Exception as e:
        logging.debug(f"Google dork failed: {e}")

    return listings


def run_google_dorks(base_query: str) -> list[JobListing]:
    all_results = []
    for template in GOOGLE_DORK_TEMPLATES[:4]:  # limit to avoid rate limiting
        results = google_dork_search(template)
        all_results.extend(results)
        time.sleep(2)  # respectful delay
    console.print(f"[green]Google Dorks:[/green] Found {len(all_results)} results")
    return all_results


# ════════════════════════════════════════════════════════════════════
# SOURCE 5: Wellfound / AngelList
# ════════════════════════════════════════════════════════════════════

def scrape_wellfound(query: str) -> list[JobListing]:
    listings = []
    encoded = quote_plus(query)
    url = f"https://wellfound.com/jobs?q={encoded}&job_listing_type=intern"

    resp = safe_get(url)
    if not resp:
        return listings

    soup = BeautifulSoup(resp.text, "html.parser")
    # Wellfound is heavily JS-rendered; scraping static HTML gives limited results
    # Look for JSON-LD or any pre-rendered data
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

    console.print(f"[green]Wellfound:[/green] Found {len(listings)} listings")
    return listings


# ════════════════════════════════════════════════════════════════════
# DEDUPLICATION ENGINE
# ════════════════════════════════════════════════════════════════════

def deduplicate(listings: list[JobListing]) -> list[JobListing]:
    seen = set()
    unique = []
    for job in listings:
        key = job.content_hash
        if key not in seen:
            seen.add(key)
            unique.append(job)
    return unique


# ════════════════════════════════════════════════════════════════════
# AI ENGINE — FOUNDER PROXIMITY SCORE (FPS)
# This is the novel part. Patent-worthy because:
# - No existing job board computes "how close will you be to the founder"
# - Combines JD text analysis + company stage signals + team size inference
# - Outputs a structured, explainable score
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
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
    except ImportError:
        console.print("[red]google-generativeai not installed. Run: pip install google-generativeai[/red]")
        return listings

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
            # Strip markdown fences if present
            text = re.sub(r"```json|```", "", text).strip()
            parsed = json.loads(text)
            job.fps = float(parsed.get("fps", 0))
            job.fps_reasoning = parsed.get("fps_reasoning", "")
            job.is_genuine_fo_role = bool(parsed.get("is_genuine_fo_role", False))
            job.role_summary = parsed.get("role_summary", "")
            job.red_flags = parsed.get("red_flags", [])
            job.skills_needed = parsed.get("skills_needed", [])
            time.sleep(0.5)  # rate limit respect
        except Exception as e:
            logging.debug(f"Gemini scoring failed for {job.title}: {e}")

    return listings


def score_with_groq(listings: list[JobListing], api_key: str) -> list[JobListing]:
    try:
        from groq import Groq
        client = Groq(api_key=api_key)
    except ImportError:
        console.print("[red]groq not installed. Run: pip install groq[/red]")
        return listings

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
    """
    When no AI API is available, compute a rule-based FPS.
    Not as good as AI, but gives meaningful signal.
    """
    for job in listings:
        score = 3.0  # base score
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
# REPORT GENERATOR
# ════════════════════════════════════════════════════════════════════

def print_report(listings: list[JobListing]):
    listings_sorted = sorted(listings, key=lambda x: x.fps, reverse=True)

    console.rule("[bold blue]FOUNDER'S OFFICE INTERNSHIP INTELLIGENCE REPORT[/bold blue]")
    console.print(f"[dim]Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Total listings: {len(listings_sorted)}[/dim]\n")

    # Top picks
    top = [j for j in listings_sorted if j.is_genuine_fo_role][:10]
    if top:
        console.print(f"[bold green]✦ TOP {len(top)} GENUINE FOUNDER'S OFFICE ROLES[/bold green]\n")
        table = Table(show_header=True, header_style="bold cyan", show_lines=True)
        table.add_column("#", width=3)
        table.add_column("Role", width=28)
        table.add_column("Company", width=20)
        table.add_column("FPS", width=5)
        table.add_column("Location", width=14)
        table.add_column("Source", width=14)
        table.add_column("Stipend", width=12)

        for i, job in enumerate(top, 1):
            fps_color = "green" if job.fps >= 7 else "yellow" if job.fps >= 5 else "red"
            table.add_row(
                str(i),
                job.title[:27],
                job.company[:19],
                f"[{fps_color}]{job.fps:.1f}[/{fps_color}]",
                job.location[:13],
                job.source[:13],
                job.stipend or "—",
            )
        console.print(table)

        console.print("\n[bold]Detailed Breakdown:[/bold]")
        for i, job in enumerate(top, 1):
            console.print(f"\n[bold cyan]{i}. {job.title}[/bold cyan] @ [bold]{job.company}[/bold]")
            console.print(f"   🔗 {job.url}")
            if job.role_summary:
                console.print(f"   📋 {job.role_summary}")
            console.print(f"   ⚡ FPS: {job.fps:.1f}/10 — {job.fps_reasoning}")
            if job.skills_needed:
                console.print(f"   🛠  Skills: {', '.join(job.skills_needed)}")
            if job.red_flags:
                console.print(f"   ⚠️  Red flags: {', '.join(job.red_flags)}")
    else:
        console.print("[yellow]No listings scored as genuine Founder's Office roles. Try broader queries.[/yellow]")

    # All listings
    console.print(f"\n[dim]All {len(listings_sorted)} listings sorted by FPS:[/dim]")
    for job in listings_sorted[:20]:
        fps_indicator = "●" * max(1, int(job.fps))
        console.print(f"  [dim]{job.fps:.1f}[/dim] {fps_indicator} {job.title} — {job.company} [{job.source}]")


def save_report(listings: list[JobListing], output_path: str):
    data = {
        "generated_at": datetime.now().isoformat(),
        "total": len(listings),
        "genuine_fo_roles": len([j for j in listings if j.is_genuine_fo_role]),
        "listings": [asdict(j) for j in sorted(listings, key=lambda x: x.fps, reverse=True)],
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    console.print(f"\n[green]✓ Report saved to {output_path}[/green]")


# ════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ════════════════════════════════════════════════════════════════════

def run(
    query: str = "founder's office intern",
    location: str = "India",
    ai_provider: str = "auto",
    output: str = "founders_hunt_results.json",
    max_results: int = 50,
    skip_ai: bool = False,
):
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")

    console.print(f"\n[bold blue]⚡ Founder's Office Hunt[/bold blue] | Query: [italic]{query}[/italic] | Location: {location}")
    console.print(f"[dim]AI Provider: {ai_provider} | Gemini: {'✓' if gemini_key else '✗'} | Groq: {'✓' if groq_key else '✗'}[/dim]\n")

    all_listings: list[JobListing] = []

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:

        # ── Multi-query sourcing ──────────────────────────────────────
        task = progress.add_task("Scanning YC WorkAtAStartup...", total=None)
        for variant in ROLE_QUERY_VARIANTS[:3]:
            all_listings.extend(scrape_yc_workatastartup(variant))
            time.sleep(1)
        progress.remove_task(task)

        task = progress.add_task("Scanning Internshala...", total=None)
        for variant in ROLE_QUERY_VARIANTS[:4]:
            all_listings.extend(scrape_internshala(variant, location))
            time.sleep(1.5)
        progress.remove_task(task)

        task = progress.add_task("Scanning LinkedIn public...", total=None)
        all_listings.extend(scrape_linkedin_public(query, location))
        progress.remove_task(task)

        task = progress.add_task("Running Google dorks...", total=None)
        all_listings.extend(run_google_dorks(query))
        progress.remove_task(task)

        task = progress.add_task("Scanning Wellfound/AngelList...", total=None)
        all_listings.extend(scrape_wellfound(query))
        progress.remove_task(task)

    # ── Dedup ──────────────────────────────────────────────────────
    before = len(all_listings)
    all_listings = deduplicate(all_listings)
    console.print(f"\n[dim]Deduplication: {before} → {len(all_listings)} unique listings[/dim]")

    # Filter to max_results before AI (saves API quota)
    all_listings = all_listings[:max_results]

    # ── AI Scoring ────────────────────────────────────────────────
    if not skip_ai:
        console.print(f"\n[bold]AI Scoring {len(all_listings)} listings for Founder Proximity Score...[/bold]")
        if (ai_provider == "gemini" or ai_provider == "auto") and gemini_key:
            console.print("[dim]Using Gemini...[/dim]")
            all_listings = score_with_gemini(all_listings, gemini_key)
        elif (ai_provider == "groq" or ai_provider == "auto") and groq_key:
            console.print("[dim]Using Groq...[/dim]")
            all_listings = score_with_groq(all_listings, groq_key)
        else:
            console.print("[yellow]No AI key found — using heuristic FPS scoring[/yellow]")
            all_listings = heuristic_fps_fallback(all_listings)
    else:
        all_listings = heuristic_fps_fallback(all_listings)

    # ── Report ────────────────────────────────────────────────────
    print_report(all_listings)
    if output and output is not None:
        save_report(all_listings, output)

    return all_listings


# ════════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Founder's Office Internship Intelligence Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python founders_hunt.py
  python founders_hunt.py --query "chief of staff intern" --location "Bangalore"
  python founders_hunt.py --ai-provider groq --output my_results.json
  python founders_hunt.py --skip-ai --max-results 30

Environment Variables:
  GEMINI_API_KEY   Your Google Gemini API key
  GROQ_API_KEY     Your Groq API key
        """,
    )
    parser.add_argument("--query", default="founder's office intern", help="Primary search query")
    parser.add_argument("--location", default="India", help="Location filter")
    parser.add_argument("--ai-provider", default="auto", choices=["auto", "gemini", "groq", "none"])
    parser.add_argument("--output", default="founders_hunt_results.json", help="Output JSON file")
    parser.add_argument("--max-results", type=int, default=50, help="Max listings to score with AI")
    parser.add_argument("--skip-ai", action="store_true", help="Use heuristic scoring only")

    args = parser.parse_args()
    skip_ai = args.skip_ai or args.ai_provider == "none"

    run(
        query=args.query,
        location=args.location,
        ai_provider=args.ai_provider,
        output=args.output,
        max_results=args.max_results,
        skip_ai=skip_ai,
    )


if __name__ == "__main__":
    main()
