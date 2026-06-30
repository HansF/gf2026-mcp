# Gentse Feesten 2026 MCP-server

Een publieke, alleen-lezen MCP-server voor het programma van de Gentse
Feesten 2026. Geeft AI-assistenten toegang tot alle festivaldata met
**alias-expansion**, **experience tags**, **zones**, **vector search**, een
**festival guide** en een **ChatGPT Apps widget**.

De server draait stateless over Streamable HTTP. Geen authenticatie — alle data
is publiek.

## Live endpoint

<https://gf2026.doplr.com/mcp> (gehost op Hetzner VPS, nginx + Let's Encrypt).

## Architectuur

```
gf2026-mcp/
├── server.py              # MCP server (19 tools + Apps widget + vector search)
├── aliases.json           # 104 alias groups, 2729 keywords
├── experience_tags.json   # 1624 events with experience tags
├── zones.json             # 25 editorial zones
├── experience_tags.py     # Generate experience_tags.json
├── zone_builder.py        # Generate initial zones.json
├── cli.py                 # CLI for zone curation
├── curate.py              # TUI for zone curation (Textual)
├── maintain.py            # TUI for data download + vector rebuild
├── requirements.txt       # mcp, textual, zvec, sentence-transformers
└── site/data/             # Festival data (events, locations, themes)
```

### Data flow

```
Gentse Feesten API (data.stad.gent)
        ↓
    maintain.py → events.json (download)
        ↓
    event_pages.json (processed)
        ↓
    zone_builder.py → zones.json (curated)
    experience_tags.py → experience_tags.json
    aliases.json (manual curation)
        ↓
    server.py loads all at startup
        ↓
    zvec index (1625 events, 384-dim embeddings)
        ↓
    MCP tools (search, suggest, guide, zones, semantic_search)
```

## MCP-interface

### Tools

| Tool | Doel |
|------|------|
| `show_festival_explorer(query, day, mode)` | Open de ChatGPT Apps widget voor zoeken, dagplanning, gidsen en zones. |
| `search_events(query, day, theme, free_only, outdoor_only, genre, wheelchair, time_window, child_friendly, participatory, indoor_outdoor, limit)` | Zoek met alias-expansion en experience tags. `query` kan een string of array zijn. |
| `semantic_search(query, topk, mode)` | **NIEUW** — Zoek met vector similarity. Modes: "semantic" (384-dim embeddings), "fts" (keyword search), "hybrid" (FTS + vector rank). |
| `get_event_detail(uuid, detail="full")` | Detail van één evenement. `detail="summary"` sluit beeld/video uit. |
| `get_event_details(uuids, detail="summary")` | Details van meerdere evenementen **in één call**. |
| `suggest(mood)` | Aanbevelingen op basis van intent parsing + experience tags. |
| `create_festival_guide(vibe, energy_level, social_mode, max_paid_events, day)` | Persoonlijke festivalgids met anchor/fallback events en energiestrategie. |
| `find_similar_events(uuid, limit)` | Vind vergelijkbare evenementen via vector similarity + parent/sibling relaties: same_vibe, nearby, good_afterwards, related. |
| `get_parent_event(uuid)` | **NIEUW** — Haal parent event op met alle occurrences (kind-evenementen). |
| `plan_day(day)` | Volledig dagoverzicht: daginfo, gratis highlights, thematische tips. |
| `get_today()` | Vandaag + of het binnen het festival valt. |
| `list_themes()` | Thema's met aantallen. |
| `list_days()` | Festivaldagen met weekdag en aantallen. |
| `events_by_location(location_name, day)` | Evenementen op een locatie, optioneel per dag. |
| `free_highlights(day)` | Top 10 gratis evenementen. |
| `list_zones()` | Lijst van alle festivalzones. |
| `zone_detail(zone_name)` | Detail van een zone: locaties, vibe, good_for, evenementen. |
| `get_zone_profile(zone_name)` | Uitgebreid zone-profiel met aanbevolen evenementen. |
| `search_by_zone(zone_name, query, day, theme, ...)` | Zoek gefilterd op zone. |

### Resources

| URI | Doel |
|-----|------|
| `gf://overview` | Basistatistieken en gebruikstips. |
| `gf://days` | Alle dagen met weekdag en aantal. |
| `gf://themes` | Alle thema's met aantallen. |
| `gf://tags` | Top 50 tags met aantallen. |
| `gf://zones` | Alle festivalzones met vibe en good_for tags. |
| `ui://widget/gf2026-explorer-v1.html` | ChatGPT Apps widget template (`text/html;profile=mcp-app`). |

### ChatGPT Apps widget

De server is ook een ChatGPT App met een interactieve, read-only Festival
Explorer widget. `show_festival_explorer()` retourneert compacte
`structuredContent` voor het model en verwijst via tool metadata naar
`ui://widget/gf2026-explorer-v1.html`. De widget gebruikt de MCP Apps bridge
(`tools/call`) om dezelfde read-only servertools aan te roepen voor zoeken,
dagplannen, persoonlijke gidsen en zones.

### Response formaat

Alle tools retourneren een standaard envelope:

```json
{
  "ok": true,
  "data": [...],
  "meta": {
    "tool": "search_events",
    "dataset_year": 2026,
    "result_count": 10
  },
  "warnings": []
}
```

## Alias systeem

Zoekopdrachten worden automatisch uitgebreid met synoniemen:

| Query | Expansie |
|-------|----------|
| `bataclan` | cirq, absurd, participatief, vlasmarkt, weird |
| `boombal` | folk, dansinitiatie, social dance, het bal |
| `burlesque` | cabaret, drag, glitter, mardi gras, showgirl |
| `niet te braaf` | cabaret, queer, burlesque, weird, late_night |
| `zwoel` | sensueel, cabaret, queer, burlesque, drag |

Negaties worden correct afgehandeld: "niet te braaf" zoekt naar cabaret/queer/burlesque in plaats van familie/museum.

## Experience tags

Elk evenement krijgt experience tags op basis van keywords, thema's en beschrijving:

| Tag | Betekenis |
|-----|-----------|
| `cabaret` | Burlesque, drag, revue, mardi gras |
| `queer` | LGBTQ+, drag, pride, inclusief |
| `weird` | Absurd, cirQ, bizar, participatief |
| `calm` | Rustig, klassiek, museum, wandeling |
| `participatory` | Workshops, initiaties, meedoen |
| `late_night` | Nacht, club, DJ, tot laat |
| `family` | Kinderen, gezin, familie |
| `date_night` | Romantisch, intiem, sfeer |
| `high_energy` | Dans, feest, party, intensief |
| `solo_friendly` | Alleen gaan, laagdrempelig |

Tags worden gebruikt voor ranking en match_reasons in resultaten.

## Zones

25 festivalzones gebaseerd op editorial analysis:

| Zone | Vibe | Goed voor |
|------|------|-----------|
| Vlasmarkt | nacht, dans, alternatief | dansvolk, nachtraven |
| Bij Sint-Jacobs / Trefpunt | volks, roots, historisch | Gentenaars, folk |
| Korenmarkt | mainstream, grote hits | breed publiek, toeristen |
| Sint-Baafsplein | monumentaal, spektakel | grote acts, massa |
| Kouter / Boomtown | gratis muziek, indie | eerste bezoekers, budget |

Gebruik `list_zones()` of `zone_detail("Vlasmarkt")` voor meer info.

## Workflow tips voor LLMs

1. **"Wat kan ik doen op zaterdag?"** → `plan_day("zaterdag")`
2. **"Ik mis de oude Bataclan vibe"** → `suggest("Bataclan vibe")` — alias expansion vindt cirQ, weird, participatief
3. **"Iets niet te braaf"** → `suggest("niet te braaf")` — vindt cabaret, queer, burlesque
4. **"Maak een gids voor burlesque, cirQ, dansen"** → `create_festival_guide(vibe="burlesque, cirQ, dansen")`
5. **"Wat is er vergelijkbaar met dit evenement?"** → `find_similar_events(uuid)` — inclusief parent/sibling relaties
6. **"Wat zijn alle occurrences van dit evenement?"** → `get_parent_event(uuid)` — parent + alle datums/tijden
7. **"Regenproof cabaret"** → `suggest("regenproof cabaret")` — indoor events
8. **"Ik ga alleen, iets sociaals"** → `suggest("alleen gaan, sociaal")` — solo_friendly + participatory
9. **"Muziek op een plein"** → `semantic_search("muziek op een plein")` — vector similarity vindt straatmuziek
10. **"Jazz met hoge kwaliteit"** → `semantic_search("jazz", mode="hybrid")` — FTS + vector rank
11. **"Zwoel queer cabaret"** → `semantic_search("zwoel queer cabaret", mode="hybrid")` — semantisch + keywords

## Vector search

De server gebruikt [zvec](https://github.com/alibaba/zvec) met `all-MiniLM-L6-v2` (384-dim) embeddings voor semantische zoekfunctionaliteit.

### Modi

| Mode | Beschrijving | Gebruik |
|------|--------------|---------|
| `semantic` | Vector similarity — begrijpt intentie | "muziek op een plein", "iets romantisch" |
| `fts` | Full-text search op naam/keywords/themes | "jazz", "cabaret", exacte termen |
| `hybrid` | FTS filter → vector rank | Combinatie van precies + semantisch |

### Hoe het werkt

1. Bij opstarten worden 5379 events geïndexeerd (~20 seconden)
2. Elke event wordt geëmbed als: `{naam}. {beschrijving}. {keywords}. {themes}. {locatie}`
3. Zoekopdrachten worden geëmbed en vergeleken met alle events
4. Resultaten worden gerangschikt op cosine similarity

### Parent events

Het API bevat parent events (zonder startdate, met subevent URLs) en child events (met startdate). Beide zijn doorzoekbaar in de vector store.

- `get_parent_event(uuid)` — Haal parent op met alle occurrences
- `find_similar_events(uuid)` — Vind vergelijkbare events + parent/sibling relaties

### Fallback

Als zvec niet beschikbaar is, werkt de server gewoon door met keyword-based search.

## Vereisten

- Python 3.10 of nieuwer
- Festivaldata in `../site/data/` of stel `GF_MCP_DATA_DIR` in
- **Optioneel**: zvec + sentence-transformers voor vector search

Server verwacht deze bestanden:

```
├── days.json
├── event_pages.json
├── events.json
├── locations.json
├── themes.json
└── organizers.json
```

## Installatie

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Starten

```bash
python server.py
```

Endpoint: `http://127.0.0.1:8000/mcp`

## Zone curation

### CLI

```bash
# Lijst alle zones
python cli.py list -v

# Zone details
python cli.py show "Vlasmarkt"

# Zone hernoemen
python cli.py rename "Oude naam" "Nieuwe naam"

# Vibe tags aanpassen
python cli.py vibe "Vlasmarkt" -a "techno" -r "folk"

# Locatie verplaatsen
python cli.py move "Korenmarkt" "Emile Braunplein"

# Zones samenvoegen
python cli.py merge "Zone A" "Zone B"

# Route per type bezoeker
python cli.py route night
python cli.py route family

# Zoek op stemming
python cli.py mood "zwoel queer cabaret"
python cli.py mood "rustig klassiek"
python cli.py mood "dansen alleen"

# Events in een zone
python cli.py events "Vlasmarkt" -n 5
```

### TUI (Textual)

#### Zone Curator
```bash
python curate.py
```

Keybindings:
- `j/k` — Navigeer zones
- `r` — Zone hernoemen
- `v` — Vibe tags bewerken
- `g` — Good_for tags bewerken
- `m` — Locatie verplaatsen
- `M` — Zones samenvoegen
- `s` — Zone splitsen
- `p` — Events preview
- `Ctrl+S` — Opslaan
- `q` — Afsluiten

#### Maintenance TUI
```bash
python maintain.py
```

Keybindings:
- `Enter` — Voer actie uit
- `r` — Vernieuw status
- `q` — Afsluiten

Acties:
- **Download evenementen** — Haal 5000+ events op van data.stad.gent API
- **Herbouw vector store** — Herindexeer events met embeddings
- **Volledige heropbouw** — Download + herbouw vector store

### Data genereren

```bash
# Genereer zones.json uit locatie/event data
python zone_builder.py

# Regenereer experience tags
python experience_tags.py
```

## Deployen

Zie [DEPLOYMENT.md](DEPLOYMENT.md) voor details. Kort samengevat:

```bash
rsync -av server.py requirements.txt aliases.json experience_tags.json zones.json hans@178.105.240.159:~/gf2026-mcp/mcp/
ssh hans@178.105.240.159 'sudo systemctl restart gf2026-mcp'
```

## Transport

Stateless JSON via Streamable HTTP. Schaalt horizontaal zonder gedeelde
sessiestatus.
