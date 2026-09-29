"""Gör om notiser till embed-beskrivningar (oberoende av discord.py)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import (
    AWAY,
    GOAL,
    GOALIE_IN,
    GOALIE_OUT,
    HOME,
    INJURY,
    PENALTY,
    PENALTY_SHOT,
    SHOOTOUT,
    Event,
    GameInfo,
    first,
    is_match_penalty,
    strength_kind,
    truthy,
)
from .stats import GameStats, TeamLine
from .tracker import Notification

COLORS = {
    "goal": 0x2ECC71,
    "penalty": 0xE67E22,
    "injury": 0xE74C3C,
    "goalie": 0x3498DB,
    "period": 0x95A5A6,
    "final": 0xF1C40F,
    "start": 0x1ABC9C,
    "disallowed": 0xC0392B,
    "info": 0x7289DA,
}

FIELD_LIMIT = 1024

# Spelsituation i swehockeys målrader, t.ex. "2-1 (PP1)"
SWE_STRENGTH = {"PP": "Powerplay", "SH": "Boxplay", "EN": "Tom kasse", "PS": "Straffslag"}


@dataclass
class EmbedSpec:
    title: str
    description: str = ""
    color: int = COLORS["info"]
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    footer: str | None = None

    def add(self, name: str, value: str, inline: bool = False) -> None:
        if value:
            self.fields.append((name, truncate(value, FIELD_LIMIT), inline))

    def as_text(self) -> str:
        lines = [f"== {self.title} =="]
        if self.description:
            lines.append(self.description)
        for name, value, _ in self.fields:
            lines.append(f"-- {name}")
            lines.append(value)
        if self.footer:
            lines.append(f"({self.footer})")
        return "\n".join(lines)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    closing = "\n```" if text.startswith("```") else ""
    return text[: limit - len(closing) - 2] + "…" + closing


def period_name(p: int, shootout_period: int = 5) -> str:
    if p >= shootout_period:
        return "Straffläggning"
    if p == 1:
        return "1:a perioden"
    if p == 2:
        return "2:a perioden"
    if p == 3:
        return "3:e perioden"
    return "Förlängning" if p == 4 else f"Förlängning {p - 3}"


def period_short(p: int) -> str:
    return f"P{p}" if p <= 3 else ("ÖT" if p == 4 else f"ÖT{p - 3}")


def code(game: GameInfo, side: str | None) -> str:
    if side == HOME:
        return game.home.code
    if side == AWAY:
        return game.away.code
    return "?"


def team_name(game: GameInfo, side: str | None) -> str:
    if side == HOME:
        return game.home.name
    if side == AWAY:
        return game.away.name
    return "Okänt lag"


def scoreline(game: GameInfo, score: tuple[int, int] | None) -> str:
    h, a = score or (0, 0)
    return f"{game.home.code} {h}–{a} {game.away.code}"


def strength_label(e: Event) -> str | None:
    if e.is_empty_net:
        return "Tom kasse"
    kind = strength_kind(e.strength)
    s = (e.strength or "").upper()
    if kind == "pp":
        return f"Powerplay ({s})" if s and s not in ("PP", "POWERPLAY") else "Powerplay"
    if kind == "sh":
        return "Boxplay"
    if kind == "ps":
        return "Straffslag"
    return None


# ---------------------------------------------------------------------------
# Tabeller
# ---------------------------------------------------------------------------


def _pct(num: int | None, den: int | None) -> str:
    if not den or num is None:
        return "–"
    return f"{100 * num / den:.0f}%"


def _v(v: int | None) -> str:
    return "–" if v is None else str(v)


def _on_goalie(line: TeamLine) -> int | None:
    """Skott som målvakten mötte (utan mål i tom kasse)."""
    return None if line.shots is None else line.shots - (line.en_goals or 0)


def stat_rows(home: TeamLine, away: TeamLine, has_faceoffs: bool) -> list[tuple[str, str, str]]:
    rows = [
        ("Mål", _v(home.goals), _v(away.goals)),
        ("Skott på mål", _v(home.shots), _v(away.shots)),
        ("Räddningar", _v(home.saves), _v(away.saves)),
        ("Räddning%", _pct(home.saves, _on_goalie(away)), _pct(away.saves, _on_goalie(home))),
        ("Utv.minuter", _v(home.pim), _v(away.pim)),
        ("PP", f"{home.pp_goals}/{home.pp_opps}", f"{away.pp_goals}/{away.pp_opps}"),
        ("PP%", _pct(home.pp_goals, home.pp_opps), _pct(away.pp_goals, away.pp_opps)),
        (
            "PK%",
            _pct(away.pp_opps - away.pp_goals, away.pp_opps),
            _pct(home.pp_opps - home.pp_goals, home.pp_opps),
        ),
    ]
    if home.sh_goals or away.sh_goals:
        rows.append(("Boxplaymål", _v(home.sh_goals), _v(away.sh_goals)))
    if has_faceoffs and (home.faceoffs_won is not None or away.faceoffs_won is not None):
        total = (home.faceoffs_won or 0) + (away.faceoffs_won or 0)
        rows.append(("Tekningar", _v(home.faceoffs_won), _v(away.faceoffs_won)))
        rows.append(("Tekning%", _pct(home.faceoffs_won, total), _pct(away.faceoffs_won, total)))
    optional = [
        ("Skott utanför", "shots_missed"),
        ("Blockerade", "blocked"),
        ("Tacklingar", "hits"),
        ("Puckförluster", "giveaways"),
        ("Puckvinster", "takeaways"),
    ]
    for label, attr in optional:
        h, a = getattr(home, attr), getattr(away, attr)
        if h is not None or a is not None:
            rows.append((label, _v(h), _v(a)))
    return rows


def stat_table(game: GameInfo, home: TeamLine, away: TeamLine, has_faceoffs: bool) -> str:
    rows = stat_rows(home, away, has_faceoffs)
    hc, ac = game.home.code[:6], game.away.code[:6]
    lines = [f"{'':<13}{hc:>6}{ac:>6}"]
    lines += [f"{label:<13}{h:>6}{a:>6}" for label, h, a in rows]
    return "```\n" + "\n".join(lines) + "\n```"


def per_period_table(game: GameInfo, stats: GameStats, shootout_period: int) -> str:
    periods = sorted(p for p in stats.periods if p < shootout_period)
    if not periods:
        return ""
    # Smal tabell så att den inte radbryts i mobilen
    head = f"{'':<10}" + "".join(f"{period_short(p):>4}" for p in periods) + f"{'Tot':>4}"
    lines = [head]
    total = stats.total()
    shots_known = all(stats.periods[p].side(s).shots is not None for p in periods for s in (HOME, AWAY))
    rows = [("Mål", "goals"), ("Skott", "shots"), ("Utv", "pim")] if shots_known else [
        ("Mål", "goals"), ("Utv", "pim")
    ]
    for label, attr in rows:
        for side, tcode, tot in ((HOME, game.home.code, total.home), (AWAY, game.away.code, total.away)):
            vals = "".join(f"{_v(getattr(stats.periods[p].side(side), attr)):>4}" for p in periods)
            lines.append(f"{(label + ' ' + tcode)[:10]:<10}{vals}{_v(getattr(tot, attr)):>4}")
    lines.append("Utv = utvisningsminuter")
    return "```\n" + "\n".join(lines) + "\n```"


def short_name(label: str) -> str:
    """"#30 Emil Larsson" -> "E. Larsson"."""
    parts = [p for p in label.split() if not p.startswith("#")]
    if len(parts) >= 2:
        return f"{parts[0][0]}. {' '.join(parts[1:])}"
    return " ".join(parts) or label


def goalie_table(game: GameInfo, stats: GameStats) -> str:
    if not stats.goalies:
        return ""
    # Smal tabell (max 25 tecken) så att den inte radbryts i mobilen
    lines = [f"{'':<13}{'Rd':>6}{'Rd%':>6}"]
    for g in stats.goalies:
        pct = f"{100 * g.save_pct:.1f}" if g.save_pct is not None else "–"
        last = short_name(g.name).split(". ", 1)[-1]
        name = f"{code(game, g.side)} {last}"[:12]
        lines.append(f"{name:<13}{f'{g.saves}/{g.shots_against}':>6}{pct:>6}")
    lines.append("Rd = räddningar/skott")
    return "```\n" + "\n".join(lines) + "\n```"


# ---------------------------------------------------------------------------
# Listor över händelser
# ---------------------------------------------------------------------------


def goal_line(game: GameInfo, e: Event, score: tuple[int, int] | None) -> str:
    parts = [f"`{period_short(e.period)} {e.time}`", f"**{code(game, e.side)}**"]
    if score:
        parts.append(f"{score[0]}–{score[1]}")
    parts.append(e.player or "Okänd målskytt")
    if e.assists:
        parts.append(f"({', '.join(e.assists)})")
    lbl = strength_label(e)
    if lbl:
        parts.append(f"*{lbl}*")
    return " ".join(parts)


# SHL:s förkortningar för förseelser
OFFENCES = {
    "HOOK": "Hakning",
    "HI-ST": "Hög klubba",
    "TRIP": "Fällning",
    "HOLD": "Fasthållning",
    "HO-ST": "Fasthållning av klubba",
    "INTRF": "Obstruktion",
    "INTF": "Obstruktion",
    "SLASH": "Slag",
    "ROUGH": "Ruffning",
    "CROSS": "Crosscheck",
    "BOARD": "Boarding",
    "CHARG": "Charging",
    "ELBOW": "Armbåge",
    "KNEE": "Knätackling",
    "CHE-H": "Tackling mot huvudet",
    "CHEHD": "Tackling mot huvudet",
    "CHE-B": "Tackling bakifrån",
    "CHEBH": "Tackling bakifrån",
    "INTEF": "Obstruktion",
    "DELAY": "Fördröjning av spelet",
    "DELAY-G": "Fördröjning av spelet",
    "TOO-M": "För många spelare på isen",
    "TOOMA": "För många spelare på isen",
    "UN-SP": "Osportsligt uppträdande",
    "UNSPO": "Osportsligt uppträdande",
    "UNSP": "Osportsligt uppträdande",
    "DIVE": "Filmning",
    "EMB": "Filmning",
    "FIGHT": "Slagsmål",
    "SPEAR": "Spjutning",
    "BUTT": "Stötning med klubbskaft",
    "KICK": "Sparkning",
    "HEAD": "Skalltackling",
    "MISC": "Tiominutersstraff",
    "GM": "Matchstraff",
    "MP": "Matchstraff",
    "GAME": "Matchstraff",
    "ABUSE": "Ovårdat språk",
    "BENCH": "Lagstraff",
    "INTER": "Obstruktion",
    "BROKE": "Spel med bruten klubba",
    "THROW": "Kastad klubba",
    "PUCK": "Spela pucken med handen",
    "HAND": "Spela pucken med handen",
}


def offence_text(offence: str) -> str:
    return OFFENCES.get(offence.strip().upper(), offence)


def penalty_line(game: GameInfo, e: Event) -> str:
    mins = f"{e.penalty_minutes} min" if e.penalty_minutes else "utvisning"
    who = e.player or "Lagstraff"
    reason = f" – {offence_text(e.offence)}" if e.offence else ""
    return f"`{period_short(e.period)} {e.time}` **{code(game, e.side)}** {who} {mins}{reason}"


def goal_list(game: GameInfo, events: list[Event], shootout_period: int) -> str:
    h = a = 0
    lines = []
    for e in events:
        if e.kind != GOAL or e.period >= shootout_period:
            continue
        if e.home_goals is not None and e.away_goals is not None:
            h, a = e.home_goals, e.away_goals
        elif e.side == HOME:
            h += 1
        elif e.side == AWAY:
            a += 1
        lines.append(goal_line(game, e, (h, a)))
    return "\n".join(lines)


def penalty_list(game: GameInfo, events: list[Event]) -> str:
    """Rapporterna tar bara med matchstraff."""
    return "\n".join(penalty_line(game, e) for e in events if is_match_penalty(e))


def period_scores(game: GameInfo, stats: GameStats, shootout_period: int) -> str:
    parts = []
    for p in sorted(stats.periods):
        if p >= shootout_period:
            continue
        st = stats.periods[p]
        parts.append(f"{st.home.goals}–{st.away.goals}")
    return f"({', '.join(parts)})" if parts else ""


# ---------------------------------------------------------------------------
# Notiser -> embeds
# ---------------------------------------------------------------------------


def render(n: Notification, shootout_period: int = 5) -> EmbedSpec:
    g = n.game
    if n.kind == "event" and n.event:
        return render_event(n, shootout_period)
    if n.kind == "start":
        spec = EmbedSpec(f"🏒 Nedsläpp! {g.home.name} – {g.away.name}", color=COLORS["start"])
        if g.venue:
            spec.description = f"📍 {g.venue}"
        goalies = [e for e in n.events if e.kind == GOALIE_IN]
        if goalies:
            spec.add(
                "Startande målvakter",
                "\n".join(f"**{code(g, e.side)}** {e.player}" for e in goalies),
            )
        return spec
    if n.kind == "score_goal":
        side = n.extra.get("side")
        spec = EmbedSpec(f"🚨 MÅL! {scoreline(g, n.score)}", color=COLORS["goal"])
        st = n.status
        swe_goal = n.extra.get("swe_goal")
        if swe_goal and swe_goal.scorer:
            lines = [f"**{swe_goal.scorer}** ({team_name(g, side)})"]
            if swe_goal.assists:
                lines.append(f"Assist: {', '.join(swe_goal.assists)}")
            spec.description = "\n".join(lines)
            secs = int(swe_goal.time.split(":")[0]) * 60 + int(swe_goal.time.split(":")[1])
            period = secs // 1200 + 1
            footer = f"{period_name(period, shootout_period)} {(secs % 1200) // 60:02d}:{secs % 60:02d}"
            lbl = SWE_STRENGTH.get(swe_goal.strength.upper().rstrip("0123456789"), None)
            spec.footer = f"{footer} · {lbl}" if lbl else footer
        else:
            spec.description = (
                f"**{team_name(g, side)}** gör mål\n*Målskytt och assist fylls i när SHL har registrerat målet.*"
            )
            if st and st.period:
                spec.footer = f"{period_name(st.period, shootout_period)} {st.clock or ''}".strip()
        return spec
    if n.kind == "lineup":
        team = n.extra["team"]
        spec = EmbedSpec(f"📋 Förändringar i {team.code}:s uppställning", color=COLORS["info"])
        prev = n.extra.get("previous")
        spec.description = f"{g.title}\nJämfört med förra matchen" + (f" ({prev})" if prev else "") + "."
        missing, new = n.extra.get("missing") or [], n.extra.get("new") or []
        if not missing and not new:
            spec.description += "\n\nSamma spelare som förra matchen."
        spec.add("Saknas", "\n".join(missing))
        spec.add("Nya i laguppställningen", "\n".join(new))
        spec.footer = "Orsaken (skada, sjukdom, vila m.m.) framgår inte av laguppställningen."
        return spec
    if n.kind == "starters":
        spec = EmbedSpec("🥅 Startande målvakter", color=COLORS["goalie"])
        spec.description = "\n".join(f"**{code(g, e.side)}** {e.player}" for e in n.events)
        return spec
    if n.kind == "tracking":
        spec = EmbedSpec(f"👀 Följer nu {g.title}", color=COLORS["info"])
        st = n.status
        where = f"{period_name(st.period, shootout_period)} {st.clock or ''}".strip() if st and st.period else ""
        spec.description = f"Ställning: **{scoreline(g, n.score)}**" + (f"\n{where}" if where else "")
        return spec
    if n.kind == "correction" and n.event:
        spec = EmbedSpec(f"✏️ Målet korrigerat – {code(g, n.event.side)}", color=COLORS["goal"])
        spec.description = goal_line(g, n.event, n.score)
        return spec
    if n.kind == "disallowed":
        spec = EmbedSpec("❌ Mål bortdömt", color=COLORS["disallowed"])
        spec.description = f"Ny ställning: **{scoreline(g, n.score)}**"
        return spec
    if n.kind == "period" and n.period and n.stats:
        return render_period(n, shootout_period)
    if n.kind == "final" and n.stats:
        return render_final(n, shootout_period)
    return EmbedSpec(g.title, description=n.kind)


def render_event(n: Notification, shootout_period: int) -> EmbedSpec:
    g, e = n.game, n.event
    assert e is not None
    footer = f"{period_name(e.period, shootout_period)} {e.time}"
    team = code(g, e.side)

    if e.kind == GOAL:
        spec = EmbedSpec(f"🚨 MÅL! {scoreline(g, n.score)}", color=COLORS["goal"])
        lines = [f"**{e.player or 'Okänd målskytt'}** ({team_name(g, e.side)})"]
        if e.assists:
            lines.append(f"Assist: {', '.join(e.assists)}")
        lbl = strength_label(e)
        if lbl:
            footer += f" · {lbl}"
        spec.description = "\n".join(lines)
    elif e.kind == PENALTY:
        if is_match_penalty(e):
            spec = EmbedSpec(f"🟥 Matchstraff – {team}", color=COLORS["injury"])
        else:
            spec = EmbedSpec(f"⛔ Utvisning – {team}", color=COLORS["penalty"])
        mins = f"{e.penalty_minutes} min" if e.penalty_minutes else "Utvisning"
        spec.description = f"**{e.player or 'Lagstraff'}** · {mins}" + (f"\n{offence_text(e.offence)}" if e.offence else "")
    elif e.kind == INJURY:
        spec = EmbedSpec(f"🩹 Skada – {team}", color=COLORS["injury"])
        spec.description = "\n".join(x for x in (f"**{e.player}**" if e.player else "", e.description or "") if x)
    elif e.kind == GOALIE_IN:
        spec = EmbedSpec(f"🔄 Målvaktsbyte – {team}", color=COLORS["goalie"])
        lines = [f"⬆️ **{e.player or 'Okänd'}** går in"]
        if n.extra.get("replaced"):
            lines.append(f"⬇️ {n.extra['replaced']} går ut")
        spec.description = "\n".join(lines)
    elif e.kind == GOALIE_OUT:
        spec = EmbedSpec(f"🥅 Tom kasse – {team}", color=COLORS["goalie"])
        spec.description = f"**{e.player or 'Målvakten'}** lämnar kassen för en extra utespelare"
    elif e.kind in (PENALTY_SHOT, SHOOTOUT):
        title = "🎯 Straffslag" if e.kind == PENALTY_SHOT else "🎯 Straffläggning"
        spec = EmbedSpec(f"{title} – {team}", color=COLORS["goalie"])
        scored = first(e.raw, "isGoal", "goal", "scored", "resultedInGoal")
        result = str(first(e.raw, "result", "outcome", default="")).lower()
        if scored is not None or result:
            ok = truthy(scored) or result in ("goal", "mål", "scored")
            outcome = "✅ MÅL" if ok else "❌ Miss"
        else:
            outcome = ""
        spec.description = f"**{e.player or 'Okänd'}** {outcome}".strip()
    else:
        spec = EmbedSpec(f"{e.raw_type} – {team}", description=e.description or "")
    spec.footer = footer
    return spec


def render_period(n: Notification, shootout_period: int) -> EmbedSpec:
    g, p, stats = n.game, n.period, n.stats
    assert p is not None and stats is not None
    spec = EmbedSpec(
        f"⏸️ Efter {period_name(p, shootout_period).lower()}: {scoreline(g, n.score)}",
        color=COLORS["period"],
    )
    in_period = [e for e in n.events if e.period == p]
    spec.add(f"Mål i {period_short(p)}", goal_list_for_period(g, n.events, p, shootout_period) or "Inga mål")
    spec.add(f"Matchstraff i {period_short(p)}", penalty_list(g, in_period))
    st = stats.periods.get(p)
    if st:
        spec.add(f"Statistik {period_short(p)}", stat_table(g, st.home, st.away, stats.has_faceoffs))
    if p > 1:
        tot = stats.total(upto=p)
        spec.add("Totalt i matchen", stat_table(g, tot.home, tot.away, stats.has_faceoffs))
    spec.add("Målvakter", goalie_table(g, stats))
    return spec


def goal_list_for_period(game: GameInfo, events: list[Event], period: int, shootout_period: int) -> str:
    all_lines = goal_list(game, events, shootout_period).split("\n")
    goals = [e for e in events if e.kind == GOAL and e.period < shootout_period]
    return "\n".join(line for line, e in zip(all_lines, goals) if e.period == period)


def render_final(n: Notification, shootout_period: int) -> EmbedSpec:
    g, stats = n.game, n.stats
    assert stats is not None
    periods = [p for p in stats.periods if p < shootout_period]
    suffix = ""
    if any(e.period >= shootout_period for e in n.events if e.kind in (SHOOTOUT, GOAL)):
        suffix = " (efter straffar)"
    elif periods and max(periods) >= 4:
        suffix = " (efter förlängning)"
    spec = EmbedSpec(f"🏁 Slutresultat: {scoreline(g, n.score)}{suffix}", color=COLORS["final"])
    spec.description = f"**{g.title}** {period_scores(g, stats, shootout_period)}"
    spec.add("Mål", goal_list(g, n.events, shootout_period) or "Inga mål")
    spec.add("Matchstraff", penalty_list(g, n.events))
    tot = stats.total()
    spec.add("Statistik – hela matchen", stat_table(g, tot.home, tot.away, stats.has_faceoffs))
    spec.add("Per period", per_period_table(g, stats, shootout_period))
    spec.add("Målvakter", goalie_table(g, stats))
    return spec
