# SHL-bot-v1

En Discord-bot som följer SHL-matcher live och postar i en kanal:

- **Var 10:e sekund:** mål (målskytt, assist, ställning, PP/boxplay/tom kasse), utvisningar
  (spelare, minuter, förseelse), skador, målvaktsbyten och tom kasse sent i matchen.
- **Efter varje period:** mål och utvisningar i perioden, periodens statistik och totalt i matchen
  (skott på mål, räddningar, räddning%, utvisningsminuter, PP, PP%, PK%, boxplaymål, tekningar m.m.)
  samt målvaktsstatistik.
- **Slutresultat:** resultat per period, alla mål och utvisningar, statistik för hela matchen,
  statistik per period och målvaktsstatistik (skott mot, insläppta mål, räddningar, räddning%).
- Rättade mål (ny målskytt/assist) och bortdömda mål postas som egna notiser.

Slash-kommandon: `/matcher` (dagens matcher och ställning) och `/stats lag:FBK` (aktuell statistik).

## Kom igång

1. Skapa en applikation på <https://discord.com/developers/applications>, gå till **Bot** och kopiera token.
2. Bjud in boten: **OAuth2 → URL Generator**, välj `bot` + `applications.commands` och
   behörigheterna *Send Messages* och *Embed Links*.
3. Installera och konfigurera:

   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # fyll i DISCORD_TOKEN och DISCORD_CHANNEL_ID
   python -m shlbot run
   ```

   Med Docker: `docker build -t shl-bot . && docker run --env-file .env -v shl-data:/data shl-bot`

Sätt `SHL_TEAMS=FBK,LHF` om du bara vill följa vissa lag.

## Verktyg för felsökning

```bash
python -m shlbot games              # dagens matcher med uuid
python -m shlbot run --dry-run      # bevaka live men skriv i terminalen i stället för Discord
python -m shlbot probe <uuid>       # spara SHL:s rådata för en match i probe/<uuid>/
python -m shlbot replay probe/<uuid> # spela upp sparad match och visa alla notiser
```

## Datakälla – viktigt att veta

Boten använder samma (inofficiella) JSON-API som shl.se själv använder:

| Data | Endpoint | Status |
|---|---|---|
| Säsong/serie | `/api/sports-v2/season-series-game-types-filter` | Används av andra öppna projekt |
| Spelschema | `/api/sports-v2/game-schedule` | Används av andra öppna projekt |
| Matchstatus | `/api/gameday/game-overview/{uuid}` | Ej verifierad |
| Händelser | `/api/gameday/play-by-play/{uuid}` | Ej verifierad |
| Lagstatistik | `/api/gameday/team-stats/{uuid}` | Ej verifierad, valfri |

API:et är inte dokumenterat och kan ändras utan förvarning. Parsningen är därför defensiv (flera
möjliga fältnamn prövas) och alla adresser går att ändra i `.env`. **Kör `probe` + `replay` på en
riktig match första gången** för att se att allt tolkas rätt. Visar `replay` fel, öppna en match på
shl.se med webbläsarens utvecklarverktyg (fliken Network, filtrera på `api`) för att hitta rätt
adresser.

Hur statistiken tas fram:

- Mål, skott, räddningar, utvisningsminuter, PP/PK och målvaktsstatistik räknas fram ur
  händelseflödet. PP-chanser räknas som motståndarens 2-, 4- och 5-minutersutvisningar som inte
  kvittas samtidigt – en god uppskattning, men inte alltid exakt som SHL:s officiella siffra.
- Svarar lagstatistik-endpointen används dess siffror i första hand, och den ger då även
  tekningar, tacklingar, blockerade skott m.m. Utan den visas inte tekningar.
- **Skador** postas bara om SHL publicerar dem som händelser i matchflödet, vilket sällan görs.

## Utveckling

```bash
pip install -r requirements-dev.txt
pytest
```

Koden är uppdelad så att logiken går att testa utan Discord och nätverk:

- `shlbot/models.py` – tolkning av SHL:s JSON till händelser och matchstatus
- `shlbot/stats.py` – period-, match- och målvaktsstatistik
- `shlbot/tracker.py` – avgör vad som är nytt och ska postas (dubblettskydd, rättade/bortdömda mål)
- `shlbot/formatting.py` – bygger meddelandena
- `shlbot/monitor.py` – bevakningsloopen, `shlbot/bot.py` – Discord, `shlbot/api.py` – HTTP
