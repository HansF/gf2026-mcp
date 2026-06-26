# Gentse Feesten 2026 MCP-server

Een publieke, alleen-lezen MCP-server voor het programma van de Gentse
Feesten 2026. Geeft AI-assistenten toegang tot alle festivaldata. Pronkcriteria:
**maximale verwerking, minimale tokens**.

De server draait stateless over Streamable HTTP. Geen authenticatie — alle data
is publiek.

## Live endpoint

<https://gf2026.doplr.com/mcp> (gehost op Hetzner VPS, nginx + Let's Encrypt).

## MCP-interface

### Tools

| Tool | Doel |
|------|------|
| `search_events(query, day, theme, free_only, outdoor_only, genre, wheelchair, limit)` | Zoek evenementen. `query` kan een string of een array van strings zijn (batch-modus, één call voor meerdere zoektermen). |
| `get_event_detail(uuid, detail="full")` | Detail van één evenement. `detail="summary"` sluit beeld/video uit — lichter voor planning. |
| `get_event_details(uuids, detail="summary")` | Details van meerdere evenementen **in één call**. Onbeperkt aantal UUIDs, behoudt volgorde. |
| `suggest(mood)` | Aanbevelingen op basis van een vrije omschrijving. |
| `plan_day(day)` | Volledig dagoverzicht in één call: daginfo, gratis highlights en thematische tips. |
| `get_today()` | Vandaag + of het binnen het festival valt. Gebruik bij "vandaag" of "vanavond". |
| `list_themes()` | Thema's met aantallen. |
| `list_days()` | Festivaldagen met weekdag en aantallen. |
| `events_by_location(location_name, day)` | Evenementen op een locatie, optioneel per dag. |
| `free_highlights(day)` | Top 10 gratis evenementen voor een dag of heel festival. |

### Resources

| URI | Doel |
|-----|------|
| `gf://overview` | Basistatistieken en gebruikstips. |
| `gf://days` | Alle dagen met weekdag en aantal. |
| `gf://themes` | Alle thema's met aantallen. |
| `gf://tags` | Top 50 tags met aantallen. |

## Workflow tips voor LLMs

1. **"Wat kan ik doen op zaterdag?"** → `plan_day("zaterdag")` — haalt alles in één call op.
2. **"Vertel me meer over evenement X"** → `get_event_detail(uuid, detail="summary")` — gebruik `summary` tenzij je beeld/video nodig hebt.
3. **"Ik wil details van 9 evenementen"** → `get_event_details(uuids, detail="summary")` — geen loop, één call.
4. **"Zoek jazz en theater"** → `search_events(query=["jazz", "theater"], limit=10)` — batch in één call.
5. **"Wat staat er vandaag?"** → `get_today()` eerst, daarna `plan_day` met de gevonden datum.
6. **"Welke evenementen op Vrijdagmarkt?"** → `events_by_location("Vrijdagmarkt")`.

## Vereisten

- Python 3.10 of nieuwer
- Festivaldata in `../site/data/` (productielayout) of `../gf2026/site/data/` (lokaal)
- Stel `GF_MCP_DATA_DIR` in als de data elders staat

Server verwacht deze bestanden:

```
├── days.json
├── event_pages.json
├── events.json
├── locations.json
└── themes.json
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

Globaal binden:

```bash
GF_MCP_HOST=0.0.0.0 GF_MCP_PORT=8000 python server.py
```

## Verbinden

```bash
claude mcp add --transport http gentse-feesten https://gf2026.doplr.com/mcp
```

## Deployen

Zie [DEPLOYMENT.md](DEPLOYMENT.md) voor details. Kort samengevat:

```bash
rsync -av server.py requirements.txt hans@178.105.240.159:~/gf2026-mcp/mcp/
ssh hans@178.105.240.159 'sudo systemctl restart gf2026-mcp'
```

Service-status of logs:

```bash
ssh hans@178.105.240.159 'systemctl is-active gf2026-mcp'
ssh hans@178.105.240.159 'journalctl -u gf2026-mcp -f'
```

## Gebruik publiceren

HTTPS via reverse proxy. Minimale Caddy-configuratie:

```caddyfile
gf2026.doplr.com {
    reverse_proxy 127.0.0.1:8000
}
```

Let op:

- gebruik HTTPS;
- stel rate limiting en maximale requestgrootte in;
- stel time-outs in;
- publiceer alleen `/mcp` via de proxy;
- CORS is niet nodig voor server→server MCP.

## Transport

Stateless JSON via Streamable HTTP. Schaalt horizontaal zonder gedeelde
sessiestatus.
