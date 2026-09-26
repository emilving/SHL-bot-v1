# SHL-bot-v1
A Discord bot that displays live statistics for the SHL

## NHL-livebot (`bot.py`)

Postar live-notiser från NHL:s API i en Discord-kanal: nedsläpp, periodstart/-slut,
mål (och bortdömda mål), utvisningar, lineups före match och en slutrapport med
lagstatistik, målvakter och three stars.

### Kom igång

```bash
pip install -r requirements.txt
export TOKEN="din-discord-token"
export CHANNEL_ID="1552338794001469640"   # kanalen notiserna skickas till
export GUILD_ID="din-server-id"           # valfritt: slash-kommandon syncas direkt
python bot.py
```

`DEBUG=0` stänger av den detaljerade loggningen.

### Slash-kommandon

| Kommando | Beskrivning |
|---|---|
| `/ping` | Kolla att boten är online |
| `/status` | Matcher som bevakas just nu, med ställning och period |
| `/lineup [lag]` | Lineups för pågående/kommande matcher (valfritt filter, t.ex. `TOR`) |
| `/testmal` | Skicka en testnotis för ett mål |
