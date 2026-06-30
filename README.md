# GPS Strategy — Setup and Play Guide

A location-aware multiplayer game played through a Telegram bot. Two asymmetric teams: **System** (defenders, identify the other side) and **Opposition** (attackers, capture nodes and build a chain between two targets). Backend is a single Python process (FastAPI + asyncio + SQLite); clients are Telegram WebApp pages opened from the bot.

This document walks through cloning the repository, configuring it, and running both a self-contained demo (no live players needed) and a real game with people.

---

## 1. Prerequisites

- **Python 3.10+** with `pip` and `venv`
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- Your own **Telegram user ID** (send any message to [@userinfobot](https://t.me/userinfobot) and it replies with the ID)
- **cloudflared** binary — used to expose the local server with a public HTTPS URL so Telegram's WebApp can reach it. No account needed for quick tunnels.

The system was developed and tested on Linux and macOS. Windows works if `cloudflared.exe` is in the project directory.

---

## 2. Install

Clone the repository, then from the project root:

```bash
python3 -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Download cloudflared into the project directory:

```bash
# Linux x86_64
wget -O cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared

# macOS (Apple Silicon)
curl -L -o cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64
chmod +x cloudflared

# Windows: download cloudflared-windows-amd64.exe and rename to cloudflared.exe
```

---

## 3. Configure

Copy the template config and edit it:

```bash
cp config_change_and_rename.py config.py
```

Open `config.py` and fill in **two** values:

```python
BOT_TOKEN = "123456:ABC-DEF…"   # from @BotFather
ADMIN_ID  = 123456789           # your Telegram user ID
```

Leave `SERVER_URL = ""` — `bot.py` rewrites it automatically when cloudflared comes up.

Defaults for everything else are tuned for indoor testing (short timers, small map). The most important tunables and what to change them to for a real outdoor game are at the end of this document.

---

## 4. Run

From the project root, with the virtualenv active:

```bash
python3 bot.py
```

A successful start prints something like:

```
Starting cloudflared...
✅ Cloudflare URL: https://random-words.trycloudflare.com
Bot + server started on port 8001
```

Leave this terminal open. The SQLite file `game.db` is created on first start and persists between restarts — delete it if you want a truly clean slate.

The cloudflared URL changes on every restart. The bot updates the in-memory `SERVER_URL` automatically and players always get a working WebApp link from `/map`, but anyone who left a WebApp tab open from a previous run needs to reopen it.

---

## 5. Quick smoke test (no live players)

The fastest way to verify everything works end-to-end is to run the bundled demo. It creates four fake players (two Opposition, two System), drives them around the map, captures nodes, builds the chain between the two targets, triggers the finale, and runs the final identification — all in a couple of minutes against real server timers (compressed via a time-scale factor).

Before running the demo you still need to seed the map with at least three nodes (a target A, a target B, and one intermediate). The simplest way:

1. Send `/start` to your bot in Telegram, register as **System**.
2. Send `/admin_map`. The bot replies with a URL. Open it in a desktop browser.
3. Click anywhere on the map to place the first node. Name it `ALEX_NODE` (any name containing `ALEX` becomes target A). Accept the suggested cap, click `ADD NODE`.
4. Place another node ~80–150m away. Name it `BEATRICE_NODE` (target B). Accept the cap.
5. Place an intermediate node roughly between them. Name it anything (e.g. `BRIDGE`). Accept the cap.
6. In a separate terminal:
   ```bash
   source venv/bin/activate
   python3 demo_scenario.py
   ```

The demo activates the game itself (no need to call `/admin_start` first) and prints a narrated log of what each fake is doing. Open `/admin_presentation` (link via the `/admin_presentation` bot command) in a browser to watch the action on the map. Total runtime ≈ 4–8 minutes depending on `RENDEZVOUS_PHASE_SEC`.

If the demo finishes with a final scoreboard, everything is wired correctly.

---

## 6. Running a real game

### 6.1 Admin pre-game setup

1. **`/start`** in the bot — register yourself. Pick **System** so you can see everything.
2. **`/admin_map`** — opens the admin map editor in your browser.
3. **Place the two target nodes** (names must contain `ALEX` and `BEATRICE`). These are the win-condition endpoints — Opposition wins by connecting them with a chain of captured nodes whose growth circles overlap.
4. **Place intermediate nodes** (5–10 regular nodes). When you click an empty spot, the form auto-suggests a **max radius (cap)** based on the two nearest neighbours plus a 20m buffer. You can accept the default or override.
5. **Per-node growth cap** — new in this version: each node has its own `max_radius_m` (the dotted circle on the admin map). Captured Opposition nodes grow over time, but only up to their per-node cap. This is the main lever the admin uses to balance the map: too tight and Opposition can never connect through; too generous and they connect trivially. To edit a cap on an existing node, click the node → `EDIT CAP` in the popup.
6. (Optional) **Place a finale-hub node** (type: `finale`). This is the meeting point for the final identification stage. If you don't place one explicitly, the server picks the geometric centre between the two targets when the chain forms.
7. (Optional) **`/admin_presentation`** — opens a read-only spectator view. Useful to project on a screen during the game.

### 6.2 Players join

Each player sends **`/start`** to the bot, picks a team (System or Opposition), and is assigned an `AGENT-XXXX` anonymous ID. Then **`/map`** opens their Telegram WebApp showing the live map, their location, and team-specific actions.

### 6.3 Admin starts the game

**`/admin_start`** — everyone gets a push notification "🚀 The game has started!". From this moment:
- Opposition can capture nodes (stand inside a node's circle for `CAPTURE_TIME_SEC` continuously, then solve a puzzle).
- Captured Opposition nodes start growing at `RADIUS_GROWTH_STEP_M` every `RADIUS_GROWTH_INTERVAL_SEC` while any Opposition player is inside, up to that node's `max_radius_m`.
- System defends by walking up to attacked nodes (this freezes the capture timer) and identifies nearby Opposition players (`/defend` action in the WebApp).
- The scheduler checks every 30s whether captured Opposition nodes form a connected chain of overlapping circles from `ALEX_NODE` to `BEATRICE_NODE`. When they do, the **finale** starts.

### 6.4 Finale (two-stage)

Triggered automatically when the chain completes:

1. **Rendezvous stage** (default 5 min). All Opposition players must physically walk to the finale-hub circle. System also gathers there. Captures are blocked during the finale.
2. **Identification stage** (default 5 min). System has a UI to map each `AGENT-XXXX` to a real player. Anyone Opposition who did not show up in time is **auto-identified** as a no-show (points go to System). System submits one final guess per team.

Scoring at the end:
- System: `nodes_owned × 10 + identifications × 15 + final_stage_points`
- Opposition: `nodes_owned × 10 + survival_bonus_per_wrong_guess`

Whichever total is higher wins.

### 6.5 Admin recovery commands

- **`/admin_reset`** — clears captures, scores, identifications, puzzles. Keeps the nodes and their caps in place, so you don't have to re-seed the map.
- **`/admin_replay`** — chronological event log.
- **`/admin_help`** — full list of admin commands (including `/admin_spawn`, `/admin_move`, etc. for debugging).

---

## 7. Game mechanics — quick reference

The mechanics matter because the per-node cap only makes sense in context.

- **Base radius** (set at node creation, default 80m): the zone you stand in to interact (capture or defend). Does not change during the game.
- **Current radius** (the visible solid circle): for captured Opposition nodes, this grows over time. For System-owned nodes, it equals the base radius. This radius is what determines whether two captured nodes form a chain link (their circles must overlap).
- **Max radius / cap** (the dotted circle, admin-only view): the ceiling for current radius. Per-node, editable.
- **Puzzle bonus**: when an Opposition player solves a puzzle during capture, the node's radius jumps by `+30m` (also capped at the per-node cap).
- **Chain**: a path of overlapping captured Opposition circles from target A to target B. The scheduler computes this every 30s; the puzzle-submit endpoint also triggers an immediate check.

---

## 8. Tunables that matter for outdoor play

Open `config.py` and change these to match your venue and timing budget:

| Setting | Default (indoor test) | Suggested outdoor |
|---|---|---|
| `CAPTURE_TIME_SEC` | 90 | 180 |
| `PHASE_DURATION_SEC` | 300 (5 min) | 900 (15 min) |
| `RADIUS_GROWTH_INTERVAL_SEC` | 10 | 30 |
| `RADIUS_GROWTH_STEP_M` | 10 | 10 |
| `RADIUS_MAX_M` | 200 | Used only as fallback for nodes with no per-node cap. Leave at 200 or higher. |
| `MIN_NODE_RADIUS_M` / `MAX_NODE_RADIUS_M` | 5 / 1000 | 20 / 200 |
| `LOCATION_FRESH_SEC` | 30 | 90 |
| `RENDEZVOUS_PHASE_SEC` | 300 | 300 |
| `IDENTIFICATION_PHASE_SEC` | 300 | 300 |
| `RENDEZVOUS_RADIUS_M` | 30 | 50 |

Per-node caps are set through the admin map UI, not the config.

---

## 9. Troubleshooting

**The bot answers `/start` but `/map` shows a blank page.**
The cloudflared URL probably changed since the player opened the tab. Have them re-send `/map` and open the fresh link.

**"Game over!" fires the moment a chain completes, skipping the finale.**
This was an early bug, now fixed. If it still happens, the `game.db` schema is from an older version — delete `game.db` and restart `bot.py`.

**Opposition captures a node but the radius never grows.**
Check three things:
1. The Opposition player's location has been pinged in the last `LOCATION_FRESH_SEC` seconds (their phone must be open and granted GPS).
2. The player is physically inside the node's current radius circle (not just the base radius).
3. The node's `current_radius_m` hasn't already hit `max_radius_m` (look at the dotted cap circle on the admin map).

**The chain "should" be complete but the finale doesn't trigger.**
On the admin map, eyeball the solid circles of captured nodes. They must form an unbroken overlapping path from `ALEX_NODE` to `BEATRICE_NODE`. If two adjacent nodes' circles don't visibly touch, raise their caps via `EDIT CAP` so they have room to grow further.

**Cloudflared command not found / connection refused.**
The binary needs to be in the project root, executable (`chmod +x cloudflared` on Unix), and not blocked by a firewall. On macOS you may need to right-click → Open the first time to bypass Gatekeeper.

**Telegram refuses to open the map ("Cannot open this URL").**
Telegram WebApps require HTTPS. Cloudflared provides that automatically. If you're trying to test with a plain `http://localhost:8001` URL, it won't work — always go through the cloudflared link.

---

## 10. What's in the repository

```
bot.py                  Entry point — launches cloudflared, FastAPI server, bot polling
server.py               HTTP + WebSocket API
database.py             SQLite schema, migrations, CRUD
config_change_and_rename.py  Template config (copy → config.py)
requirements.txt        Python dependencies

handlers/
  common.py             /start, /map, registration FSM
  system.py             System team commands (/defend, /finale)
  opposition.py         Opposition team commands (/capture)
  admin.py              All /admin_* commands

game/
  scheduler.py          Background tasks: radius growth, chain check, finale stages
  geo.py                Haversine, chain detection
  puzzles/              Four puzzle types: untangle, mines, sudoku, magnets

admin_map.html          Admin map editor (place nodes, set caps)
map_trento.html         Player map (WebApp)
puzzle.html             Puzzle-solving WebApp
finale.html             Final identification WebApp

demo_scenario.py        End-to-end self-running demo with four fake players
```
