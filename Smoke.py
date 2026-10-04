"""
Drives a complete auction over HTTP against a running server.

This is the test that would have caught every bug that actually mattered:
squads that never fill, agents that never bid, lots that never resolve,
and money going negative.

Run:
  DRAFT_HOST_TOKEN=test python -m draftnight.server --no-llm --port 7111 &
  python tests/smoke.py http://127.0.0.1:7111
"""

import sys
import time

import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7111"
HEAD = {"X-Host-Token": "test"}
FAILURES = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'pass' if ok else 'FAIL'}  {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(label)


def state() -> dict:
    return requests.get(f"{BASE}/api/state", timeout=5).json()


def main() -> int:
    print("\nlobby")
    for name in ("Rohit", "Dev"):
        r = requests.post(f"{BASE}/api/join", json={"name": name}, timeout=5)
        check(f"{name} joined", r.ok, r.text[:80])
    seats = {m["name"]: m["mid"] for m in state()["managers"]}

    r = requests.post(f"{BASE}/api/join", json={"name": "rohit"}, timeout=5)
    check("duplicate name refused", r.status_code == 400)

    r = requests.post(
        f"{BASE}/api/agent",
        json={"name": "Sam", "strategy": "Pace up front, keeper early, never overpay."},
        headers=HEAD,
        timeout=5,
    )
    check("proxy manager added", r.ok, r.text[:80])

    r = requests.post(f"{BASE}/api/agent", json={"name": "Nope", "strategy": "x"}, headers=HEAD, timeout=5)
    check("thin strategy refused", r.status_code == 400)

    r = requests.post(f"{BASE}/api/start", json={}, timeout=5)
    check("start without host token refused", r.status_code == 403)

    r = requests.post(f"{BASE}/api/start", json={}, headers=HEAD, timeout=5)
    check("auction started", r.ok, r.text[:80])

    print("\nbidding")
    s = state()
    check("a lot is live", s["phase"] == "bidding" and s["lot"] is not None)

    lot = s["lot"]
    r = requests.post(
        f"{BASE}/api/bid", json={"mid": seats["Rohit"], "amount": 10_000}, timeout=5
    )
    check("absurd overbid refused", r.status_code == 400, r.json().get("error", "")[:60])

    r = requests.post(
        f"{BASE}/api/bid", json={"mid": seats["Rohit"], "amount": s["next_bid"]}, timeout=5
    )
    check("legal bid accepted", r.ok, r.text[:80])

    s = state()
    check("bid shows as leading", s["high_bidder"] == seats["Rohit"])
    r = requests.post(
        f"{BASE}/api/bid", json={"mid": seats["Rohit"], "amount": s["next_bid"]}, timeout=5
    )
    check("cannot outbid yourself", r.status_code == 400)

    print("\nrunning the draft to completion")
    started = time.monotonic()
    bids_placed = 0
    lots_seen = set()

    # The clock is left to run on its own rather than hammered closed, so the
    # proxy manager gets its reaction window like it would on the night.
    while time.monotonic() - started < 220:
        s = state()
        if s["phase"] == "review":
            break
        if s["phase"] == "bidding" and s["lot"]:
            lots_seen.add(s["lot_number"])
            for name in ("Rohit", "Dev"):
                m = next(x for x in s["managers"] if x["name"] == name)
                nb = s["next_bid"]
                affordable = m["left"] // max(1, m["slots_left"])
                if (
                    not m["leading"]
                    and m["needs"].get(s["lot"]["pos"])
                    and m["max_bid"] >= nb
                    and nb <= affordable + 4
                ):
                    rr = requests.post(
                        f"{BASE}/api/bid", json={"mid": m["mid"], "amount": nb}, timeout=5
                    )
                    if rr.ok:
                        bids_placed += 1
        time.sleep(0.3)

    s = state()
    check("auction reached review", s["phase"] == "review", f"stuck in {s['phase']}")
    check("multiple lots ran", len(lots_seen) >= 8, f"{len(lots_seen)} lots")
    check("human bids landed", bids_placed > 0, f"{bids_placed} bids")

    print("\nfinal accounting")
    squad_size = sum(s["formation"].values())
    for m in s["managers"]:
        spent = sum(r["price"] for r in m["roster"])
        check(f"{m['name']}: never overspent", spent <= m["budget"], f"{spent}/{m['budget']}")
        check(f"{m['name']}: budget left is not negative", m["left"] >= 0, str(m["left"]))
        check(
            f"{m['name']}: squad of {len(m['roster'])}/{squad_size}",
            len(m["roster"]) == squad_size,
            f"{m['slots_left']} unfilled",
        )
        counts: dict[str, int] = {}
        for r in m["roster"]:
            counts[r["pos"]] = counts.get(r["pos"], 0) + 1
        check(
            f"{m['name']}: formation respected",
            all(counts.get(p, 0) == n for p, n in s["formation"].items()),
            str(counts),
        )

    agent = next(m for m in s["managers"] if m["is_ai"])
    check("proxy manager bought players", len(agent["roster"]) > 0)
    check("valuations were made", s["stats"]["heuristic_calls"] > 0, str(s["stats"]))

    print(f"\n{len(FAILURES)} failures" if FAILURES else "\nall good")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
