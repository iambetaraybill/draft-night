"""
Loading a player pool from CSV.

Column names vary wildly between exports, so this accepts the common aliases
rather than demanding one exact header row. Positions get folded down to the
four buckets the auction cares about, because nobody at a draft night wants
to argue about whether a left wing-back counts as a defender.
"""

from __future__ import annotations

import csv
import pathlib
import re

from .rules import POSITIONS, Player

NAME_KEYS = ("name", "player", "player_name", "short_name", "long_name", "full_name")
POS_KEYS = ("pos", "position", "positions", "player_positions", "best_position")
RATING_KEYS = ("rating", "overall", "ovr", "overall_rating", "score")
CLUB_KEYS = ("club", "team", "club_name", "team_name")
NATION_KEYS = ("nation", "country", "nationality", "nationality_name")

# Everything EA, SoFIFA and friends might call a position, folded into four.
POS_MAP = {
    "GK": "GK",
    "CB": "DEF", "LB": "DEF", "RB": "DEF", "LWB": "DEF", "RWB": "DEF",
    "SW": "DEF", "DEF": "DEF", "D": "DEF", "DF": "DEF", "DEFENDER": "DEF",
    "CM": "MID", "CDM": "MID", "CAM": "MID", "LM": "MID", "RM": "MID",
    "DM": "MID", "AM": "MID", "MID": "MID", "M": "MID", "MF": "MID",
    "MIDFIELDER": "MID",
    "ST": "FWD", "CF": "FWD", "LW": "FWD", "RW": "FWD", "LF": "FWD",
    "RF": "FWD", "FWD": "FWD", "F": "FWD", "FW": "FWD", "ATT": "FWD",
    "STRIKER": "FWD", "FORWARD": "FWD", "WINGER": "FWD",
}


class PoolError(Exception):
    """Raised when a CSV cannot be turned into a usable pool."""


def _pick(row: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        for actual, value in row.items():
            if actual and actual.strip().lower() == key and value and value.strip():
                return value.strip()
    return None


def normalise_position(raw: str) -> str | None:
    """
    'ST, LW' -> FWD. Takes the first listed position, since that is almost
    always the player's primary one.
    """
    if not raw:
        return None
    first = re.split(r"[,/|;]", raw)[0].strip().upper()
    first = re.sub(r"[^A-Z]", "", first)
    return POS_MAP.get(first)


def load_pool(path: str | pathlib.Path) -> list[Player]:
    path = pathlib.Path(path)
    if not path.exists():
        raise PoolError(f"No player file at {path}")

    players: list[Player] = []
    skipped = 0

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise PoolError(f"{path.name} has no header row")

        for i, row in enumerate(reader):
            name = _pick(row, NAME_KEYS)
            pos = normalise_position(_pick(row, POS_KEYS) or "")
            rating_raw = _pick(row, RATING_KEYS)

            if not name or not pos or not rating_raw:
                skipped += 1
                continue
            try:
                rating = int(float(rating_raw))
            except ValueError:
                skipped += 1
                continue

            players.append(
                Player(
                    pid=f"p{i}",
                    name=name,
                    pos=pos,
                    rating=rating,
                    club=_pick(row, CLUB_KEYS) or "",
                    nation=_pick(row, NATION_KEYS) or "",
                )
            )

    if not players:
        raise PoolError(
            f"{path.name}: found no usable rows. Needs columns for name, "
            f"position and rating (any of {NAME_KEYS[0]}/{NAME_KEYS[1]}, "
            f"{POS_KEYS[0]}/{POS_KEYS[1]}, {RATING_KEYS[0]}/{RATING_KEYS[1]})."
        )

    players.sort(key=lambda p: (-p.rating, p.name))
    if skipped:
        print(f"[pool] loaded {len(players)} players, skipped {skipped} unusable rows")
    return players


def check_pool_depth(players: list[Player], formation, manager_count: int) -> list[str]:
    """
    Warn before the auction starts rather than stranding someone with an
    unfillable slot three hours in.
    """
    warnings = []
    for pos in POSITIONS:
        needed = formation.required(pos) * manager_count
        if not needed:
            continue
        have = sum(1 for p in players if p.pos == pos)
        if have < needed:
            warnings.append(
                f"Only {have} {pos} in the pool but {manager_count} managers "
                f"need {needed}. Add more or lower the {pos} requirement."
            )
    return warnings
