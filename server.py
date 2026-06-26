#!/usr/bin/env python3
"""Publieke HTTP MCP-server voor de Gentse Feesten 2026.

Geeft AI-assistenten toegang tot alle festivaldata zodat ze suggesties
kunnen doen over wat te doen tijdens de Gentse Feesten.

Run:
  python server.py

De server gebruikt uitsluitend Streamable HTTP. De standaard-URL is:
  http://127.0.0.1:8000/mcp
"""
import json
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import TypedDict, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse

PROJECT_DIR = Path(__file__).resolve().parent


def _data_dir() -> Path:
    configured = os.getenv("GF_MCP_DATA_DIR")
    if configured:
        return Path(configured).expanduser()

    candidates = (
        PROJECT_DIR.parent / "site" / "data",
        PROJECT_DIR.parent / "gf2026" / "site" / "data",
    )
    for candidate in candidates:
        if (candidate / "event_pages.json").is_file():
            return candidate
    return candidates[0]


DATA_DIR = _data_dir()


class EventOccurrence(TypedDict):
    start: str
    end: str
    day: str
    time: str
    time_end: str
    location: str
    weekday: str


class EventOffer(TypedDict):
    price: str
    currency: str
    desc: str


class EventContact(TypedDict):
    email: str
    tel: str
    url: str


class EventVideo(TypedDict):
    embed: str
    thumb: str
    caption: str


class EventDetail(TypedDict):
    uuid: str
    name: str
    desc: str
    themes: list[str]
    organizers: list[str]
    location: str
    street: str
    postal: str
    city: str
    lat: float | None
    lon: float | None
    free: bool
    age: str
    genre: str
    url: str
    image: str
    occurrences: list[EventOccurrence]
    count: int
    first: str
    offers: list[EventOffer]
    contacts: list[EventContact]
    keywords: list[str]
    duration: str
    frequency: str
    languages: list[str]
    wheelchair_ok: bool
    outdoor: bool
    videos: list[EventVideo]
    image_caption: str
    image_copyright: str


class EventDetailError(TypedDict):
    error: str


class BatchSearchResult(TypedDict):
    query: str
    events: list[dict]


class BatchEventDetailResult(TypedDict):
    uuid: str
    event: EventDetail | None
    error: str | None


# Laad alles éénmalig bij opstarten
pages: list[dict] = json.loads((DATA_DIR / "event_pages.json").read_text(encoding="utf-8"))
events: list[dict] = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
themes_data: list[dict] = json.loads((DATA_DIR / "themes.json").read_text(encoding="utf-8"))
locations_data: list[dict] = json.loads((DATA_DIR / "locations.json").read_text(encoding="utf-8"))
days_data: list[dict] = json.loads((DATA_DIR / "days.json").read_text(encoding="utf-8"))

# Index voor snelle uuid-lookup
_pages_by_uuid: dict[str, dict] = {p["uuid"]: p for p in pages}

# Dag-lookups
NL_DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
_iso_to_nl: dict[str, str] = {}
_nl_to_iso: dict[str, str] = {}
for d in days_data:
    iso = d["day"]
    wd = NL_DAYS[date.fromisoformat(iso).weekday()]
    _iso_to_nl[iso] = wd
    _nl_to_iso[wd] = iso

# Alle muziekgenres
_all_genres = sorted({e["genre"] for e in events if e.get("genre")})

# Trefwoorden/tags, hoofdletterongevoelig samengevoegd. Bewaar als label de
# schrijfwijze die het vaakst in de brondata voorkomt.
_keyword_counts: Counter[str] = Counter()
_keyword_labels: dict[str, Counter[str]] = {}
for p in pages:
    for keyword in p.get("keywords") or []:
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        label = keyword.strip()
        normalized = label.casefold()
        _keyword_counts[normalized] += 1
        _keyword_labels.setdefault(normalized, Counter())[label] += 1

tags_data: list[dict] = [
    {
        "name": _keyword_labels[tag].most_common(1)[0][0],
        "count": count,
    }
    for tag, count in sorted(
        _keyword_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )
]
TAG_RESOURCE_LIMIT = 50

HOST = os.getenv("GF_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("GF_MCP_PORT", "8000"))

mcp = FastMCP(
    "Gentse Feesten 2026",
    host=HOST,
    port=PORT,
    stateless_http=True,
    json_response=True,
)

READ_ONLY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=False,
    destructiveHint=False,
)

OPENAI_APPS_CHALLENGE_TOKEN = "v_bO2xepAywAqMN84rtXBGVESnwXSQS-CkGeEJjI7Lw"

LEGAL_PAGE_STYLE = """
body {
    color: #1f2937;
    font: 16px/1.6 system-ui, sans-serif;
    margin: 0 auto;
    max-width: 760px;
    padding: 2rem 1.25rem 4rem;
}
h1, h2 { color: #111827; line-height: 1.25; }
a { color: #075985; }
.updated { color: #4b5563; }
"""


@mcp.custom_route(
    "/.well-known/openai-apps-challenge",
    methods=["GET"],
    include_in_schema=False,
)
async def openai_apps_challenge(_: Request) -> PlainTextResponse:
    return PlainTextResponse(OPENAI_APPS_CHALLENGE_TOKEN)


def _legal_page(title: str, content: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} — Gentse Feesten 2026</title>
  <style>{LEGAL_PAGE_STYLE}</style>
</head>
<body>
  <main>
    <h1>{title}</h1>
    <p class="updated">Last updated: June 24, 2026</p>
    {content}
  </main>
</body>
</html>"""
    )


@mcp.custom_route("/privacy", methods=["GET"], include_in_schema=False)
async def privacy_policy(_: Request) -> HTMLResponse:
    return _legal_page(
        "Privacy Policy",
        """
<p>Gentse Feesten 2026 provides read-only access to public festival
information through an MCP server.</p>
<h2>Information we process</h2>
<p>The service does not require an account and does not intentionally request
or store names, email addresses, payment information, credentials, or other
personal information. Requests may contain search terms and preferences that
are used only to return relevant festival information.</p>
<h2>Technical logs</h2>
<p>The hosting provider or reverse proxy may temporarily process standard
technical information such as IP addresses, timestamps, requested paths, and
user-agent data for security, reliability, rate limiting, and troubleshooting.
These logs are not used for advertising or profiling.</p>
<h2>Data sharing and retention</h2>
<p>The app does not sell personal information. Technical data may be processed
by infrastructure providers solely to operate and secure the service, and may
be retained according to their operational policies or legal requirements.</p>
<h2>Third-party links</h2>
<p>Festival records may contain links to organizers, ticket providers, or
other websites. Their privacy practices are governed by their own policies.</p>
<h2>Contact</h2>
<p>Questions about this policy can be submitted through the
<a href="https://github.com/HansF/gf2026-mcp/issues">project issue tracker</a>.</p>
""",
    )


@mcp.custom_route("/terms", methods=["GET"], include_in_schema=False)
async def terms_of_service(_: Request) -> HTMLResponse:
    return _legal_page(
        "Terms of Service",
        """
<p>By using the Gentse Feesten 2026 MCP service, you agree to these terms.</p>
<h2>Service scope</h2>
<p>The service provides read-only search, recommendations, and event details
from public Gentse Feesten 2026 program data. It does not sell tickets, make
reservations, process payments, or act on behalf of event organizers.</p>
<h2>Accuracy and availability</h2>
<p>Program details may change or contain errors. Verify important information,
including schedules, prices, accessibility, and availability, with the event
organizer or official festival source. The service may be changed, suspended,
or discontinued without notice.</p>
<h2>Acceptable use</h2>
<p>Do not misuse the service, attempt unauthorized access, disrupt its
operation, evade rate limits, or use it in violation of applicable law or
third-party rights.</p>
<h2>Disclaimer</h2>
<p>The service is provided “as is” and “as available,” without warranties of
accuracy, availability, fitness for a particular purpose, or non-infringement,
to the extent permitted by law.</p>
<h2>Limitation of liability</h2>
<p>To the extent permitted by law, the service operator is not liable for
indirect, incidental, special, consequential, or reliance-based losses arising
from use of the service or third-party event information.</p>
<h2>Contact</h2>
<p>Questions about these terms can be submitted through the
<a href="https://github.com/HansF/gf2026-mcp/issues">project issue tracker</a>.</p>
""",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_day(s: str) -> str | None:
    """'vrijdag' of '2026-07-17' -> ISO-datum, of None."""
    if not s:
        return None
    s = s.strip().lower()
    if s in _nl_to_iso:
        return _nl_to_iso[s]
    # accept partial ISO match
    for iso in _iso_to_nl:
        if iso.startswith(s) or s == iso:
            return iso
    return None


def _short(p: dict) -> dict:
    occ = p.get("occurrences") or []
    days_list = sorted({o["day"] for o in occ})
    return {
        "uuid": p["uuid"],
        "name": p["name"],
        "days": days_list,
        "first_weekday": _iso_to_nl.get(days_list[0], "") if days_list else "",
        "first_time": occ[0]["time"] if occ else "",
        "location": p.get("location", ""),
        "free": p.get("free", False),
        "outdoor": p.get("outdoor", False),
        "themes": p.get("themes", []),
    }


def _filter_pages(
    query: str = "",
    day: str = "",
    theme: str = "",
    free_only: bool = False,
    outdoor_only: bool = False,
    genre: str = "",
    wheelchair: bool = False,
) -> list[dict]:
    iso_day = _resolve_day(day) if day else None
    q = query.strip().lower()
    th = theme.strip().lower()
    ge = genre.strip().lower()
    result = []
    for p in pages:
        if free_only and not p.get("free"):
            continue
        if outdoor_only and not p.get("outdoor"):
            continue
        if wheelchair and not p.get("wheelchair_ok"):
            continue
        if iso_day:
            days_in_p = {o["day"] for o in (p.get("occurrences") or [])}
            if iso_day not in days_in_p:
                continue
        if th and not any(th in t.lower() for t in (p.get("themes") or [])):
            # also check keywords
            if not any(th in k.lower() for k in (p.get("keywords") or [])):
                continue
        if ge and (p.get("genre") or "").lower() != ge:
            continue
        if q:
            haystack = " ".join([
                p.get("name") or "",
                p.get("desc") or "",
                " ".join(p.get("keywords") or []),
                " ".join(p.get("themes") or []),
                p.get("location") or "",
            ]).lower()
            if q not in haystack:
                continue
        result.append(p)
    return result


def _score_highlight(p: dict) -> tuple:
    """Hogere score = beter voor free_highlights."""
    return (
        1 if p.get("outdoor") else 0,
        1 if p.get("desc") else 0,
        p.get("count", 0),
        p.get("name", ""),
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_events(
    query: str = "",
    day: str = "",
    theme: str = "",
    free_only: bool = False,
    outdoor_only: bool = False,
    genre: str = "",
    wheelchair: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Search events by text, day, theme, genre, free/outdoor/wheelchair filters."""
    hits = _filter_pages(query, day, theme, free_only, outdoor_only, genre, wheelchair)
    return [_short(p) for p in hits[:limit]]


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_events_batch(
    queries: list[str],
    day: str = "",
    theme: str = "",
    free_only: bool = False,
    outdoor_only: bool = False,
    genre: str = "",
    wheelchair: bool = False,
    limit_per_query: int = 10,
) -> list[BatchSearchResult]:
    """Search multiple queries at once with shared filters. Each query returns its own result list."""
    clean_queries = [query.strip() for query in queries if query.strip()][:20]
    safe_limit = max(1, min(limit_per_query, 50))
    result: list[BatchSearchResult] = []
    for query in clean_queries:
        hits = _filter_pages(
            query,
            day,
            theme,
            free_only,
            outdoor_only,
            genre,
            wheelchair,
        )
        result.append({
            "query": query,
            "events": [_short(p) for p in hits[:safe_limit]],
        })
    return result


_LIGHT_EXCLUDE = {"image_caption", "image_copyright"}


def _event_detail(uuid: str) -> EventDetail | EventDetailError:
    clean_uuid = uuid.strip()
    p = _pages_by_uuid.get(clean_uuid)
    if not p:
        return {"error": f"unknown uuid '{uuid}'"}
    result = {k: v for k, v in p.items() if k not in _LIGHT_EXCLUDE}
    result["occurrences"] = [
        {**o, "weekday": _iso_to_nl.get(o.get("day", ""), "")}
        for o in (p.get("occurrences") or [])
    ]
    return cast(EventDetail, result)


def _event_summary(uuid: str) -> dict:
    clean_uuid = uuid.strip()
    p = _pages_by_uuid.get(clean_uuid)
    if not p:
        return {"error": f"unknown uuid '{uuid}'"}
    exclude = _LIGHT_EXCLUDE | {"image", "videos"}
    result = {k: v for k, v in p.items() if k not in exclude}
    result["occurrences"] = [
        {**o, "weekday": _iso_to_nl.get(o.get("day", ""), "")}
        for o in (p.get("occurrences") or [])
    ]
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_event_detail(uuid: str, detail: str = "full") -> dict:
    """Get event detail by UUID. detail='full' includes videos/images; summary excludes them."""
    if detail == "summary":
        return _event_summary(uuid)
    return _event_detail(uuid)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_event_details(uuids: list[str], detail: str = "summary") -> list[BatchEventDetailResult]:
    """Fetch details for up to 50 events. Preserves order. detail='full' for images/videos."""
    result: list[BatchEventDetailResult] = []
    for uuid in uuids[:50]:
        fn = _event_detail if detail == "full" else _event_summary
        d = fn(uuid)
        if "error" in d:
            result.append({"uuid": uuid, "event": None, "error": d["error"]})
        else:
            result.append({"uuid": uuid, "event": d, "error": None})
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def suggest(mood: str) -> list[dict]:
    """Get recommendations from a natural-language description of what you want.

    Examples: 'gratis jazz buiten zaterdag', 'iets voor kinderen', 'rolstoeltoegankelijk comedy'
    """
    m = mood.lower()

    # Parseer mood naar filters
    day = ""
    for nl_day in NL_DAYS:
        if nl_day in m:
            day = nl_day
            break

    free_only = any(w in m for w in ["gratis", "free", "kosteloos"])
    outdoor_only = any(w in m for w in ["buiten", "outdoor", "openlucht", "open lucht"])
    wheelchair = any(w in m for w in ["rolstoel", "toegankelijk", "wheelchair"])

    # Thema-hints
    theme = ""
    theme_hints = [
        ("kinder", ["kinder", "gezin", "familie", "kids", "jeugd"]),
        ("dans", ["dans", "dansen", "bal", "tango", "swing", "salsa"]),
        ("theater", ["theater", "toneel", "voorstelling"]),
        ("comedy", ["comedy", "stand-up", "humor", "grappig", "lachen"]),
        ("circus", ["circus", "acrobat", "straattheater", "straat"]),
        ("jazz", ["jazz"]),
        ("boot", ["boot", "boot", "rondvaart", "water"]),
        ("wandeling", ["wandeling", "wandel", "rondleiding"]),
        ("expo", ["museum", "expo", "tentoonstelling", "bezoek"]),
        ("markt", ["markt"]),
        ("vertel", ["verhaal", "lezing", "poëzie", "poetry"]),
    ]
    for hint, keywords in theme_hints:
        if any(kw in m for kw in keywords):
            theme = hint
            break

    # Genre-hints
    genre = ""
    for g in _all_genres:
        if g.lower() in m:
            genre = g
            break

    hits = _filter_pages(
        query="",
        day=day,
        theme=theme,
        free_only=free_only,
        outdoor_only=outdoor_only,
        genre=genre,
        wheelchair=wheelchair,
    )

    # Als te weinig resultaten: versoepel outdoor en free
    if len(hits) < 3 and (free_only or outdoor_only):
        hits = _filter_pages(
            query="",
            day=day,
            theme=theme,
            free_only=False,
            outdoor_only=False,
            genre=genre,
            wheelchair=wheelchair,
        )

    # Sorteer: buiten-bonus, heeft beschrijving, meeste occurrences
    hits.sort(key=_score_highlight, reverse=True)

    return [_short(p) for p in hits[:8]]


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_themes() -> list[dict]:
    """List all festival themes with event counts."""
    return themes_data


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_days() -> list[dict]:
    """List all festival dates with weekday and event count."""
    return [
        {"day": d["day"], "weekday": _iso_to_nl.get(d["day"], ""), "count": d["count"]}
        for d in days_data
    ]


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_tags(query: str, limit: int = 20) -> list[dict]:
    """Search event tags. Returns matching tags sorted by event count."""
    needle = query.strip().casefold()
    safe_limit = max(1, min(limit, 100))
    if not needle:
        return tags_data[:safe_limit]
    return [
        tag for tag in tags_data
        if needle in tag["name"].casefold()
    ][:safe_limit]


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def events_by_location(location_name: str, day: str = "") -> list[dict]:
    """Find events at a location. Partial name match, optional day filter."""
    needle = location_name.strip().lower()
    iso_day = _resolve_day(day) if day else None

    result = []
    for p in pages:
        if needle not in (p.get("location") or "").lower():
            continue
        if iso_day:
            days_in_p = {o["day"] for o in (p.get("occurrences") or [])}
            if iso_day not in days_in_p:
                continue
        result.append(_short(p))

    result.sort(key=lambda x: (x["days"][0] if x["days"] else "", x["first_time"]))
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def free_highlights(day: str = "") -> list[dict]:
    """Top 10 free event picks for a day (or entire festival if no day given)."""
    hits = _filter_pages(day=day, free_only=True)
    hits.sort(key=_score_highlight, reverse=True)
    return [_short(p) for p in hits[:10]]


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_today() -> dict:
    """Get current date and festival day info. Use when user says 'today' or 'tonight'."""
    today = date.today().isoformat()
    in_festival = today in _iso_to_nl
    return {
        "date": today,
        "weekday": _iso_to_nl.get(today, ""),
        "in_festival": in_festival,
        "festival_range": f"{days_data[0]['day']} to {days_data[-1]['day']}",
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def plan_day(day: str = "") -> dict:
    """Get a full day plan: day info, free highlights, and themed picks in one call."""
    iso = _resolve_day(day) if day else None
    if not iso:
        from datetime import date
        today = date.today().isoformat()
        iso = today if today in _iso_to_nl else None

    day_info = None
    if iso:
        for d in days_data:
            if d["day"] == iso:
                day_info = {"day": iso, "weekday": _iso_to_nl.get(iso, ""), "count": d["count"]}
                break

    free = _filter_pages(day=iso or "", free_only=True)
    free.sort(key=_score_highlight, reverse=True)

    themes_seen = set()
    picks = []
    for p in _filter_pages(day=iso or "")[:50]:
        short = _short(p)
        for t in p.get("themes", []):
            if t not in themes_seen and len(picks) < 5:
                themes_seen.add(t)
                picks.append({
                    "theme": t,
                    "name": short["name"],
                    "location": short["location"],
                    "time": short["first_time"],
                    "uuid": short["uuid"],
                })
                break

    return {
        "day_info": day_info,
        "free_highlights": [_short(p) for p in free[:10]],
        "picks": picks,
    }


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("gf://overview")
def overview() -> str:
    """Overzicht van de Gentse Feesten 2026 — gebruik als achtergrondcontext."""
    n_free = sum(1 for p in pages if p.get("free"))
    n_outdoor = sum(1 for p in pages if p.get("outdoor"))
    n_video = sum(1 for p in pages if p.get("videos"))
    theme_names = ", ".join(t["name"] for t in themes_data[:10])
    genre_names = ", ".join(_all_genres[:12])
    day_range = f"{days_data[0]['day']} t/m {days_data[-1]['day']}"

    return f"""# Gentse Feesten 2026

11-day free city festival in Gent, Belgium ({day_range}).

- **Unique events:** {len(pages)} ({len(events)} total performances)
- **Free:** {n_free}/{len(pages)} ({n_free*100//len(pages)}%) · **Outdoor:** {n_outdoor}
- **Themes:** {theme_names}
- **Genres:** {genre_names}

Tools: `plan_day` (full day), `search_events` (filtered), `suggest` (natural language), `get_event_summary`/`get_event_detail` (event info), `free_highlights`, `events_by_location`.
"""


@mcp.resource("gf://days")
def resource_days() -> str:
    """Alle festivaldagen met weekdag en aantal evenementen."""
    lines = ["# Gentse Feesten 2026 — Festivaldagen\n"]
    for d in days_data:
        wd = _iso_to_nl.get(d["day"], "")
        lines.append(f"- {wd.capitalize()} {d['day']}: {d['count']} evenementen")
    return "\n".join(lines)


@mcp.resource("gf://themes")
def resource_themes() -> str:
    """Alle festivalthema's met het aantal gekoppelde evenementen."""
    lines = [f"# Gentse Feesten 2026 — Thema's ({len(themes_data)})\n"]
    for theme in themes_data:
        lines.append(f"- {theme['name']}: {theme['count']} evenementen")
    return "\n".join(lines)


@mcp.resource("gf://tags")
def resource_tags() -> str:
    """Top 50 event tags with event counts. Use search_tags for full list."""
    lines = [
        f"# Gentse Feesten 2026 — Top tags",
        "",
        f"{len(tags_data)} unique tags available. Showing top 50 by event count.",
        "Use `search_tags` to find specific tags.",
        "",
    ]
    for tag in tags_data[:TAG_RESOURCE_LIMIT]:
        lines.append(f"- {tag['name']}: {tag['count']} evenementen")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
