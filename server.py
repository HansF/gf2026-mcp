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
import math
import os
import re
import shutil
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

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


class EventDetail(TypedDict, total=False):
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

# Aliases en experience tags
_aliases_data = json.loads((PROJECT_DIR / "aliases.json").read_text(encoding="utf-8"))
ALIASES: dict[str, list[str]] = _aliases_data.get("aliases", {})
EXPERIENCE_MAPPING: dict[str, list[str]] = _aliases_data.get("experience_mapping", {})

_experience_cache = json.loads((PROJECT_DIR / "experience_tags.json").read_text(encoding="utf-8")) if (PROJECT_DIR / "experience_tags.json").is_file() else {}
EXPERIENCE_TAGS: dict[str, list[str]] = _experience_cache


# ---------------------------------------------------------------------------
# Zones — dynamically cluster locations by proximity
# ---------------------------------------------------------------------------

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Afstand in meter tussen twee coördinaten (Haversine)."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _build_zones() -> list[dict]:
    """Cluster locations by proximity (~200m) and derive vibe from event data."""
    locs_with_coords = [l for l in locations_data if l.get("lat") and l.get("lon")]
    locs_without = [l for l in locations_data if not l.get("lat") or not l.get("lon")]

    # Union-Find for clustering
    parent: dict[str, str] = {l["name"]: l["name"] for l in locs_with_coords}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Cluster locations within 100m
    for i, a in enumerate(locs_with_coords):
        for b in locs_with_coords[i + 1:]:
            if _haversine(a["lat"], a["lon"], b["lat"], b["lon"]) < 100:
                union(a["name"], b["name"])

    # Group locations by cluster
    clusters: dict[str, list[dict]] = {}
    for loc in locs_with_coords:
        root = find(loc["name"])
        clusters.setdefault(root, []).append(loc)

    # Build zone for each cluster (min 2 locations, or high-count singles)
    zones: list[dict] = []
    for root, cluster_locs in clusters.items():
        loc_names = [l["name"] for l in cluster_locs]
        total_count = sum(l.get("count", 0) for l in cluster_locs)

        # Skip tiny single-location clusters unless they have many events
        if len(cluster_locs) < 2 and total_count < 50:
            continue

        # Zone name: use the location with most events, or shortest name
        zone_loc = max(cluster_locs, key=lambda l: l.get("count", 0))
        zone_name = zone_loc["name"]
        # Simplify: use street area if multiple locations
        if len(cluster_locs) > 1:
            streets = {l.get("street", "").split()[0] for l in cluster_locs if l.get("street")}
            if len(streets) == 1:
                zone_name = list(streets)[0]
            else:
                zone_name = zone_loc["name"]

        # Calculate center
        avg_lat = sum(l["lat"] for l in cluster_locs) / len(cluster_locs)
        avg_lon = sum(l["lon"] for l in cluster_locs) / len(cluster_locs)

        # Derive vibe from events at these locations
        vibe_counter: Counter[str] = Counter()
        good_for_counter: Counter[str] = Counter()
        for p in pages:
            if p.get("location") not in loc_names:
                continue
            for theme in p.get("themes") or []:
                vibe_counter[theme.lower()] += 1
            for kw in p.get("keywords") or []:
                if isinstance(kw, str):
                    vibe_counter[kw.lower()] += 1
            # Derive good_for from themes/keywords
            themes_kw = " ".join((p.get("themes") or []) + (p.get("keywords") or [])).lower()
            if any(w in themes_kw for w in ["kinder", "gezin", "familie", "kids"]):
                good_for_counter["kinderen"] += 1
            if any(w in themes_kw for w in ["dans", "dansen", "bal", "tango"]):
                good_for_counter["dans"] += 1
            if any(w in themes_kw for w in ["eten", "drank", "food", "bar"]):
                good_for_counter["eten & drinken"] += 1
            if any(w in themes_kw for w in ["circus", "straattheater", "acrobat"]):
                good_for_counter["circus & straat"] += 1
            if any(w in themes_kw for w in ["muziek", "concert", "live"]):
                good_for_counter["muziek"] += 1
            if any(w in themes_kw for w in ["theater", "toneel", "voorstelling"]):
                good_for_counter["theater"] += 1
            if any(w in themes_kw for w in ["comedy", "humor", "cabaret"]):
                good_for_counter["comedy & cabaret"] += 1
            if any(w in themes_kw for w in ["expo", "museum", "tentoonstelling"]):
                good_for_counter["expo & kunst"] += 1
            if any(w in themes_kw for w in ["markt", "verkoop"]):
                good_for_counter["markten"] += 1
            if p.get("outdoor"):
                good_for_counter["buiten"] += 1

        vibe = [tag for tag, _ in vibe_counter.most_common(5)]
        good_for = [tag for tag, _ in good_for_counter.most_common(5) if _ >= 3]

        outdoor_events = sum(1 for p in pages if p.get("location") in loc_names and p.get("outdoor"))
        total_events = sum(1 for p in pages if p.get("location") in loc_names)
        zones.append({
            "zone": zone_name,
            "locations": sorted(loc_names),
            "vibe": vibe,
            "good_for": good_for,
            "event_count": total_count,
            "location_count": len(cluster_locs),
            "outdoor_ratio": f"{outdoor_events}/{total_events}" if total_events else "0/0",
            "lat": round(avg_lat, 6),
            "lon": round(avg_lon, 6),
        })

    # Add standalone high-count locations as their own zones
    clustered_names = {l["name"] for cl in clusters.values() for l in cl}
    for loc in locs_without:
        if loc.get("count", 0) >= 40:
            zones.append({
                "zone": loc["name"],
                "locations": [loc["name"]],
                "vibe": [],
                "good_for": [],
                "event_count": loc.get("count", 0),
                "location_count": 1,
                "outdoor_ratio": f"{'1' if loc.get('outdoor') else '0'}/1",
                "lat": None,
                "lon": None,
            })

    # Add standalone single-location clusters with enough events
    for root, cluster_locs in clusters.items():
        if len(cluster_locs) == 1 and cluster_locs[0]["name"] not in clustered_names:
            loc = cluster_locs[0]
            if loc.get("count", 0) >= 50:
                zones.append({
                    "zone": loc["name"],
                    "locations": [loc["name"]],
                    "vibe": [],
                    "good_for": [],
                    "event_count": loc.get("count", 0),
                    "location_count": 1,
                    "outdoor_ratio": f"{'1' if loc.get('outdoor') else '0'}/1",
                    "lat": loc["lat"],
                    "lon": loc["lon"],
                })

    zones.sort(key=lambda z: -z["event_count"])
    return zones


ZONES_FILE = PROJECT_DIR / "zones.json"


def _load_zones() -> list[dict]:
    """Load zones from zones.json if present, otherwise build dynamically."""
    if ZONES_FILE.exists():
        data = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
        zones = []
        for z in data.get("zones", []):
            zones.append({
                "zone": z.get("name", z.get("zone", "")),
                "locations": z.get("locations", []),
                "vibe": z.get("vibe", []),
                "good_for": z.get("good_for", []),
                "event_count": z.get("event_count", 0),
                "location_count": z.get("location_count", len(z.get("locations", []))),
                "outdoor_ratio": z.get("outdoor_ratio", "0/0"),
                "lat": z.get("lat"),
                "lon": z.get("lon"),
                "notes": z.get("notes", ""),
                "best_moment": z.get("best_moment", ""),
                "target_audience": z.get("target_audience", ""),
            })
        return zones
    return _build_zones()


zones_data: list[dict] = _load_zones()
_zones_by_name: dict[str, dict] = {z["zone"].lower(): z for z in zones_data}

# Map location name -> zone name for fast lookup
_location_to_zone: dict[str, str] = {}
# Build a set of all known zone locations for matching
_zone_location_sets: dict[str, set[str]] = {}
for z in zones_data:
    locs = {loc.lower() for loc in z["locations"]}
    _zone_location_sets[z["zone"]] = locs
    for loc in z["locations"]:
        _location_to_zone[loc.lower()] = z["zone"]

# Compute event counts dynamically (zones.json may have stale zeros)
for z in zones_data:
    zone_locs = _zone_location_sets.get(z["zone"], set())
    count = sum(1 for p in pages if (p.get("location") or "").lower() in zone_locs)
    z["event_count"] = count
    z["location_count"] = len(z["locations"])

# ---------------------------------------------------------------------------
# Vector index for semantic search
# ---------------------------------------------------------------------------

EMBED_DIM = 384
VECTOR_DB_PATH = PROJECT_DIR / "zvec_gentsefeesten_db"
_vector_coll = None
_vector_emb = None


def _build_vector_index():
    """Build zvec collection with FTS + vector index for semantic search."""
    global _vector_coll, _vector_emb

    try:
        import zvec
        from zvec import CollectionOption, DataType, Doc, FieldSchema, FtsIndexParam
        from zvec.extension import DefaultLocalDenseEmbedding
    except ImportError:
        print("zvec not available — vector search disabled")
        return

    if VECTOR_DB_PATH.exists():
        shutil.rmtree(VECTOR_DB_PATH)

    zvec.init(log_level=zvec.LogLevel.WARN, query_threads=4)

    fts = lambda: FtsIndexParam(tokenizer_name="standard", filters=["lowercase"])
    schema = zvec.CollectionSchema(
        name="gentse_feesten",
        fields=[
            FieldSchema("name", DataType.STRING, nullable=False, index_param=fts()),
            FieldSchema("description", DataType.STRING, nullable=False, index_param=fts()),
            FieldSchema("keywords", DataType.STRING, nullable=True, index_param=fts()),
            FieldSchema("themes", DataType.STRING, nullable=True, index_param=fts()),
            FieldSchema("location", DataType.STRING, nullable=True, index_param=fts()),
            FieldSchema("organizer", DataType.STRING, nullable=True),
            FieldSchema("startdate", DataType.STRING, nullable=True),
            FieldSchema("free", DataType.STRING, nullable=True),
            FieldSchema("outdoors", DataType.STRING, nullable=True),
            FieldSchema("parent_uuid", DataType.STRING, nullable=True),
            FieldSchema("embed_text", DataType.STRING, nullable=True),
        ],
        vectors=zvec.VectorSchema("embedding", zvec.DataType.VECTOR_FP32, EMBED_DIM),
    )

    _vector_coll = zvec.create_and_open(
        path=str(VECTOR_DB_PATH),
        schema=schema,
        option=CollectionOption(read_only=False, enable_mmap=True),
    )

    _vector_emb = DefaultLocalDenseEmbedding(batch_size=64)

    # Build genre lookup from events.json for richer embeddings
    _genre_by_uuid: dict[str, str] = {e["uuid"]: e.get("genre", "") for e in events}

    BATCH = 500
    batch = []
    for i, p in enumerate(pages):
        uuid = p.get("uuid", "")
        genre = _genre_by_uuid.get(uuid, "")
        exp_tags = " ".join(EXPERIENCE_TAGS.get(uuid, []))
        embed_text = f"{p.get('name', '')}. {p.get('desc', '')}. {' '.join(p.get('keywords', []))}. {' '.join(p.get('themes', []))}. {p.get('location', '')}. {genre}. {exp_tags}"
        vec = _vector_emb.embed(embed_text)
        doc = Doc(id=f"{p.get('uuid', '')}_{i}", fields={
            "name": p.get("name", ""),
            "description": p.get("desc", ""),
            "keywords": " ".join(p.get("keywords", [])),
            "themes": " ".join(p.get("themes", [])),
            "location": p.get("location", ""),
            "organizer": " ".join(p.get("organizers", [])),
            "startdate": p.get("first", ""),
            "free": "1" if p.get("free") else "0",
            "outdoors": "1" if p.get("outdoor") else "0",
            "parent_uuid": p.get("parent_uuid", ""),
            "embed_text": embed_text,
        }, vectors={"embedding": vec})
        batch.append(doc)
        if len(batch) == BATCH:
            _vector_coll.insert(batch)
            batch = []
    if batch:
        _vector_coll.insert(batch)

    print(f"Vector index: {_vector_coll.stats.doc_count} events indexed")


if os.getenv("GF_MCP_DISABLE_VECTOR_INDEX", "").lower() in {"1", "true", "yes"}:
    print("Vector index disabled by GF_MCP_DISABLE_VECTOR_INDEX")
else:
    try:
        _build_vector_index()
    except Exception as exc:
        print(f"Vector index disabled — startup failed: {exc}")
        _vector_coll = None
        _vector_emb = None


HOST = os.getenv("GF_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("GF_MCP_PORT", "8000"))

# Response wrapper for consistent format
def _wrap(tool_name: str, data, warnings: list[str] | None = None) -> WrapResponse:
    """Wrap tool responses in standard envelope."""
    return {
        "ok": True,
        "data": data,
        "meta": {
            "tool": tool_name,
            "dataset_year": 2026,
            "result_count": len(data) if isinstance(data, list) else 1,
        },
        "warnings": warnings or [],
    }


def _expand_query(query: str) -> list[str]:
    """Expand query using aliases. Returns [primary, ...expanded] where primary is the original query."""
    q = query.strip().lower()
    # Always keep original query as primary
    expanded = [q]

    # Check for negation patterns
    negation_words = ["niet", "geen", "no", "not", "zonder", "minus"]
    has_negation = any(neg in q for neg in negation_words)

    for alias_key, alias_values in ALIASES.items():
        # Skip "braaf" alias if query has negation
        if alias_key == "braaf" and has_negation:
            continue
        # Skip "gratis" alias if query has negation
        if alias_key == "gratis" and has_negation:
            continue

        alias_key_normalized = alias_key.replace("_", " ")
        matched = False

        # Direct match on alias key (whole word only)
        if re.search(r'\b' + re.escape(alias_key) + r'\b', q) or \
           re.search(r'\b' + re.escape(alias_key_normalized) + r'\b', q):
            matched = True
        # Match on any alias value (whole word only)
        elif any(re.search(r'\b' + re.escape(av) + r'\b', q) for av in alias_values):
            matched = True
        # Fuzzy match for negated aliases (whole word only)
        elif has_negation and alias_key.startswith("niet_"):
            non_negated = alias_key[5:]
            non_negated_normalized = non_negated.replace("_", " ")
            if re.search(r'\b' + re.escape(non_negated) + r'\b', q) or \
               re.search(r'\b' + re.escape(non_negated_normalized) + r'\b', q):
                matched = True

    return list(dict.fromkeys(expanded))  # Dedupe preserving order


def _get_experience_tags(uuid: str) -> list[str]:
    """Get experience tags for an event."""
    return EXPERIENCE_TAGS.get(uuid, [])


def _score_event(p: dict, intent: dict | None = None) -> tuple:
    """Score an event for ranking. Higher = better."""
    # Base: prefer events with descriptions, outdoor, more occurrences
    base_score = (
        1 if p.get("outdoor") else 0,
        1 if p.get("desc") else 0,
        p.get("count", 0),
        p.get("name", ""),
    )

    if not intent:
        return base_score

    # Compute intent match score
    uuid = p.get("uuid", "")
    event_tags = set(_get_experience_tags(uuid))
    event_text = " ".join([
        p.get("name", ""),
        p.get("desc", ""),
        " ".join(p.get("themes", [])),
        " ".join(p.get("keywords", [])),
    ]).lower()

    score = 0

    # High-value: experience tag matches (max 30)
    exp_matches = sum(1 for req in intent.get("experience", []) if req in event_tags)
    score += min(exp_matches * 10, 30)

    # Medium-value: activity matches (max 15)
    act_matches = sum(1 for req in intent.get("activity", []) if req in event_tags)
    score += min(act_matches * 5, 15)

    # Medium-value: genre matches in text (max 15)
    genre_matches = sum(1 for req in intent.get("genre", []) if req in event_text)
    score += min(genre_matches * 5, 15)

    # Bonus: price/setting matches
    if intent.get("price") == "free" and p.get("free"):
        score += 5
    if intent.get("setting") == "outdoor" and p.get("outdoor"):
        score += 5
    if intent.get("setting") == "indoor" and not p.get("outdoor"):
        score += 5

    return (score,) + base_score


def _parse_intent(mood: str) -> dict:
    """Parse mood string into structured intent."""
    m = mood.lower()
    intent = {
        "experience": [],
        "activity": [],
        "genre": [],
        "price": "any",
        "setting": "any",
        "time": "any",
        "energy": "any",
    }

    # Check for negation patterns first
    negation_words = ["niet", "geen", "no", "not", "zonder"]
    has_negation = any(neg in m for neg in negation_words)

    # Handle negated concepts
    if has_negation:
        # "niet te braaf" → cabaret, queer, burlesque, weird, late_night
        if "braaf" in m or "familie" in m or "kinder" in m:
            intent["experience"].extend(["cabaret", "queer", "burlesque", "weird", "late_night"])
        # "niet gratis" → paid events
        if "gratis" in m or "free" in m:
            intent["price"] = "paid"
        # "niet buiten" → indoor
        if "buiten" in m or "outdoor" in m:
            intent["setting"] = "indoor"

    # Experience tags from EXPERIENCE_MAPPING
    for exp_tag, keywords in EXPERIENCE_MAPPING.items():
        if any(kw in m for kw in keywords):
            if exp_tag not in intent["experience"]:
                intent["experience"].append(exp_tag)

    # Activity
    if any(w in m for w in ["dans", "dansen", "bal", "dansinitiatie"]):
        intent["activity"].append("dance")
    if any(w in m for w in ["eten", "drank", "food", "bar"]):
        intent["activity"].append("food")
    if any(w in m for w in ["wandeling", "wandelen", "rondleiding"]):
        intent["activity"].append("walking")
    if any(w in m for w in ["luisteren", "rustig", "kalm"]):
        intent["activity"].append("listening")
    if any(w in m for w in ["lachen", "humor", "grappig"]):
        intent["activity"].append("comedy")

    # Price
    if any(w in m for w in ["gratis", "free", "kosteloos"]):
        intent["price"] = "free"
    elif any(w in m for w in ["betalen", "ticket", "prijs"]):
        intent["price"] = "paid"

    # Setting
    if any(w in m for w in ["buiten", "outdoor", "openlucht", "park"]):
        intent["setting"] = "outdoor"
    elif any(w in m for w in ["binnen", "indoor", "zaal"]):
        intent["setting"] = "indoor"

    # Time
    if any(w in m for w in ["ochtend", "vroeg", "morgend"]):
        intent["time"] = "morning"
    elif any(w in m for w in ["middag", "namiddag", "overdag"]):
        intent["time"] = "afternoon"
    elif any(w in m for w in ["avond", "nacht", "laat"]):
        intent["time"] = "evening"

    # Energy
    if any(w in m for w in ["rustig", "kalm", "ontspannen"]):
        intent["energy"] = "low"
    elif any(w in m for w in ["actief", "dans", "feest", "intensief"]):
        intent["energy"] = "high"
    elif any(w in m for w in ["gemengd", "afwisseling", "beetje alles"]):
        intent["energy"] = "mixed"

    # Genre
    for g in _all_genres:
        if g.lower() in m:
            intent["genre"].append(g.lower())

    # Theme mapping
    theme_map = {
        "jazz": ["jazz"],
        "comedy": ["comedy"],
        "theater": ["theater", "toneel"],
        "circus": ["circus", "straattheater"],
        "kinder": ["kinder", "gezin"],
    }
    for theme, keywords in theme_map.items():
        if any(kw in m for kw in keywords):
            intent["genre"].append(theme)

    return intent

mcp = FastMCP(
    "Gentse Feesten 2026",
    instructions="""Gentse Feesten 2026 — 11-day free city festival in Ghent, Belgium (July 17–27, 2026). 1600+ unique events across 25 zones. All data is public; no auth needed.

## Tool selection (most specific match wins)

Match the user's query to the FIRST row that fits — do not default to suggest() or semantic_search().

| User intent | Tool |
|---|---|
| Artist / event name mentioned | search_events(query="name") — ALWAYS use for named artists, bands, events. Check the `keywords` field to confirm matches; names may be truncated. |
| "What should I do on [day]?" | plan_day(day) — one call replaces list_days + free_highlights + search_events |
| Mood / vibe (no specific name) | suggest(mood) — Dutch phrases auto-expand: "niet te braaf" → cabaret/queer/burlesque; "zwoel" → romantic/sensual |
| Personal multi-day plan | create_festival_guide(vibe, energy_level, social_mode) |
| Conceptual / thematic question | semantic_search(query, mode="hybrid") — for meaning-based queries like "romantic evening", not proper nouns |
| "What's happening at [place]?" | events_by_location(location_name, day) |
| "Tell me about this event" | get_event_detail(uuid, detail="summary") or get_event_details([...uuids]) for batch |
| "What zone should I visit?" | list_zones() → get_zone_profile(zone_name) or search_by_zone(zone_name, ...) |
| "What's on during the festival?" | search_events(query, day, theme, ...) — supports free_only, outdoor_only, genre, wheelchair, time_window, participatory filters |
| "Events like this one" | find_similar_events(uuid) — vector similarity + parent/sibling relations |
| All occurrences of a multi-date event | get_parent_event(uuid) — parent + all child dates/times |
| "Show/display details" or user asks to see results visually | show_festival_explorer(mode="detail", uuid="...") for one event, or mode="search"/"day"/"guide"/"zones" for lists |

## Don'ts

- NEVER use suggest() for artist/band/event name lookups — it's mood-based, not name-based
- NEVER use semantic_search() for proper nouns, artist names, or exact event titles
- NEVER use get_event_detail in a loop — use get_event_details([...uuids]) for batch
- NEVER call semantic_search with mode="hybrid" as a fallback when search_events already works

## Key conventions

- **day parameter** in any tool: accepts ISO date ("2026-07-19") or Dutch weekday ("vrijdag", "zaterdag"). A festival day runs 06:00 to 05:59 the next calendar day, so late-night events after midnight belong to the previous festival day. Use get_today() to resolve "today".
- **Dutch queries work natively** in suggest() and search_events(): "niet te braaf" → cabaret/queer/burlesque; "zwoel" → sensueel/romantic.
- **semantic_search modes**: "semantic" for meaning ("romantic evening"), "fts" for exact keywords ("jazz", "cirQ"), "hybrid" combines both.
- **Festival language**: respond in the language the user uses. Festival content is Dutch; translate event names only when helpful.
- **Respond with event info**: always include event name, time, location, and whether it's free. Link UUID for follow-up.""",
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
APP_WIDGET_URI = "ui://widget/gf2026-explorer-v1.html"
APP_WIDGET_MIME_TYPE = "text/html;profile=mcp-app"
APP_WIDGET_DOMAIN = "https://gf2026.doplr.com"

_event_short_schema = {
    "type": "object",
    "properties": {
        "uuid": {"type": "string"},
        "name": {"type": "string"},
        "days": {"type": "array", "items": {"type": "string"}},
        "first_weekday": {"type": "string"},
        "first_time": {"type": "string"},
        "occurrence_time": {"type": "string"},
        "location": {"type": "string"},
        "free": {"type": "boolean"},
        "outdoor": {"type": "boolean"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "recurring_all_days": {"type": "boolean"},
        "experience_tags": {"type": "array", "items": {"type": "string"}},
        "match_reasons": {"type": "array", "items": {"type": "string"}},
    },
}

_zone_detail_schema = {
    "type": "object",
    "properties": {
        "zone": {"type": "string"},
        "locations": {"type": "array", "items": {"type": "string"}},
        "vibe": {"type": "array", "items": {"type": "string"}},
        "good_for": {"type": "array", "items": {"type": "string"}},
        "event_count": {"type": "integer"},
        "location_count": {"type": "integer"},
        "outdoor_ratio": {"type": "string", "description": "Fraction of outdoor events: outdoor_count/total_events"},
        "lat": {"type": "number"},
        "lon": {"type": "number"},
        "events": {"type": "array", "items": _event_short_schema},
    },
}


# Output schema TypedDicts for structured_output=True
class _Meta(TypedDict):
    tool: str
    dataset_year: int
    result_count: int

class WrapResponse(TypedDict):
    ok: bool
    data: Any
    meta: _Meta
    warnings: list[str]

class ZoneDetailOutput(TypedDict):
    zone: str
    locations: list[str]
    vibe: list[str]
    good_for: list[str]
    event_count: int
    location_count: int
    outdoor_ratio: str
    lat: float
    lon: float
    events: list[dict]

class ZoneProfileOutput(TypedDict):
    zone: str
    vibe: list[str]
    good_for: list[str]
    best_moment: str
    target_audience: str
    notes: str
    events: list[dict]
    event_count: int

class FindSimilarOutput(TypedDict):
    source_event: dict
    same_vibe: list[dict]
    nearby: list[dict]
    good_afterwards: list[dict]
    related: list[dict]

class ParentEventOutput(TypedDict):
    uuid: str
    name: str
    occurrences: list[dict]
    children: list[dict]
    total_dates: int

class PlanDayOutput(TypedDict):
    day_info: dict
    free_highlights: list[dict]
    themed_picks: list[dict]

class CreateGuideOutput(TypedDict):
    guide_title: str
    vibe_summary: str
    anchor_events: list[dict]
    fallback_events: list[dict]
    zones: list[dict]
    energy_strategy: list[str]
    total_events_considered: int

class GetTodayOutput(TypedDict):
    date: str
    weekday: str
    in_festival: bool
    festival_range: str
    next_festival_days: int

class FestivalExplorerOutput(TypedDict):
    title: str
    mode: str
    query: str
    day: str
    items: list[dict]
    sections: list[dict]
    meta: dict



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
IMPRINT = """
<h2>Imprint</h2>
<p>
  Hans Fraiponts<br>
  Emiel Lossystraat 37<br>
  9040 Ghent, Belgium<br>
  VAT: BE 0873.510.437<br>
  <a href="mailto:info@gogogonzo.be">info@gogogonzo.be</a>
</p>
"""



@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def index(_: Request) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gentse Feesten 2026 — experimental MCP service</title>
  <style>{LEGAL_PAGE_STYLE}</style>
</head>
<body>
  <main>
    <h1>Gentse Feesten 2026</h1>
    <p>This is an <strong>experimental</strong> service for the city of Ghent.
    It provides read-only AI access to the public Gentse Feesten 2026 festival programme.
    Nothing here is guaranteed: the service may be wrong, incomplete, change without notice,
    or disappear entirely.</p>
    <p>The festival takes place <strong>July 17-27, 2026</strong>, Ghent, Belgium.</p>
    <p>
      <a href="/privacy">Privacy policy</a> &middot;
      <a href="/terms">Terms of service</a>
    </p>
    {IMPRINT}
  </main>
</body>
</html>"""
)


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
    {IMPRINT}
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


def _next_iso_day(iso_day: str) -> str:
    return (date.fromisoformat(iso_day) + timedelta(days=1)).isoformat()


def _occurrence_time(occurrence: dict) -> str:
    return occurrence.get("time") or "00:00"


def _occurrence_in_festival_day(occurrence: dict, iso_day: str) -> bool:
    """Festival day runs 06:00 on iso_day through 05:59 the next calendar day."""
    occ_day = occurrence.get("day", "")
    occ_time = _occurrence_time(occurrence)
    if occ_day == iso_day and occ_time >= "06:00":
        return True
    if occ_day == _next_iso_day(iso_day) and occ_time < "06:00":
        return True
    return False


def _festival_day_occurrences(page: dict, iso_day: str = "") -> list[dict]:
    occurrences = page.get("occurrences") or []
    if not iso_day:
        return occurrences
    return [occ for occ in occurrences if _occurrence_in_festival_day(occ, iso_day)]


def _page_in_festival_day(page: dict, iso_day: str = "") -> bool:
    return bool(_festival_day_occurrences(page, iso_day)) if iso_day else True


def _matches_time_window(times: list[str], time_window: str) -> bool:
    if not time_window:
        return True
    if time_window == "morning":
        return any("06:00" <= t < "12:00" for t in times)
    if time_window == "afternoon":
        return any("12:00" <= t < "18:00" for t in times)
    if time_window == "evening":
        return any("18:00" <= t < "22:00" for t in times)
    if time_window == "night":
        return any(t >= "22:00" or t < "06:00" for t in times)
    if "-" in time_window:
        start, end = [part.strip() for part in time_window.split("-", 1)]
        if start and end:
            if start <= end:
                return any(start <= t < end for t in times)
            return any(t >= start or t < end for t in times)
    return True


def _short(p: dict, target_day: str = "") -> dict:
    occ = p.get("occurrences") or []
    days_list = sorted({o["day"] for o in occ})
    matched_time = ""
    if target_day:
        matched_occurrences = _festival_day_occurrences(p, target_day)
        if matched_occurrences:
            matched_time = matched_occurrences[0].get("time", "")
    return {
        "uuid": p["uuid"],
        "name": p["name"],
        "days": days_list,
        "first_weekday": _iso_to_nl.get(target_day or (days_list[0] if days_list else ""), "") if (target_day or days_list) else "",
        "first_time": occ[0]["time"] if occ else "",
        "occurrence_time": matched_time or (occ[0]["time"] if occ else ""),
        "location": p.get("location", ""),
        "free": p.get("free", False),
        "outdoor": p.get("outdoor", False),
        "themes": p.get("themes", []),
        "recurring_all_days": len(days_list) > 5,
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
            if not _page_in_festival_day(p, iso_day):
                continue
        if th and not any(th in t.lower() for t in (p.get("themes") or [])):
            # also check keywords
            if not any(th in k.lower() for k in (p.get("keywords") or [])):
                continue
        if ge and ge not in (p.get("genre") or "").lower():
            # Also check themes and keywords for genre match
            theme_kw = " ".join((p.get("themes") or []) + (p.get("keywords") or [])).lower()
            if ge not in theme_kw:
                continue
        if q:
            haystack = " ".join([
                p.get("name") or "",
                p.get("desc") or "",
                " ".join(p.get("keywords") or []),
                " ".join(p.get("themes") or []),
            ]).lower()
            if q not in haystack:
                continue
        result.append(p)
    return result
def _score_highlight(p: dict) -> tuple:
    """Hogere score = beter voor free_highlights. Deprioritize all-day recurring."""
    return (
        0 if len({o["day"] for o in (p.get("occurrences") or [])}) > 5 else 1,
        1 if p.get("outdoor") else 0,
        1 if p.get("desc") else 0,
        p.get("count", 0),
        p.get("name", ""),
    )


def _vector_doc_to_short(doc, score=None):
    """Convert zvec Doc to short event dict."""
    # zvec IDs are stored as "{uuid}_{index}" — strip suffix for canonical UUID
    raw_id = doc.id
    uuid = raw_id.rsplit("_", 1)[0] if "_" in raw_id else raw_id
    d = {
        "uuid": uuid,
        "name": doc.fields.get("name", ""),
        "location": doc.fields.get("location", ""),
        "themes": doc.fields.get("themes", ""),
        "free": doc.fields.get("free", "0") == "1",
        "outdoor": doc.fields.get("outdoors", "0") == "1",
    }
    if score is not None:
        d["score"] = round(score, 4)
    return d


def _widget_event(item: dict, target_day: str = "") -> dict:
    """Small stable event shape for the ChatGPT Apps widget."""
    days = item.get("days") or []
    themes = item.get("themes") or []
    if isinstance(themes, str):
        themes = [t for t in themes.split(" ") if t]
    page = _pages_by_uuid.get(item.get("uuid", "")) or {}
    occurrences = item.get("occurrences") or page.get("occurrences", [])
    first_occurrence = occurrences[0] if occurrences else {}
    return {
        "uuid": item.get("uuid", ""),
        "name": item.get("name", ""),
        "location": item.get("location", ""),
        "time": item.get("occurrence_time") or item.get("first_time") or item.get("time", "") or first_occurrence.get("time", ""),
        "day": target_day or (days[0] if days else item.get("day", "")),
        "weekday": item.get("first_weekday", ""),
        "free": bool(item.get("free")),
        "outdoor": bool(item.get("outdoor")),
        "themes": themes[:4],
        "match_reasons": (item.get("match_reasons") or [])[:3],
        "url": item.get("url", ""),
        "image": item.get("image") or page.get("image", ""),
        "image_caption": item.get("image_caption") or page.get("image_caption", ""),
        "image_copyright": item.get("image_copyright") or page.get("image_copyright", ""),
        "desc": item.get("desc") or page.get("desc", ""),
        "occurrences": occurrences,
        "offers": item.get("offers") or page.get("offers", []),
        "contacts": item.get("contacts") or page.get("contacts", []),
    }


def _widget_section(title: str, items: list[dict], kind: str = "events", target_day: str = "") -> dict:
    return {
        "title": title,
        "kind": kind,
        "items": [_widget_event(item, target_day=target_day) for item in items],
    }


def _zone_summary_item(zone: dict) -> dict:
    return {
        "name": zone.get("zone") or zone.get("name", ""),
        "event_count": zone.get("event_count", 0),
        "location_count": zone.get("location_count", 0),
        "vibe": (zone.get("vibe") or [])[:4],
        "good_for": (zone.get("good_for") or [])[:4],
        "notes": zone.get("notes", ""),
    }


def _find_widget_detail_event(uuid: str = "", query: str = "", day: str = "") -> dict | None:
    clean_uuid = uuid.strip()
    if clean_uuid and clean_uuid in _pages_by_uuid:
        return _event_summary(clean_uuid)
    needle = query.strip().lower()
    if not needle:
        return None
    iso_day = _resolve_day(day) if day else None
    for page in pages:
        if needle == (page.get("name") or "").lower():
            return _event_summary_from_page(page)
    for page in pages:
        haystack = " ".join([
            page.get("name") or "",
            page.get("desc") or "",
            " ".join(page.get("keywords") or []),
        ]).lower()
        if needle in haystack:
            return _event_summary_from_page(page)
    stopwords = {"live", "show", "event", "the", "de", "het", "een", "and", "en", "op"}
    tokens = [
        token
        for token in re.findall(r"[\w'-]+", needle)
        if len(token) > 3 and token not in stopwords
    ]
    if tokens:
        scored: list[tuple[int, int, dict]] = []
        for page in pages:
            haystack = " ".join([
                page.get("name") or "",
                page.get("desc") or "",
                " ".join(page.get("keywords") or []),
            ]).lower()
            score = sum(1 for token in tokens if token in haystack)
            if not score:
                continue
            day_match = 1 if iso_day and _page_in_festival_day(page, iso_day) else 0
            scored.append((day_match, score, page))
        if scored:
            scored.sort(key=lambda row: (row[0], row[1], row[2].get("name", "")), reverse=True)
            return _event_summary_from_page(scored[0][2])
    return None


def _festival_explorer_payload(query: str = "", day: str = "", mode: str = "search", uuid: str = "") -> FestivalExplorerOutput:
    clean_query = query.strip()
    clean_day = day.strip()
    target_day = _resolve_day(clean_day) or clean_day
    clean_mode = (mode or "search").strip().lower()
    if clean_mode not in {"search", "day", "guide", "zones", "detail"}:
        clean_mode = "search"

    if clean_mode == "detail":
        detail = _find_widget_detail_event(uuid=uuid, query=clean_query, day=clean_day)
        if not detail or detail.get("error"):
            return {
                "title": "Event detail",
                "mode": "detail",
                "query": clean_query,
                "day": clean_day,
                "items": [],
                "sections": [{"title": "Event detail", "kind": "detail", "items": []}],
                "meta": {"result_count": 0, "dataset_year": 2026, "warnings": ["Event not found"]},
            }
        item = _widget_event(detail, target_day=target_day)
        return {
            "title": item.get("name", "Event detail"),
            "mode": "detail",
            "query": clean_query,
            "day": clean_day,
            "items": [item],
            "sections": [{"title": "Event detail", "kind": "detail", "items": [item]}],
            "meta": {"result_count": 1, "dataset_year": 2026},
        }

    if clean_mode == "zones":
        zone_items = [_zone_summary_item(zone) for zone in list_zones()]
        return {
            "title": "Festival zones",
            "mode": "zones",
            "query": clean_query,
            "day": clean_day,
            "items": zone_items,
            "sections": [{"title": "Festival zones", "kind": "zones", "items": zone_items}],
            "meta": {"result_count": len(zone_items), "dataset_year": 2026},
        }

    if clean_mode == "day":
        plan = plan_day(clean_day)
        day_info = plan.get("day_info") or {}
        title_day = day_info.get("weekday") or clean_day or "festival day"
        sections = [
            _widget_section("Free highlights", plan.get("free_highlights", []), target_day=target_day),
            _widget_section("Themed picks", plan.get("themed_picks", []), target_day=target_day),
        ]
        items = [item for section in sections for item in section["items"]]
        return {
            "title": f"Day plan: {title_day}",
            "mode": "day",
            "query": clean_query,
            "day": clean_day,
            "items": items,
            "sections": sections,
            "meta": {"result_count": len(items), "dataset_year": 2026, "day_info": day_info},
        }

    if clean_mode == "guide":
        guide = create_festival_guide(vibe=clean_query or "afwisseling", day=clean_day)
        zone_items = [_zone_summary_item({"zone": z.get("name", ""), **z}) for z in guide.get("zones", [])]
        sections = [
            _widget_section("Anchor events", guide.get("anchor_events", []), target_day=target_day),
            _widget_section("Fallback events", guide.get("fallback_events", []), target_day=target_day),
        ]
        if zone_items:
            sections.append({"title": "Relevant zones", "kind": "zones", "items": zone_items})
        items = [item for section in sections if section["kind"] == "events" for item in section["items"]]
        return {
            "title": guide.get("guide_title", "Festival guide"),
            "mode": "guide",
            "query": clean_query,
            "day": clean_day,
            "items": items,
            "sections": sections,
            "meta": {
                "result_count": len(items),
                "dataset_year": 2026,
                "vibe_summary": guide.get("vibe_summary", ""),
                "energy_strategy": guide.get("energy_strategy", []),
            },
        }

    if clean_query:
        search = search_events(query=clean_query, day=clean_day, limit=12)
        raw_items = search.get("data", [])
        warnings = search.get("warnings", [])
        title = f"Search: {clean_query}"
    else:
        raw_items = free_highlights(clean_day)
        warnings = []
        title = "Free highlights"

    items = [_widget_event(item, target_day=target_day) for item in raw_items]
    return {
        "title": title,
        "mode": "search",
        "query": clean_query,
        "day": clean_day,
        "items": items,
        "sections": [{"title": title, "kind": "events", "items": items}],
        "meta": {"result_count": len(items), "dataset_year": 2026, "warnings": warnings},
    }


def _festival_explorer_widget_html() -> str:
    return r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gentse Feesten Explorer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #fffaf0;
      --panel: #ffffff;
      --ink: #1f2937;
      --muted: #667085;
      --line: #ded7c9;
      --accent: #c2410c;
      --accent-2: #047857;
      --accent-3: #1d4ed8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; }
    .app { max-width: 820px; margin: 0 auto; padding: 14px; }
    .top {
      display: grid;
      gap: 10px;
      grid-template-columns: 1fr;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line);
    }
    h1 { margin: 0; font-size: 1.25rem; line-height: 1.2; }
    .status { min-height: 20px; color: var(--muted); }
    .controls {
      display: grid;
      grid-template-columns: minmax(0, 1.8fr) minmax(120px, .8fr) minmax(110px, .7fr) auto;
      gap: 8px;
      align-items: center;
    }
    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      padding: 8px 10px;
    }
    button {
      min-height: 38px;
      border: 0;
      border-radius: 8px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      padding: 8px 14px;
      cursor: pointer;
    }
    button:disabled { opacity: .65; cursor: wait; }
    .section { margin-top: 16px; }
    .section h2 { margin: 0 0 8px; font-size: .95rem; color: #344054; }
    .grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
    .card {
      min-width: 0;
      overflow: hidden;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
    }
    .thumb {
      display: block;
      width: 100%;
      aspect-ratio: 16 / 9;
      object-fit: cover;
      background: #f2f4f7;
      border-bottom: 1px solid var(--line);
    }
    .card-body { padding: 12px; }
    .card h3 { margin: 0 0 8px; font-size: 1rem; line-height: 1.25; }
    .meta, .tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .pill {
      border-radius: 999px;
      background: #f2f4f7;
      color: #344054;
      font-size: .75rem;
      padding: 3px 8px;
      white-space: nowrap;
    }
    .pill.free { background: #dcfce7; color: #166534; }
    .pill.paid { background: #fee2e2; color: #991b1b; }
    .pill.time { background: #dbeafe; color: #1e40af; }
    .muted { color: var(--muted); }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 18px;
      text-align: center;
    }
    @media (max-width: 620px) {
      .controls { grid-template-columns: 1fr 1fr; }
      .controls input { grid-column: 1 / -1; }
      .controls button { grid-column: 1 / -1; }
    }
  </style>
</head>
<body>
  <main class="app">
    <div class="top">
      <div>
        <h1 id="title">Gentse Feesten Explorer</h1>
        <div id="status" class="status">Waiting for festival data...</div>
      </div>
      <form id="controls" class="controls">
        <input id="query" name="query" type="search" placeholder="Search, mood, artist, zone..." autocomplete="off">
        <select id="day" name="day" aria-label="Day">
          <option value="">Any day</option>
          <option value="vrijdag">Friday</option>
          <option value="zaterdag">Saturday</option>
          <option value="zondag">Sunday</option>
          <option value="maandag">Monday</option>
          <option value="dinsdag">Tuesday</option>
          <option value="woensdag">Wednesday</option>
          <option value="donderdag">Thursday</option>
        </select>
        <select id="mode" name="mode" aria-label="Mode">
          <option value="search">Search</option>
          <option value="day">Day plan</option>
          <option value="guide">Guide</option>
          <option value="zones">Zones</option>
          <option value="detail">Detail</option>
        </select>
        <button id="submit" type="submit">Show</button>
      </form>
    </div>
    <div id="content"></div>
  </main>
  <script>
    const state = { pending: new Map(), seq: 1 };
    const title = document.getElementById("title");
    const status = document.getElementById("status");
    const content = document.getElementById("content");
    const form = document.getElementById("controls");
    const queryInput = document.getElementById("query");
    const dayInput = document.getElementById("day");
    const modeInput = document.getElementById("mode");
    const submit = document.getElementById("submit");

    function text(value) {
      return value == null ? "" : String(value);
    }

    function el(tag, className, value) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (value != null) node.textContent = text(value);
      return node;
    }

    function pill(value, extra) {
      return el("span", "pill" + (extra ? " " + extra : ""), value);
    }

    function renderEvent(item) {
      const card = el("article", "card");
      if (item.image) {
        const img = document.createElement("img");
        img.className = "thumb";
        img.src = item.image;
        img.alt = item.image_caption || item.name || "Event image";
        img.loading = "lazy";
        img.decoding = "async";
        card.appendChild(img);
      }
      const body = el("div", "card-body");
      body.appendChild(el("h3", "", item.name || "Untitled event"));
      const meta = el("div", "meta");
      if (item.time) meta.appendChild(pill(item.time, "time"));
      if (item.location) meta.appendChild(pill(item.location));
      meta.appendChild(pill(item.free ? "Free" : "Paid", item.free ? "free" : "paid"));
      if (item.outdoor) meta.appendChild(pill("Outdoor"));
      body.appendChild(meta);
      if (item.match_reasons && item.match_reasons.length) {
        body.appendChild(el("p", "muted", item.match_reasons.join(" · ")));
      }
      if (item.themes && item.themes.length) {
        const tags = el("div", "tags");
        item.themes.slice(0, 4).forEach((theme) => tags.appendChild(pill(theme)));
        body.appendChild(tags);
      }
      card.appendChild(body);
      return card;
    }

    function renderDetail(item) {
      const card = renderEvent(item);
      const body = card.querySelector(".card-body");
      if (body && item.desc) {
        body.appendChild(el("p", "muted", item.desc));
      }
      if (body && item.occurrences && item.occurrences.length) {
        const wrap = el("div", "tags");
        item.occurrences.slice(0, 8).forEach((occ) => {
          const label = [occ.weekday || occ.day, occ.time || "", occ.location || ""].filter(Boolean).join(" · ");
          wrap.appendChild(pill(label || "Occurrence"));
        });
        body.appendChild(wrap);
      }
      if (body && item.offers && item.offers.length) {
        const offers = item.offers.map((offer) => [offer.price, offer.currency, offer.desc].filter(Boolean).join(" ")).filter(Boolean);
        if (offers.length) body.appendChild(el("p", "muted", offers.join(" · ")));
      }
      if (body && item.contacts && item.contacts.length) {
        const links = el("div", "tags");
        item.contacts.slice(0, 3).forEach((contact) => {
          if (!contact.url) return;
          const link = document.createElement("a");
          link.className = "pill";
          link.href = contact.url;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.textContent = "Website";
          links.appendChild(link);
        });
        if (links.children.length) body.appendChild(links);
      }
      return card;
    }

    function renderZone(item) {
      const card = el("article", "card");
      card.appendChild(el("h3", "", item.name || "Unnamed zone"));
      card.appendChild(el("p", "muted", `${item.event_count || 0} events · ${item.location_count || 0} locations`));
      const tags = el("div", "tags");
      [...(item.vibe || []), ...(item.good_for || [])].slice(0, 6).forEach((tag) => tags.appendChild(pill(tag)));
      card.appendChild(tags);
      return card;
    }

    function renderSection(section) {
      const wrap = el("section", "section");
      wrap.appendChild(el("h2", "", section.title || "Results"));
      const grid = el("div", "grid");
      const items = section.items || [];
      items.forEach((item) => {
        grid.appendChild(section.kind === "zones" ? renderZone(item) : section.kind === "detail" ? renderDetail(item) : renderEvent(item));
      });
      wrap.appendChild(grid);
      return wrap;
    }

    function render(payload) {
      const data = payload && payload.structuredContent ? payload.structuredContent : payload;
      if (!data) return;
      title.textContent = data.title || "Gentse Feesten Explorer";
      status.textContent = `${data.meta?.result_count ?? 0} results · ${data.mode || "search"}`;
      queryInput.value = data.query || "";
      dayInput.value = data.day || "";
      modeInput.value = data.mode || "search";
      content.replaceChildren();
      const sections = data.sections && data.sections.length ? data.sections : [{ title: data.title, kind: "events", items: data.items || [] }];
      if (!sections.some((section) => section.items && section.items.length)) {
        content.appendChild(el("div", "empty", "No matching festival items yet."));
        return;
      }
      sections.forEach((section) => {
        if (section.items && section.items.length) content.appendChild(renderSection(section));
      });
    }

    function callTool(name, args) {
      const id = state.seq++;
      const message = { jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } };
      window.parent.postMessage(message, "*");
      return new Promise((resolve, reject) => {
        state.pending.set(id, { resolve, reject });
        setTimeout(() => {
          if (state.pending.has(id)) {
            state.pending.delete(id);
            reject(new Error("Tool call timed out"));
          }
        }, 20000);
      });
    }

    window.addEventListener("message", (event) => {
      if (event.source !== window.parent) return;
      const message = event.data;
      if (!message || message.jsonrpc !== "2.0") return;
      if (message.method === "ui/notifications/tool-result") {
        render(message.params);
        return;
      }
      if (message.id && state.pending.has(message.id)) {
        const pending = state.pending.get(message.id);
        state.pending.delete(message.id);
        if (message.error) pending.reject(message.error);
        else pending.resolve(message.result);
      }
    }, { passive: true });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      submit.disabled = true;
      status.textContent = "Loading...";
      try {
        const result = await callTool("show_festival_explorer", {
          query: queryInput.value,
          day: dayInput.value,
          mode: modeInput.value
        });
        render(result);
      } catch (error) {
        status.textContent = error && error.message ? error.message : "Could not load results.";
      } finally {
        submit.disabled = false;
      }
    });
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
    structured_output=True,
    meta={
        "ui": {"resourceUri": APP_WIDGET_URI},
        "openai/outputTemplate": APP_WIDGET_URI,
        "openai/toolInvocation/invoking": "Opening the festival explorer...",
        "openai/toolInvocation/invoked": "Festival explorer ready.",
    },
)
def show_festival_explorer(
    query: str = "",
    day: str = "",
    mode: Literal["search", "day", "guide", "zones", "detail"] = "search",
    uuid: str = "",
) -> FestivalExplorerOutput:
    """Open the interactive ChatGPT widget for exploring Gentse Feesten 2026 events, day plans, guides, and zones."""
    return _festival_explorer_payload(query=query, day=day, mode=mode, uuid=uuid)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def search_events(
    query: str | list[str] = "",
    day: str = "",
    theme: str = "",
    free_only: bool = False,
    outdoor_only: bool = False,
    genre: str = "",
    wheelchair: bool = False,
    time_window: str = "",
    child_friendly: bool = False,
    participatory: bool = False,
    indoor_outdoor: str = "",
    limit: int = 10,
) -> WrapResponse:
    """Search events. query supports free text, Dutch aliases, and experience tags. Pass a list of strings for batch search with shared filters.
    time_window format: "HH:MM-HH:MM". indoor_outdoor: "indoor" or "outdoor"."""
    warnings = []
    expanded_queries = []

    if isinstance(query, list):
        clean_queries = [q.strip() for q in query if q.strip()][:20]
        for q in clean_queries:
            expanded = _expand_query(q)
            expanded_queries.extend(expanded)
    elif query.strip():
        expanded_queries = _expand_query(query)

    # Use first expanded query for primary search
    primary_query = expanded_queries[0] if expanded_queries else ""
    all_queries = " ".join(expanded_queries) if expanded_queries else ""

    # Build intent from query for scoring
    intent = _parse_intent(all_queries + " " + theme + " " + genre)

    iso_day = _resolve_day(day) if day else None
    th = theme.strip().lower()
    ge = genre.strip().lower()

    result = []
    for p in pages:
        # Hard filters
        if free_only and not p.get("free"):
            continue
        if outdoor_only and not p.get("outdoor"):
            continue
        if wheelchair and not p.get("wheelchair_ok"):
            continue
        if iso_day:
            if not _page_in_festival_day(p, iso_day):
                continue
        if th and not any(th in t.lower() for t in (p.get("themes") or [])):
            if not any(th in k.lower() for k in (p.get("keywords") or [])):
                continue
        if ge and (p.get("genre") or "").lower() != ge:
            continue

        # Child-friendly filter
        if child_friendly:
            exp_tags = _get_experience_tags(p.get("uuid", ""))
            if "family" not in exp_tags:
                # Also check keywords for fallback
                kw_text = " ".join(p.get("keywords", []) + p.get("themes", [])).lower()
                if not any(w in kw_text for w in ["kinder", "gezin", "familie", "kids"]):
                    continue

        # Participatory filter
        if participatory:
            exp_tags = _get_experience_tags(p.get("uuid", ""))
            if "participatory" not in exp_tags:
                continue

        # Indoor/outdoor filter
        if indoor_outdoor == "indoor" and p.get("outdoor"):
            continue
        if indoor_outdoor == "outdoor" and not p.get("outdoor"):
            continue

        # Time window filter
        if time_window:
            scoped_occurrences = _festival_day_occurrences(p, iso_day or "")
            times = [_occurrence_time(o) for o in scoped_occurrences]
            if not _matches_time_window(times, time_window):
                continue

        # Query matching
        if primary_query:
            haystack = " ".join([
                p.get("name") or "",
                p.get("desc") or "",
                " ".join(p.get("keywords") or []),
                " ".join(p.get("themes") or []),
                p.get("location") or "",
            ]).lower()
            if not any(q in haystack for q in expanded_queries):
                continue

        result.append(p)

    # Fallback: if no results, try with just the primary query (relaxed matching)
    if not result and primary_query:
        for p in pages:
            if free_only and not p.get("free"):
                continue
            if outdoor_only and not p.get("outdoor"):
                continue
            if iso_day:
                if not _page_in_festival_day(p, iso_day):
                    continue
            # Apply time_window filter in fallback too
            if time_window:
                scoped_occurrences = _festival_day_occurrences(p, iso_day or "")
                times = [_occurrence_time(o) for o in scoped_occurrences]
                if not _matches_time_window(times, time_window):
                    continue
            # Relaxed: check if primary query words appear in text
            haystack = " ".join([
                p.get("name") or "",
                p.get("desc") or "",
                " ".join(p.get("keywords") or []),
                " ".join(p.get("themes") or []),
            ]).lower()
            # Check if any word from primary query matches
            query_words = [w for w in primary_query.split() if len(w) > 2]
            if query_words and any(w in haystack for w in query_words):
                result.append(p)
        if result:
            warnings.append("Used relaxed matching for query")

    # Fallback 2: if still no results, return top scored events
    # (skip when time_window is set — don't return random events for a time-specific query)
    if not result and not time_window:
        for p in pages:
            if free_only and not p.get("free"):
                continue
            if outdoor_only and not p.get("outdoor"):
                continue
            result.append(p)
        result.sort(key=lambda p: _score_event(p, intent), reverse=True)
        result = result[:limit]
        warnings.append("No exact match found, showing top rated events")
    else:
        # Score and rank
        result.sort(key=lambda p: _score_event(p, intent), reverse=True)

    # Deduplicate by UUID (alias expansion can produce duplicates)
    seen_uuids: set[str] = set()
    deduped = []
    for p in result:
        uid = p.get("uuid", "")
        if uid in seen_uuids:
            continue
        seen_uuids.add(uid)
        deduped.append(p)
    result = deduped[:limit]

    # Add experience tags and match reasons to results
    enriched = []
    for p in result:
        uuid = p.get("uuid", "")
        short = _short(p, target_day=iso_day or "")
        exp_tags = _get_experience_tags(uuid)

        # Compute match reasons
        match_reasons = []
        for exp in intent.get("experience", []):
            if exp in exp_tags:
                match_reasons.append(f"matches {exp} preference")
        for act in intent.get("activity", []):
            if act in exp_tags:
                match_reasons.append(f"good for {act}")
        if intent.get("price") == "free" and p.get("free"):
            match_reasons.append("free entry")
        if intent.get("setting") == "outdoor" and p.get("outdoor"):
            match_reasons.append("outdoor venue")
        if not match_reasons and primary_query:
            match_reasons.append("keyword match")

        short["experience_tags"] = exp_tags
        short["match_reasons"] = match_reasons
        short["keywords"] = p.get("keywords", [])
        enriched.append(short)

    return _wrap("search_events", enriched, warnings)

@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def semantic_search(query: str, topk: int = 10, mode: str = "semantic") -> WrapResponse:
    """Vector search over events. mode: "semantic" (meaning-based, e.g. "romantic evening"), "fts" (exact keywords like "cirQ", "jazz"), "hybrid" (both — recommended default)."""
    if not _vector_coll or not _vector_emb:
        return _wrap("semantic_search", [], warnings=["Vector index not available — install zvec"])

    topk = min(topk, 50)

    if mode == "fts":
        from zvec.model.param.query import Fts, Query
        results = _vector_coll.query(
            queries=Query(field_name="name", fts=Fts(match_string=query)),
            topk=topk,
        )
        return _wrap("semantic_search", [_vector_doc_to_short(d) for d in results])

    if mode == "hybrid":
        from zvec.model.param.query import Fts, Query
        # Search across name, keywords, and themes
        fts_results = []
        for field in ["name", "keywords", "themes"]:
            try:
                results = _vector_coll.query(
                    queries=Query(field_name=field, fts=Fts(match_string=query)),
                    topk=min(topk * 2, 30),
                )
                fts_results.extend(results)
            except Exception:
                pass

        # Dedupe by canonical UUID (strip zvec index suffix)
        seen: set[str] = set()
        unique = []
        for doc in fts_results:
            canonical = doc.id.rsplit("_", 1)[0] if "_" in doc.id else doc.id
            if canonical in seen:
                continue
            seen.add(canonical)
            unique.append(doc)
        fts_results = unique[:min(topk * 3, 50)]

        # If FTS found nothing, fall back to pure semantic
        if not fts_results:
            q_vec = _vector_emb.embed(query)
            results = _vector_coll.query(
                queries=Query(field_name="embedding", vector=q_vec),
                topk=topk,
            )
            return _wrap("semantic_search", [_vector_doc_to_short(d, d.score) for d in results])

        # Fetch full docs with vectors for FTS hits
        q_vec = _vector_emb.embed(query)
        import numpy as np
        scored = []
        for doc in fts_results:
            # Re-fetch to get vectors
            fetched = _vector_coll.fetch(doc.id)
            full_doc = fetched.get(doc.id)
            if not full_doc:
                continue
            vec = full_doc.vectors.get("embedding")
            if vec is None:
                # Compute vector from embed_text
                embed_text = full_doc.fields.get("embed_text", "")
                vec = _vector_emb.embed(embed_text)
            sim = float(np.dot(q_vec, vec) / (np.linalg.norm(q_vec) * np.linalg.norm(vec)))
            scored.append((sim, full_doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return _wrap("semantic_search", [_vector_doc_to_short(d, s) for s, d in scored[:topk]])

    # Default: semantic vector search
    from zvec.model.param.query import Query
    q_vec = _vector_emb.embed(query)
    results = _vector_coll.query(
        queries=Query(field_name="embedding", vector=q_vec),
        topk=topk * 3,  # Fetch more to account for duplicates
    )

    # Deduplicate by UUID (keep first/highest scored)
    seen_uuids = set()
    unique = []
    for d in results:
        uuid = d.id.rsplit("_", 1)[0] if "_" in d.id else d.id
        if uuid not in seen_uuids:
            seen_uuids.add(uuid)
            unique.append(_vector_doc_to_short(d, d.score))
        if len(unique) >= topk:
            break

    return _wrap("semantic_search", unique)


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


def _event_summary_from_page(page: dict) -> dict:
    exclude = {"videos"}
    result = {k: v for k, v in page.items() if k not in exclude}
    result["occurrences"] = [
        {**o, "weekday": _iso_to_nl.get(o.get("day", ""), "")}
        for o in (page.get("occurrences") or [])
    ]
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def get_event_detail(uuid: str, detail: str = "full") -> EventDetail | EventDetailError:
    """Get event detail by UUID. detail='full' includes videos/images; summary excludes them."""
    if detail == "summary":
        return _event_summary(uuid)
    return _event_detail(uuid)


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_event_details(uuids: list[str], detail: str = "summary") -> list[BatchEventDetailResult]:
    """Batch fetch event details by UUIDs. Prefer over multiple get_event_detail calls. detail="summary" (default, no images) or "full" (with media)."""
    result: list[BatchEventDetailResult] = []
    for uuid in uuids:
        fn = _event_detail if detail == "full" else _event_summary
        d = fn(uuid)
        if "error" in d:
            result.append({"uuid": uuid, "event": None, "error": d["error"]})
        else:
            result.append({"uuid": uuid, "event": d, "error": None})
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def suggest(mood: str) -> WrapResponse:
    """Get recommendations from a natural-language mood or vibe. Dutch phrases auto-expand: "niet te braaf" → cabaret/queer/burlesque, "zwoel" → romantic/sensual, "iets voor kinderen" → family. Include day words for timing."""
    intent = _parse_intent(mood)
    m = mood.lower()

    day = ""
    iso_day = ""
    for nl_day in NL_DAYS:
        if nl_day in m:
            day = nl_day
            break
    if day:
        iso_day = _nl_to_iso.get(day, "")

    # Expand query for search
    expanded = _expand_query(mood)
    primary_query = expanded[0] if expanded else ""

    # Build theme from intent
    theme = ""
    theme_hints = [
        ("kinder", ["kinder", "gezin", "familie", "kids", "jeugd"]),
        ("dans", ["dans", "dansen", "bal", "tango", "swing", "salsa"]),
        ("theater", ["theater", "toneel", "voorstelling"]),
        ("comedy", ["comedy", "stand-up", "humor", "grappig", "lachen"]),
        ("circus", ["circus", "acrobat", "straattheater", "straat"]),
        ("jazz", ["jazz"]),
        ("boot", ["boot", "rondvaart", "water"]),
        ("wandeling", ["wandeling", "wandel", "rondleiding"]),
        ("expo", ["museum", "expo", "tentoonstelling", "bezoek"]),
        ("markt", ["markt"]),
        ("vertel", ["verhaal", "lezing", "poëzie", "poetry"]),
    ]
    for hint, keywords in theme_hints:
        if any(kw in m for kw in keywords):
            theme = hint
            break

    hits = _filter_pages(
        query=primary_query,
        day=iso_day or day,
        theme=theme,
        free_only=(intent["price"] == "free"),
        outdoor_only=(intent["setting"] == "outdoor"),
        genre=intent["genre"][0] if intent["genre"] else "",
        wheelchair=False,
    )

    # If too few results, relax filters progressively
    if len(hits) < 3:
        # First relax: remove free/outdoor constraints
        if intent["price"] == "free" or intent["setting"] == "outdoor":
            hits = _filter_pages(
                query=primary_query,
                day=iso_day or day,
                theme=theme,
                free_only=False,
                outdoor_only=False,
                genre=intent["genre"][0] if intent["genre"] else "",
                wheelchair=False,
            )
        # Second relax: try without query matching
        if len(hits) < 3:
            hits = _filter_pages(
                query="",
                day=iso_day or day,
                theme=theme,
                free_only=False,
                outdoor_only=False,
                genre=intent["genre"][0] if intent["genre"] else "",
                wheelchair=False,
            )
        # Third relax: just get top events
        if len(hits) < 3:
            hits = _filter_pages(day=iso_day or day, free_only=False)
    # Deduplicate by UUID (keep highest scored)
    seen_uuids = set()
    unique_hits = []
    for p in hits:
        uuid = p.get("uuid", "")
        if uuid not in seen_uuids:
            seen_uuids.add(uuid)
            unique_hits.append(p)

    # Enrich results with experience tags and match reasons
    enriched = []
    for p in unique_hits[:8]:
        uuid = p.get("uuid", "")
        short = _short(p, target_day=iso_day)
        exp_tags = _get_experience_tags(uuid)

        match_reasons = []
        for exp in intent.get("experience", []):
            if exp in exp_tags:
                match_reasons.append(f"matches {exp} preference")
        for act in intent.get("activity", []):
            if act in exp_tags:
                match_reasons.append(f"good for {act}")
        if intent.get("price") == "free" and p.get("free"):
            match_reasons.append("free entry")
        if intent.get("setting") == "outdoor" and p.get("outdoor"):
            match_reasons.append("outdoor venue")
        if not match_reasons:
            match_reasons.append("general match")

        short["experience_tags"] = exp_tags
        short["match_reasons"] = match_reasons
        enriched.append(short)

    return _wrap("suggest", enriched, warnings=[])


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
def events_by_location(location_name: str, day: str = "") -> list[dict]:
    needle = location_name.strip().lower()
    iso_day = _resolve_day(day) if day else None
    result: list[dict] = []
    for p in pages:
        if needle not in (p.get("location") or "").lower():
            continue
        if iso_day:
            if not _page_in_festival_day(p, iso_day):
                continue
        result.append(_short(p, target_day=iso_day or ""))

    result.sort(key=lambda x: (x["days"][0] if x["days"] else "", x["first_time"]))
    return result


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def free_highlights(day: str = "") -> list[dict]:
    """Top 10 free event picks for a day (or entire festival if no day given)."""
    iso_day = _resolve_day(day) if day else None
    hits = _filter_pages(day=day, free_only=True)
    hits.sort(key=_score_highlight, reverse=True)
    return [_short(p, target_day=iso_day or "") for p in hits[:10]]


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def get_today() -> GetTodayOutput:
    """Get current date and festival day info. Use when user says 'today' or 'tonight'."""
    today = date.today().isoformat()
    in_festival = today in _iso_to_nl
    return {
        "date": today,
        "weekday": _iso_to_nl.get(today, ""),
        "in_festival": in_festival,
        "festival_range": f"{days_data[0]['day']} to {days_data[-1]['day']}",
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def plan_day(day: str = "") -> PlanDayOutput:
    """Full day overview in one call: day info, top 10 free highlights, and 5 themed picks. day: ISO date or Dutch weekday; empty = today."""
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
        short = _short(p, target_day=iso or "")
        for t in p.get("themes", []):
            if t not in themes_seen and len(picks) < 5:
                themes_seen.add(t)
                picks.append({
                    "theme": t,
                    "name": short["name"],
                    "location": short["location"],
                    "time": short["occurrence_time"],
                    "uuid": short["uuid"],
                })
                break

    return {
        "day_info": day_info,
        "free_highlights": [_short(p, target_day=iso or "") for p in free[:10]],
        "themed_picks": picks,
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def create_festival_guide(
    vibe: str,
    energy_level: Literal["low", "medium", "high", "mixed"] = "mixed",
    social_mode: Literal["solo", "couple", "group", "family", "mixed"] = "mixed",
    max_paid_events: int = 5,
    day: str = "",
) -> CreateGuideOutput:
    """Build a personalized festival guide. vibe: comma-separated interests/moods (Dutch OK). energy_level: "low"/"medium"/"high"/"mixed". social_mode: "solo"/"couple"/"group"/"family"/"mixed". Returns anchor+fallback events, zone picks, and energy pacing strategy."""
    intent = _parse_intent(vibe)
    iso_day = _resolve_day(day) if day else None

    # Get all events with filters
    all_events = _filter_pages(
        day=day,
        free_only=(intent["price"] == "free"),
        outdoor_only=(intent["setting"] == "outdoor"),
    )

    # Score and rank
    for p in all_events:
        p["_score"] = _score_event(p, intent)
    all_events.sort(key=lambda p: p["_score"], reverse=True)

    # Split into anchor (high score) and fallback (lower score)
    anchor_events = []
    fallback_events = []
    paid_count = 0

    for p in all_events:
        short = _short(p, target_day=iso_day or "")
        uuid = p.get("uuid", "")
        exp_tags = _get_experience_tags(uuid)

        short["experience_tags"] = exp_tags
        short["match_reasons"] = []
        for exp in intent.get("experience", []):
            if exp in exp_tags:
                short["match_reasons"].append(f"matches {exp}")
        for act in intent.get("activity", []):
            if act in exp_tags:
                short["match_reasons"].append(f"good for {act}")

        is_free = p.get("free", False)
        if not is_free:
            paid_count += 1

        if len(anchor_events) < 5 and (is_free or paid_count <= max_paid_events) and (short["match_reasons"] or not intent.get("experience")):
            anchor_events.append(short)
        elif len(fallback_events) < 5:
            fallback_events.append(short)

    # Find relevant zones
    relevant_zones = []
    for z in zones_data:
        zone_text = " ".join(z.get("vibe", []) + z.get("good_for", []) + [z.get("notes", "")]).lower()
        for exp in intent.get("experience", []):
            if exp in zone_text:
                relevant_zones.append({
                    "name": z["zone"],
                    "vibe": z["vibe"],
                    "good_for": z["good_for"],
                    "notes": z.get("notes", ""),
                })
                break

    # Energy strategy
    energy_strategy = []
    if energy_level == "low" or intent.get("energy") == "low":
        energy_strategy = [
            "Start rustig met een wandeling of museumbezoek",
            "Pauzeer in Baudelopark of Kouter",
            "Einde met iets zachts: jazz, klassiek of een tentoonstelling"
        ]
    elif energy_level == "high" or intent.get("energy") == "high":
        energy_strategy = [
            "Begin met iets actiefs: dans, straattheater, deelname",
            "Bouw op naar het hoogtepunt in de avond",
            "Einde op een feestplein: Vlasmarkt, Korenmarkt"
        ]
    else:
        energy_strategy = [
            "Wissel af tussen actief en rustig",
            "Gebruik zones als overgang: parken, markten, pleinen",
            "Houd een fallback achter de hand voor als je moe wordt"
        ]

    # Guide title based on vibe
    vibe_words = [w.strip() for w in vibe.split(",")[:3]]
    guide_title = " & ".join(vibe_words).title() if vibe_words else "Jouw Festivalgids"

    return {
        "guide_title": guide_title,
        "vibe_summary": f"Gids voor: {vibe}",
        "anchor_events": anchor_events,
        "fallback_events": fallback_events,
        "zones": relevant_zones[:3],
        "energy_strategy": energy_strategy,
        "total_events_considered": len(all_events),
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def find_similar_events(uuid: str, limit: int = 5) -> FindSimilarOutput:
    """Find events similar to the given event. Returns categorized results: same_vibe (vector similarity), nearby (same location), good_afterwards (available later same day), related (parent/sibling events)."""
    # Find the event in pages (may have multiple records with same UUID)
    p = None
    for page in pages:
        if page.get("uuid") == uuid:
            p = page
            break
    if not p:
        return []

    source_location = (p.get("location") or "").lower()
    source_occurrences = p.get("occurrences") or []
    source_times = [o.get("time", "12:00") for o in source_occurrences]

    if not _vector_coll or not _vector_emb:
        return {
            "source_event": _short(p),
            "same_vibe": [],
            "nearby": [],
            "good_afterwards": [],
            "related": [],
        }

    # Fetch source event from vector index - find the one with this UUID
    doc = None
    for i in range(len(pages)):
        result = _vector_coll.fetch(f"{uuid}_{i}")
        fetched = result.get(f"{uuid}_{i}")
        if fetched:
            doc = fetched
            break

    if not doc:
        return {
            "source_event": _short(p),
            "same_vibe": [],
            "nearby": [],
            "good_afterwards": [],
            "related": [],
        }

    vec = doc.vectors.get("embedding")
    if vec is None:
        embed_text = doc.fields.get("embed_text", "")
        vec = _vector_emb.embed(embed_text)

    # Find nearest neighbors
    from zvec.model.param.query import Query
    results = _vector_coll.query(
        queries=Query(field_name="embedding", vector=vec),
        topk=limit + 5,
    )

    same_vibe = []
    nearby = []
    good_afterwards = []
    related = []
    seen_uuids = set()

    for hit in results:
        # Extract UUID from hit ID (format: uuid_index)
        hit_uuid = hit.id.rsplit("_", 1)[0] if "_" in hit.id else hit.id
        if hit_uuid == uuid or hit_uuid in seen_uuids:
            continue
        seen_uuids.add(hit_uuid)

        short = _vector_doc_to_short(hit, hit.score)
        short["match_reasons"] = [f"vector similarity: {hit.score:.2f}"]

        # Check if nearby (same location)
        hit_location = (hit.fields.get("location") or "").lower()
        if hit_location and source_location and hit_location == source_location:
            short["match_reasons"].append("same location")
            nearby.append(short)

            # Good afterwards check - find this event in pages
            for hp in pages:
                if hp.get("uuid") == hit_uuid:
                    hit_occurrences = hp.get("occurrences") or []
                    hit_times = [o.get("time", "12:00") for o in hit_occurrences]
                    if hit_times and source_times:
                        if max(hit_times) > min(source_times):
                            short["match_reasons"].append("available after this event")
                            good_afterwards.append(short)
                    break
        else:
            same_vibe.append(short)

    # Add parent/sibling relationships
    parent_uuid = p.get("parent_uuid", "")
    if parent_uuid:
        # This is a child event - add parent and siblings
        for other in pages:
            if other.get("uuid") == parent_uuid:
                parent_short = _short(other)
                parent_short["match_reasons"] = ["parent event"]
                related.append(parent_short)
                break

        for other in pages:
            if other.get("parent_uuid") == parent_uuid and other.get("uuid") != uuid:
                sibling_short = _short(other)
                sibling_short["match_reasons"] = ["sibling event (same parent)"]
                related.append(sibling_short)
    else:
        # Check if this is a parent event - add children
        subevent_urls = p.get("subevent", "")
        if subevent_urls:
            for other in pages:
                if other.get("parent_uuid") == uuid:
                    child_short = _short(other)
                    child_short["match_reasons"] = ["child event"]
                    related.append(child_short)

    return {
        "source_event": _short(p),
        "same_vibe": same_vibe[:limit],
        "nearby": nearby[:limit],
        "good_afterwards": good_afterwards[:limit],
        "related": related[:limit],
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def get_parent_event(uuid: str) -> ParentEventOutput:
    """Given a parent or child event UUID, return the parent event with all occurrences (child events + all dates/times)."""
    p = None
    for page in pages:
        if page.get("uuid") == uuid:
            p = page
            break
    if not p:
        return {"uuid": uuid, "name": "", "occurrences": [], "children": [], "total_dates": 0}

    subevent_urls = p.get("subevent", "")
    if subevent_urls:
        children = []
        for other in pages:
            if other.get("parent_uuid") == uuid:
                children.append(_short(other))
        children.sort(key=lambda c: c.get("first_time", ""))
        return {"uuid": p["uuid"], "name": p["name"], "occurrences": p.get("occurrences", []), "children": children, "total_dates": len(children)}

    parent_uuid = p.get("parent_uuid", "")
    if parent_uuid:
        parent = None
        for other in pages:
            if other.get("uuid") == parent_uuid:
                parent = other
                break
        if parent:
            siblings = []
            for other in pages:
                if other.get("parent_uuid") == parent_uuid:
                    siblings.append(_short(other))
            siblings.sort(key=lambda s: s.get("first_time", ""))
            return {"uuid": parent["uuid"], "name": parent["name"], "occurrences": parent.get("occurrences", []), "children": siblings, "total_dates": len(siblings)}

    return {"uuid": p["uuid"], "name": p["name"], "occurrences": p.get("occurrences", []), "children": [], "total_dates": len(p.get("occurrences", []))}


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)
def get_zone_profile(zone_name: str) -> ZoneProfileOutput:
    """Rich zone profile: vibe, good_for, best_moment, target_audience, and top 20 events with experience tags. Partial name match."""
    z = None
    for zone in zones_data:
        if zone_name.lower() in zone["zone"].lower():
            z = zone
            break

    if not z:
        return None

    # Get events in this zone
    zone_locations = {loc.lower() for loc in z.get("locations", [])}
    zone_events = []
    for p in pages:
        if (p.get("location") or "").lower() in zone_locations:
            short = _short(p)
            uuid = p.get("uuid", "")
            short["experience_tags"] = _get_experience_tags(uuid)
            zone_events.append(short)


    zone_events.sort(key=lambda e: (e.get("days", [""])[0] if e.get("days") else "", e.get("first_time", "")))

    return {
        "zone": z["zone"],
        "vibe": z.get("vibe", []),
        "good_for": z.get("good_for", []),
        "notes": z.get("notes", ""),
        "best_moment": z.get("best_moment", ""),
        "target_audience": z.get("target_audience", ""),
        "event_count": len(zone_events),
        "events": zone_events[:20],
    }


@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def list_zones() -> list[dict]:
    """List all festival zones with vibe and good_for tags."""
    return [
        {
            "zone": z["zone"],
            "vibe": z["vibe"],
            "good_for": z["good_for"],
            "event_count": z["event_count"],
            "location_count": z["location_count"],
        }
        for z in zones_data
    ]
@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS, structured_output=True)

def zone_detail(zone_name: str) -> ZoneDetailOutput:
    """Zone details: locations list, coordinates, outdoor ratio, and top events. For richer profile with audience tips, use get_zone_profile."""
    z = _zones_by_name.get(zone_name.strip().lower())
    if not z:
        # Try partial match
        for key, val in _zones_by_name.items():
            if zone_name.strip().lower() in key:
                z = val
                break
    if not z:
        return {"error": f"unknown zone '{zone_name}'", "available_zones": [zz["zone"] for zz in zones_data]}

    # Get top events in this zone
    zone_events = []
    for p in pages:
        if (p.get("location") or "").lower() in {loc.lower() for loc in z["locations"]}:
            zone_events.append(_short(p))
    zone_events.sort(key=lambda x: (x["days"][0] if x["days"] else "", x["first_time"]))

    return {
        "zone": z["zone"],
        "locations": z["locations"],
        "vibe": z["vibe"],
        "good_for": z["good_for"],
        "event_count": z["event_count"],
        "location_count": z["location_count"],
        "outdoor_ratio": z["outdoor_ratio"],
        "lat": z["lat"],
        "lon": z["lon"],
        "events": zone_events[:20],
    }

@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_by_zone(
    zone_name: str,
    query: str = "",
    day: str = "",
    theme: str = "",
    free_only: bool = False,
    outdoor_only: bool = False,
    genre: str = "",
    wheelchair: bool = False,
    limit: int = 10,
) -> list[dict]:
    """Search events within a festival zone. Supports same filters as search_events. Use list_zones for zone names."""
    z = _zones_by_name.get(zone_name.strip().lower())
    if not z:
        for key, val in _zones_by_name.items():
            if zone_name.strip().lower() in key:
                z = val
                break
    if not z:
        return {"error": f"unknown zone '{zone_name}'", "available_zones": [zz["zone"] for zz in zones_data]}

    zone_locations = {loc.lower() for loc in z["locations"]}
    iso_day = _resolve_day(day) if day else None
    q = query.strip().lower()
    th = theme.strip().lower()
    ge = genre.strip().lower()

    result = []
    for p in pages:
        if (p.get("location") or "").lower() not in zone_locations:
            continue
        if free_only and not p.get("free"):
            continue
        if outdoor_only and not p.get("outdoor"):
            continue
        if wheelchair and not p.get("wheelchair_ok"):
            continue
        if iso_day:
            if not _page_in_festival_day(p, iso_day):
                continue
        # Genre filter: check genre field, themes, and keywords
        if ge:
            in_genre = ge in (p.get("genre") or "").lower()
            in_themes_kw = ge in " ".join((p.get("themes") or []) + (p.get("keywords") or [])).lower()
            if not in_genre and not in_themes_kw:
                continue
        if q:
            haystack = " ".join([
                p.get("name") or "",
                p.get("desc") or "",
                " ".join(p.get("keywords") or []),
                " ".join(p.get("themes") or []),
            ]).lower()
            if q not in haystack:
                continue
        result.append(_short(p, target_day=iso_day or ""))

    result.sort(key=lambda x: (x["days"][0] if x["days"] else "", x["first_time"]))
    return result[:limit]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource(
    APP_WIDGET_URI,
    name="gf2026-explorer-widget",
    title="Gentse Feesten Explorer Widget",
    description="Interactive ChatGPT Apps widget for exploring Gentse Feesten 2026.",
    mime_type=APP_WIDGET_MIME_TYPE,
    meta={
        "ui": {
            "prefersBorder": True,
            "domain": APP_WIDGET_DOMAIN,
            "csp": {
                "connectDomains": [APP_WIDGET_DOMAIN],
                "resourceDomains": [],
            },
        },
        "openai/widgetDescription": "Shows an interactive Gentse Feesten 2026 explorer for events, day plans, guides, and festival zones.",
    },
)
def festival_explorer_widget() -> str:
    """ChatGPT Apps widget template."""
    return _festival_explorer_widget_html()


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
- **Zones:** {len(zones_data)} festival zones

Tools: `plan_day` (full day), `search_events` (filtered), `suggest` (natural language), `get_event_summary`/`get_event_detail` (event info), `free_highlights`, `events_by_location`, `list_zones`, `zone_detail`, `search_by_zone`.
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


@mcp.resource("gf://zones")
def resource_zones() -> str:
    """Overzicht van alle festivalzones met vibe en good_for tags."""
    lines = [f"# Gentse Feesten 2026 — Festivalzones ({len(zones_data)})\n"]
    for z in zones_data:
        vibe_str = ", ".join(z["vibe"][:3]) if z["vibe"] else "-"
        good_str = ", ".join(z["good_for"][:3]) if z["good_for"] else "-"
        lines.append(f"- **{z['zone']}**: {z['event_count']} evenementen, {z['location_count']} locaties")
        lines.append(f"  Vibe: {vibe_str} · Goed voor: {good_str}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")































