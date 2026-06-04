import os
import re
import json
import time
import random
import hashlib
import asyncio
import logging
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import quote_plus, urljoin

import aiohttp
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

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
    fps: float = 0.0
    fps_reasoning: str = ""
    is_genuine_fo_role: bool = False
    role_summary: str = ""
    red_flags: list = field(default_factory=list)
    skills_needed: list = field(default_factory=list)
    confidence: float = 0.0
    fps_dimensions: dict = field(default_factory=dict)
    content_hash: str = ""

    def __post_init__(self):
        raw = f"{self.company}-{self.title}-{self.location}".lower()
        raw = re.sub(r'[^a-z0-9]', '', raw)
        self.content_hash = hashlib.md5(raw.encode()).hexdigest()[:10]

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Rotating User-Agent Pool
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


def get_random_headers() -> Dict[str, str]:
    """Return request headers with a randomly chosen User-Agent."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }


# ---------------------------------------------------------------------------
# Founder-Office Ontology  (semantic keyword clusters)
# ---------------------------------------------------------------------------

FOUNDER_OFFICE_ONTOLOGY: Dict[str, List[str]] = {
    "founder_access": [
        "founder's office", "founders office", "founder office",
        "office of the ceo", "shadow the ceo", "ceo's office",
        "right hand to founder", "right hand to ceo",
        "work directly with founder", "work directly with ceo",
        "report to founder", "report to ceo", "report directly to founder",
        "founder's right hand", "ceo shadow", "founder shadow",
    ],
    "generalist_role": [
        "chief of staff", "cos intern", "founding team member",
        "generalist intern", "no two days the same", "wear many hats",
        "cross-functional role", "jack of all trades",
        "founding team", "founding intern", "first hire",
        "employee number", "early team member",
    ],
    "zero_to_one": [
        "0 to 1", "zero to one", "0-1", "build from scratch",
        "ground up", "from the ground", "greenfield",
        "build the plane while flying", "early stage",
    ],
    "strategy_ops": [
        "strategy intern", "strategy and operations",
        "business operations", "biz ops", "special projects",
        "strategic initiatives", "ceo projects",
        "founder projects", "growth strategy",
    ],
    "startup_signals": [
        "pre-seed", "seed stage", "seed funded", "series a",
        "just raised", "stealth mode", "stealth startup",
        "bootstrapped", "angel funded", "yc backed", "y combinator",
        "techstars", "backed by", "incubator", "accelerator",
    ],
    "eir_style": [
        "entrepreneur in residence", "eir", "venture fellow",
        "startup fellow", "innovation fellow",
        "founder associate", "founder's associate",
        "venture intern", "vc intern",
    ],
    "autonomy_signals": [
        "own this", "take ownership", "end to end",
        "drive this", "lead this", "run this",
        "full ownership", "autonomous", "self-starter",
        "figure it out", "scrappy",
    ],
}

# ---------------------------------------------------------------------------
# Role Query Variants  (15 variants)
# ---------------------------------------------------------------------------

ROLE_QUERY_VARIANTS: List[str] = [
    "founder's office intern",
    "chief of staff intern startup",
    "CEO office intern",
    "shadow the CEO internship",
    "report directly to founder intern",
    "founder's associate intern",
    "strategy and operations intern startup",
    "founding team intern early stage",
    "special projects intern founder",
    "right hand to founder internship",
    "startup generalist intern",
    "founding team internship",
    "0 to 1 intern startup",
    "work directly with founder intern",
    "ceo shadow intern",
]

# ---------------------------------------------------------------------------
# Founder Proximity Signals  (cumulative scoring)
# ---------------------------------------------------------------------------

FOUNDER_PROXIMITY_SIGNALS: Dict[str, List[str]] = {
    "high": [
        "founder's office", "founders office", "founder office",
        "report to founder", "report to ceo", "shadow the ceo",
        "right hand to founder", "right hand to ceo",
        "work directly with founder", "work directly with ceo",
        "ceo's office", "office of the ceo",
        "founder's right hand", "ceo shadow", "founder shadow",
        "chief of staff",
    ],
    "medium": [
        "early stage", "founding team", "founding intern",
        "0 to 1", "zero to one", "build from scratch",
        "strategy and operations", "special projects",
        "generalist", "wear many hats", "cross-functional",
        "pre-seed", "seed stage", "stealth", "first hire",
        "entrepreneur in residence", "venture fellow",
        "own this", "take ownership", "scrappy",
        "founder associate", "founder's associate",
    ],
    "negative": [
        "data entry", "telecalling", "tele-calling", "door to door",
        "commission only", "unpaid", "mlm", "network marketing",
        "seo executive", "social media post", "content writing only",
        "sales target", "cold calling only", "bulk hiring 100+",
        "customer support executive", "chat process",
    ],
}

# ---------------------------------------------------------------------------
# Query Expansion
# ---------------------------------------------------------------------------

# Pre-compute a word → cluster-name mapping for fast lookup
_WORD_TO_CLUSTERS: Dict[str, set] = {}
for _cluster_name, _phrases in FOUNDER_OFFICE_ONTOLOGY.items():
    for _phrase in _phrases:
        for _word in _phrase.split():
            _w = _word.lower().strip()
            if len(_w) > 2:
                _WORD_TO_CLUSTERS.setdefault(_w, set()).add(_cluster_name)


def expand_query(user_query: str) -> List[str]:
    """Generate 4-6 search query variants from a raw user query using the ontology."""
    q_lower = user_query.lower().strip()
    words = re.findall(r'[a-z0-9]+', q_lower)

    # Determine which ontology clusters are relevant
    matched_clusters: set = set()
    for w in words:
        matched_clusters.update(_WORD_TO_CLUSTERS.get(w, set()))

    # If nothing matched, default to founder_access + generalist_role
    if not matched_clusters:
        matched_clusters = {"founder_access", "generalist_role"}

    # Build adjacency: if founder_access matched, also consider generalist_role, etc.
    adjacency: Dict[str, List[str]] = {
        "founder_access": ["generalist_role", "strategy_ops"],
        "generalist_role": ["founder_access", "zero_to_one"],
        "zero_to_one": ["startup_signals", "founder_access"],
        "strategy_ops": ["founder_access", "generalist_role"],
        "startup_signals": ["zero_to_one", "eir_style"],
        "eir_style": ["founder_access", "startup_signals"],
        "autonomy_signals": ["zero_to_one", "generalist_role"],
    }
    expanded_clusters: set = set(matched_clusters)
    for mc in matched_clusters:
        for adj in adjacency.get(mc, []):
            expanded_clusters.add(adj)

    # Pick representative phrases from each matched/adjacent cluster
    variants: List[str] = [user_query]  # always include original
    variants.append("founder's office intern")  # always include default

    for cluster_name in expanded_clusters:
        phrases = FOUNDER_OFFICE_ONTOLOGY.get(cluster_name, [])
        if phrases:
            chosen = random.choice(phrases[:5])
            variant = f"{chosen} intern" if "intern" not in chosen else chosen
            variants.append(variant)

    # Deduplicate while preserving order
    seen: set = set()
    deduped: List[str] = []
    for v in variants:
        key = v.lower().strip()
        if key not in seen:
            seen.add(key)
            deduped.append(v)

    return deduped[:6]


# ---------------------------------------------------------------------------
# Async HTTP helper
# ---------------------------------------------------------------------------

async def safe_get(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int = 12,
    retries: int = 2,
) -> Optional[str]:
    """Async HTTP GET with retries, random delays, rotating UAs."""
    for attempt in range(retries):
        try:
            headers = get_random_headers()
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return await resp.text()
                logger.debug("HTTP %d for %s", resp.status, url)
                await asyncio.sleep(1 + random.random())
        except Exception as e:
            logger.debug("Request failed (%s): %s", url, e)
            await asyncio.sleep(1.5 + random.random())
    return None


# ---------------------------------------------------------------------------
# Scraper 1 — Internshala
# ---------------------------------------------------------------------------

async def scrape_internshala(
    session: aiohttp.ClientSession,
    query: str,
    location: str = "",
) -> Tuple[List[JobListing], bool]:
    """Scrape Internshala for internship listings."""
    encoded = query.strip().replace(" ", "-")
    url = f"https://internshala.com/internships/keywords-{encoded}"
    html = await safe_get(session, url)
    if html is None:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    listings: List[JobListing] = []

    containers = soup.find_all("div", class_=lambda c: c and (
        "individual_internship" in c or "internship_meta" in c
    ))

    if not containers:
        # Fallback: look for any container with internship data
        containers = soup.select("div.internship_meta, div.individual_internship, div[class*='individual_internship']")

    is_healthy = bool(containers) or ("internshala" in html.lower())

    for container in containers[:15]:
        try:
            # Title
            title_el = (
                container.find("a", class_=lambda c: c and ("job-title-href" in c or "heading_4_5" in c))
                or container.find("h3")
                or container.find("a", class_=lambda c: c and ("title" in c or "heading" in c))
            )
            title = title_el.get_text(strip=True) if title_el else ""

            # Company
            company_el = container.find(class_=lambda c: c and ("company_name" in c or "company" in str(c).lower()))
            company = company_el.get_text(strip=True) if company_el else ""

            # Location
            loc_el = (
                container.find("a", id=lambda i: i and "location" in str(i).lower())
                or container.find(class_=lambda c: c and "location_link" in str(c))
                or container.find(class_=lambda c: c and "location" in str(c).lower())
            )
            loc_text = loc_el.get_text(strip=True) if loc_el else location

            # Stipend
            stipend_el = container.find("span", class_=lambda c: c and "stipend" in str(c))
            stipend = stipend_el.get_text(strip=True) if stipend_el else ""

            # Link
            link_el = container.find("a", href=True)
            link = ""
            if link_el:
                href = link_el["href"]
                link = href if href.startswith("http") else f"https://internshala.com{href}"

            if not title:
                continue

            listings.append(JobListing(
                title=title,
                company=company,
                location=loc_text,
                source="Internshala",
                url=link,
                stipend=stipend,
            ))
        except Exception:
            continue

    return listings, is_healthy


# ---------------------------------------------------------------------------
# Scraper 2 — LinkedIn Guest
# ---------------------------------------------------------------------------

async def scrape_linkedin_guest(
    session: aiohttp.ClientSession,
    query: str,
    location: str = "India",
) -> Tuple[List[JobListing], bool]:
    """Scrape LinkedIn guest jobs API."""
    await asyncio.sleep(1 + random.random())
    encoded_q = quote_plus(query)
    encoded_loc = quote_plus(location)
    url = (
        f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
        f"?keywords={encoded_q}&location={encoded_loc}&f_TPR=r604800&start=0"
    )
    html = await safe_get(session, url, timeout=15)
    if html is None:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    listings: List[JobListing] = []

    cards = soup.find_all("li") or soup.find_all("div", class_="base-card")

    for card in cards[:15]:
        try:
            title_el = card.find("h3", class_=lambda c: c and "base-search-card__title" in c)
            company_el = card.find("h4", class_=lambda c: c and "base-search-card__subtitle" in c)
            loc_el = card.find("span", class_=lambda c: c and "job-search-card__location" in c)
            link_el = card.find("a", class_=lambda c: c and "base-card__full-link" in c)
            date_el = card.find("time")

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            loc_text = loc_el.get_text(strip=True) if loc_el else location
            link = link_el["href"].split("?")[0] if link_el and link_el.get("href") else ""
            posted = date_el.get("datetime", "") if date_el else ""

            if not title:
                continue

            listings.append(JobListing(
                title=title,
                company=company,
                location=loc_text,
                source="LinkedIn",
                url=link,
                posted_date=posted,
            ))
        except Exception:
            continue

    return listings, True


# ---------------------------------------------------------------------------
# Scraper 3 — Cutshort
# ---------------------------------------------------------------------------

async def scrape_cutshort(
    session: aiohttp.ClientSession,
    query: str,
) -> Tuple[List[JobListing], bool]:
    """Scrape Cutshort for intern listings."""
    encoded = quote_plus(query)
    url = f"https://cutshort.io/jobs?q={encoded}&type=intern"
    html = await safe_get(session, url, timeout=12)
    if html is None:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    listings: List[JobListing] = []

    # Try card-based layout first
    cards = soup.find_all("div", class_=lambda c: c and "job-card" in str(c).lower())
    if not cards:
        cards = soup.find_all("div", class_=lambda c: c and "card" in str(c).lower() and "job" in str(c).lower())

    for card in cards[:10]:
        try:
            title_el = (
                card.find("h3")
                or card.find("a", class_=lambda c: c and ("title" in str(c).lower() or "job-title" in str(c).lower()))
            )
            company_el = card.find(class_=lambda c: c and ("company" in str(c).lower() or "org-name" in str(c).lower()))
            loc_el = card.find(class_=lambda c: c and "location" in str(c).lower())
            link_el = card.find("a", href=lambda h: h and h.startswith("/job/"))

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            loc_text = loc_el.get_text(strip=True) if loc_el else ""
            link = f"https://cutshort.io{link_el['href']}" if link_el else ""

            if not title:
                continue

            listings.append(JobListing(
                title=title,
                company=company,
                location=loc_text,
                source="Cutshort",
                url=link,
            ))
        except Exception:
            continue

    # Fallback: Next.js JSON data
    if not listings:
        next_data = soup.find("script", id="__NEXT_DATA__")
        if next_data:
            try:
                data = json.loads(next_data.string or "")
                jobs = _extract_jobs_recursive(data, max_depth=8)
                for jd in jobs[:10]:
                    title = jd.get("title") or jd.get("role") or ""
                    company = (
                        jd.get("companyName")
                        or (jd.get("company") or {}).get("name", "")
                        or (jd.get("startup") or {}).get("name", "")
                        or ""
                    )
                    slug = jd.get("slug") or jd.get("id") or ""
                    link = f"https://cutshort.io/job/{slug}" if slug else ""
                    if title:
                        listings.append(JobListing(
                            title=title,
                            company=company,
                            location=jd.get("location", ""),
                            source="Cutshort",
                            url=link,
                        ))
            except (json.JSONDecodeError, TypeError):
                pass

        # Also try any embedded JSON blobs
        if not listings:
            for script in soup.find_all("script", type="application/json"):
                try:
                    data = json.loads(script.string or "")
                    jobs = _extract_jobs_recursive(data, max_depth=8)
                    for jd in jobs[:10]:
                        title = jd.get("title") or jd.get("role") or ""
                        if title:
                            listings.append(JobListing(
                                title=title,
                                company=jd.get("companyName", ""),
                                location=jd.get("location", ""),
                                source="Cutshort",
                                url="",
                            ))
                except (json.JSONDecodeError, TypeError):
                    continue

    is_healthy = "cutshort" in html.lower()
    return listings[:10], is_healthy


# ---------------------------------------------------------------------------
# Scraper 4 — Wellfound (AngelList)
# ---------------------------------------------------------------------------

async def scrape_wellfound(
    session: aiohttp.ClientSession,
    query: str,
) -> Tuple[List[JobListing], bool]:
    """Scrape Wellfound for startup intern roles."""
    encoded = quote_plus(query)
    url = f"https://wellfound.com/role/l/intern/{encoded}"
    html = await safe_get(session, url, timeout=15)
    if html is None:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    next_data_tag = soup.find("script", id="__NEXT_DATA__")

    if not next_data_tag:
        return [], "wellfound" in html.lower()

    listings: List[JobListing] = []
    try:
        data = json.loads(next_data_tag.string or "")
        page_props = data.get("props", {}).get("pageProps", {})

        # Try multiple known keys
        job_lists: List[list] = []
        for key in [
            "listings", "jobs", "jobListings",
            "seoLandingPageJobSearchResults", "results",
        ]:
            if key in page_props and isinstance(page_props[key], list):
                job_lists.append(page_props[key])

        # Recursive fallback: find any array of job-like dicts
        if not job_lists:
            found = _extract_jobs_recursive(page_props, max_depth=8)
            if found:
                job_lists.append(found)

        for jlist in job_lists:
            for jd in jlist:
                if not isinstance(jd, dict):
                    continue
                title = jd.get("title") or jd.get("role") or ""
                company = (
                    (jd.get("startup") or {}).get("name", "")
                    or (jd.get("company") or {}).get("name", "")
                    or jd.get("companyName", "")
                    or ""
                )
                loc_text = jd.get("location") or ("Remote" if jd.get("remote") else "")
                slug = jd.get("slug") or jd.get("id") or ""
                link = f"https://wellfound.com/jobs/{slug}" if slug else ""

                if not title:
                    continue

                listings.append(JobListing(
                    title=title,
                    company=company,
                    location=loc_text,
                    source="Wellfound",
                    url=link,
                ))
                if len(listings) >= 10:
                    break
            if len(listings) >= 10:
                break

    except (json.JSONDecodeError, TypeError, KeyError):
        pass

    return listings[:10], True


def _extract_jobs_recursive(data: Any, max_depth: int = 8, _depth: int = 0) -> List[dict]:
    """Recursively traverse a nested structure to find arrays of job-like dicts."""
    if _depth > max_depth:
        return []
    results: List[dict] = []

    if isinstance(data, list) and len(data) > 0:
        # Check if this is an array of job-like objects
        sample = data[0] if isinstance(data[0], dict) else None
        if sample and ("title" in sample or "role" in sample) and (
            "slug" in sample or "company" in sample or "startup" in sample or "companyName" in sample
        ):
            return [d for d in data if isinstance(d, dict)]
        # Recurse into each element
        for item in data:
            results.extend(_extract_jobs_recursive(item, max_depth, _depth + 1))
    elif isinstance(data, dict):
        for value in data.values():
            results.extend(_extract_jobs_recursive(value, max_depth, _depth + 1))

    return results


# ---------------------------------------------------------------------------
# Scraper 5 — DuckDuckGo HTML (lite)
# ---------------------------------------------------------------------------

async def scrape_duckduckgo(
    session: aiohttp.ClientSession,
    query: str,
) -> Tuple[List[JobListing], bool]:
    """Scrape DuckDuckGo lite HTML search for job postings."""
    encoded = quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    html = await safe_get(session, url, timeout=12)
    if html is None:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    listings: List[JobListing] = []

    results = soup.find_all("div", class_=lambda c: c and ("result" in str(c)))
    if not results:
        results = soup.find_all("div", class_="results_links")

    for result in results[:8]:
        try:
            title_el = result.find("a", class_="result__a")
            snippet_el = result.find("a", class_="result__snippet")
            if not snippet_el:
                snippet_el = result.find("td", class_="result__snippet")

            if not title_el:
                continue

            title = title_el.get_text(strip=True)
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            # Extract and decode URL
            href = title_el.get("href", "")
            # DDG lite sometimes wraps URLs
            if "uddg=" in href:
                from urllib.parse import parse_qs, urlparse
                parsed = urlparse(href)
                qs = parse_qs(parsed.query)
                href = qs.get("uddg", [href])[0]

            # Extract domain for company name
            try:
                from urllib.parse import urlparse as _urlparse
                domain = _urlparse(href).netloc
                company = re.sub(r'^www\.', '', domain)
                company = re.sub(r'\.(com|io|co|org|in|net|ai)$', '', company)
                company = company.replace('.', ' ').replace('-', ' ').title()
            except Exception:
                company = ""

            listings.append(JobListing(
                title=title[:120],
                company=company,
                location="",
                source=f"DuckDuckGo → {company}",
                url=href,
                description=snippet[:500],
            ))
        except Exception:
            continue

    return listings[:8], True


# ---------------------------------------------------------------------------
# Scraper 6 — Nitter (Twitter/X mirror)
# ---------------------------------------------------------------------------

_NITTER_MIRRORS = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.net",
]

_HIRING_KEYWORDS = {"hiring", "looking for", "join", "apply", "dm", "intern", "opening", "opportunity"}


async def scrape_nitter(
    session: aiohttp.ClientSession,
    query: str,
) -> Tuple[List[JobListing], bool]:
    """Scrape Nitter mirrors for hiring tweets."""
    encoded = quote_plus(f"{query} hiring intern")
    listings: List[JobListing] = []
    any_healthy = False

    for mirror in _NITTER_MIRRORS:
        url = f"{mirror}/search?q={encoded}&f=tweets"
        html = await safe_get(session, url, timeout=10)
        if html is None:
            continue

        any_healthy = True
        soup = BeautifulSoup(html, "html.parser")

        tweets = soup.find_all("div", class_=lambda c: c and (
            "timeline-item" in str(c) or "tweet-body" in str(c)
        ))

        for tweet in tweets:
            try:
                content_el = tweet.find("div", class_=lambda c: c and (
                    "tweet-content" in str(c) or "tweet-body" in str(c)
                ))
                username_el = tweet.find("a", class_=lambda c: c and "username" in str(c))

                if not content_el:
                    continue

                text = content_el.get_text(strip=True)
                text_lower = text.lower()

                # Only include tweets with hiring-related keywords
                if not any(kw in text_lower for kw in _HIRING_KEYWORDS):
                    continue

                username = username_el.get_text(strip=True) if username_el else "unknown"
                username = username.lstrip("@")

                # Try to find the tweet link
                tweet_link_el = tweet.find("a", href=lambda h: h and "/status/" in str(h))
                if tweet_link_el:
                    tweet_url = f"{mirror}{tweet_link_el['href']}"
                else:
                    tweet_url = f"{mirror}/{username}"

                listings.append(JobListing(
                    title=text[:80],
                    company=username,
                    location="",
                    source="Twitter/X",
                    url=tweet_url,
                    description=text[:500],
                ))

                if len(listings) >= 7:
                    break
            except Exception:
                continue

        if listings:
            break  # Got results from this mirror, no need to try others

    return listings[:7], any_healthy


# ---------------------------------------------------------------------------
# Scraper 7 — Hashjob
# ---------------------------------------------------------------------------

async def scrape_hashjob(
    session: aiohttp.ClientSession,
    query: str,
) -> Tuple[List[JobListing], bool]:
    """Scrape Hashjob for listings."""
    encoded = quote_plus(query)
    url = f"https://hashjob.co/search?q={encoded}"
    html = await safe_get(session, url, timeout=12)
    if html is None:
        return [], False

    soup = BeautifulSoup(html, "html.parser")
    listings: List[JobListing] = []

    cards = soup.find_all(["div", "li"], class_=lambda c: c and any(
        kw in str(c).lower() for kw in ["job", "listing", "card"]
    ))

    for card in cards[:10]:
        try:
            title_el = card.find(["h3", "a"], class_=lambda c: c and "title" in str(c).lower()) or card.find("h3")
            company_el = card.find(class_=lambda c: c and ("company" in str(c).lower() or "org" in str(c).lower()))
            loc_el = card.find(class_=lambda c: c and "location" in str(c).lower())
            link_el = card.find("a", href=True)

            title = title_el.get_text(strip=True) if title_el else ""
            company = company_el.get_text(strip=True) if company_el else ""
            loc_text = loc_el.get_text(strip=True) if loc_el else ""
            link = ""
            if link_el:
                href = link_el["href"]
                link = href if href.startswith("http") else f"https://hashjob.co{href}"

            if not title:
                continue

            listings.append(JobListing(
                title=title,
                company=company,
                location=loc_text,
                source="Hashjob",
                url=link,
            ))
        except Exception:
            continue

    return listings[:10], True


# ---------------------------------------------------------------------------
# Scraper 8 — DuckDuckGo Dork Queries
# ---------------------------------------------------------------------------

_DORK_TEMPLATES = [
    '"founder\'s office" intern {location} site:linkedin.com/jobs',
    '"chief of staff" intern startup {location}',
    '"report to founder" OR "report to CEO" internship {location}',
    '"work directly with founder" internship apply',
    '"founding team" intern startup {location} 2025 2026',
]


async def run_duckduckgo_dorks(
    session: aiohttp.ClientSession,
    base_query: str,
    location: str = "India",
) -> Tuple[List[JobListing], bool]:
    """Run multiple DuckDuckGo dork queries and combine results."""
    all_listings: List[JobListing] = []
    any_healthy = False

    for template in _DORK_TEMPLATES:
        dork_query = template.format(location=location)
        results, healthy = await scrape_duckduckgo(session, dork_query)
        all_listings.extend(results)
        if healthy:
            any_healthy = True
        await asyncio.sleep(1.5 + random.random() * 0.5)

    return all_listings, any_healthy


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def deduplicate(listings: List[JobListing]) -> List[JobListing]:
    """Remove duplicate listings by content hash."""
    seen: set = set()
    unique: List[JobListing] = []
    for job in listings:
        if job.content_hash not in seen:
            seen.add(job.content_hash)
            unique.append(job)
    return unique


# ---------------------------------------------------------------------------
# Heuristic Pre-Filter
# ---------------------------------------------------------------------------

def heuristic_prefilter_score(job: JobListing) -> float:
    """Fast keyword-based relevance score (0-10). Jobs < 2.0 are filtered out."""
    score = 1.0
    text = f"{job.title} {job.description} {job.company}".lower()

    # High signals — cumulative
    for signal in FOUNDER_PROXIMITY_SIGNALS["high"]:
        if signal in text:
            score += 1.5

    # Medium signals — cumulative
    for signal in FOUNDER_PROXIMITY_SIGNALS["medium"]:
        if signal in text:
            score += 0.5

    # Negative signals — cumulative
    for signal in FOUNDER_PROXIMITY_SIGNALS["negative"]:
        if signal in text:
            score -= 1.5

    # Bonus: title contains key phrases
    title_lower = job.title.lower()
    if any(kw in title_lower for kw in ["founder", "ceo", "chief of staff", "founding"]):
        score += 2.0

    # Penalty: clearly irrelevant roles
    if any(kw in title_lower for kw in [
        "seo", "data entry", "telecall", "tele-call", "customer support", "content writer",
    ]):
        score -= 3.0

    return max(0.0, min(10.0, score))


# ---------------------------------------------------------------------------
# Description Enrichment
# ---------------------------------------------------------------------------

async def enrich_descriptions(
    session: aiohttp.ClientSession,
    listings: List[JobListing],
    max_enrich: int = 10,
) -> List[JobListing]:
    """Fetch full job descriptions for top candidates with short/empty descriptions."""
    to_enrich = [
        j for j in listings
        if len(j.description) < 100 and j.url and not j.url.startswith("http://nitter")
    ][:max_enrich]

    async def fetch_desc(job: JobListing) -> None:
        try:
            html = await safe_get(session, job.url, timeout=8)
            if not html:
                return
            soup = BeautifulSoup(html, "lxml")

            # Try common job description containers
            selectors = [
                "div.job-description",
                "div.description",
                "div[class*='description']",
                "div[class*='jd']",
                "section[class*='description']",
                "article",
            ]
            for selector in selectors:
                el = soup.select_one(selector)
                if el and len(el.get_text(strip=True)) > 50:
                    job.description = el.get_text(strip=True)[:1500]
                    return

            # Fallback: largest text block from paragraphs
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs)
            if len(text) > 100:
                job.description = text[:1500]
        except Exception:
            pass

    await asyncio.gather(*[fetch_desc(j) for j in to_enrich])
    return listings


# ---------------------------------------------------------------------------
# AI Scoring — System Prompt
# ---------------------------------------------------------------------------

FPS_SYSTEM_PROMPT = """You are an expert analyst of startup internship opportunities. Analyze job listings and compute a FOUNDER PROXIMITY SCORE (FPS).

FPS measures how closely an intern will work with the actual founder(s).

Score each listing on 5 dimensions (each 0.0 to 1.0):

1. REPORTING CHAIN DISTANCE (RCD, weight 0.35):
   - 1.0: Reports directly to founder/CEO
   - 0.7: Reports to co-founder or CXO in <10 person team
   - 0.4: Reports to a manager but small team with founder access
   - 0.1: Reports to middle management, no founder mention

2. COMPANY FORMATION STAGE (CFS, weight 0.25):
   - 1.0: Pre-product / idea stage / pre-seed
   - 0.8: Seed funded, <15 people
   - 0.6: Series A, 15-50 people
   - 0.3: Series B, 50-200 people
   - 0.1: Series C+ or large company

3. ROLE CENTRALITY (RC, weight 0.20):
   - 1.0: Cross-functional generalist, touches strategy+ops+product
   - 0.7: Strategy/ops role spanning multiple domains
   - 0.4: Single-domain role but with breadth
   - 0.1: Narrowly specialized (only SEO, only data entry)

4. AUTONOMY INDICATORS (AI_DIM, weight 0.10):
   - 1.0: "Own this project", "drive", "lead"
   - 0.5: "Contribute to", "assist with"
   - 0.1: "Support the team", "follow instructions"

5. FOUNDER REFERENCE DENSITY (FRD, weight 0.10):
   - 1.0: Founder/CEO mentioned 3+ times in JD
   - 0.5: Mentioned once
   - 0.1: Not mentioned at all

FPS = (RCD*0.35 + CFS*0.25 + RC*0.20 + AI_DIM*0.10 + FRD*0.10) * 10

For EACH listing, respond with a JSON object. When given multiple listings, respond with a JSON array.

JSON format per listing:
{
  "fps": <float 0-10>,
  "fps_reasoning": "<1-2 sentence explanation>",
  "is_genuine_fo_role": <true/false>,
  "role_summary": "<what the intern will actually do, 1 sentence>",
  "red_flags": ["<flag1>", "<flag2>"],
  "skills_needed": ["<skill1>", "<skill2>", "<skill3>"],
  "confidence": <float 0.0-1.0>,
  "dimensions": {"rcd": <float>, "cfs": <float>, "rc": <float>, "ai_dim": <float>, "frd": <float>}
}

Set confidence < 0.5 if description is missing/very short or JD is vague.
Respond with ONLY valid JSON. No markdown fences, no explanation outside JSON."""


# ---------------------------------------------------------------------------
# Helper: Build AI prompt for a batch of listings
# ---------------------------------------------------------------------------

def _build_batch_prompt(batch: List[JobListing]) -> str:
    """Build the user prompt for a batch of listings to score."""
    parts: List[str] = []
    for idx, job in enumerate(batch, 1):
        desc_preview = job.description[:600] if job.description else "(no description available)"
        parts.append(
            f"--- Listing {idx} ---\n"
            f"Title: {job.title}\n"
            f"Company: {job.company}\n"
            f"Location: {job.location}\n"
            f"Source: {job.source}\n"
            f"Stipend: {job.stipend or 'N/A'}\n"
            f"Description: {desc_preview}\n"
        )
    return "\n".join(parts) + (
        f"\n\nScore ALL {len(batch)} listings above. "
        "Return a JSON array with one object per listing, in order."
    )


def _parse_ai_scores(raw: str) -> List[dict]:
    """Parse the AI's JSON response, tolerating markdown fences."""
    text = raw.strip()
    # Strip markdown code fences if present
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()

    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return parsed
    return []


def _apply_score(job: JobListing, score: dict) -> None:
    """Apply a parsed score dict onto a JobListing."""
    job.fps = float(score.get("fps", 0.0))
    job.fps_reasoning = score.get("fps_reasoning", "")
    job.is_genuine_fo_role = bool(score.get("is_genuine_fo_role", False))
    job.role_summary = score.get("role_summary", "")
    job.red_flags = score.get("red_flags", [])
    job.skills_needed = score.get("skills_needed", [])
    job.confidence = float(score.get("confidence", 0.0))
    dims = score.get("dimensions", {})
    job.fps_dimensions = {
        "rcd": float(dims.get("rcd", 0)),
        "cfs": float(dims.get("cfs", 0)),
        "rc": float(dims.get("rc", 0)),
        "ai_dim": float(dims.get("ai_dim", 0)),
        "frd": float(dims.get("frd", 0)),
    }


# ---------------------------------------------------------------------------
# AI Scoring — Gemini
# ---------------------------------------------------------------------------

async def score_with_gemini_batch(
    listings: List[JobListing],
    api_key: str,
) -> List[JobListing]:
    """Score listings using Google Gemini 2.0 Flash in batches of 4."""
    try:
        import google.generativeai as genai
    except ImportError:
        logger.warning("google-generativeai not installed; skipping Gemini scoring.")
        return listings

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        "gemini-2.0-flash",
        system_instruction=FPS_SYSTEM_PROMPT,
    )

    batch_size = 4
    for i in range(0, len(listings), batch_size):
        batch = listings[i : i + batch_size]
        prompt = _build_batch_prompt(batch)
        try:
            response = model.generate_content(prompt)
            scores = _parse_ai_scores(response.text)
            for job, score in zip(batch, scores):
                _apply_score(job, score)
        except Exception as e:
            logger.warning("Gemini batch scoring failed, falling back to individual: %s", e)
            # Fallback: score individually
            for job in batch:
                try:
                    single_prompt = _build_batch_prompt([job])
                    response = model.generate_content(single_prompt)
                    scores = _parse_ai_scores(response.text)
                    if scores:
                        _apply_score(job, scores[0])
                except Exception as e2:
                    logger.debug("Individual Gemini scoring failed for '%s': %s", job.title, e2)

        if i + batch_size < len(listings):
            await asyncio.sleep(0.5)

    return listings


# ---------------------------------------------------------------------------
# AI Scoring — Groq
# ---------------------------------------------------------------------------

async def score_with_groq_batch(
    listings: List[JobListing],
    api_key: str,
) -> List[JobListing]:
    """Score listings using Groq (LLaMA 3.3 70B) in batches of 3."""
    try:
        from groq import Groq
    except ImportError:
        logger.warning("groq not installed; skipping Groq scoring.")
        return listings

    client = Groq(api_key=api_key)

    batch_size = 3
    for i in range(0, len(listings), batch_size):
        batch = listings[i : i + batch_size]
        prompt = _build_batch_prompt(batch)
        try:
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": FPS_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=2048,
            )
            raw = completion.choices[0].message.content or ""
            scores = _parse_ai_scores(raw)
            for job, score in zip(batch, scores):
                _apply_score(job, score)
        except Exception as e:
            logger.warning("Groq batch scoring failed, falling back to individual: %s", e)
            for job in batch:
                try:
                    single_prompt = _build_batch_prompt([job])
                    completion = client.chat.completions.create(
                        model="llama-3.3-70b-versatile",
                        messages=[
                            {"role": "system", "content": FPS_SYSTEM_PROMPT},
                            {"role": "user", "content": single_prompt},
                        ],
                        temperature=0.1,
                        max_tokens=512,
                    )
                    raw = completion.choices[0].message.content or ""
                    scores = _parse_ai_scores(raw)
                    if scores:
                        _apply_score(job, scores[0])
                except Exception as e2:
                    logger.debug("Individual Groq scoring failed for '%s': %s", job.title, e2)

        if i + batch_size < len(listings):
            await asyncio.sleep(0.4)

    return listings


# ---------------------------------------------------------------------------
# Heuristic FPS Fallback (no AI)
# ---------------------------------------------------------------------------

def heuristic_fps_fallback(listings: List[JobListing]) -> List[JobListing]:
    """Compute rule-based FPS when no AI API is available. Cumulative scoring."""
    for job in listings:
        text = f"{job.title} {job.description} {job.company}".lower()
        score = 1.0

        rcd = 0.1
        cfs = 0.3
        rc = 0.3
        ai_dim = 0.3
        frd = 0.1

        # Reporting Chain Distance
        for signal in FOUNDER_PROXIMITY_SIGNALS["high"]:
            if signal in text:
                rcd = min(1.0, rcd + 0.25)
                score += 1.5

        # Company Formation Stage signals
        stage_signals = {
            "pre-seed": 1.0, "idea stage": 1.0,
            "seed": 0.8, "seed stage": 0.8, "seed funded": 0.8,
            "series a": 0.6, "early stage": 0.7,
            "series b": 0.3, "series c": 0.1,
            "stealth": 0.9, "bootstrapped": 0.8,
        }
        for sig, val in stage_signals.items():
            if sig in text:
                cfs = max(cfs, val)

        # Role Centrality
        generalist_kw = ["generalist", "cross-functional", "wear many hats", "strategy and operations",
                         "special projects", "multiple domains", "jack of all trades"]
        for kw in generalist_kw:
            if kw in text:
                rc = min(1.0, rc + 0.2)

        # Medium signals
        for signal in FOUNDER_PROXIMITY_SIGNALS["medium"]:
            if signal in text:
                score += 0.5

        # Autonomy
        autonomy_kw = ["own this", "take ownership", "drive this", "lead this",
                       "full ownership", "end to end", "autonomous"]
        for kw in autonomy_kw:
            if kw in text:
                ai_dim = min(1.0, ai_dim + 0.2)

        # Founder Reference Density
        founder_mentions = sum(1 for kw in ["founder", "ceo", "co-founder"] if kw in text)
        if founder_mentions >= 3:
            frd = 1.0
        elif founder_mentions >= 1:
            frd = 0.5

        # Negative signals
        for signal in FOUNDER_PROXIMITY_SIGNALS["negative"]:
            if signal in text:
                score -= 1.5

        # Title bonus
        title_lower = job.title.lower()
        if any(kw in title_lower for kw in ["founder", "ceo", "chief of staff", "founding"]):
            score += 2.0

        # Title penalty
        if any(kw in title_lower for kw in [
            "seo", "data entry", "telecall", "tele-call", "customer support", "content writer",
        ]):
            score -= 3.0

        computed_fps = (rcd * 0.35 + cfs * 0.25 + rc * 0.20 + ai_dim * 0.10 + frd * 0.10) * 10
        # Blend heuristic keyword score with dimensional score
        final_fps = max(0.0, min(10.0, (computed_fps + max(0.0, min(10.0, score))) / 2))

        job.fps = round(final_fps, 1)
        job.fps_reasoning = "Scored by keyword analysis (no AI available)"
        job.confidence = 0.4
        job.is_genuine_fo_role = final_fps >= 5.5
        job.fps_dimensions = {
            "rcd": round(rcd, 2),
            "cfs": round(cfs, 2),
            "rc": round(rc, 2),
            "ai_dim": round(ai_dim, 2),
            "frd": round(frd, 2),
        }
        job.role_summary = ""
        job.red_flags = []
        job.skills_needed = []

    return listings


# ---------------------------------------------------------------------------
# Main Search Orchestrator
# ---------------------------------------------------------------------------

async def run_search(
    query: str = "founder's office intern",
    location: str = "India",
    ai_provider: str = "auto",
    max_results: int = 50,
    skip_ai: bool = False,
) -> List[Dict[str, Any]]:
    """
    Main entry-point. Scrapes multiple sources, deduplicates, filters,
    enriches descriptions, and scores with AI (or heuristic fallback).

    Returns a list of dicts sorted by FPS descending.
    """
    # ── 1. API keys ──────────────────────────────────────────────────────
    gemini_key = os.getenv("GEMINI_API_KEY", "")
    groq_key = os.getenv("GROQ_API_KEY", "")

    # ── 2. Query expansion ───────────────────────────────────────────────
    query_variants = expand_query(query)
    logger.info("Query expanded to %d variants: %s", len(query_variants), query_variants)

    # ── 3-4. Run ALL scrapers concurrently ───────────────────────────────
    connector = aiohttp.TCPConnector(limit=20, ssl=False)
    session = aiohttp.ClientSession(connector=connector)

    try:
        # Build task list
        scraper_tasks: List[Tuple[str, Any]] = []

        # Internshala — first 3 query variants
        for qv in query_variants[:3]:
            scraper_tasks.append(
                ("Internshala", scrape_internshala(session, qv, location))
            )

        # LinkedIn guest — first 2 variants
        for qv in query_variants[:2]:
            scraper_tasks.append(
                ("LinkedIn", scrape_linkedin_guest(session, qv, location))
            )

        # Cutshort — original query
        scraper_tasks.append(
            ("Cutshort", scrape_cutshort(session, query))
        )

        # Wellfound — original query
        scraper_tasks.append(
            ("Wellfound", scrape_wellfound(session, query))
        )

        # DuckDuckGo dorks
        scraper_tasks.append(
            ("DDG Dorks", run_duckduckgo_dorks(session, query, location))
        )

        # Nitter
        nitter_query = f"founder hiring intern {location}"
        scraper_tasks.append(
            ("Nitter/Twitter", scrape_nitter(session, nitter_query))
        )

        # Hashjob
        scraper_tasks.append(
            ("Hashjob", scrape_hashjob(session, query))
        )

        # Run all concurrently
        raw_results = await asyncio.gather(
            *[task for _, task in scraper_tasks],
            return_exceptions=True,
        )

        # ── 5-6. Collect results and track health ───────────────────────
        all_listings: List[JobListing] = []
        scraper_health: Dict[str, Dict[str, Any]] = {}

        for (name, _), result in zip(scraper_tasks, raw_results):
            if isinstance(result, Exception):
                logger.warning("Scraper %s raised exception: %s", name, result)
                scraper_health.setdefault(name, {"count": 0, "healthy": False})
                continue

            listings_batch, is_healthy = result
            all_listings.extend(listings_batch)

            existing = scraper_health.get(name, {"count": 0, "healthy": False})
            scraper_health[name] = {
                "count": existing["count"] + len(listings_batch),
                "healthy": existing["healthy"] or is_healthy,
            }

        for name, info in scraper_health.items():
            logger.info(
                "Scraper %-15s → %3d listings (healthy=%s)",
                name, info["count"], info["healthy"],
            )

        logger.info("Total raw listings: %d", len(all_listings))

        # ── 7. Deduplicate ──────────────────────────────────────────────
        all_listings = deduplicate(all_listings)
        logger.info("After dedup: %d listings", len(all_listings))

        # ── 8. Heuristic pre-filter ─────────────────────────────────────
        scored_listings: List[Tuple[float, JobListing]] = []
        for job in all_listings:
            pre_score = heuristic_prefilter_score(job)
            scored_listings.append((pre_score, job))

        scored_listings.sort(key=lambda x: x[0], reverse=True)
        filtered = [job for score, job in scored_listings if score >= 2.0]

        if not filtered:
            # If everything was filtered, keep the best ones anyway
            filtered = [job for _, job in scored_listings[:max_results]]

        logger.info("After pre-filter: %d listings (removed %d)",
                     len(filtered), len(all_listings) - len(filtered))

        # Cap to max_results before expensive operations
        filtered = filtered[:max_results]

        # ── 9. Enrich descriptions ──────────────────────────────────────
        filtered = await enrich_descriptions(session, filtered, max_enrich=10)

    finally:
        await session.close()

    # ── 10. AI scoring ──────────────────────────────────────────────────
    if skip_ai:
        heuristic_fps_fallback(filtered)
    else:
        # Check daily quota
        use_heuristic = False
        try:
            from supabase_client import get_ai_call_count_today, increment_ai_call_count
            daily_count = get_ai_call_count_today()
            daily_limit = 14400
            if daily_count > daily_limit * 0.8:
                logger.warning(
                    "AI quota at %d/%d (%.0f%%), using heuristic fallback.",
                    daily_count, daily_limit, daily_count / daily_limit * 100,
                )
                use_heuristic = True
            else:
                increment_ai_call_count(len(filtered))
        except ImportError:
            logger.debug("supabase_client not available; skipping quota check.")
        except Exception as e:
            logger.debug("Quota check failed: %s", e)

        if use_heuristic:
            heuristic_fps_fallback(filtered)
        elif ai_provider == "gemini" and gemini_key:
            await score_with_gemini_batch(filtered, gemini_key)
        elif ai_provider == "groq" and groq_key:
            await score_with_groq_batch(filtered, groq_key)
        elif ai_provider == "auto":
            if gemini_key:
                await score_with_gemini_batch(filtered, gemini_key)
            elif groq_key:
                await score_with_groq_batch(filtered, groq_key)
            else:
                logger.info("No AI API key found; using heuristic fallback.")
                heuristic_fps_fallback(filtered)
        else:
            heuristic_fps_fallback(filtered)

    # ── 11. Post-AI filter ──────────────────────────────────────────────
    if len(filtered) > 10:
        filtered = [j for j in filtered if j.fps >= 4.0]
        if not filtered:
            # Shouldn't happen, but safety net
            filtered = sorted(all_listings, key=lambda j: j.fps, reverse=True)[:10]

    # ── 12. Sort by FPS descending ──────────────────────────────────────
    filtered.sort(key=lambda j: j.fps, reverse=True)

    # ── 13. Return as dicts ─────────────────────────────────────────────
    return [j.to_dict() for j in filtered]


# ---------------------------------------------------------------------------
# CLI Entry-point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    cli_query = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "founder's office intern"
    cli_location = os.getenv("SEARCH_LOCATION", "India")

    async def _main() -> None:
        results = await run_search(
            query=cli_query,
            location=cli_location,
            ai_provider="auto",
            max_results=50,
            skip_ai=False,
        )
        print(f"\n{'='*70}")
        print(f"  Found {len(results)} listings for: {cli_query}")
        print(f"{'='*70}\n")

        for i, r in enumerate(results, 1):
            genuine = "✅" if r.get("is_genuine_fo_role") else "❌"
            print(f"{i:>3}. [{r['fps']:.1f}/10] {genuine}  {r['title']}")
            print(f"     🏢 {r['company']}  |  📍 {r['location']}  |  📡 {r['source']}")
            if r.get("fps_reasoning"):
                print(f"     💡 {r['fps_reasoning']}")
            if r.get("red_flags"):
                print(f"     🚩 {', '.join(r['red_flags'])}")
            print(f"     🔗 {r['url']}")
            print()

    asyncio.run(_main())