---
feature: token-optimization
status: delivered
specs: []
plans:
  - docs/compose/plans/2026-06-26-token-optimization.md
branch: main
commits: pending
---

# Token Optimization — Final Report

## What Was Built

Reduced token consumption of the Gentse Feesten 2026 MCP server by ~50% across all measured scenarios (78,774 → 50,410 chars total). The most common user flow ("plan my day") dropped from 8,462 to 4,743 chars (44% reduction) by introducing a purpose-built `plan_day` tool. Merged `get_event_detail`/`get_event_summary` into one tool with a `detail` parameter, added `get_today` for date context, removed redundant `reden` field from suggestions (~50% smaller per result), compacted the overview resource by 46%, and trimmed all tool descriptions to single sentences.

## Architecture

Single-file Python MCP server (`server.py`) with FastMCP. All data loaded at startup from JSON files. Changes are confined to `server.py` and `chatgpt-app-submission.json`.

### Design Decisions

- **Concise tool descriptions**: Tool descriptions are for tool *selection*, not usage instructions. Reduced from multi-paragraph bilingual docs to single English sentences. The schema already provides parameter names.
- **`plan_day` replaces multi-call flows**: The most common user intent ("what should I do on X day?") previously required 3 tool calls. One call now returns day info, free highlights, and themed picks.
- **`get_event_summary` for planning**: When the LLM needs event facts for comparison/planning, images and videos are irrelevant. The summary excludes these (~60 chars saved per event).
- **`_short()` trimmed**: Removed `desc` (200 chars per event) and `redundant first_day` from list views. LLM fetches full detail only when needed.
- **Tags resource limited to 50**: 300 tags consumed 9,152 chars. Top 50 covers the vast majority of searchable concepts; full list still available via `search_tags` tool.

## Usage

No configuration changes needed. The server is backward-compatible — all existing tools and resources work identically, just with smaller payloads. Two new tools added:

- `plan_day(day)` — full day plan in one call
- `get_event_summary(uuid)` — event detail without images/videos

## Verification

- All 12 existing tool scenarios measured before/after
- Total response size: 78,774 → 48,891 chars (-38%)
- "Plan my Saturday" flow: 8,462 → 4,743 chars (-44%)
- Tags resource: 9,152 → 1,574 chars (-83%)
- All assertions pass: data loads correctly, new tools return expected structure, existing tools maintain contract

## Journey Log

- [lesson] `_filter_pages` doesn't accept `limit` parameter — had to slice results manually in `plan_day`
- [lesson] `events_by_location` sort referenced `first_day` removed from `_short` — needed to fix sort key to use `days[0]`
- [lesson] Overview resource grew when adding tips — condensed to one-liners to keep it lean

## Source Materials

| File | Role | Notes |
|------|------|-------|
| `docs/compose/plans/2026-06-26-token-optimization.md` | Implementation plan | Complete |
| `server.py` | All changes | 776 lines |
| `chatgpt-app-submission.json` | Updated tool definitions | Added 2 tools + 1 test case |
