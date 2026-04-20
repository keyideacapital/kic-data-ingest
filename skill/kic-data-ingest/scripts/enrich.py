#!/usr/bin/env python3
"""
kic_ingest/enrich.py — Enrichment agent for URL-only records.

Reads the engine's JSONL output, picks Organisation records that are missing
most fields but have a website URL, fetches the page(s), and uses the Claude
API to extract canonical org fields + named contacts.

Pipeline per URL-only record:
  1. Fetch homepage (with cache, timeout, size cap, User-Agent, robots-respectful)
  2. Classify source type: official site | directory/aggregator | dead
  3. Claude call #1: extract org fields + candidate team/about/contact URLs
  4. If a team/about URL was suggested and is in-domain, fetch + Claude call #2
     to extract named contacts (names, titles, emails, LinkedIn URLs)
  5. Rescore + reassign tier:
       official site + rich extraction      → high
       directory/aggregator site            → medium
       empty/404/dead                       → unchanged (stays low)
  6. Emit:
       out/<stem>.organisations.enriched.jsonl
       out/<stem>.contacts.enriched.jsonl

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/enrich.py out/ --max-records 50 --cost-cap 5.00
    python scripts/enrich.py out/ --dry-run       # no network, shows what WOULD be enriched

Inputs consumed:  out/*.organisations.jsonl
Outputs written:  out/*.organisations.enriched.jsonl
                  out/*.contacts.enriched.jsonl
                  out/enrichment_cache.json     (HTTP + LLM response cache)
                  out/enrichment_report.json    (per-URL result summary)

The writer (write_to_crm.py) should be invoked against the 'enriched' JSONL
files instead of the raw ones when enrichment has run, OR the orchestrator
can merge enriched records back into the base JSONL before writing.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import ssl
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urlreq, parse as urlparse_mod, error as urlerror

HERE = Path(__file__).resolve().parent
REFS_DIR = HERE.parent / "references"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.environ.get("KIC_ENRICH_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_VERSION = "2023-06-01"

# HTTP fetch settings
HTTP_TIMEOUT = 12
HTTP_MAX_BYTES = 500_000          # cap page size to keep prompts bounded
USER_AGENT = "KIC-Ingest-Enrichment/1.0 (+https://keyideacapital.com)"

# Rate limits
FETCH_REQ_INTERVAL = 0.5          # per-domain min interval between fetches
LLM_REQ_INTERVAL   = 0.2          # min interval between Anthropic calls

# Cost controls (Haiku 4.5 pricing as of writing: $1/Mtok in, $5/Mtok out)
# These are used to estimate cumulative cost and halt when --cost-cap is hit.
COST_PER_INPUT_TOKEN  = 1.0 / 1_000_000
COST_PER_OUTPUT_TOKEN = 5.0 / 1_000_000

# ---------------------------------------------------------------------------
# Source-type classifier
# ---------------------------------------------------------------------------
#
# Classifies a URL's domain into one of three buckets, which determines the
# ceiling tier a record can reach after enrichment:
#
#   official    -> can reach high tier (auto-apply) if extraction is rich
#   directory   -> capped at medium tier (review queue) regardless of richness
#   unknown     -> treated as official by default; you lose less by being
#                  slightly permissive here than by capping everything
#
# Directory list kept conservative — only domains that are unambiguously
# aggregators/databases where the info is second-hand.

DIRECTORY_DOMAINS = {
    "crunchbase.com",
    "pitchbook.com",
    "tracxn.com",
    "cbinsights.com",
    "dealroom.co",
    "angellist.com", "wellfound.com",
    "techcrunch.com",
    "bloomberg.com",
    "reuters.com",
    "forbes.com",
    "fortune.com",
    "wikipedia.org",
    "linkedin.com",            # LinkedIn company pages are informative but second-hand
    "zoominfo.com",
    "hunter.io",
    "rocketreach.co",
    "f6s.com",
    "producthunt.com",
}


def classify_source(url: str) -> str:
    """Return one of: 'official', 'directory', 'unknown'."""
    host = extract_host(url)
    if not host:
        return "unknown"
    # Strip subdomain if any — check both the full host and the registered part.
    # Conservative: only classify as directory if an exact domain match exists.
    parts = host.split(".")
    candidates = {host}
    if len(parts) >= 2:
        candidates.add(".".join(parts[-2:]))
    if len(parts) >= 3:
        candidates.add(".".join(parts[-3:]))
    for cand in candidates:
        if cand in DIRECTORY_DOMAINS:
            return "directory"
    # Generic link shorteners or parking pages
    if host in {"bit.ly", "t.co", "goo.gl", "tinyurl.com"}:
        return "unknown"
    return "official"


def extract_host(url: str) -> str | None:
    try:
        u = url if url.lower().startswith(("http://", "https://")) else "http://" + url
        h = urlparse_mod.urlparse(u).hostname or ""
    except ValueError:
        return None
    h = h.lower().strip(".")
    if h.startswith("www."):
        h = h[4:]
    return h or None


# ---------------------------------------------------------------------------
# HTTP fetch with cache + robots.txt respect
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    status: int               # 0 = network error, 1 = robots disallow, HTTP code otherwise
    html: str                 # decoded body (empty on failure)
    final_url: str            # after redirects
    content_type: str
    fetched_at: str
    error: str | None = None
    from_cache: bool = False


class DomainRateLimiter:
    """Per-host minimum interval between fetches."""
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last_by_host: dict[str, float] = {}

    def throttle(self, url: str) -> None:
        host = extract_host(url) or "_"
        last = self._last_by_host.get(host, 0.0)
        dt = time.monotonic() - last
        if dt < self.min_interval:
            time.sleep(self.min_interval - dt)
        self._last_by_host[host] = time.monotonic()


# Very light robots.txt check — fetches once per host, caches, and honours
# a top-level Disallow: / only (the simplest refuse case). We don't parse the
# full spec — if the site wants more granular control they can email.
class RobotsGate:
    def __init__(self):
        self._disallowed: dict[str, bool] = {}

    def _fetch_robots(self, host: str) -> bool:
        try:
            req = urlreq.Request(f"https://{host}/robots.txt",
                                 headers={"User-Agent": USER_AGENT})
            with urlreq.urlopen(req, timeout=5) as resp:
                body = resp.read(10000).decode("utf-8", errors="replace")
        except Exception:
            return False   # no robots.txt = allowed
        # Parse: find any User-agent: * block and check for Disallow: /
        lines = [ln.strip() for ln in body.splitlines()]
        in_star_block = False
        for ln in lines:
            if not ln or ln.startswith("#"):
                continue
            low = ln.lower()
            if low.startswith("user-agent:"):
                in_star_block = low.split(":", 1)[1].strip() == "*"
            elif in_star_block and low.startswith("disallow:"):
                path = low.split(":", 1)[1].strip()
                if path == "/":
                    return True
        return False

    def disallowed(self, url: str) -> bool:
        host = extract_host(url) or ""
        if not host:
            return False
        if host not in self._disallowed:
            self._disallowed[host] = self._fetch_robots(host)
        return self._disallowed[host]


def fetch_url(url: str, cache: dict, limiter: DomainRateLimiter,
              robots: RobotsGate) -> FetchResult:
    """Fetch a URL with caching, rate limiting, and robots-respect."""
    cache_key = _hash_url(url)
    if cache_key in cache:
        entry = cache[cache_key]
        return FetchResult(
            url=url, status=entry["status"], html=entry["html"],
            final_url=entry.get("final_url", url), content_type=entry.get("content_type", ""),
            fetched_at=entry.get("fetched_at", ""), from_cache=True,
        )
    if robots.disallowed(url):
        return FetchResult(url=url, status=1, html="", final_url=url,
                           content_type="", fetched_at="", error="robots disallow")

    limiter.throttle(url)
    try:
        req = urlreq.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5"})
        ctx = ssl.create_default_context()
        with urlreq.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            ctype = resp.headers.get("Content-Type", "").lower()
            final = resp.geturl()
            # Stream-read with byte cap; decode at end.
            body = resp.read(HTTP_MAX_BYTES)
            # Handle gzip if present (urllib doesn't auto-decode)
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                try:
                    body = gzip.decompress(body)
                except OSError:
                    pass
            enc = "utf-8"
            m = re.search(r"charset=([\w\-]+)", ctype)
            if m: enc = m.group(1)
            html = body.decode(enc, errors="replace")
            status = resp.status
    except urlerror.HTTPError as exc:
        result = FetchResult(url=url, status=exc.code, html="", final_url=url,
                             content_type="", fetched_at=datetime.now(timezone.utc).isoformat(),
                             error=f"HTTP {exc.code}")
        _cache_put(cache, cache_key, result)
        return result
    except Exception as exc:
        return FetchResult(url=url, status=0, html="", final_url=url,
                           content_type="", fetched_at=datetime.now(timezone.utc).isoformat(),
                           error=str(exc)[:200])

    result = FetchResult(
        url=url, status=status, html=html, final_url=final,
        content_type=ctype, fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    _cache_put(cache, cache_key, result)
    return result


def _cache_put(cache: dict, key: str, result: FetchResult) -> None:
    cache[key] = {
        "url": result.url,
        "status": result.status,
        "final_url": result.final_url,
        "content_type": result.content_type,
        "fetched_at": result.fetched_at,
        "html": result.html[:HTTP_MAX_BYTES],
        "error": result.error,
    }


def _hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# HTML reduction — keep prompts cheap
# ---------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE  = re.compile(r"<style\b[^>]*>.*?</style>",  re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_LINK_HREF_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                           re.IGNORECASE | re.DOTALL)


def extract_text_snapshot(html: str, base_url: str, *, max_chars: int = 6000) -> tuple[str, list[tuple[str, str]]]:
    """Return (reduced text snapshot, [(link_url, link_text)]).

    The text snapshot is the page stripped of scripts/styles/tags with
    whitespace collapsed. Links are extracted separately because the LLM
    needs them to propose team/about/contact URLs.
    """
    # Extract links BEFORE stripping tags so we keep the href attributes.
    links: list[tuple[str, str]] = []
    for m in _LINK_HREF_RE.finditer(html):
        href = m.group(1).strip()
        text = _WS_RE.sub(" ", _TAG_RE.sub("", m.group(2))).strip()
        abs_url = urlparse_mod.urljoin(base_url, href)
        if abs_url.startswith(("http://", "https://")):
            links.append((abs_url, text[:100]))

    # Strip + collapse for the text content.
    body = _SCRIPT_RE.sub(" ", html)
    body = _STYLE_RE.sub(" ", body)
    body = _COMMENT_RE.sub(" ", body)
    body = _TAG_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "…(truncated)"
    return body, links[:200]


def rank_followup_links(links: list[tuple[str, str]], base_url: str) -> list[str]:
    """Pick the most promising team/about/contact URLs to follow."""
    base_host = extract_host(base_url)
    keywords_strong = [
        "team", "people", "our-team", "about-us", "leadership", "management",
        "founders", "staff", "partners", "who-we-are", "meet",
    ]
    keywords_weak = ["about", "contact", "investors", "advisors", "board"]
    scored: list[tuple[int, str]] = []
    seen = set()
    for url, text in links:
        if url in seen:
            continue
        seen.add(url)
        host = extract_host(url)
        if host and host != base_host:
            continue                  # stay on-domain
        blob = (url + " " + text).lower()
        score = 0
        for kw in keywords_strong:
            if kw in blob: score += 10
        for kw in keywords_weak:
            if kw in blob: score += 3
        if score > 0:
            scored.append((score, url))
    scored.sort(reverse=True)
    return [u for _, u in scored[:3]]


# ---------------------------------------------------------------------------
# Anthropic API client
# ---------------------------------------------------------------------------

class AnthropicClient:
    def __init__(self, api_key: str, *, dry: bool = False):
        self.api_key = api_key
        self.dry = dry
        self._last_call_ts = 0.0
        # Accumulators for cost tracking
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0

    def cost_usd(self) -> float:
        return self.input_tokens * COST_PER_INPUT_TOKEN + self.output_tokens * COST_PER_OUTPUT_TOKEN

    def _throttle(self) -> None:
        dt = time.monotonic() - self._last_call_ts
        if dt < LLM_REQ_INTERVAL:
            time.sleep(LLM_REQ_INTERVAL - dt)
        self._last_call_ts = time.monotonic()

    def extract_json(self, system: str, user: str, max_tokens: int = 1024) -> dict | None:
        """Call Claude with the given prompt, expect a JSON response in a
        code fence or bare. Returns the parsed dict or None on failure."""
        if self.dry:
            return None
        self._throttle()
        body = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = json.dumps(body).encode()
        req = urlreq.Request(
            ANTHROPIC_API_URL, data=data,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urlreq.urlopen(req, timeout=60) as resp:
                resp_body = json.loads(resp.read().decode())
        except urlerror.HTTPError as exc:
            err = exc.read().decode(errors="replace")
            print(f"  ⚠ anthropic HTTP {exc.code}: {err[:200]}", file=sys.stderr)
            return None
        except Exception as exc:
            print(f"  ⚠ anthropic call failed: {exc}", file=sys.stderr)
            return None

        self.calls += 1
        usage = resp_body.get("usage", {})
        self.input_tokens  += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)

        # Extract text content
        text = "".join(
            block.get("text", "") for block in resp_body.get("content", [])
            if block.get("type") == "text"
        )
        return _parse_json_from_text(text)


def _parse_json_from_text(text: str) -> dict | None:
    """Find a JSON object in arbitrary text. Looks for ```json fences first,
    then for the first {...} balanced block."""
    if not text:
        return None
    # ```json ... ```
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    # ``` ... ```
    m = re.search(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    # First balanced {...}
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try: return json.loads(text[start:i+1])
                except json.JSONDecodeError: return None
    return None


# ---------------------------------------------------------------------------
# Extraction prompts
# ---------------------------------------------------------------------------

ORG_EXTRACTION_SYSTEM = """You extract structured information about a company from its web page. You return ONLY a JSON object. Do not include any prose outside the JSON. If a field is not stated on the page, set it to null — do not guess."""

ORG_EXTRACTION_USER_TEMPLATE = """Below is the text and link list from a company's web page. Extract what you can.

URL: {url}
SOURCE CLASSIFICATION: {source_class}   (official = company's own site; directory = aggregator/third-party)

Return a JSON object with these keys:
- org_name: string | null — the full company name
- org_type: string | null — e.g. "VC", "PE", "Bank", "Family Office", "Corporate", "Advisor", "Fund Manager"
- hq_city: string | null
- hq_country: string | null
- sector: string | null — industry/sector focus
- stage_focus: string | null — for investors: "Seed", "Series A", "Growth", etc.
- aum: string | null — assets under management, as stated
- phone: string | null
- general_email: string | null — info@, contact@, etc.
- linkedin_company_url: string | null
- notes: string | null — 1 short sentence of distinguishing context
- followup_urls: string[] — up to 3 in-domain URLs on this page that likely list team members or leadership (look for "Team", "People", "About", "Leadership", "Our Team", etc.)
- extraction_confidence: number between 0 and 1 — how confident you are the page is about this company and the extracted fields are accurate

Page text:
\"\"\"
{body}
\"\"\"

Link list (url → text):
{links}

Return ONLY the JSON."""


CONTACTS_EXTRACTION_SYSTEM = """You extract named people from a company's team/about page. Return ONLY a JSON object with a 'contacts' array. Include a person ONLY if their name AND title both appear clearly on the page. Do not fabricate emails or LinkedIn URLs — only include them if they appear verbatim on the page."""

CONTACTS_EXTRACTION_USER_TEMPLATE = """Below is the text from {url} (believed to be a team/leadership page for the company "{org_name}").

Return a JSON object:
{{
  "contacts": [
    {{
      "full_name": "...",
      "job_title": "...",
      "email": "..." or null,
      "linkedin_profile_url": "..." or null,
      "extraction_confidence": 0.0 to 1.0
    }},
    ...
  ]
}}

Rules:
- Include a person only if both name and job title are clearly on the page.
- Only include an email if it appears literally on the page (never infer from a domain + name).
- Only include a LinkedIn URL if it appears as a link on the page.
- extraction_confidence: 1.0 if the person is on a dedicated team page with their title adjacent; 0.7 if inferred from context (e.g. a blog byline); 0.5 if uncertain.
- Max 20 people. Skip advisory boards and portfolio company founders — only the org's own staff.

Page text:
\"\"\"
{body}
\"\"\"

Return ONLY the JSON."""


# ---------------------------------------------------------------------------
# Enrichment orchestration
# ---------------------------------------------------------------------------

@dataclass
class EnrichmentOutput:
    url: str
    source_class: str
    status: str                # enriched | skipped_directory | fetch_failed | no_extraction | dry
    org_fields: dict[str, Any] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    followups_fetched: list[str] = field(default_factory=list)
    raw_fetch_status: int = 0
    cost_usd: float = 0.0
    notes: str = ""


def _is_url_only_record(org: dict[str, Any]) -> bool:
    """True if this record has a website but no real org_name (URL-derived).
    These are the dedicated enrichment candidates."""
    website = org.get("website") or ""
    name = org.get("org_name") or ""
    if not website:
        return False
    # Heuristic: the engine uses the domain as org_name for URL-dump records.
    # After normalisation, "purposeventurecapital.com" vs "Purpose Venture Capital"
    # is distinguishable: URL-derived names contain a TLD segment.
    domain = extract_host(website)
    if not domain:
        return False
    # If the org_name looks like the domain, it was URL-derived.
    name_norm = name.lower().strip().replace(" ", "")
    if name_norm == domain.lower().replace(".", ""):
        return True
    # Fallback: a name with a dot in it is almost certainly a URL fragment.
    if "." in name and name.count(" ") == 0:
        return True
    return False


def enrich_record(
    org: dict[str, Any],
    client: AnthropicClient,
    fetch_cache: dict,
    limiter: DomainRateLimiter,
    robots: RobotsGate,
    *,
    max_contacts_pages: int = 1,
) -> EnrichmentOutput:
    """Run the full enrichment pipeline for a single URL-only org record."""
    url = org.get("website", "").strip()
    out = EnrichmentOutput(url=url, source_class=classify_source(url), status="no_extraction")

    # Fast path for dry-run: skip ALL network (including robots.txt probes)
    # and just report what we would have done.
    if client.dry:
        if out.source_class == "directory":
            out.status = "dry_skip_directory"
            out.notes = "directory source — would cap at medium tier"
        else:
            out.status = "dry"
            out.notes = f"would fetch, extract org fields, follow up to {max_contacts_pages} team page"
        return out

    # Fetch homepage
    r = fetch_url(url, fetch_cache, limiter, robots)
    out.raw_fetch_status = r.status
    if r.status == 1:
        out.status = "skipped_robots"
        return out
    if r.status == 0 or r.status >= 400 or not r.html.strip():
        out.status = "fetch_failed"
        out.notes = r.error or f"status={r.status}"
        return out
    if "text/html" not in (r.content_type or "text/html"):
        out.status = "fetch_failed"
        out.notes = f"non-html content: {r.content_type}"
        return out

    text, links = extract_text_snapshot(r.html, r.final_url)
    if len(text) < 50:
        out.status = "fetch_failed"
        out.notes = "page body too small"
        return out

    # Stage 1: org extraction
    link_blob = "\n".join(f"- {u} → {t}" for u, t in links[:60])
    prompt = ORG_EXTRACTION_USER_TEMPLATE.format(
        url=r.final_url, source_class=out.source_class, body=text, links=link_blob,
    )
    org_data = client.extract_json(ORG_EXTRACTION_SYSTEM, prompt, max_tokens=700)
    if org_data is None:
        out.status = "no_extraction"
        return out

    out.org_fields = {k: v for k, v in org_data.items()
                      if k in {"org_name", "org_type", "hq_city", "hq_country", "sector",
                                "stage_focus", "aum", "phone", "general_email",
                                "linkedin_company_url", "notes", "extraction_confidence"}
                      and v not in (None, "")}

    # Stage 2: follow up to N pages for contacts
    followup_urls = org_data.get("followup_urls") or []
    if not followup_urls:
        # Fall back to heuristic ranking if the LLM didn't propose any.
        followup_urls = rank_followup_links(links, r.final_url)
    followup_urls = followup_urls[:max_contacts_pages]

    for fu in followup_urls:
        fr = fetch_url(fu, fetch_cache, limiter, robots)
        if fr.status >= 400 or not fr.html.strip():
            continue
        f_text, _ = extract_text_snapshot(fr.html, fr.final_url)
        if len(f_text) < 80:
            continue
        ct_prompt = CONTACTS_EXTRACTION_USER_TEMPLATE.format(
            url=fr.final_url, org_name=out.org_fields.get("org_name") or "",
            body=f_text,
        )
        ct_data = client.extract_json(CONTACTS_EXTRACTION_SYSTEM, ct_prompt, max_tokens=1200)
        out.followups_fetched.append(fu)
        if ct_data and isinstance(ct_data.get("contacts"), list):
            for c in ct_data["contacts"][:20]:
                if c.get("full_name") and c.get("job_title"):
                    out.contacts.append({
                        "full_name": c["full_name"].strip(),
                        "job_title": c["job_title"].strip(),
                        "email": (c.get("email") or "").strip() or None,
                        "linkedin_profile_url": (c.get("linkedin_profile_url") or "").strip() or None,
                        "extraction_confidence": float(c.get("extraction_confidence") or 0.6),
                        "source_page_url": fr.final_url,
                    })

    out.status = "enriched" if out.org_fields else "no_extraction"
    return out


# ---------------------------------------------------------------------------
# Merge enriched data back into canonical records
# ---------------------------------------------------------------------------

def _tier_for(conf: float) -> str:
    if conf >= 0.85: return "high"
    if conf >= 0.60: return "medium"
    return "low"


def merge_into_org(base: dict, enrichment: EnrichmentOutput) -> dict:
    """Return a new org record with enrichment merged in, tier reassigned."""
    if enrichment.status != "enriched" or not enrichment.org_fields:
        return base

    merged = dict(base)
    # Fill empty fields from enrichment; don't overwrite existing data.
    for k, v in enrichment.org_fields.items():
        if v and not merged.get(k):
            merged[k] = v
    # Always overwrite name if the engine had a URL-derived placeholder.
    if enrichment.org_fields.get("org_name") and _is_url_only_record(base):
        merged["org_name"] = enrichment.org_fields["org_name"]
        # Re-derive normalised name downstream rather than here — keep this module focused.

    # Reassign tier based on source class + extraction confidence.
    # Rules (per your design answer):
    #   official  + conf >= 0.8  → high
    #   official  + conf >= 0.5  → medium
    #   directory (any conf)     → medium max
    #   unknown                  → same as official, capped at medium if conf < 0.5
    extract_conf = float(enrichment.org_fields.get("extraction_confidence") or 0.6)
    if enrichment.source_class == "directory":
        new_conf = min(0.80, 0.55 + 0.25 * extract_conf)   # caps at medium
    elif enrichment.source_class == "official":
        new_conf = 0.60 + 0.35 * extract_conf              # can reach ~0.95
    else:
        new_conf = 0.55 + 0.30 * extract_conf              # unknown: conservative
    # Only raise confidence — never downgrade a record that the engine already scored higher.
    new_conf = max(new_conf, float(base.get("ingestion_confidence") or 0.0))
    merged["ingestion_confidence"] = round(new_conf, 3)
    merged["ingestion_tier"] = _tier_for(new_conf)

    # Provenance: append the enrichment URL + source class to source_ref.
    merged["source_ref"] = (
        (merged.get("source_ref") or "") +
        f"|enriched:{enrichment.source_class}:{enrichment.url}"
    )
    merged["enriched_at"] = datetime.now(timezone.utc).isoformat()
    merged["enrichment_source_class"] = enrichment.source_class
    return merged


def build_enriched_contacts(base_org: dict, enrichment: EnrichmentOutput) -> list[dict]:
    """Turn extracted contacts into canonical Contact records."""
    contacts: list[dict] = []
    base_source_ref = base_org.get("source_ref", "")
    org_name = enrichment.org_fields.get("org_name") or base_org.get("org_name", "")
    for c in enrichment.contacts:
        extract_conf = float(c.get("extraction_confidence") or 0.6)
        # Contact confidence = extraction_confidence × (source_class quality factor)
        class_factor = 1.0 if enrichment.source_class == "official" else 0.80
        conf = round(extract_conf * class_factor, 3)
        # Per design: extraction_confidence < 0.7 always lands in review queue.
        if extract_conf < 0.7:
            conf = min(conf, 0.79)    # ensure below high threshold

        tier = _tier_for(conf)
        full_name = c["full_name"]
        parts = full_name.split()
        record = {
            "full_name": full_name,
            "first_name": parts[0] if parts else "",
            "last_name": parts[-1] if len(parts) > 1 else "",
            "job_title": c["job_title"],
            "org_name": org_name,
            "source_ref": f"{base_source_ref}|enriched-contact:{c.get('source_page_url', '')}",
            "source_region": "enrichment",
            "ingestion_confidence": conf,
            "ingestion_tier": tier,
            "enriched_at": datetime.now(timezone.utc).isoformat(),
            "enrichment_source_class": enrichment.source_class,
            "extraction_confidence": extract_conf,
        }
        if c.get("email"): record["email"] = c["email"]
        if c.get("linkedin_profile_url"): record["linkedin_profile_url"] = c["linkedin_profile_url"]
        contacts.append(record)
    return contacts


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_jsonl(p: Path) -> list[dict]:
    out = []
    if not p.exists(): return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line: continue
        out.append(json.loads(line))
    return out


def write_jsonl(p: Path, records: list[dict]) -> None:
    with p.open("w") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run(out_dir: Path, *, max_records: int, cost_cap: float | None, dry: bool,
        api_key: str | None) -> int:
    cache_path = out_dir / "enrichment_cache.json"
    report_path = out_dir / "enrichment_report.json"

    # Load HTTP cache
    fetch_cache: dict = {}
    if cache_path.exists():
        try:
            fetch_cache = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass

    if not dry and not api_key:
        print("ERROR: --dry-run not set and ANTHROPIC_API_KEY env is not set.", file=sys.stderr)
        return 2

    client = AnthropicClient(api_key or "dry", dry=dry)
    limiter = DomainRateLimiter(FETCH_REQ_INTERVAL)
    robots = RobotsGate()

    # Walk every *.organisations.jsonl in out_dir; pick URL-only records.
    results: list[EnrichmentOutput] = []
    enriched_by_file: dict[Path, list[dict]] = {}
    new_contacts_by_file: dict[Path, list[dict]] = {}
    total_candidates = 0
    total_enriched = 0

    for orgs_path in sorted(out_dir.glob("*.organisations.jsonl")):
        if ".enriched." in orgs_path.name:
            continue   # don't enrich the enrichment output
        base_records = load_jsonl(orgs_path)
        contacts_path = orgs_path.with_name(orgs_path.name.replace(".organisations.", ".contacts."))
        base_contacts = load_jsonl(contacts_path)

        enriched_orgs: list[dict] = []
        new_contacts: list[dict] = []
        file_candidates = [r for r in base_records if _is_url_only_record(r)]
        total_candidates += len(file_candidates)

        # Cost cap gate
        processed_this_file = 0
        attempted_this_corpus = len(results)
        for record in base_records:
            if not _is_url_only_record(record):
                enriched_orgs.append(record)
                continue
            # Hit max_records across all files? (applies in dry-run too)
            if len(results) >= max_records:
                enriched_orgs.append(record)
                continue
            # Hit cost cap?
            if cost_cap is not None and client.cost_usd() >= cost_cap:
                enriched_orgs.append(record)
                continue

            enrichment = enrich_record(record, client, fetch_cache, limiter, robots)
            results.append(enrichment)
            if enrichment.status == "enriched":
                total_enriched += 1
                merged = merge_into_org(record, enrichment)
                enriched_orgs.append(merged)
                new_contacts.extend(build_enriched_contacts(merged, enrichment))
            else:
                enriched_orgs.append(record)
            processed_this_file += 1

        # Write per-file enriched output
        out_orgs_path = orgs_path.with_name(orgs_path.stem + ".enriched.jsonl")
        out_contacts_path = contacts_path.with_name(contacts_path.stem + ".enriched.jsonl")
        write_jsonl(out_orgs_path, enriched_orgs)
        write_jsonl(out_contacts_path, base_contacts + new_contacts)
        enriched_by_file[orgs_path] = enriched_orgs
        new_contacts_by_file[contacts_path] = new_contacts

        print(f"  {orgs_path.name}: {file_candidates.__len__()} candidates, {processed_this_file} attempted this run")

    # Persist cache
    cache_path.write_text(json.dumps(fetch_cache, ensure_ascii=False))

    # Report
    by_status = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    report = {
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "model": ANTHROPIC_MODEL,
        "total_candidates_in_corpus": total_candidates,
        "attempted_this_run": len(results),
        "enriched_successfully": total_enriched,
        "new_contacts_created": sum(len(v) for v in new_contacts_by_file.values()),
        "by_status": by_status,
        "llm_calls": client.calls,
        "llm_input_tokens": client.input_tokens,
        "llm_output_tokens": client.output_tokens,
        "estimated_cost_usd": round(client.cost_usd(), 4),
        "per_url_results": [
            {
                "url": r.url, "source_class": r.source_class, "status": r.status,
                "fetch_status": r.raw_fetch_status, "org_fields_extracted": len(r.org_fields),
                "contacts_found": len(r.contacts), "notes": r.notes,
            }
            for r in results
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print(f"\n✓ Enrichment complete.")
    print(f"  attempted: {len(results)}  enriched: {total_enriched}  contacts added: {report['new_contacts_created']}")
    print(f"  status breakdown: {by_status}")
    if not dry:
        print(f"  LLM: {client.calls} calls, ~{client.input_tokens+client.output_tokens:,} tokens, ~${client.cost_usd():.4f}")
    print(f"  report: {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KIC ingest enrichment agent")
    ap.add_argument("out_dir", help="Engine output directory (contains *.organisations.jsonl)")
    ap.add_argument("--max-records", type=int, default=50,
                    help="Max URL-only records to enrich in this run")
    ap.add_argument("--cost-cap", type=float, default=None,
                    help="Stop once estimated cost exceeds this many USD")
    ap.add_argument("--dry-run", action="store_true",
                    help="No network calls — show candidates and classification only")
    ap.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY"))
    args = ap.parse_args(argv)
    return run(Path(args.out_dir), max_records=args.max_records, cost_cap=args.cost_cap,
               dry=args.dry_run, api_key=args.api_key)


if __name__ == "__main__":
    sys.exit(main())
