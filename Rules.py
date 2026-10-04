"""
Deterministic auction rules. No I/O, no model calls, no randomness.

This module is the single source of truth for money and squad legality.
The language model never touches anything in here: it decides what a player
is worth to an absent manager, and this code decides whether that bid is
allowed to happen. Keeping the split sharp is what stops a confused model
from bankrupting someone's squad.
"""

from __future__ import annotations

from dataclasses import dataclass, field

POSITIONS = ("GK", "DEF", "MID", "FWD")


@dataclass(frozen=True)
class Player:
    pid: str
    name: str
    pos: str
    rating: int
    club: str = ""
    nation: str = ""

    def __post_init__(self) -> None:
        if self.pos not in POSITIONS:
            raise ValueError(f"{self.name}: unknown position {self.pos!r}")


@dataclass
class Signing:
    player: Player
    price: int


@dataclass
class Manager:
    mid: str
    name: str
    budget: int
    is_ai: bool = False
    strategy: str = ""
    roster: list[Signing] = field(default_factory=list)

    @property
    def spent(self) -> int:
        return sum(s.price for s in self.roster)


@dataclass(frozen=True)
class Formation:
    """How many players each squad must end up with, by position."""

    slots: dict[str, int]

    @property
    def size(self) -> int:
        return sum(self.slots.values())

    def required(self, pos: str) -> int:
        return self.slots.get(pos, 0)


DEFAULT_FORMATION = Formation({"GK": 1, "DEF": 3, "MID": 2, "FWD": 2})


class Rules:
    """Budget and squad legality for one auction configuration."""

    def __init__(
        self,
        formation: Formation = DEFAULT_FORMATION,
        min_bid: int = 1,
        increment: int = 1,
    ) -> None:
        if min_bid < 1:
            raise ValueError("min_bid must be at least 1")
        if increment < 1:
            raise ValueError("increment must be at least 1")
        self.formation = formation
        self.min_bid = min_bid
        self.increment = increment

    # --- squad shape -----------------------------------------------------

    def filled(self, m: Manager, pos: str) -> int:
        return sum(1 for s in m.roster if s.player.pos == pos)

    def needs(self, m: Manager) -> dict[str, int]:
        """Remaining slots per position."""
        return {
            pos: max(0, self.formation.required(pos) - self.filled(m, pos))
            for pos in POSITIONS
        }

    def slots_left(self, m: Manager) -> int:
        return max(0, self.formation.size - len(m.roster))

    def squad_full(self, m: Manager) -> bool:
        return self.slots_left(m) == 0

    def needs_position(self, m: Manager, pos: str) -> bool:
        return self.needs(m).get(pos, 0) > 0

    # --- money -----------------------------------------------------------

    def budget_left(self, m: Manager) -> int:
        return m.budget - m.spent

    def max_bid(self, m: Manager) -> int:
        """
        Most a manager may commit to the current lot while still being able
        to fill every remaining slot at the minimum price. Reserving that
        money up front is what stops someone spending 95% of their budget on
        a galactico and then fielding eight empty shirts.
        """
        slots = self.slots_left(m)
        if slots <= 0:
            return 0
        reserve = (slots - 1) * self.min_bid
        return max(0, self.budget_left(m) - reserve)

    def next_bid(self, current_bid: int | None) -> int:
        """Smallest legal bid against the current high bid."""
        if current_bid is None:
            return self.min_bid
        return current_bid + self.increment

    # --- validation ------------------------------------------------------

    def check_bid(
        self,
        m: Manager,
        player: Player,
        amount: int,
        current_bid: int | None = None,
        current_bidder: str | None = None,
    ) -> tuple[bool, str]:
        """
        Returns (ok, reason). The reason is written to be shown straight to a
        person on a phone, so it says what to do rather than what failed.
        """
        if self.squad_full(m):
            return False, "Your squad is full"
        if current_bidder == m.mid:
            return False, "You already hold the top bid"
        if not self.needs_position(m, player.pos):
            need = ", ".join(p for p, n in self.needs(m).items() if n)
            return False, f"You don't need another {player.pos}. Still open: {need}"
        if amount < self.next_bid(current_bid):
            return False, f"Bid at least {self.next_bid(current_bid)}"

        cap = self.max_bid(m)
        if amount > cap:
            slots = self.slots_left(m)
            if slots > 1:
                return (
                    False,
                    f"{cap} is your limit. {slots - 1} more slots to fill "
                    f"at {self.min_bid} each.",
                )
            return False, f"{cap} is all you have left"
        return True, ""

    def can_bid_at_all(
        self,
        m: Manager,
        player: Player,
        current_bid: int | None = None,
        current_bidder: str | None = None,
    ) -> bool:
        """Is any legal bid available to this manager on this lot?"""
        ok, _ = self.check_bid(
            m, player, self.next_bid(current_bid), current_bid, current_bidder
        )
        return ok

    # --- settlement ------------------------------------------------------

    def award(self, m: Manager, player: Player, price: int) -> Signing:
        """Commit a won lot. Raises if the sale would be illegal."""
        if self.squad_full(m):
            raise ValueError(f"{m.name} has a full squad")
        if not self.needs_position(m, player.pos):
            raise ValueError(f"{m.name} does not need a {player.pos}")
        if price > self.max_bid(m):
            raise ValueError(f"{m.name} cannot afford {price}")
        signing = Signing(player=player, price=price)
        m.roster.append(signing)
        return signing

    def eligible_buyers(self, managers: list[Manager], player: Player) -> list[Manager]:
        """Managers who could open the bidding on this player."""
        return [m for m in managers if self.can_bid_at_all(m, player)]
