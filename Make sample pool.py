"""
Builds data/players_sample.csv: a pool of invented footballers.

The sample pool is synthetic on purpose. EA's player ratings are EA's, and
the scraped copies floating around on Kaggle don't actually come with the
right to redistribute them, so none of that belongs in a public repo. Point
the app at your own CSV instead (see scripts/load_players.py).

Run: python scripts/make_sample_pool.py
"""

import csv
import pathlib
import random

random.seed(2026)  # deterministic, so the sample pool never churns in git

FIRST = [
    "Didier", "Kwame", "Mateus", "Yuto", "Ravi", "Emre", "Olu", "Niko",
    "Tomas", "Jamal", "Luca", "Ismail", "Patrice", "Sven", "Arun", "Diego",
    "Hakim", "Bo", "Casper", "Ren", "Milos", "Tariq", "Felipe", "Noor",
    "Andrej", "Kofi", "Rafa", "Jin", "Marek", "Salif", "Bruno", "Viktor",
    "Idris", "Pedro", "Elias", "Shin", "Dawit", "Lars", "Omar", "Nico",
]

LAST = [
    "Okonkwo", "Vasquez", "Lindqvist", "Tanaka", "Mehta", "Yilmaz", "Adeyemi",
    "Petrovic", "Havel", "Boateng", "Ferrari", "Benali", "Mbeki", "Larsen",
    "Krishnan", "Moreno", "Ziani", "Jensen", "Holm", "Watanabe", "Jovic",
    "Haddad", "Cardoso", "Rahman", "Novak", "Asante", "Delgado", "Park",
    "Kowalski", "Diarra", "Almeida", "Ilic", "Baraka", "Santos", "Virtanen",
    "Nakamura", "Tesfaye", "Dahl", "Said", "Esposito", "Kubica", "Roux",
]

CLUBS = [
    "Northgate United", "Real Calavera", "Spartak Vesna", "FC Harbourside",
    "Atletico Monte", "Veldspar FC", "Dynamo Ostrava", "Kestrel Rovers",
    "Club Estrella", "Ironworks AFC", "Pelagos SC", "Rotherhill Town",
    "Meridian 04", "CF Alondra", "Baltic Vik", "Sunder Athletic",
]

NATIONS = [
    "Brazil", "Nigeria", "Sweden", "Japan", "India", "Turkey", "Ghana",
    "Serbia", "Czechia", "Italy", "Morocco", "Denmark", "Spain", "Egypt",
    "Portugal", "Finland", "South Korea", "Ethiopia", "Poland", "France",
]

# pool shape: roughly double what six managers of eight need, so there is
# real competition for every position instead of a polite queue
SHAPE = {"GK": 12, "DEF": 32, "MID": 24, "FWD": 22}


def rating_for(index: int, total: int) -> int:
    """
    Spread ratings 62-91 with few at the top, so the pool has a handful of
    genuine marquee players worth fighting over and a long tail of squad
    filler. A flat distribution makes for a boring auction.
    """
    frac = index / max(1, total - 1)
    return round(91 - (frac ** 0.55) * 29)


def main() -> None:
    used_names: set[str] = set()
    rows = []

    for pos, count in SHAPE.items():
        for i in range(count):
            while True:
                name = f"{random.choice(FIRST)} {random.choice(LAST)}"
                if name not in used_names:
                    used_names.add(name)
                    break
            rows.append(
                {
                    "name": name,
                    "pos": pos,
                    "rating": rating_for(i, count),
                    "club": random.choice(CLUBS),
                    "nation": random.choice(NATIONS),
                }
            )

    out = pathlib.Path(__file__).resolve().parent.parent / "data" / "players_sample.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["name", "pos", "rating", "club", "nation"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} invented players to {out}")


if __name__ == "__main__":
    main()
