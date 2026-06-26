# Gentse Feesten 2026 MCP-server

Een publieke, alleen-lezen MCP-server voor het programma van de Gentse
Feesten 2026. De server gebruikt uitsluitend MCP Streamable HTTP; stdio wordt
niet aangeboden.

De tools ondersteunen onder andere zoeken, suggesties, evenementdetails,
gratis highlights en programma's per locatie. Alle aangeboden gegevens zijn
publiek en de server vereist daarom geen authenticatie.

## MCP-interface

Tools:

- `search_events(query, day, theme, free_only, outdoor_only, genre, wheelchair, limit)` — search with filters
- `search_events_batch(queries, ...)` — search multiple queries at once
- `get_event_detail(uuid, detail="full")` — event detail; use `detail="summary"` to exclude images/videos
- `get_event_details(uuids, detail="summary")` — batch detail for up to 50 events
- `suggest(mood)` — natural-language recommendations
- `plan_day(day)` — full day plan: highlights + themed picks in one call
- `get_today()` — current date and festival context
- `list_themes()` — all themes with event counts
- `list_days()` — all festival dates with counts
- `search_tags(query, limit)` — search event tags
- `events_by_location(location_name, day)` — events at a location
- `free_highlights(day)` — top 10 free events for a day

Resources:

- `gf://overview` — festival stats and usage tips
- `gf://days` — festival dates with counts
- `gf://themes` — themes with counts
- `gf://tags` — top 50 tags with counts

## Vereisten

- Python 3.10 of nieuwer
- De festivaldata in `../site/data/` (productielayout) of de naastgelegen
  repository `../gf2026/site/data/` (lokale ontwikkellayout)

Stel `GF_MCP_DATA_DIR` in wanneer de data ergens anders staat. De server
verwacht deze bestanden:

```text
gf2026/site/data/
├── days.json
├── event_pages.json
├── events.json
├── locations.json
└── themes.json
```

## Installatie

Maak bij voorkeur een virtuele omgeving:

```bash
python -m venv .venv
```

Activeer deze op Linux of macOS:

```bash
source .venv/bin/activate
```

Of op Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Installeer daarna de dependency:

```bash
python -m pip install -r requirements.txt
```

## Starten

Voor lokaal gebruik:

```bash
python server.py
```

De MCP-endpoint is dan:

```text
http://127.0.0.1:8000/mcp
```

Publieke juridische pagina's:

```text
http://127.0.0.1:8000/privacy
http://127.0.0.1:8000/terms
```

Voor een container of een host die rechtstreeks op het netwerk luistert:

```bash
GF_MCP_HOST=0.0.0.0 GF_MCP_PORT=8000 python server.py
```

Windows PowerShell:

```powershell
$env:GF_MCP_HOST = "0.0.0.0"
$env:GF_MCP_PORT = "8000"
python server.py
```

Beschikbare configuratie:

| Variabele | Standaard | Betekenis |
| --- | --- | --- |
| `GF_MCP_HOST` | `127.0.0.1` | Luisteradres |
| `GF_MCP_PORT` | `8000` | HTTP-poort |
| `GF_MCP_DATA_DIR` | Automatisch gedetecteerd | Map met de JSON-programmadata |

## Verbinden

Configureer een MCP-client met Streamable HTTP en de volledige endpoint-URL:

```text
https://mcp.example.be/mcp
```

Voorbeeld voor Claude Code:

```bash
claude mcp add --transport http gentse-feesten https://mcp.example.be/mcp
```

## Publiek beschikbaar maken

Gebruik in productie HTTPS via een reverse proxy of hostingplatform. Laat de
Python-server intern op poort 8000 luisteren en stuur extern verkeer door naar
`/mcp`.

Een minimale Caddy-configuratie:

```caddyfile
mcp.example.be {
    reverse_proxy 127.0.0.1:8000
}
```

Authenticatie is bewust niet ingeschakeld. Omdat het endpoint publiek is,
blijven transportbeveiliging en capaciteitsbescherming wel nodig:

- gebruik HTTPS;
- stel rate limiting en een maximale requestgrootte in bij de proxy;
- stel time-outs in;
- publiceer alleen de MCP-poort via de reverse proxy;
- log geen onnodige clientgegevens.

CORS is niet nodig voor normale server-to-server MCP-clients. Voeg het alleen
toe wanneer een browserapp rechtstreeks met deze endpoint moet verbinden.

## Transportgedrag

De server draait stateless en geeft JSON-responses via Streamable HTTP. Dit
maakt meerdere instanties en horizontaal schalen mogelijk zonder gedeelde
MCP-sessiestatus.
