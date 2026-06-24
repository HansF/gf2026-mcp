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

from mcp.server.fastmcp import FastMCP

DATA_DIR = Path(__file__).parent.parent / "site" / "data"

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
TAG_RESOURCE_LIMIT = 300

HOST = os.getenv("GF_MCP_HOST", "127.0.0.1")
PORT = int(os.getenv("GF_MCP_PORT", "8000"))

mcp = FastMCP(
    "Gentse Feesten 2026",
    host=HOST,
    port=PORT,
    stateless_http=True,
    json_response=True,
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
    """Compact event-record voor lijstweergave."""
    occ = p.get("occurrences") or []
    days_list = sorted({o["day"] for o in occ})
    return {
        "uuid": p["uuid"],
        "name": p["name"],
        "days": days_list,
        "first_day": days_list[0] if days_list else "",
        "first_weekday": _iso_to_nl.get(days_list[0], "") if days_list else "",
        "first_time": occ[0]["time"] if occ else "",
        "location": p.get("location", ""),
        "free": p.get("free", False),
        "outdoor": p.get("outdoor", False),
        "themes": p.get("themes", []),
        "desc": (p.get("desc") or "")[:200],
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

@mcp.tool()
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
    """Zoek evenementen van de Gentse Feesten 2026.

    Args:
        query: Vrije zoektekst (naam, beschrijving, trefwoorden).
        day: Festivaldag als ISO-datum ('2026-07-19') of Nederlands dagwoord
             ('vrijdag', 'zaterdag', ...).
        theme: Deelstring van een thema ('jazz', 'kinder', 'dans', 'theater',
               'comedy', 'circus', 'wandeling', 'boot', 'markt', ...).
        free_only: Toon alleen gratis evenementen.
        outdoor_only: Toon alleen buitenevenementen.
        genre: Muziekgenre ('jazz', 'rock', 'klassiek', 'folk', 'pop', 'blues',
               'world', 'electronica', 'soul', 'metal', 'chanson', ...).
        wheelchair: Toon alleen rolstoeltoegankelijke evenementen.
        limit: Maximum aantal resultaten (default 10).
    """
    hits = _filter_pages(query, day, theme, free_only, outdoor_only, genre, wheelchair)
    return [_short(p) for p in hits[:limit]]


@mcp.tool()
def get_event_detail(uuid: str) -> dict:
    """Geef het volledige detail van één evenement op UUID.

    Returns alle velden: beschrijving, alle data/tijdstippen (occurrences),
    prijs (offers), contactinfo, trefwoorden, duur, video-links,
    toegankelijkheid, etc.
    """
    p = _pages_by_uuid.get(uuid.strip())
    if not p:
        return {"error": f"Geen evenement gevonden met uuid '{uuid}'."}
    # Verrijkt met weekdagen per occurrence
    result = dict(p)
    result["occurrences"] = [
        {**o, "weekday": _iso_to_nl.get(o.get("day", ""), "")}
        for o in (p.get("occurrences") or [])
    ]
    return result


@mcp.tool()
def suggest(mood: str) -> list[dict]:
    """Aanbevelingen op basis van wat je wil doen of voelen.

    Omschrijf vrij wat je zoekt en de server filtert de beste opties eruit.

    Voorbeelden:
      'gratis jazz concert buiten vanavond'
      'iets voor kinderen op zaterdag'
      'romantisch dansen'
      'rustig museumbezoek'
      'circus of straattheater'
      'rolstoeltoegankelijk comedy'

    Args:
        mood: Vrije omschrijving van wat je zoekt.
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

    # Bouw reden-veld per event
    result = []
    for p in hits[:8]:
        short = _short(p)
        reasons = []
        if free_only and p.get("free"):
            reasons.append("gratis")
        if outdoor_only and p.get("outdoor"):
            reasons.append("buiten")
        if theme and any(theme in t.lower() for t in p.get("themes", [])):
            reasons.append(f"thema: {', '.join(p['themes'])}")
        if genre and p.get("genre"):
            reasons.append(f"genre: {p['genre']}")
        if wheelchair and p.get("wheelchair_ok"):
            reasons.append("rolstoeltoegankelijk")
        if not reasons:
            reasons.append("past bij je zoekopdracht")
        short["reden"] = " · ".join(reasons)
        result.append(short)

    return result


@mcp.tool()
def list_themes() -> list[dict]:
    """Geef alle 18 thema's van de Gentse Feesten met het aantal evenementen per thema.

    Thema's zijn: Varia, Concerten (jazz/rock/klassiek/divers),
    Tentoonstellingen, Kinder- en jeugdprogramma's, Wandelingen, Theater,
    Comedy, Bals/Dans, Spel & sport, Boottochten, Vertellingen, Circus, ...
    """
    return themes_data


@mcp.tool()
def list_days() -> list[dict]:
    """Geef alle 11 festivaldagen met het aantal evenementen per dag.

    Het festival loopt van vrijdag 17 juli tot maandag 27 juli 2026 in Gent.
    """
    return [
        {"day": d["day"], "weekday": _iso_to_nl.get(d["day"], ""), "count": d["count"]}
        for d in days_data
    ]


@mcp.tool()
def search_tags(query: str, limit: int = 20) -> list[dict]:
    """Zoek in alle trefwoorden/tags van de evenementen.

    Args:
        query: Deel van een tag, niet hoofdlettergevoelig.
        limit: Maximum aantal resultaten (default 20, maximum 100).

    Resultaten zijn gesorteerd op aantal gekoppelde evenementen.
    """
    needle = query.strip().casefold()
    safe_limit = max(1, min(limit, 100))
    if not needle:
        return tags_data[:safe_limit]
    return [
        tag for tag in tags_data
        if needle in tag["name"].casefold()
    ][:safe_limit]


@mcp.tool()
def events_by_location(location_name: str, day: str = "") -> list[dict]:
    """Geef alle evenementen op een specifieke locatie.

    Args:
        location_name: (Deel van) de locatienaam, niet hoofdlettergevoelig.
                       Bijv. 'Vrijdagmarkt', 'Gravensteen', 'Vooruit'.
        day: Optioneel filter op dag (ISO-datum of Nederlands dagwoord).
    """
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

    result.sort(key=lambda x: (x["first_day"], x["first_time"]))
    return result


@mcp.tool()
def free_highlights(day: str = "") -> list[dict]:
    """Top 10 gratis aanraders voor een dag of het hele festival.

    Selecteert gratis evenementen en sorteert op: buitenlocatie bonus,
    aanwezigheid van beschrijving, en aantal voorstellingen.

    Args:
        day: Optioneel filter op dag (ISO-datum of Nederlands dagwoord).
             Zonder dag: beste gratis events van het hele festival.
    """
    hits = _filter_pages(day=day, free_only=True)
    hits.sort(key=_score_highlight, reverse=True)
    return [_short(p) for p in hits[:10]]


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

Het grootste gratis stadsfestival van Europa, 10 dagen lang in het hart van Gent (België).

## Basisinfo
- **Datum:** {day_range} (11 dagen)
- **Locatie:** Gent, België — meer dan 300 locaties door de hele stad
- **Unieke evenementen:** {len(pages)}
- **Totale voorstellingen:** {len(events)}

## Highlights
- **Gratis evenementen:** {n_free} van {len(pages)} ({n_free*100//len(pages)}%)
- **Buitenevenementen:** {n_outdoor}
- **Evenementen met video:** {n_video}

## Thema's
{theme_names}, ...

## Muziekgenres
{genre_names}, ...

## Tips voor gebruik
- Gebruik `search_events` met filters voor gerichte zoekacties
- Gebruik `suggest` voor vrije omschrijvingen ("gratis jazz buiten zaterdag")
- Gebruik `get_event_detail(uuid)` voor volledige info inclusief tickets en contact
- Gebruik `free_highlights` voor de beste gratis opties per dag
- Gebruik `events_by_location` om te zien wat er speelt op een specifieke plek
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
    """De 300 meest gebruikte trefwoorden/tags met evenementenaantallen."""
    lines = [
        f"# Gentse Feesten 2026 — Top {TAG_RESOURCE_LIMIT} tags",
        "",
        (
            f"Er zijn {len(tags_data)} unieke tags. Hoofdlettervarianten zijn "
            "samengevoegd en de tags zijn gesorteerd op aantal evenementen."
        ),
        "Gebruik `search_tags` om in de volledige taglijst te zoeken.",
        "",
    ]
    for tag in tags_data[:TAG_RESOURCE_LIMIT]:
        lines.append(f"- {tag['name']}: {tag['count']} evenementen")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
