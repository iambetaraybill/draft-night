# Draft Night

An auction draft for a living room full of friends. Everyone bids from their
phone, the TV shows the lot on the block, and whoever couldn't make it is
represented by a proxy manager: a local open-weight model that bids on their
behalf, inside their budget, from instructions they wrote in plain English.

Runs on one laptop over your own wifi. No accounts, no cloud, no internet, no
API keys, nothing to pay per bid.

```
  GK
  Niko Jovic                          Managers
  rated 91 · FC Harbourside           Rohit              148
                                        1 GK · 2 DEF
      42  with Rohit                  Sam  proxy · in     92
                                        2 DEF · 1 FWD
  ████████████░░░░░░░░                Dev                 61
  6.4s left. Any bid puts 7              1 MID · 2 FWD
  seconds back on the clock.
```

## Why this exists

Six of us run a FIFA tournament on a PS5. Picking squads used to be a
spreadsheet and an argument. An auction is better: you get a budget, you bid
for the players you want, and you live with the squad you built.

The problem is attendance. Draft night only works if everyone is there, and
somebody always has a shift. The usual fix is for a friend to "bid for" the
absent one, which means either bidding badly on their behalf or quietly
building themselves a better squad.

A proxy manager fixes it properly. The absent friend writes down what they
want. A model reads it and bids for them, live, against everyone in the room,
with the same budget and the same rules.

## What makes it work

Open weights, running locally, for three reasons that aren't about cost.

**It has to work in a flat with bad wifi.** The whole thing runs on one
laptop. Phones reach it over the local network. If the router has no uplink,
the auction still happens.

**A cloud round trip would break the bidding.** Lots are decided in seconds.
Proxy valuations have to be ready before the hammer, and that means inference
on the same machine as the auction clock.

**Your friends' instructions are theirs.** "Don't let Rohit get a keeper" is
a daft thing to send to someone else's server, and there's no reason to.

Swapping models is a one-line change, which matters because the right model
here is a small one.

## The split: model decides, code enforces

The model never touches money. It answers one question per lot — what is this
player worth to this manager, and why — and plain Python does everything else:
budgets, bid legality, squad limits, the clock, the increments.

A model that replies `max_bid: 99999` produces a legal bid of exactly that
manager's cap and a logged clamp. There is a test for this.

**One model call per lot, not per bid.** On a CPU-only laptop a small model
needs one to three seconds for a short JSON answer, and a bidding war has a
dozen exchanges inside fifteen seconds. Calling the model on every exchange
would either stall the auction or force a model too small to read a strategy
properly. So the model sets a ceiling once, and deterministic tactics bid up
to it with a human-feeling pause between raises.

The reserve rule is the other half. Before any bid is allowed, the server
checks you can still fill every remaining slot at the minimum price. You
cannot spend 95% of your budget on a galactico and field empty shirts.

## Setup

Needs Python 3.10 or newer.

```bash
pip install -r requirements.txt
python -m draftnight.server
```

That runs with the synthetic sample pool and no model: proxy managers fall
back to a rating heuristic, and everything else works. Open the two URLs it
prints, one on the TV and one on a phone.

### Adding the model

Install [Ollama](https://ollama.com), then pull a small instruct model:

```bash
ollama pull gemma3:4b
python -m draftnight.server
```

It tells you on startup which mode it's in. On 8–16 GB with no GPU,
`gemma3:4b` is a reasonable default and `gemma3:1b` is the fallback if
valuations feel slow. Point at anything else with `DRAFT_MODEL`:

```bash
DRAFT_MODEL=qwen2.5:3b python -m draftnight.server
```

Benchmark your own machine rather than trusting a number off a blog. The
review screen reports real average valuation latency at the end of a draft.

### Running a draft

1. Start the server on the laptop that's plugged into the TV.
2. Open the host URL on the TV. It has a token in it; that's what stops a
   phone from skipping lots.
3. Everyone opens the `/play` URL and types their name.
4. For anyone absent, paste their instructions into the host screen and add
   them as a proxy manager.
5. Start the auction.

Players come up best-first. Each lot runs a 14-second clock, and any bid puts
7 seconds back on it, so nobody wins by sniping at the buzzer.

### Options

```bash
python -m draftnight.server --help
```

| Flag | Default | What it does |
| --- | --- | --- |
| `--pool` | sample pool | player CSV |
| `--budget` | 200 | points per manager |
| `--formation` | `1-3-2-2` | squad shape, GK-DEF-MID-FWD |
| `--max-players` | 0 | auction only the best N |
| `--min-bid` / `--increment` | 1 / 1 | opening price and raise size |
| `--lot-seconds` | 14 | clock on a fresh lot |
| `--fast` | off | very short timings, for trying it out alone |
| `--no-llm` | off | heuristic proxy bidders, no model |
| `--port` | 7000 | |

A squad of 8 across 6 managers is about 48 lots, which is a comfortable
evening. Bigger squads make for a longer night.

## Your own players

The sample pool is 90 invented footballers. It is synthetic on purpose: EA's
ratings are EA's, and the scraped copies on dataset sites don't come with the
right to redistribute them, so none of that belongs in a public repo.

Use your own export instead. It stays on your machine, and `.gitignore` keeps
it out of commits.

```bash
python scripts/load_players.py ~/Downloads/players.csv --managers 6
python -m draftnight.server --pool data/my_pool.csv
```

Column names are matched loosely, so `name`/`short_name`/`player`,
`pos`/`position`/`player_positions` and `rating`/`overall` all work. Detailed
positions fold down to GK, DEF, MID and FWD. A good pool has a few stars and
a long tail of cheap filler, so if yours comes out too uniform, lower
`--min-rating` to let the bargains back in.

This project is not affiliated with EA or any rating provider.

## Tests

```bash
python -m unittest discover -s tests -t . -p "test_*.py"
```

36 tests, mostly about money. The one that matters most spends the maximum
allowed on every single lot and checks the squad still completes legally.

There's also an end-to-end run that drives a whole auction over HTTP:

```bash
DRAFT_HOST_TOKEN=test DRAFT_AGENT_DELAY_MIN=0.2 DRAFT_AGENT_DELAY_MAX=0.6 \
  python -m draftnight.server --no-llm --port 7111 --fast --max-players 42 &
python tests/smoke.py http://127.0.0.1:7111
```

It checks every squad ends up full and formation-legal, nobody goes negative,
and the proxy manager actually buys players.

## How it's put together

```
draftnight/
  rules.py     budgets and squad legality. no I/O, no model, no randomness
  agents.py    proxy bidders: model valuations, clamping, heuristic fallback
  room.py      state machine, clock, agent scheduling, event broadcast
  players.py   CSV loading with loose column matching
  server.py    Flask routes and the server-sent event stream
static/        host.html for the TV, play.html for phones, one stylesheet
```

Flask and server-sent events rather than websockets, because two dependencies
and no build step means a phone needs nothing but a browser. System fonts
only, since webfonts would want a CDN.

All state lives on the server. Phones render whatever snapshot they last
received, so one can drop off the wifi, reconnect, and be correct
immediately.

## Known limits

- The host screen's token is the only access control. This is a party on a
  LAN, not a public service.
- Flask's development server is what serves it. Fine for eight devices in a
  living room; not for anything else.
- There's no persistence. Crash mid-draft and the draft is gone.
- Proxy managers can't respond to things that aren't in their written
  strategy or the squad state. They don't watch the room.

## Next

Voice bidding is the obvious one: shout "fifty-five for Jovic" and have local
Whisper plus the model turn it into a validated bid, with a deterministic
number parser as the fast path. That's the phase-two reason this is written in
Python.
