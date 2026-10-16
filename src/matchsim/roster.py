"""Fictional clubs, players and formations.

Everything here is invented: club names, colours, venues and player names are
procedurally generated so no real Premier League entity is reproduced.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field

# (name, short, primary colour, secondary colour, venue, style)
# style tweaks how the club plays: possession share, pressing, directness.
CLUBS: list[dict] = [
    {"name": "Riverside FC", "short": "RIV", "colors": ["#C8102E", "#FFFFFF"], "venue": "Millbank Stadium",
     "style": {"possession": 0.62, "press": 0.70, "directness": 0.30}},
    {"name": "Northgate United", "short": "NGU", "colors": ["#1D428A", "#FDB913"], "venue": "Northgate Park",
     "style": {"possession": 0.45, "press": 0.45, "directness": 0.65}},
    {"name": "Harbour City", "short": "HBC", "colors": ["#6CABDD", "#1C2C5B"], "venue": "The Quayside",
     "style": {"possession": 0.66, "press": 0.75, "directness": 0.25}},
    {"name": "Ashford Rovers", "short": "ASR", "colors": ["#00A650", "#000000"], "venue": "Rovers Lane",
     "style": {"possession": 0.50, "press": 0.55, "directness": 0.50}},
    {"name": "Kingsbridge Athletic", "short": "KBA", "colors": ["#7A263A", "#95BFE5"], "venue": "Kingsbridge Road",
     "style": {"possession": 0.48, "press": 0.60, "directness": 0.55}},
    {"name": "Castleton Wanderers", "short": "CSW", "colors": ["#FF6B00", "#231F20"], "venue": "Castle Meadow",
     "style": {"possession": 0.42, "press": 0.40, "directness": 0.70}},
]

FIRST_NAMES = [
    "Adam", "Alex", "Amadou", "Andrés", "Ben", "Callum", "Daniel", "Diego", "Elias", "Emil", "Felix",
    "Gabriel", "Hugo", "Isaac", "Jakub", "Jonas", "Joel", "Kai", "Kofi", "Leo", "Liam", "Luca", "Mateo",
    "Matteo", "Max", "Moussa", "Noah", "Oliver", "Oscar", "Rafael", "Ruben", "Sami", "Theo", "Tomás",
    "Victor", "Yusuf", "Zeki", "Ethan", "Marek", "Nico", "Owen", "Pedro", "Rio", "Sven", "Tariq",
]
_SYL_A = ["Bran", "Cor", "Dal", "Fen", "Gar", "Hol", "Kes", "Lan", "Mor", "Nor", "Pel", "Quin", "Ros",
          "Sel", "Tor", "Val", "Wex", "Ard", "Bel", "Dov", "Ema", "Kal", "Ost", "Ruv"]
_SYL_B = ["by", "den", "dale", "ford", "ham", "ley", "mont", "ner", "rick", "sen", "ton", "vik", "well",
          "ari", "ero", "ini", "ovic", "aro", "eau", "iak", "ssen", "ton", "wood", "ez"]

# Formation slots in an attacking frame: x is metres from own goal line, y across the pitch.
FORMATIONS: dict[str, list[tuple[str, float, float]]] = {
    "4-3-3": [
        ("GK", 5, 34), ("RB", 30, 60), ("CB", 22, 44), ("CB", 22, 24), ("LB", 30, 8),
        ("DM", 40, 34), ("CM", 50, 48), ("CM", 50, 20), ("RW", 72, 60), ("ST", 78, 34), ("LW", 72, 8),
    ],
    "4-2-3-1": [
        ("GK", 5, 34), ("RB", 30, 60), ("CB", 22, 44), ("CB", 22, 24), ("LB", 30, 8),
        ("DM", 40, 44), ("DM", 40, 24), ("RW", 66, 58), ("AM", 64, 34), ("LW", 66, 10), ("ST", 80, 34),
    ],
    "3-5-2": [
        ("GK", 5, 34), ("CB", 22, 50), ("CB", 20, 34), ("CB", 22, 18), ("RWB", 50, 62),
        ("DM", 42, 34), ("CM", 52, 46), ("CM", 52, 22), ("LWB", 50, 6), ("ST", 78, 42), ("ST", 78, 26),
    ],
}

# Base attribute profile per role (0-100); individual players vary around it.
ROLE_PROFILE: dict[str, dict[str, int]] = {
    "GK":  {"passing": 55, "vision": 45, "dribbling": 25, "finishing": 10, "shot_power": 40, "tackling": 20, "pace": 45, "goalkeeping": 78},
    "CB":  {"passing": 64, "vision": 52, "dribbling": 50, "finishing": 30, "shot_power": 60, "tackling": 78, "pace": 62, "goalkeeping": 5},
    "RB":  {"passing": 68, "vision": 60, "dribbling": 66, "finishing": 38, "shot_power": 58, "tackling": 70, "pace": 76, "goalkeeping": 5},
    "LB":  {"passing": 68, "vision": 60, "dribbling": 66, "finishing": 38, "shot_power": 58, "tackling": 70, "pace": 76, "goalkeeping": 5},
    "RWB": {"passing": 68, "vision": 62, "dribbling": 70, "finishing": 45, "shot_power": 60, "tackling": 64, "pace": 80, "goalkeeping": 5},
    "LWB": {"passing": 68, "vision": 62, "dribbling": 70, "finishing": 45, "shot_power": 60, "tackling": 64, "pace": 80, "goalkeeping": 5},
    "DM":  {"passing": 76, "vision": 70, "dribbling": 64, "finishing": 45, "shot_power": 66, "tackling": 74, "pace": 62, "goalkeeping": 5},
    "CM":  {"passing": 78, "vision": 74, "dribbling": 70, "finishing": 55, "shot_power": 70, "tackling": 62, "pace": 68, "goalkeeping": 5},
    "AM":  {"passing": 78, "vision": 80, "dribbling": 78, "finishing": 66, "shot_power": 70, "tackling": 45, "pace": 72, "goalkeeping": 5},
    "RW":  {"passing": 70, "vision": 70, "dribbling": 80, "finishing": 68, "shot_power": 68, "tackling": 40, "pace": 84, "goalkeeping": 5},
    "LW":  {"passing": 70, "vision": 70, "dribbling": 80, "finishing": 68, "shot_power": 68, "tackling": 40, "pace": 84, "goalkeeping": 5},
    "ST":  {"passing": 64, "vision": 64, "dribbling": 72, "finishing": 80, "shot_power": 78, "tackling": 38, "pace": 78, "goalkeeping": 5},
}
_BENCH_ROLES = ["GK", "CB", "RB", "DM", "CM", "AM", "LW", "ST", "CB"]


@dataclass(slots=True)
class Player:
    player_id: str
    name: str
    short_name: str
    number: int
    role: str
    attributes: dict[str, int]
    top_speed_kmh: float
    stamina: float  # 0-1 at kickoff
    base_x: float = 0.0
    base_y: float = 0.0
    starter: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("base_x"), d.pop("base_y")
        return d


@dataclass(slots=True)
class Team:
    team_id: str
    name: str
    short: str
    colors: list[str]
    venue: str
    formation: str
    style: dict[str, float]
    strength: float  # overall quality multiplier around 1.0
    players: list[Player] = field(default_factory=list)  # starters first, then bench

    def to_dict(self) -> dict:
        return {
            "team_id": self.team_id, "name": self.name, "short": self.short, "colors": self.colors,
            "formation": self.formation, "style": self.style, "strength": round(self.strength, 3),
            "players": [p.to_dict() for p in self.players],
        }


def _surname(rng: random.Random) -> str:
    return rng.choice(_SYL_A) + rng.choice(_SYL_B)


def _make_player(rng: random.Random, team_id: str, idx: int, number: int, role: str, strength: float,
                 used_names: set[str]) -> Player:
    while True:
        first, last = rng.choice(FIRST_NAMES), _surname(rng)
        if f"{first} {last}" not in used_names:
            used_names.add(f"{first} {last}")
            break
    quality = rng.gauss(0, 5) + (strength - 1.0) * 40
    attrs = {k: int(max(5, min(97, v + quality + rng.gauss(0, 6)))) for k, v in ROLE_PROFILE[role].items()}
    attrs["stamina"] = int(max(50, min(97, rng.gauss(78, 8))))
    attrs["composure"] = int(max(40, min(97, rng.gauss(70, 9) + quality)))
    top_speed = round(28.5 + attrs["pace"] / 100 * 7.5 + rng.gauss(0, 0.6), 1)
    return Player(
        player_id=f"{team_id}-{idx:02d}", name=f"{first} {last}", short_name=f"{first[0]}. {last}",
        number=number, role=role, attributes=attrs, top_speed_kmh=top_speed, stamina=1.0,
    )


def make_team(rng: random.Random, club: dict, team_id: str, strength: float, formation: str | None = None) -> Team:
    formation = formation or rng.choice(list(FORMATIONS))
    team = Team(team_id=team_id, name=club["name"], short=club["short"], colors=club["colors"],
                venue=club["venue"], formation=formation, style=dict(club["style"]), strength=strength)
    used: set[str] = set()
    numbers = rng.sample(range(2, 40), 25)
    for i, (role, bx, by) in enumerate(FORMATIONS[formation]):
        number = 1 if role == "GK" else numbers.pop()
        p = _make_player(rng, team_id, i + 1, number, role, strength, used)
        p.base_x, p.base_y = bx, by
        team.players.append(p)
    for j, role in enumerate(_BENCH_ROLES):
        p = _make_player(rng, team_id, 12 + j, numbers.pop(), role, strength * 0.98, used)
        p.starter = False
        team.players.append(p)
    return team


def pick_clubs(rng: random.Random, home: str | None, away: str | None) -> tuple[dict, dict]:
    by_name = {c["name"].lower(): c for c in CLUBS} | {c["short"].lower(): c for c in CLUBS}
    h = by_name[home.lower()] if home else rng.choice(CLUBS)
    rest = [c for c in CLUBS if c is not h]
    a = by_name[away.lower()] if away else rng.choice(rest)
    if a is h:
        raise ValueError("home and away club must differ")
    return h, a
