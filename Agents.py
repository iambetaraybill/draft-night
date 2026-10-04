"""
Proxy bidders for managers who cannot make draft night.

The friend writes a strategy in plain English. A local open-weight model
reads it and decides, once per lot, what that player is worth to them and
why. Everything after that decision is deterministic: the bidding tactics,
the increments, the budget ceiling.

Why once per lot rather than once per bid. On a CPU-only laptop a small
model needs roughly one to three seconds for a short JSON answer. A bidding
war has a dozen exchanges inside fifteen seconds, so calling the model on
every exchange would either stall the auction or force a model so small it
stops reading the strategy properly. Asking "what is he worth to you?" once,
then letting plain code bid up to that figure, puts the model where judgement
is actually needed and keeps it off the critical path.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field

import requests

from .rules import Manager, Player, Rules

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get("DRAFT_MODEL", "gemma3:4b")
LLM_ENABLED = os.environ.get("DRAFT_LLM", "on").lower() not in ("off", "0", "false")
TIMEOUT = float(os.environ.get("DRAFT_LLM_TIMEOUT", "25"))

# Structured output schema. Ollama constrains generation to this, which is
# what makes a 4B model on a laptop reliable enough to put in the loop.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "want": {"type": "boolean"},
        "max_bid": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["want", "max_bid", "reason"],
}

SYSTEM_PROMPT = """You are bidding at a football player auction on behalf of \
an absent manager. You follow their written strategy closely, even when you \
would choose differently.

Answer with JSON only:
  want: true if this player is worth bidding on at all
  max_bid: the most you would pay, as a whole number, never above the stated limit
  reason: one short sentence, under 15 words, addressed to the room

Judge value against the manager's strategy and their unfilled positions, not \
rating alone. A squad of eight needs a goalkeeper more than a fourth striker. \
Leave yourself money for the slots you still have to fill."""


@dataclass
class Plan:
    """One agent's intent for one lot."""

    want: bool
    max_bid: int
    reason: str
    source: str = "llm"  # llm | heuristic | skipped
    latency_ms: int = 0


@dataclass
class AgentStats:
    """Counters worth reporting honestly in a write-up."""

    llm_calls: int = 0
    llm_failures: int = 0
    heuristic_calls: int = 0
    total_latency_ms: int = 0
    clamped: int = 0  # times the model named a figure above the hard cap
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, plan: Plan, clamped: bool = False) -> None:
        with self._lock:
            if plan.source == "llm":
                self.llm_calls += 1
                self.total_latency_ms += plan.latency_ms
            elif plan.source == "heuristic":
                self.heuristic_calls += 1
            if clamped:
                self.clamped += 1

    def fail(self) -> None:
        with self._lock:
            self.llm_failures += 1

    def snapshot(self) -> dict:
        with self._lock:
            avg = self.total_latency_ms // self.llm_calls if self.llm_calls else 0
            return {
                "llm_calls": self.llm_calls,
                "llm_failures": self.llm_failures,
                "heuristic_calls": self.heuristic_calls,
                "avg_latency_ms": avg,
                "clamped": self.clamped,
            }


STATS = AgentStats()


# --- the open-weight model path ------------------------------------------


def model_available() -> tuple[bool, str]:
    """Check Ollama is up and the configured model is pulled."""
    if not LLM_ENABLED:
        return False, "disabled by DRAFT_LLM=off"
    try:
        resp = requests.get(f"{OLLAMA_URL}/api/tags", timeout=4)
        resp.raise_for_status()
        tags = [m.get("name", "") for m in resp.json().get("models", [])]
    except Exception as exc:
        return False, f"cannot reach Ollama at {OLLAMA_URL} ({type(exc).__name__})"

    if any(t == MODEL or t.startswith(MODEL.split(":")[0] + ":") for t in tags):
        return True, MODEL
    available = ", ".join(tags[:6]) or "none"
    return False, f"model {MODEL!r} not pulled. Available: {available}"


def _build_prompt(
    manager: Manager, player: Player, rules: Rules, cap: int, pool_note: str
) -> str:
    needs = ", ".join(f"{n} {pos}" for pos, n in rules.needs(manager).items() if n)
    roster = (
        "; ".join(f"{s.player.name} ({s.player.pos}, paid {s.price})" for s in manager.roster)
        or "nobody yet"
    )
    return f"""Manager: {manager.name}

Their strategy, in their own words:
"{manager.strategy or 'No strategy given. Build a balanced squad.'}"

Up for auction now:
  {player.name} - {player.pos}, rated {player.rating}, {player.club or 'unattached'} ({player.nation or 'unknown'})

{manager.name}'s position:
  Budget left: {rules.budget_left(manager)} of {manager.budget}
  Squad so far: {roster}
  Still to fill: {needs or 'nothing'}
  Hard limit on this lot: {cap}
{pool_note}
Is this player worth it, and what is your maximum?"""


def _call_ollama(prompt: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "format": PLAN_SCHEMA,
        "options": {"temperature": 0.4, "num_predict": 160},
    }
    resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)

    if resp.status_code == 400:
        # Older Ollama builds reject a schema object. Fall back to plain JSON
        # mode and lean on the system prompt for shape.
        payload["format"] = "json"
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=TIMEOUT)

    resp.raise_for_status()
    content = resp.json().get("message", {}).get("content", "")
    return json.loads(content)


# --- the no-model path ----------------------------------------------------


def heuristic_plan(manager: Manager, player: Player, rules: Rules, cap: int) -> Plan:
    """
    Fallback when Ollama is not running, and the baseline to measure the
    model against. It can read a rating. It cannot read a sentence like
    "don't let Rohit get a keeper", which is the whole point of the model.
    """
    strength = max(0.0, min(1.0, (player.rating - 60) / 31))
    slots = max(1, rules.slots_left(manager))
    per_slot = rules.budget_left(manager) / slots
    willing = int(per_slot * (0.55 + 1.7 * strength))
    willing = max(rules.min_bid, min(cap, willing))

    needs = rules.needs(manager)
    scarce = needs.get(player.pos, 0) >= max(needs.values() or [0])
    want = strength > 0.15 or scarce

    return Plan(
        want=want,
        max_bid=willing if want else 0,
        reason=f"Rated {player.rating}, filling a {player.pos} slot.",
        source="heuristic",
    )


# --- public entry point ---------------------------------------------------


def plan_for_lot(
    manager: Manager,
    player: Player,
    rules: Rules,
    use_llm: bool = True,
    pool_note: str = "",
) -> Plan:
    """
    Decide what this lot is worth to one absent manager.

    The returned max_bid is always clamped to what the rules allow, whatever
    the model says. A model that hallucinates a bid of 9999 produces a legal
    bid of exactly the manager's cap and a logged clamp, not a broken squad.
    """
    cap = rules.max_bid(manager)
    if cap < rules.min_bid or not rules.needs_position(manager, player.pos):
        return Plan(want=False, max_bid=0, reason="No slot for him.", source="skipped")

    if not use_llm:
        plan = heuristic_plan(manager, player, rules, cap)
        STATS.record(plan)
        return plan

    started = time.monotonic()
    try:
        raw = _call_ollama(_build_prompt(manager, player, rules, cap, pool_note))
        latency = int((time.monotonic() - started) * 1000)

        asked = int(raw.get("max_bid") or 0)
        clamped = asked > cap
        plan = Plan(
            want=bool(raw.get("want")) and asked >= rules.min_bid,
            max_bid=max(0, min(asked, cap)),
            reason=str(raw.get("reason") or "").strip()[:120] or "No comment.",
            source="llm",
            latency_ms=latency,
        )
        STATS.record(plan, clamped=clamped)
        return plan

    except Exception as exc:
        STATS.fail()
        print(f"[agent] {manager.name}: model call failed ({exc}), using heuristic")
        plan = heuristic_plan(manager, player, rules, cap)
        STATS.record(plan)
        return plan


def plan_all(
    managers: list[Manager],
    player: Player,
    rules: Rules,
    use_llm: bool = True,
    pool_note: str = "",
) -> dict[str, Plan]:
    """
    Plan for every AI manager at once. Threads rather than sequential calls:
    Ollama will queue them internally, but overlapping the HTTP round trips
    still beats paying each one end to end.
    """
    ai = [m for m in managers if m.is_ai]
    if not ai:
        return {}

    out: dict[str, Plan] = {}
    lock = threading.Lock()

    def work(m: Manager) -> None:
        plan = plan_for_lot(m, player, rules, use_llm=use_llm, pool_note=pool_note)
        with lock:
            out[m.mid] = plan

    threads = [threading.Thread(target=work, args=(m,), daemon=True) for m in ai]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=TIMEOUT + 5)
    return out


# --- tactics (deterministic) ---------------------------------------------


DELAY_MIN = float(os.environ.get("DRAFT_AGENT_DELAY_MIN", "0.9"))
DELAY_MAX = float(os.environ.get("DRAFT_AGENT_DELAY_MAX", "2.6"))


def bid_delay() -> float:
    """
    A human-feeling pause before an agent answers a bid. Without it the
    agents snipe instantly and the room never gets a chance to react, which
    is less fun and makes the auction feel rigged.
    """
    return random.uniform(DELAY_MIN, DELAY_MAX)


def tactical_bid(plan: Plan, current_bid: int | None, rules: Rules) -> int | None:
    """
    How much to actually bid, given a plan and the state of the lot. Mostly
    the minimum raise, with the occasional jump when the agent values the
    player well above the current price, the way a confident bidder does.
    """
    if not plan.want:
        return None
    floor = rules.next_bid(current_bid)
    if floor > plan.max_bid:
        return None

    headroom = plan.max_bid - floor
    if headroom > rules.increment * 6 and random.random() < 0.3:
        jump = floor + rules.increment * random.randint(2, 4)
        return min(jump, plan.max_bid)
    return floor
