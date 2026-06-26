# Token Optimization Implementation Plan

> [!NOTE]
> This document may not reflect the current implementation.
> See the final report for up-to-date state:
> [Final Report](../reports/token-optimization.md)

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce token consumption of the Gentse Feesten 2026 MCP server by 40-60% for typical "plan my day" user flows.

**Architecture:** Trim tool descriptions, add purpose-built tools for common flows, reduce payload sizes by removing rarely-used fields from default responses, and limit resource sizes.

**Tech Stack:** Python 3.10+, FastMCP

---

## Analysis Summary

Measured baseline (chars / ~tokens per tool response):
- `list_days` + `free_highlights(day)` + `search_events(day=day)`: 12956 chars / ~3239 tokens
- `get_event_detail`: 2225 chars / ~556 tokens (includes images, videos, copyright)
- `gf://tags` resource: 9152 chars / ~2288 tokens (300 tags)
- `gf://overview` resource: 1188 chars / ~297 tokens
- Total tool schema overhead: significant (verbose Dutch descriptions)

---

### Task 1: Trim Tool Descriptions and Annotations

**Files:**
- Modify: `/home/hans/Projects/gf2026-mcp/server.py` (all `@mcp.tool` decorators and docstrings)

**Changes:**
- Remove bilingual descriptions (keep English only)
- Shorten docstrings to 1-2 lines (just enough for tool selection)
- Remove redundant parameter descriptions that duplicate type hints

Specifically:

```python
# Current:
@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_events(
    query: str = "",
    day: str = "",
    ...
) -> list[dict]:
    """Zoek evenementen van de Gentse Feesten 2026.

    Args:
        query: Vrije zoektekst (naam, beschrijving, trefwoorden).
        day: Festivaldag als ISO-datum ('2026-07-19') of Nederlands dagwoord
             ('vrijdag', 'zaterdag', ...).
        theme: Deelstring van een thema ('jazz', 'kinder', 'dans', 'theater',
               'comedy', 'circus', 'wandeling', 'boot', 'markt', ...).
        ...
    """
    ...

# New:
@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def search_events(
    query: str = "",
    day: str = "",
    ...
) -> list[dict]:
    """Search events. Supports free text, day, theme, genre, accessibility, and outdoor filters."""
    ...
```

Similarly trim all 10 tools. The key principle: tool descriptions are for the LLM to decide *which* tool to call, not how to use it. The schema already provides parameter names.

- [ ] **Step 1:** Edit all `@mcp.tool` docstrings to be 1-2 lines each
- [ ] **Step 2:** Verify server starts: `python3 server.py > /tmp/test.log 2>&1 & sleep 3 && curl -s http://127.0.0.1:8000/mcp -X POST -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}' | python3 -c "import sys,json;[print(json.loads(l[6:])['result']['serverInfo']) for l in sys.stdin if l.startswith('data:')]"  pkill -f server.py`
- [ ] **Step 3:** Verify tools still load: `curl -s http://127.0.0.1:8000/mcp -X POST ... tools/list | python3 -c "print(len([t for t in json.loads([l for l in sys.stdin if l.startswith('data:')][0][6:])['result']['tools']))"`

---

### Task 2: Add `get_event_summary` Tool

**Files:**
- Modify: `/home/hans/Projects/gf2026-mcp/server.py`

**Purpose:** Return event detail without images, videos, video captions, or image copyright fields. Cuts `get_event_detail` from ~2225 to ~800 chars per event.

Add this function after `_event_detail`:

```python
_LIGHT_EXCLUDE = {"image", "image_caption", "image_copyright", "videos"}

def _event_summary(uuid: str) -> dict:
    clean_uuid = uuid.strip()
    p = _pages_by_uuid.get(clean_uuid)
    if not p:
        return {"error": f"unknown uuid '{uuid}'"}
    result = {k: v for k, v in p.items() if k not in _LIGHT_EXCLUDE}
    result["occurrences"] = [
        {**o, "weekday": _iso_to_nl.get(o.get("day", ""), "")}
        for o in (p.get("occurrences") or [])
    ]
    return result

@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def get_event_summary(uuid: str) -> dict:
    """Full event detail excluding images and video links. Use for planning and comparison."""
    return _event_summary(uuid)
```

- [ ] **Step 1:** Add `_event_summary` and `get_event_summary` after the existing `_event_detail` function
- [ ] **Step 2:** Test: call with a valid UUID, verify image/videos/image_caption/image_copyright are absent
- [ ] **Step 3:** Test: call with invalid UUID, verify error response

---

### Task 3: Add `plan_day` Tool

**Files:**
- Modify: `/home/hans/Projects/gf2026-mcp/server.py`

**Purpose:** The most common user intent is "what should I do on [day]?" Currently this requires 2-3 tool calls (list_days + free_highlights + search_events). One call replaces all three.

```python
@mcp.tool(annotations=READ_ONLY_TOOL_ANNOTATIONS)
def plan_day(day: str = "") -> dict:
    """Get a full day plan: festival day info, free highlights, and themed picks.

    Returns a structured overview with:
    - day_info: date, weekday, total events count
    - free_highlights: top 10 free events
    - picks: categorized suggestions across different themes

    Args:
        day: ISO date ('2026-07-19') or Dutch weekday ('vrijdag'). Defaults to today.
    """
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

    # Get diverse picks across themes
    themes_seen = set()
    picks = []
    for p in _filter_pages(day=iso or "", limit=50):
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
```

- [ ] **Step 1:** Add `plan_day` tool after existing tools
- [ ] **Step 2:** Test with valid day: `plan_day('zaterdag')` returns structured response
- [ ] **Step 3:** Test with empty arg: `plan_day()` returns today's plan or empty day_info
- [ ] **Step 4:** Measure response size: should be ~2000 chars vs 12956 for the 3-call approach

---

### Task 4: Trim `gf://tags` Resource

**Files:**
- Modify: `/home/hans/Projects/gf2026-mcp/server.py`

**Change:** Limit `resource_tags` to top 50 tags instead of 300. 50 tags cover the vast majority of searchable concepts. Full list still available via `search_tags` tool.

```python
@mcp.resource("gf://tags")
def resource_tags() -> str:
    """Top 50 event tags with event counts. Use search_tags for full list."""
    lines = [
        "# Gentse Feesten 2026 — Top tags",
        "",
        f"{len(tags_data)} unique tags available. Showing top 50 by event count.",
        "Use `search_tags` to find specific tags.",
        "",
    ]
    for tag in tags_data[:50]:
        lines.append(f"- {tag['name']}: {tag['count']} evenementen")
    return "\n".join(lines)
```

- [ ] **Step 1:** Change `TAG_RESOURCE_LIMIT = 50` (or just hardcode 50 in resource_tags)
- [ ] **Step 2:** Verify resource returns ~50 entries
- [ ] **Step 3:** Measure: should drop from 9152 to ~1500 chars

---

### Task 5: Trim `_short` Output and `get_event_detail`

**Files:**
- Modify: `/home/hans/Projects/gf2026-mcp/server.py`

**Changes:**
- `_short()`: Remove `desc` field (LLM can get it via get_event_summary when needed). Saves ~200 chars per event in list views.
- `get_event_detail()`: Exclude image_caption and image_copyright from default detail (keep image URL). These fields are metadata for display UIs, not useful for planning.

```python
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
```

And in `_event_detail`:
```python
def _event_detail(uuid: str) -> EventDetail | EventDetailError:
    clean_uuid = uuid.strip()
    p = _pages_by_uuid.get(clean_uuid)
    if not p:
        return {"error": f"unknown uuid '{uuid}'"}
    result = {k: v for k, v in p.items() if k not in {"image_caption", "image_copyright"}}
    result["occurrences"] = [
        {**o, "weekday": _iso_to_nl.get(o.get("day", ""), "")}
        for o in (p.get("occurrences") or [])
    ]
    return cast(EventDetail, result)
```

- [ ] **Step 1:** Edit `_short()` to remove `desc`, `first_day` (redundant with days list)
- [ ] **Step 2:** Edit `_event_detail()` to exclude image_caption and image_copyright
- [ ] **Step 3:** Update chatgpt-app-submission.json test cases that expected image fields if needed
- [ ] **Step 4:** Measure: each search result saves ~180 chars → 10 events saves ~1800 chars

---

### Task 6: Update Submission Metadata

**Files:**
- Modify: `/home/hans/Projects/gf2026-mcp/chatgpt-app-submission.json`

**Changes:**
- Add `get_event_summary` and `plan_day` to tools section with justifications
- Update `search_events` justification to mention summary tool

- [ ] **Step 1:** Add tool definitions for `get_event_summary` and `plan_day`
- [ ] **Step 2:** Add a test case for `plan_day` (user_prompt: "Plan een dag voor mij op zaterdag 18 juli")

---

### Task 7: Final Verification

**Files:** (none — verification only)

- [ ] **Step 1:** Restart server, run all measurements from `/tmp/measure_direct.py` flow, confirm total reduction
- [ ] **Step 2:** Compare "plan my day" flow: old (16516 chars) vs new (`plan_day` single call should be ~3000 chars)
- [ ] **Step 3:** Verify all existing test cases still produce correct output
- [ ] **Step 4:** Commit changes

**Target metrics:**
- Tool schema overhead: -50% (shorter descriptions)
- "Plan my day" flow: 16516 → ~3000 chars (82% reduction)
- Per-event in lists: ~180 chars saved (desc + first_day removed)
- Tags resource: 9152 → ~1500 chars (84% reduction)
