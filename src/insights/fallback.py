"""Deterministic twins of every LLM agent, plus the fact guard.

Each agent in the workflow has a rule-based twin with the same output contract.
The twin runs when Foundry is not configured, when a model call fails or times
out, or when generated text fails fact verification. The broadcast never stalls,
and every fallback is recorded in the cue's provenance.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import Explanation, Interpretation, Narrative, PersonaVariant, Recap, StatLine, ViewerProfile
from .moments import Moment

# --------------------------------------------------------------------------- fact guard

_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)")


def _numbers_in(obj: Any) -> Iterable[float]:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        yield float(obj)
    elif isinstance(obj, str):
        for n in _NUM.findall(obj):
            yield float(n.replace(",", "."))
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _numbers_in(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _numbers_in(v)


def allowed_numbers(*sources: Any) -> set[float]:
    allowed = {float(i) for i in range(0, 12)}  # counts like "two", "10 men", "11 players"
    for src in sources:
        for v in _numbers_in(src):
            allowed |= {round(v, 2), round(v, 1), float(round(v))}
            if 0 < v <= 1:
                allowed |= {round(v * 100), round(v * 100, 1)}  # probabilities shown as %
            if v > 100:  # metres -> km
                allowed |= {round(v / 1000, 1), round(v / 1000, 2)}
    return allowed


def unverified_numbers(text: str, allowed: set[float]) -> list[str]:
    bad = []
    for raw in _NUM.findall(text):
        v = float(raw.replace(",", "."))
        if v in allowed or round(v, 1) in allowed or float(round(v)) in allowed:
            continue
        if any(abs(v - a) <= (0.05 if v < 5 else 0.5) for a in allowed):
            continue
        bad.append(raw)
    return bad


# --------------------------------------------------------------------------- analyst twin


def _fmt(v: Any, unit: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        v = f"{v:.2f}".rstrip("0").rstrip(".") if v < 10 else f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{v}{unit}"


def interpret(m: Moment) -> Interpretation:
    f = m.facts
    k = m.kind
    stats: list[StatLine] = []
    pattern = ""
    if k == "goal":
        stats = [StatLine(label="xG", value=_fmt(f.get("xg"))),
                 StatLine(label="Shot speed", value=_fmt(f.get("shot_speed_kmh"), " km/h")),
                 StatLine(label="Build-up passes", value=_fmt(f.get("build_up_passes"))),
                 StatLine(label="Team xG", value=_fmt(f.get("team_xg_total")))]
        pattern = (f"A {_fmt(f.get('xg'))} xG chance finished after a {_fmt(f.get('build_up_passes'))}-pass move."
                   if f.get("xg") is not None else "Goal scored.")
    elif k in ("big_chance", "key_save"):
        xg = f.get("xg", f.get("xg_faced"))
        stats = [StatLine(label="xG", value=_fmt(xg)), StatLine(label="Shot speed", value=_fmt(f.get("shot_speed_kmh"), " km/h"))]
        pattern = f"A chance worth {_fmt(xg)} xG did not go in."
    elif k == "pass_quality":
        stats = [StatLine(label="Completion chance", value=f"{f['completion_probability_pct']}%"),
                 StatLine(label="Difficulty", value=f"{f['difficulty_rating']}/10"),
                 StatLine(label="Distance", value=_fmt(f["distance_m"], " m")),
                 StatLine(label="Ball speed", value=_fmt(f["ball_speed_kmh"], " km/h"))]
        pattern = f"Only {f['completion_probability_pct']}% of passes like this are completed."
    elif k == "player_alert":
        if f.get("alert") == "top_speed":
            stats = [StatLine(label="Top speed", value=_fmt(f["speed_kmh"], " km/h"))]
            pattern = f"Sprint above the {_fmt(f['threshold_kmh'])} km/h threshold."
        else:
            stats = [StatLine(label="Distance", value=_fmt(f["distance_km"], " km"))]
            pattern = f"{_fmt(f['distance_km'])} km covered so far."
    elif k in ("momentum_swing", "pressing_wave", "control_shift"):
        w = f["window"]
        team = f.get("team") or next(iter(w["teams"]))
        b = w["teams"][team]
        stats = [StatLine(label="Momentum", value=_fmt(abs(w["momentum"]))),
                 StatLine(label="Possession (5 min)", value=_fmt(b["possession_pct"], "%")),
                 StatLine(label="High pressures", value=_fmt(b["high_pressures"])),
                 StatLine(label="Chaos index", value=_fmt(w["chaos_index"]))]
        pattern = (f"Over the last {_fmt(w['window_minutes'])} minutes the game is {w['game_state']} "
                   f"with {_fmt(w['possession_changes_per_min'])} possession changes per minute.")
    elif k == "player_focus":
        stats = [StatLine(label="Passes", value=_fmt(f.get("passes"))),
                 StatLine(label="Key passes", value=_fmt(f.get("key_passes"))),
                 StatLine(label="xG", value=_fmt(f.get("xg"))),
                 StatLine(label="Top speed", value=_fmt(f.get("top_speed_kmh"), " km/h"))]
        pattern = f"{f['player']}: {_fmt(f.get('distance_km'))} km covered."
    elif k in ("red_card", "yellow_card"):
        stats = [StatLine(label="Players left", value=_fmt(f.get("players_remaining")))]
        pattern = f"{f['player']} has committed {_fmt(f.get('player_fouls'))} fouls."
    elif k == "substitution":
        stats = [StatLine(label="Distance (off)", value=_fmt(f.get("outgoing_distance_km"), " km"))]
        pattern = f"Change made for {f.get('reason')} reasons."
    elif k in ("half_time", "full_time"):
        h, a = f["home"], f["away"]
        stats = [StatLine(label="xG", value=f"{_fmt(h['xg'])} - {_fmt(a['xg'])}"),
                 StatLine(label="Shots", value=f"{h['shots']} - {a['shots']}"),
                 StatLine(label="Possession", value=f"{_fmt(h['possession_pct'])}% - {_fmt(a['possession_pct'])}%"),
                 StatLine(label="Pass accuracy", value=f"{_fmt(h['pass_accuracy_pct'])}% - {_fmt(a['pass_accuracy_pct'])}%")]
        pattern = f"{h['team']} {_fmt(h['xg'])} xG vs {a['team']} {_fmt(a['xg'])} xG."
    else:
        pattern = "Match update."
    return Interpretation(key_stats=stats[:4], pattern=pattern)


# --------------------------------------------------------------------------- tactician twin


def explain(m: Moment) -> Explanation:
    f, k = m.facts, m.kind
    if k == "goal":
        tags = set(f.get("tags") or [])
        if "penalty" in tags or "direct_free_kick" in tags or "set_piece" in tags:
            return Explanation(why_it_matters="A dead-ball situation decided this; set pieces can swing even games.",
                               driver="set_piece", evidence=[f"xG {_fmt(f.get('xg'))}"])
        if "counter_attack" in tags:
            return Explanation(why_it_matters=f"Turnover punished at speed: {_fmt(f.get('build_up_seconds'))} seconds from winning the ball to scoring.",
                               driver="control_vs_chaos", evidence=[f"{_fmt(f.get('build_up_passes'))} passes", f"xG {_fmt(f.get('xg'))}"])
        if (f.get("build_up_passes") or 0) >= 8:
            return Explanation(why_it_matters=f"Control turned into a goal: a patient {_fmt(f.get('build_up_passes'))}-pass move pulled the defence apart.",
                               driver="control_vs_chaos", evidence=[f"{_fmt(f.get('build_up_seconds'))} s of possession"])
        if (f.get("xg") or 1) < 0.1:
            return Explanation(why_it_matters=f"Pure finishing: chances like this ({_fmt(f.get('xg'))} xG) are rarely scored.",
                               driver="finishing", evidence=[f"xG {_fmt(f.get('xg'))}"])
        return Explanation(why_it_matters="Individual quality made the difference in a key area.",
                           driver="individual_quality", evidence=[f"xG {_fmt(f.get('xg'))}"])
    if k == "big_chance":
        return Explanation(why_it_matters=f"A {_fmt(f['xg'])} xG chance missed keeps the door open for the opponent.",
                           driver="finishing", evidence=[f"xG {_fmt(f['xg'])}"])
    if k == "key_save":
        return Explanation(why_it_matters="The goalkeeper kept the score intact against a quality chance.",
                           driver="goalkeeping", evidence=[f"xG faced {_fmt(f.get('xg_faced'))}"])
    if k == "red_card":
        return Explanation(why_it_matters=f"Down to {_fmt(f.get('players_remaining'))} men, {f['team']} must now choose between control and survival.",
                           driver="numerical_advantage", evidence=[f"{_fmt(f.get('players_remaining'))} players left"])
    if k in ("momentum_swing", "pressing_wave", "control_shift"):
        w = f["window"]
        team = f.get("team") or next(iter(w["teams"]))
        b = w["teams"][team]
        if k == "pressing_wave":
            return Explanation(why_it_matters=f"{team} are winning the ball high up the pitch, forcing errors near goal.",
                               driver="pressing", evidence=[f"{b['high_pressures']} high pressures", f"{b['high_ball_wins']} high ball wins"])
        if k == "control_shift":
            calm = w["game_state"] == "controlled"
            return Explanation(
                why_it_matters=("The game has settled: long possessions favour the side with more control."
                                if calm else "The game has broken up: frequent turnovers favour the side quicker in transition."),
                driver="control_vs_chaos", evidence=[f"chaos index {_fmt(w['chaos_index'])}",
                                                     f"{_fmt(w['possession_changes_per_min'])} changes/min"])
        return Explanation(why_it_matters=f"{team} have taken over: more territory, more entries, more threat.",
                           driver="game_rhythm", evidence=[f"{b['final_third_entries']} final-third entries",
                                                           f"xG {_fmt(b['xg'])} in the window"])
    if k == "player_alert":
        return Explanation(why_it_matters="Physical output like this stretches the opposition defence.",
                           driver="individual_quality", evidence=[])
    if k == "pass_quality":
        return Explanation(why_it_matters="A rare, high-difficulty pass that unlocked the defence.",
                           driver="individual_quality", evidence=[f"{f['completion_probability_pct']}% completion chance"])
    if k == "substitution":
        return Explanation(why_it_matters="Fresh legs change the energy of the game.", driver="fatigue", evidence=[])
    if k in ("half_time", "full_time"):
        h, a = f["home"], f["away"]
        better = h if h["xg"] >= a["xg"] else a
        so_far = " so far" if k == "half_time" else ""
        return Explanation(why_it_matters=f"{better['team']} created the better chances{so_far}.",
                           driver="game_rhythm", evidence=[f"xG {_fmt(h['xg'])} - {_fmt(a['xg'])}"])
    return Explanation(why_it_matters="", driver="game_rhythm", evidence=[])


# --------------------------------------------------------------------------- narrator + persona twins (localized)

T: dict[str, dict[str, tuple[str, str]]] = {
    # kind: (headline, caption)
    "en": {
        "goal": ("GOAL! {scorer} {minute}'", "{score}. {why}"),
        "big_chance": ("Big chance for {player}", "{xg} xG chance goes begging."),
        "key_save": ("Huge save by {goalkeeper}", "Denies {shooter} from a {xg_faced} xG chance."),
        "red_card": ("RED CARD: {player}", "{team} down to {players_remaining} men."),
        "yellow_card": ("Yellow card: {player}", "{team}"),
        "substitution": ("Change for {team}", "{player_on} on, {player_off} off."),
        "player_alert_top_speed": ("{player}: {speed_kmh} km/h", "Fastest sprint over {threshold_kmh} km/h."),
        "player_alert_distance": ("{player}: {distance_km} km", "Distance covered so far."),
        "pass_quality": ("Pass of quality: {player}", "Only a {completion_probability_pct}% chance of completion. Difficulty {difficulty_rating}/10."),
        "momentum_swing": ("{team} take control", "{why}"),
        "pressing_wave": ("{team} pressing high", "{why}"),
        "control_shift": ("The game has changed", "{why}"),
        "added_time": ("+{added_minutes} minutes added", ""),
        "player_focus": ("{player} - {minute}' check-in", "{passes} passes ({pass_accuracy_pct}%), {distance_km} km, top speed {top_speed_kmh} km/h."),
        "half_time": ("Half-time: {score}", "{why}"),
        "full_time": ("Full-time: {score}", "{why}"),
    },
    "es": {
        "goal": ("¡GOL! {scorer} {minute}'", "{score}."),
        "big_chance": ("Ocasión clara de {player}", "Una ocasión de {xg} xG desperdiciada."),
        "key_save": ("Gran parada de {goalkeeper}", "Evita el gol de {shooter} ({xg_faced} xG)."),
        "red_card": ("TARJETA ROJA: {player}", "{team} se queda con {players_remaining}."),
        "yellow_card": ("Amarilla: {player}", "{team}"),
        "substitution": ("Cambio en {team}", "Entra {player_on}, sale {player_off}."),
        "player_alert_top_speed": ("{player}: {speed_kmh} km/h", "Sprint por encima de {threshold_kmh} km/h."),
        "player_alert_distance": ("{player}: {distance_km} km", "Distancia recorrida."),
        "pass_quality": ("Pase de calidad: {player}", "Solo un {completion_probability_pct}% de probabilidad de éxito."),
        "momentum_swing": ("{team} toma el control", "Domina los últimos minutos."),
        "pressing_wave": ("{team} presiona arriba", "Recupera el balón en campo rival."),
        "control_shift": ("El partido ha cambiado", "Ritmo de juego distinto."),
        "added_time": ("+{added_minutes} minutos de añadido", ""),
        "player_focus": ("{player} - minuto {minute}", "{passes} pases ({pass_accuracy_pct}%), {distance_km} km, velocidad máx. {top_speed_kmh} km/h."),
        "half_time": ("Descanso: {score}", ""),
        "full_time": ("Final: {score}", ""),
    },
    "cs": {
        "goal": ("GÓL! {scorer} {minute}'", "{score}."),
        "big_chance": ("Velká šance: {player}", "Šance za {xg} xG zůstala nevyužita."),
        "key_save": ("Skvělý zákrok: {goalkeeper}", "Zneškodnil šanci hráče {shooter} ({xg_faced} xG)."),
        "red_card": ("ČERVENÁ KARTA: {player}", "{team} hraje v {players_remaining}."),
        "yellow_card": ("Žlutá karta: {player}", "{team}"),
        "substitution": ("Střídání: {team}", "Na hřiště {player_on}, odchází {player_off}."),
        "player_alert_top_speed": ("{player}: {speed_kmh} km/h", "Sprint nad {threshold_kmh} km/h."),
        "player_alert_distance": ("{player}: {distance_km} km", "Naběhaná vzdálenost."),
        "pass_quality": ("Parádní přihrávka: {player}", "Šance na úspěch jen {completion_probability_pct} %."),
        "momentum_swing": ("{team} přebírá kontrolu", "Dominuje posledním minutám."),
        "pressing_wave": ("{team} napadá vysoko", "Získává míče na polovině soupeře."),
        "control_shift": ("Zápas změnil tvář", "Jiný rytmus hry."),
        "added_time": ("Nastaveno +{added_minutes} min", ""),
        "player_focus": ("{player} - {minute}. minuta", "{passes} přihrávek ({pass_accuracy_pct} %), {distance_km} km, max. {top_speed_kmh} km/h."),
        "half_time": ("Poločas: {score}", ""),
        "full_time": ("Konec zápasu: {score}", ""),
    },
}

STAT_LABELS: dict[str, dict[str, str]] = {
    "es": {"xG": "xG", "Shot speed": "Velocidad del tiro", "Build-up passes": "Pases previos", "Team xG": "xG equipo",
           "Completion chance": "Prob. de éxito", "Difficulty": "Dificultad", "Distance": "Distancia",
           "Ball speed": "Velocidad del balón", "Top speed": "Velocidad máx.", "Momentum": "Dominio",
           "Possession (5 min)": "Posesión (5 min)", "High pressures": "Presión alta", "Chaos index": "Índice de caos",
           "Players left": "Jugadores", "Shots": "Tiros", "Possession": "Posesión", "Pass accuracy": "Precisión de pase",
           "Distance (off)": "Distancia (sale)",
           "Passes": "Pases", "Key passes": "Pases clave"},
    "cs": {"xG": "xG", "Shot speed": "Rychlost střely", "Build-up passes": "Přihrávky v akci", "Team xG": "xG týmu",
           "Completion chance": "Šance na úspěch", "Difficulty": "Obtížnost", "Distance": "Vzdálenost",
           "Ball speed": "Rychlost míče", "Top speed": "Max. rychlost", "Momentum": "Převaha",
           "Possession (5 min)": "Držení (5 min)", "High pressures": "Vysoký presink", "Chaos index": "Index chaosu",
           "Players left": "Hráčů na hřišti", "Shots": "Střely", "Possession": "Držení míče",
           "Pass accuracy": "Přesnost přihrávek", "Distance (off)": "Uběhl (střídaný)",
           "Passes": "Přihrávky", "Key passes": "Klíčové přihrávky"},
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return ""


def _template_key(m: Moment) -> str:
    if m.kind == "player_alert":
        return "player_alert_top_speed" if m.facts.get("alert") == "top_speed" else "player_alert_distance"
    return m.kind


def _render(lang: str, m: Moment, why: str = "") -> tuple[str, str]:
    table = T.get(lang, T["en"])
    head, cap = table.get(_template_key(m), T["en"].get(_template_key(m), ("{score}", "")))
    values = _SafeDict({k: _fmt(v) for k, v in m.facts.items() if not isinstance(v, (dict, list))})
    values["why"] = why
    return head.format_map(values).strip()[:60], cap.format_map(values).strip()[:160]


def narrate(m: Moment, explanation: Explanation | None = None) -> Narrative:
    why = explanation.why_it_matters if explanation else ""
    headline, caption = _render("en", m, why)
    commentary = f"{headline}. {caption}".strip()
    return Narrative(headline=headline, caption=caption or why[:160], commentary=commentary[:280])


def personalize(m: Moment, profile: ViewerProfile, interpretation: Interpretation | None,
                explanation: Explanation | None, narrative: Narrative | None) -> PersonaVariant:
    lang = profile.language if profile.language in T else "en"
    if lang == "en" and narrative:
        headline, caption = narrative.headline, narrative.caption
    else:
        headline, caption = _render(lang, m, explanation.why_it_matters if (explanation and lang == "en") else "")
    stats = list(interpretation.key_stats) if interpretation else []
    if profile.mode == "casual":
        stats = stats[:1]
    elif profile.mode in ("club_fan", "player_focus"):
        stats = stats[:2]
    if lang in STAT_LABELS:
        stats = [StatLine(label=STAT_LABELS[lang].get(s.label, s.label), value=s.value) for s in stats]
    why = explanation.why_it_matters if explanation else ""
    if profile.mode == "analyst" and why and lang == "en" and why not in caption:
        caption = f"{caption} {why}".strip()[:160]
    return PersonaVariant(profile_id=profile.profile_id, language=lang, headline=headline, caption=caption, stats=stats)


def recap(m: Moment) -> Recap:
    f = m.facts
    h, a = f["home"], f["away"]
    goals = [t for t in f["timeline"] if t["type"] == "goal"]
    lines = [f"{g['minute']}' {g['player']} ({g['team']}) - {g['score']}" for g in goals][:5]
    if not lines:
        lines = [f"{h['team']} {_fmt(h['xg'])} xG, {a['team']} {_fmt(a['xg'])} xG"]
    leader = f["top_performers"][0]["name"] if f["top_performers"] else ""
    phase = "Half-time" if m.kind == "half_time" else "Full-time"
    summary = (f"{phase}: {f['score']}. {h['team']} had {_fmt(h['possession_pct'])}% of the ball and "
               f"{h['shots']} shots ({_fmt(h['xg'])} xG); {a['team']} replied with {a['shots']} shots "
               f"({_fmt(a['xg'])} xG).")
    return Recap(title=f"{phase}: {f['score']}"[:70], summary=summary, key_moments=lines, player_of_the_match=leader)
