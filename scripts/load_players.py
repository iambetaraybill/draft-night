"""
Turn your own player CSV into a pool Draft Night can auction.

Exports from rating sites tend to arrive with twenty thousand rows and sixty
columns, which makes for a terrible auction: you would be bidding on the
nineteen-thousandth best left back at two in the morning. This trims one down
to a balanced pool with real competition for every position.

  python scripts/load_players.py ~/Downloads/players.csv
  python scripts/load_players.py ~/Downloads/players.csv --top 120 --min-rating 75
  python scripts/load_players.py in.csv --out data/my_pool.csv --managers 6

Column names are matched loosely, so 'name'/'short_name'/'player',
'pos'/'position'/'player_positions' and 'rating'/'overall' all work. Detailed
positions fold down to GK, DEF, MID and FWD.

Keep the output file out of a public repo if the ratings came from someone
else's database. The pool is yours to use; it is not yours to redistribute.
"""

from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from draftnight.players import PoolError, load_pool  # noqa: E402
from draftnight.rules import POSITIONS  # noqa: E402

# Roughly double what a six-manager 1-3-2-2 draft needs, so no position is a
# formality. Scaled by --managers.
SHARE = {"GK": 2, "DEF": 6, "MID": 4, "FWD": 4}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("source", help="your CSV export")
    ap.add_argument("--out", default="data/my_pool.csv", help="where to write the pool")
    ap.add_argument("--top", type=int, default=0,
                    help="keep only the best N overall, before balancing")
    ap.add_argument("--min-rating", type=int, default=0)
    ap.add_argument("--managers", type=int, default=6,
                    help="how many managers will draft, used to size the pool")
    ap.add_argument("--keep-all", action="store_true",
                    help="skip position balancing and keep everything")
    args = ap.parse_args(argv)

    try:
        players = load_pool(args.source)
    except PoolError as exc:
        print(f"\n  {exc}\n")
        return 1

    print(f"  read {len(players)} usable players from {pathlib.Path(args.source).name}")

    if args.min_rating:
        players = [p for p in players if p.rating >= args.min_rating]
        print(f"  {len(players)} left at rating {args.min_rating} or better")

    if args.top:
        players = players[: args.top]
        print(f"  trimmed to the best {len(players)}")

    if not args.keep_all:
        want = {pos: SHARE[pos] * args.managers for pos in POSITIONS}
        chosen: list = []
        for pos in POSITIONS:
            ranked = [p for p in players if p.pos == pos][: want[pos]]
            if len(ranked) < want[pos]:
                print(f"  heads up: wanted {want[pos]} {pos}, only found {len(ranked)}")
            chosen.extend(ranked)
        players = chosen

    if not players:
        print("\n  Nothing left after filtering. Loosen --min-rating or --top.\n")
        return 1

    players.sort(key=lambda p: (-p.rating, p.name))
    counts = collections.Counter(p.pos for p in players)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "pos", "rating", "club", "nation"])
        for p in players:
            w.writerow([p.name, p.pos, p.rating, p.club, p.nation])

    spread = f"{players[-1].rating}-{players[0].rating}"
    print(f"""
  wrote {len(players)} players to {out}
  {dict(counts)}, ratings {spread}

  Run it:  python -m draftnight.server --pool {out}
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
