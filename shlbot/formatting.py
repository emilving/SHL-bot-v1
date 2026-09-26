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


def stat_rows(home: TeamLine, away: TeamLine, has_faceoffs: bool) -> list[tuple[str, str, str]]:
    rows = [
        ("Mål", _v(home.goals), _v(away.goals)),
        ("Skott på mål", _v(home.shots), _v(away.shots)),
        ("Räddningar", _v(home.saves), _v(away.saves)),
        ("Räddning%", _pct(home.saves, away.shots - (away.en_goals or 0)),
         _pct(away.saves, home.shots - (home.en_goals or 0))),
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
    head = f"{'':<10}" + "".join(f"{period_short(p):>5}" for p in periods) + f"{'Tot':>5}"
    lines = [head]
    total = stats.total()
    for label, attr in (("Mål", "goals"), ("Skott", "shots"), ("Utv", "pim")):
        for side, tcode, tot in ((HOME, game.home.code, total.home), (AWAY, game.away.code, total.away)):
            vals = "".join(f"{_v(getattr(stats.periods[p].side(side), attr)):>5}" for p in periods)
            lines.append(f"{(label + ' ' + tcode)[:10]:<10}{vals}{_v(getattr(tot, attr)):>5}")
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
    lines = [f"{'Målvakt':<18}{'Sk':>4}{'IM':>4}{'Rd':>4}{'Rd%':>7}"]
    for g in stats.goalies:
        pct = f"{100 * g.save_pct:.1f}" if g.save_pct is not None else "–"
        name = f"{code(game, g.side)} {short_name(g.name)}"[:18]
        lines.append(f"{name:<18}{g.shots_against:>4}{g.goals_against:>4}{g.saves:>4}{pct:>7}")
    lines.append("Sk = skott mot, IM = insläppta mål, Rd = räddningar")
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


def penalty_line(game: GameInfo, e: Event) -> str:
    mins = f"{e.penalty_minutes} min" if e.penalty_minutes else "utvisning"
    who = e.player or "Lagstraff"
    reason = f" – {e.offence}" if e.offence else ""
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
    return "\n".join(penalty_line(game, e) for e in events if e.kind == PENALTY)


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
        spec = EmbedSpec(f"⛔ Utvisning – {team}", color=COLORS["penalty"])
        mins = f"{e.penalty_minutes} min" if e.penalty_minutes else "Utvisning"
        spec.description = f"**{e.player or 'Lagstraff'}** · {mins}" + (f"\n{e.offence}" if e.offence else "")
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
    spec.add(f"Utvisningar i {period_short(p)}", penalty_list(g, in_period) or "Inga utvisningar")
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
    spec.add("Utvisningar", penalty_list(g, n.events) or "Inga utvisningar")
    tot = stats.total()
    spec.add("Statistik – hela matchen", stat_table(g, tot.home, tot.away, stats.has_faceoffs))
    spec.add("Per period", per_period_table(g, stats, shootout_period))
    spec.add("Målvakter", goalie_table(g, stats))
    return spec
