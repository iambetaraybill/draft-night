"""
The LAN server. One laptop runs this; the TV and everyone's phone are views.

Plain HTTP and server-sent events rather than websockets, which keeps the
dependency list at two packages and means a phone needs nothing but a
browser and the URL. No accounts, no cloud, no internet.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import queue
import secrets
import socket
import sys

from flask import Flask, Response, jsonify, request, send_from_directory

from . import agents
from .players import PoolError, check_pool_depth, load_pool
from .room import FAST, Room, Timing
from .rules import POSITIONS, Formation

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"

app = Flask(__name__, static_folder=None)
ROOM: Room | None = None
# Pin this if you want a TV URL that survives a restart. Otherwise it is
# fresh every launch, which is enough to stop a phone from driving the host
# controls by accident.
HOST_TOKEN = os.environ.get("DRAFT_HOST_TOKEN") or secrets.token_urlsafe(8)


def room() -> Room:
    if ROOM is None:
        raise RuntimeError("Room not started")
    return ROOM


def lan_ip() -> str:
    """
    Best guess at the address phones should use. Opening a UDP socket to a
    public address reveals which local interface would carry the traffic
    without sending a single packet, so this still works with no internet.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def is_host(req) -> bool:
    return req.headers.get("X-Host-Token") == HOST_TOKEN


# --- pages ----------------------------------------------------------------


@app.get("/")
def host_page():
    return send_from_directory(STATIC, "host.html")


@app.get("/play")
def play_page():
    return send_from_directory(STATIC, "play.html")


@app.get("/static/<path:name>")
def static_file(name: str):
    return send_from_directory(STATIC, name)


# --- state ----------------------------------------------------------------


@app.get("/api/state")
def api_state():
    return jsonify(room().snapshot())


@app.get("/api/bootstrap")
def api_bootstrap():
    """What a freshly loaded view needs that is not in the live snapshot."""
    return jsonify(
        {
            "join_url": f"http://{lan_ip()}:{app.config['PORT']}/play",
            "model": agents.MODEL,
            "model_ok": app.config.get("MODEL_OK", False),
            "model_note": app.config.get("MODEL_NOTE", ""),
        }
    )


@app.get("/events")
def events():
    def stream():
        q = room().subscribe()
        try:
            while True:
                try:
                    snap = q.get(timeout=15)
                    yield f"data: {json.dumps(snap)}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            pass
        finally:
            room().unsubscribe(q)

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --- actions --------------------------------------------------------------


@app.post("/api/join")
def api_join():
    data = request.get_json(silent=True) or {}
    try:
        m = room().add_manager(name=data.get("name", ""), is_ai=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"mid": m.mid, "name": m.name})


@app.post("/api/agent")
def api_agent():
    """Add a proxy manager for someone who cannot be there."""
    if not is_host(request):
        return jsonify({"error": "Only the host screen can add proxy managers"}), 403
    data = request.get_json(silent=True) or {}
    strategy = (data.get("strategy") or "").strip()
    if len(strategy) < 10:
        return jsonify({"error": "Write a sentence or two of strategy first"}), 400
    try:
        m = room().add_manager(
            name=data.get("name", ""), is_ai=True, strategy=strategy
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"mid": m.mid, "name": m.name})


@app.post("/api/leave")
def api_leave():
    data = request.get_json(silent=True) or {}
    try:
        room().remove_manager(data.get("mid", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.post("/api/start")
def api_start():
    if not is_host(request):
        return jsonify({"error": "Only the host screen can start the auction"}), 403
    try:
        room().start()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


@app.post("/api/bid")
def api_bid():
    data = request.get_json(silent=True) or {}
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "Bid needs a number"}), 400

    ok, why = room().place_bid(data.get("mid", ""), amount)
    if not ok:
        return jsonify({"error": why}), 400
    return jsonify({"ok": True})


@app.post("/api/hammer")
def api_hammer():
    """Host override: end the current lot now instead of waiting out the clock."""
    if not is_host(request):
        return jsonify({"error": "Only the host screen can do that"}), 403
    r = room()
    with r._lock:
        if r.phase != "bidding":
            return jsonify({"error": "No lot is live"}), 400
        r.deadline = 0.0
    return jsonify({"ok": True})


# --- entry point ----------------------------------------------------------


def parse_formation(spec: str) -> Formation:
    """'1-3-2-2' in GK-DEF-MID-FWD order."""
    parts = spec.split("-")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("Formation looks like 1-3-2-2")
    try:
        counts = [int(p) for p in parts]
    except ValueError:
        raise argparse.ArgumentTypeError("Formation must be four numbers, e.g. 1-3-2-2")
    if counts[0] < 1 or sum(counts) < 2:
        raise argparse.ArgumentTypeError("Need at least a keeper and one outfielder")
    return Formation(dict(zip(POSITIONS, counts)))


def main(argv: list[str] | None = None) -> int:
    default_pool = STATIC.parent / "data" / "players_sample.csv"
    ap = argparse.ArgumentParser(
        prog="draft-night",
        description="Run an auction draft for a living room full of friends.",
    )
    ap.add_argument("--pool", default=str(default_pool), help="player CSV")
    ap.add_argument("--budget", type=int, default=200, help="points per manager")
    ap.add_argument("--formation", type=parse_formation, default="1-3-2-2",
                    help="squad shape as GK-DEF-MID-FWD, default 1-3-2-2")
    ap.add_argument("--min-bid", type=int, default=1)
    ap.add_argument("--increment", type=int, default=1)
    ap.add_argument("--port", type=int, default=7000)
    ap.add_argument("--managers", type=int, default=6,
                    help="expected manager count, for the pool depth warning")
    ap.add_argument("--max-players", type=int, default=0,
                    help="auction only the best N players, 0 for the whole pool")
    ap.add_argument("--lot-seconds", type=float, default=14.0,
                    help="clock on a fresh lot, default 14")
    ap.add_argument("--snipe-seconds", type=float, default=7.0,
                    help="seconds the clock resets to after a bid, default 7")
    ap.add_argument("--fast", action="store_true",
                    help="very short timings, for trying it out alone")
    ap.add_argument("--no-llm", action="store_true",
                    help="run proxy bidders on the rating heuristic instead")
    ap.add_argument("--no-commentary", action="store_true")
    args = ap.parse_args(argv)

    if isinstance(args.formation, str):
        args.formation = parse_formation(args.formation)

    try:
        pool = load_pool(args.pool)
    except PoolError as exc:
        print(f"\n  {exc}\n")
        return 1

    if args.max_players:
        pool = pool[: args.max_players]

    for warning in check_pool_depth(pool, args.formation, args.managers):
        print(f"  heads up: {warning}")

    use_llm = not args.no_llm
    ok, note = agents.model_available() if use_llm else (False, "heuristic mode")
    app.config["MODEL_OK"] = ok
    app.config["MODEL_NOTE"] = note
    app.config["PORT"] = args.port

    timing = FAST if args.fast else Timing(
        lot=args.lot_seconds,
        snipe=args.snipe_seconds,
        react=min(8.0, args.lot_seconds * 0.6),
        sold=5.0,
    )

    global ROOM
    ROOM = Room(
        pool=pool,
        formation=args.formation,
        budget=args.budget,
        min_bid=args.min_bid,
        increment=args.increment,
        use_llm=use_llm and ok,
        commentary=not args.no_commentary,
        timing=timing,
    )

    ip = lan_ip()
    shape = "-".join(str(args.formation.slots[p]) for p in POSITIONS)
    print(f"""
  Draft Night

  {len(pool)} players in the pool, {args.budget} points each, squad of
  {args.formation.size} ({shape} in GK-DEF-MID-FWD order).

  Proxy bidders: {'Gemma via Ollama (' + note + ')' if ok else 'rating heuristic (' + note + ')'}

  Put this on the TV      http://{ip}:{args.port}/?host={HOST_TOKEN}
  Friends open this       http://{ip}:{args.port}/play

  Same wifi, no internet needed. Ctrl-C to stop.
""")

    app.run(host="0.0.0.0", port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
