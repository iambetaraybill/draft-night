"""
The auction room: one live draft, held together by a server-side clock.

All state lives here and only here. Phones and the TV are dumb views that
render whatever snapshot they were last sent, which means a phone can drop
off the wifi, reconnect, and be correct immediately.
"""

from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass, field

from . import agents
from .rules import DEFAULT_FORMATION, Formation, Manager, Player, Rules

TICK = 0.2


@dataclass(frozen=True)
class Timing:
    """
    How long things take. Tuned for a living room: long enough that someone
    can look up from their phone and join a bidding war, short enough that
    forty-odd lots do not eat the whole evening.
    """

    lot: float = 14.0  # clock on a fresh lot
    snipe: float = 7.0  # floor the clock resets to after any bid
    react: float = 8.0  # time guaranteed to remain once agent plans land
    sold: float = 5.0  # pause on the result before the next lot


FAST = Timing(lot=2.0, snipe=1.0, react=1.2, sold=0.3)  # trying it out alone, and tests


@dataclass
class BidEvent:
    manager: str
    amount: int
    is_ai: bool
    at: float = field(default_factory=time.time)


@dataclass
class SoldLot:
    player: Player
    price: int | None
    winner: str | None
    reason: str = ""


class Room:
    def __init__(
        self,
        pool: list[Player],
        formation: Formation = DEFAULT_FORMATION,
        budget: int = 200,
        min_bid: int = 1,
        increment: int = 1,
        use_llm: bool = True,
        commentary: bool = True,
        timing: Timing = Timing(),
    ) -> None:
        self.rules = Rules(formation=formation, min_bid=min_bid, increment=increment)
        self.timing = timing
        self.pool = list(pool)
        self.default_budget = budget
        self.use_llm = use_llm
        self.commentary_enabled = commentary

        self.managers: dict[str, Manager] = {}
        self.order: list[str] = []

        self.phase = "lobby"  # lobby | bidding | sold | review
        self.lot: Player | None = None
        self.lot_number = 0
        self.high_bid: int | None = None
        self.high_bidder: str | None = None
        self.deadline: float = 0.0
        self.bid_log: list[BidEvent] = []
        self.plans: dict[str, agents.Plan] = {}
        self.plans_ready = False
        self.agent_next_action: dict[str, float] = {}
        self.sold_until: float = 0.0
        self.last_result: SoldLot | None = None
        self.sold_history: list[SoldLot] = []
        self.unsold: list[Player] = []
        self.commentary = ""
        self.notice = ""

        self._lock = threading.RLock()
        self._last_push = 0.0
        self._subs: list[queue.Queue] = []
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._run, daemon=True)
        self._ticker.start()

    # --- subscriptions ---------------------------------------------------

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=50)
        with self._lock:
            self._subs.append(q)
        q.put(self.snapshot())
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _broadcast(self) -> None:
        snap = self.snapshot()
        with self._lock:
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    dead.append(q)  # a view that stopped reading; drop it
            for q in dead:
                self._subs.remove(q)

    # --- lobby -----------------------------------------------------------

    def add_manager(
        self,
        name: str,
        is_ai: bool = False,
        strategy: str = "",
        budget: int | None = None,
    ) -> Manager:
        with self._lock:
            if self.phase != "lobby":
                raise ValueError("The auction has already started")
            name = name.strip()[:24]
            if not name:
                raise ValueError("Needs a name")
            if any(m.name.lower() == name.lower() for m in self.managers.values()):
                raise ValueError(f"{name} is already in the room")

            mid = f"m{len(self.managers) + 1}"
            m = Manager(
                mid=mid,
                name=name,
                budget=budget or self.default_budget,
                is_ai=is_ai,
                strategy=strategy.strip()[:600],
            )
            self.managers[mid] = m
            self.order.append(mid)
        self._broadcast()
        return m

    def remove_manager(self, mid: str) -> None:
        with self._lock:
            if self.phase != "lobby":
                raise ValueError("Too late to leave")
            self.managers.pop(mid, None)
            if mid in self.order:
                self.order.remove(mid)
        self._broadcast()

    def start(self) -> None:
        with self._lock:
            if self.phase != "lobby":
                raise ValueError("Already started")
            if len(self.managers) < 2:
                raise ValueError("Need at least two managers")
            self.phase = "bidding"
        self._open_next_lot()

    # --- lots ------------------------------------------------------------

    def _next_player(self) -> Player | None:
        """
        Best remaining player somebody can actually buy. Going in rating
        order means the marquee names go early, while everyone still has
        money and the room still has energy.
        """
        managers = list(self.managers.values())
        for p in self.pool:
            if self.rules.eligible_buyers(managers, p):
                return p
        return None

    def _open_next_lot(self) -> None:
        with self._lock:
            player = self._next_player()
            if player is None:
                self.phase = "review"
                self.lot = None
                self.notice = "Every squad is full."
                self._broadcast()
                return

            self.pool.remove(player)
            self.lot = player
            self.lot_number += 1
            self.high_bid = None
            self.high_bidder = None
            self.bid_log = []
            self.plans = {}
            self.plans_ready = False
            self.agent_next_action = {}
            self.commentary = ""
            self.notice = ""
            self.phase = "bidding"
            self.deadline = time.monotonic() + self.timing.lot
            lot_at_request = self.lot_number

        self._broadcast()
        threading.Thread(
            target=self._load_plans, args=(player, lot_at_request), daemon=True
        ).start()

    def _load_plans(self, player: Player, lot_number: int) -> None:
        """Ask every proxy agent what this player is worth. Runs off-clock."""
        managers = [m for m in self.managers.values() if m.is_ai]
        if not managers:
            with self._lock:
                self.plans_ready = True
            return

        note = ""
        remaining = sum(1 for p in self.pool if p.pos == player.pos)
        if remaining <= 2:
            note = f"\nOnly {remaining} more {player.pos} left in the pool after this one.\n"

        plans = agents.plan_all(
            managers, player, self.rules, use_llm=self.use_llm, pool_note=note
        )

        with self._lock:
            if self.lot_number != lot_number:
                return  # lot moved on while the model was thinking
            self.plans = plans
            self.plans_ready = True
            now = time.monotonic()
            for mid, plan in plans.items():
                if plan.want:
                    self.agent_next_action[mid] = now + agents.bid_delay()
            # Slow model, short clock: make sure the agents still get a say.
            if any(p.want for p in plans.values()):
                self.deadline = max(self.deadline, now + self.timing.react)
        self._broadcast()

    # --- bidding ---------------------------------------------------------

    def place_bid(self, mid: str, amount: int) -> tuple[bool, str]:
        with self._lock:
            if self.phase != "bidding" or self.lot is None:
                return False, "Nothing up for auction right now"
            m = self.managers.get(mid)
            if m is None:
                return False, "You are not in this auction"

            ok, why = self.rules.check_bid(
                m, self.lot, amount, self.high_bid, self.high_bidder
            )
            if not ok:
                return False, why

            self.high_bid = amount
            self.high_bidder = mid
            self.bid_log.append(BidEvent(manager=m.name, amount=amount, is_ai=m.is_ai))
            self.deadline = max(self.deadline, time.monotonic() + self.timing.snipe)

            # Everyone who still wants him gets another turn to respond.
            now = time.monotonic()
            for other, plan in self.plans.items():
                if other != mid and plan.want:
                    self.agent_next_action[other] = now + agents.bid_delay()

        self._broadcast()
        return True, ""

    def _agents_act(self) -> tuple[str | None, int | None]:
        """Find one agent bid that is due, if any. Returns (manager, amount)."""
        with self._lock:
            if self.phase != "bidding" or self.lot is None or not self.plans_ready:
                return None, None
            now = time.monotonic()
            due = [
                mid
                for mid, at in self.agent_next_action.items()
                if at <= now and mid != self.high_bidder
            ]
            if not due:
                return None, None

            mid = random.choice(due)
            self.agent_next_action.pop(mid, None)
            plan = self.plans.get(mid)
            amount = (
                agents.tactical_bid(plan, self.high_bid, self.rules) if plan else None
            )
        if amount is None:
            return None, None
        return mid, amount

    def _resolve_lot(self) -> None:
        with self._lock:
            if self.lot is None:
                return
            player = self.lot

            if self.high_bidder and self.high_bid:
                winner = self.managers[self.high_bidder]
                try:
                    self.rules.award(winner, player, self.high_bid)
                except ValueError as exc:
                    # Should be unreachable: every bid was checked on arrival.
                    # If it ever fires, lose the lot rather than the squad.
                    print(f"[room] refused illegal sale: {exc}")
                    self.unsold.append(player)
                    result = SoldLot(player, None, None, reason=str(exc))
                else:
                    plan = self.plans.get(self.high_bidder)
                    result = SoldLot(
                        player,
                        self.high_bid,
                        winner.name,
                        reason=plan.reason if plan and winner.is_ai else "",
                    )
            else:
                self.unsold.append(player)
                result = SoldLot(player, None, None)

            self.last_result = result
            self.sold_history.append(result)
            self.phase = "sold"
            self.sold_until = time.monotonic() + self.timing.sold
            snapshot_lot = self.lot_number

        self._broadcast()
        if self.commentary_enabled and self.use_llm:
            threading.Thread(
                target=self._write_commentary, args=(result, snapshot_lot), daemon=True
            ).start()

    def _write_commentary(self, result: SoldLot, lot_number: int) -> None:
        """One line of auctioneer patter. Purely decorative, never blocking."""
        if result.price is None or result.winner is None:
            return
        try:
            import requests

            winner = next(
                (m for m in self.managers.values() if m.name == result.winner), None
            )
            share = (
                round(100 * result.price / winner.budget) if winner and winner.budget else 0
            )
            prompt = (
                f"{result.winner} just bought {result.player.name} "
                f"({result.player.pos}, rated {result.player.rating}) for "
                f"{result.price}, which is {share}% of their whole budget. "
                f"Give one sentence of dry auctioneer commentary, under 20 words. "
                f"No preamble, no quotation marks."
            )
            resp = requests.post(
                f"{agents.OLLAMA_URL}/api/chat",
                json={
                    "model": agents.MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.8, "num_predict": 60},
                },
                timeout=agents.TIMEOUT,
            )
            resp.raise_for_status()
            line = resp.json().get("message", {}).get("content", "").strip()
            line = line.strip('"').split("\n")[0][:160]
        except Exception:
            return

        with self._lock:
            if self.lot_number != lot_number or self.phase != "sold":
                return
            self.commentary = line
        self._broadcast()

    # --- clock -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # a crashed clock kills the whole night
                print(f"[room] tick error: {exc}")
            self._stop.wait(TICK)

    def _tick(self) -> None:
        mid, amount = self._agents_act()
        if mid and amount:
            self.place_bid(mid, amount)
            return

        with self._lock:
            phase, deadline, sold_until = self.phase, self.deadline, self.sold_until
            waiting = not self.plans_ready and any(m.is_ai for m in self.managers.values())
            now = time.monotonic()

            if phase == "bidding" and now >= deadline and waiting:
                # Hold the hammer while a slow model is still deciding.
                self.deadline = now + 1.5
                self.notice = "Proxy managers deciding..."
                return

        if phase == "bidding" and now >= deadline:
            self._resolve_lot()
            return
        if phase == "sold" and now >= sold_until:
            self._open_next_lot()
            return

        # Keep the clock on every phone honest while a lot is live. The
        # anti-snipe rule moves the deadline, so views cannot just count down
        # from whatever they were last told.
        if phase == "bidding" and now - self._last_push >= 1.0:
            self._last_push = now
            self._broadcast()

    def stop(self) -> None:
        self._stop.set()

    # --- views -----------------------------------------------------------

    def snapshot(self) -> dict:
        """One complete picture of the room. Sent to every view on change."""
        with self._lock:
            now = time.monotonic()
            revealed = self.phase in ("sold", "review")

            managers = []
            for mid in self.order:
                m = self.managers.get(mid)
                if not m:
                    continue
                plan = self.plans.get(mid)
                managers.append(
                    {
                        "mid": mid,
                        "name": m.name,
                        "is_ai": m.is_ai,
                        "budget": m.budget,
                        "left": self.rules.budget_left(m),
                        "max_bid": self.rules.max_bid(m),
                        "needs": {k: v for k, v in self.rules.needs(m).items() if v},
                        "slots_left": self.rules.slots_left(m),
                        "roster": [
                            {
                                "name": s.player.name,
                                "pos": s.player.pos,
                                "rating": s.player.rating,
                                "price": s.price,
                            }
                            for s in m.roster
                        ],
                        "leading": mid == self.high_bidder,
                        # An agent's interest shows live; its ceiling does not,
                        # or the humans would just bid one more than the number.
                        "interested": bool(plan and plan.want) if m.is_ai else None,
                        "plan": (
                            {
                                "max_bid": plan.max_bid,
                                "reason": plan.reason,
                                "source": plan.source,
                                "latency_ms": plan.latency_ms,
                            }
                            if plan and revealed and m.is_ai
                            else None
                        ),
                    }
                )

            lot = None
            if self.lot:
                lot = {
                    "pid": self.lot.pid,
                    "name": self.lot.name,
                    "pos": self.lot.pos,
                    "rating": self.lot.rating,
                    "club": self.lot.club,
                    "nation": self.lot.nation,
                }

            return {
                "phase": self.phase,
                "lot": lot,
                "lot_number": self.lot_number,
                "pool_left": len(self.pool),
                "high_bid": self.high_bid,
                "high_bidder": self.high_bidder,
                "high_bidder_name": (
                    self.managers[self.high_bidder].name if self.high_bidder else None
                ),
                "next_bid": self.rules.next_bid(self.high_bid),
                "seconds_left": max(0.0, round(self.deadline - now, 1))
                if self.phase == "bidding"
                else 0.0,
                "lot_seconds": self.timing.lot,
                "snipe_seconds": self.timing.snipe,
                "bids": [
                    {"manager": b.manager, "amount": b.amount, "is_ai": b.is_ai}
                    for b in reversed(self.bid_log[-8:])
                ],
                "managers": managers,
                "result": (
                    {
                        "player": self.last_result.player.name,
                        "pos": self.last_result.player.pos,
                        "rating": self.last_result.player.rating,
                        "price": self.last_result.price,
                        "winner": self.last_result.winner,
                        "reason": self.last_result.reason,
                    }
                    if self.last_result
                    else None
                ),
                "commentary": self.commentary,
                "notice": self.notice,
                "formation": self.rules.formation.slots,
                "min_bid": self.rules.min_bid,
                "increment": self.rules.increment,
                "unsold": len(self.unsold),
                "stats": agents.STATS.snapshot(),
                "llm": self.use_llm,
            }
