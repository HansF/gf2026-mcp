# Gentse Feesten 2026 MCP-server

Een publieke, alleen-lezen MCP-server voor het programma van de Gentse
Feesten 2026. De server gebruikt uitsluitend MCP Streamable HTTP; stdio wordt
niet aangeboden.

De tools ondersteunen onder andere zoeken, suggesties, evenementdetails,
gratis highlights en programma's per locatie. Alle aangeboden gegevens zijn
publiek en de server vereist daarom geen authenticatie.

## MCP-interface

Tools:

- `search_events`
- `get_event_detail`
- `suggest`
- `list_themes`
- `list_days`
- `search_tags`
- `events_by_location`
- `free_highlights`

Resources:

- `gf://overview` — overzicht en statistieken
- `gf://days` — festivaldagen en aantallen
- `gf://themes` — thema's en aantallen
- `gf://tags` — de 300 meest gebruikte tags en aantallen

Gebruik `search_tags(query, limit)` om in de volledige taglijst te zoeken.

## Vereisten

- Python 3.10 of nieuwer
- De festivaldata in `../site/data/`

De server verwacht deze bestanden:

```text
site/data/
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
