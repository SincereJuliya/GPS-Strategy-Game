# GPS Strategy Game

A GPS-based urban strategy game for ~10–20 players, played on a real city map via a Telegram bot + Mini App. Two teams (**System** and **Opposition**) fight for control of physical nodes by walking to them and solving puzzles.

This README explains how to set the game up from scratch and run it for a real session.

---

## 1. What you need before you start

- **A computer** that will stay online for the duration of the game (laptop is fine). macOS / Linux / Windows with WSL all work.
- **Python 3.10+** installed.
- **A Telegram account** that will be the game admin.
- **A Telegram bot token** — create a new bot via [@BotFather](https://t.me/BotFather) and copy the token.
- **Your Telegram user ID** — get it from [@userinfobot](https://t.me/userinfobot).
- **`cloudflared`** — used to expose your local server over HTTPS so the Telegram Mini App can reach it. Download from <https://github.com/cloudflare/cloudflared/releases> and place the binary in the project folder (or install it globally).
- **All players need Telegram** installed on their phones with location permission allowed.

---

## 2. Install

```bash
# 1. Clone or unpack the project
cd GPS-Strategy-Game

# 2. (Recommended) create a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # on Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
pip install fastapi uvicorn aiohttp pydantic   # in case they aren't pinned

# 4. Make sure cloudflared is executable
chmod +x ./cloudflared              # Linux/macOS only
```

---

## 3. Configure

The file `config_change_and_rename.py` is a template. **Copy it to `config.py`** and fill in your values:

```bash
cp config_change_and_rename.py config.py
```

Then open `config.py` and set at minimum:

```python
BOT_TOKEN = "123456:ABC-DEF…"   # from @BotFather
ADMIN_ID  = 123456789           # your Telegram user id
```

You can leave `SERVER_URL = ""` — it will be filled in automatically by cloudflared on startup.

Other useful settings:

| Setting | What it does | Suggested for testing | Suggested for real game |
|---|---|---|---|
| `CAPTURE_TIME_SEC` | Seconds to hold a node to capture it | 90 | 180 |
| `PHASE_DURATION_SEC` | Duration of one phase | 300 | 900 |
| `PHASE_COUNT` | Number of phases per match | 3 | 3 |
| `MIN_NODE_RADIUS_M` / `MAX_NODE_RADIUS_M` | Allowed node radius range | 5 / 1000 | 20 / 200 |
| `RADIUS_GROWTH_STEP_M` | How much an Opposition node grows per tick | 10 | 10 |
| `LOCATION_FRESH_SEC` | How recent a player's location must be | 30 | 90 |

---

## 4. Start the server + bot

From the project root, with the virtualenv active:

```bash
python3 bot.py
```

You should see something like:

```
Starting cloudflared...
✅ Cloudflare URL: https://something-random.trycloudflare.com
Bot + server started on port 8001
```

Leave this terminal open for the whole session. If it crashes, just run the command again — the SQLite database (`game.db`) persists.

> **Tip:** the cloudflare URL changes every time you restart. The bot updates it in memory automatically, so players who open the map link via the bot always get a working one. If you ever need to share the URL manually, copy it from the terminal.

---

## 5. Set up the map (one time per session)

All commands below are sent to your bot in Telegram, from the admin account.

1. **`/start`** — register yourself, pick a team. As admin you usually pick **System** (you'll see all nodes and player IDs).
2. **`/admin_map`** — opens an in-bot web view where you click on the map to add nodes. For each click you set a name and a radius. Add 5–10 nodes spread across the playing area.
3. **`/admin_setnodes ALEX BEATRICE`** — pick the two target nodes Opposition must connect. Use any two node names you created. These are secret to Opposition.
4. **`/admin_setmode A`** — Mode A = win by connecting target nodes. (`B` = win by holding more than half of nodes at the end of phase 3.)
5. **`/admin_nodes`** — sanity check; lists all nodes with their owner.

---

## 6. Onboard players

Send all players this short instruction:

> 1. Open the bot **@your_bot_username** in Telegram.
> 2. Send `/start`.
> 3. Pick a team: **System** (defenders) or **Opposition** (attackers).
> 4. Allow location access when the bot asks.
> 5. The bot will send you a link to open the map — that is the main game screen.

Give it a few minutes — wait until every player has registered and opened the map at least once. As admin you can check with `/admin_nodes` and `/admin_debug`.

> **Balance:** aim for an equal split, or slightly more Opposition than System (System is reactive and tends to be more powerful per player).

---

## 7. Start the game

Once everyone is registered and standing somewhere reasonable on the playing field:

```
/admin_start
```

This kicks off phase 1. All players get a Telegram notification. The phase timer starts; after `PHASE_DURATION_SEC` it auto-advances. After the final phase the game ends and scores are posted.

---

## 8. How the game plays out (admin's perspective)

You normally don't need to do anything during the game — schedulers handle phase changes, radius growth, capture timers, and the victory check. But you have tools if something goes wrong:

| Command | Use |
|---|---|
| `/admin_nodes` | List all nodes and current owners |
| `/admin_debug` | See active captures, frozen states, player locations |
| `/admin_reset` | Hard reset — all nodes back to System, scores wiped, captures cleared |
| `/admin_help` | Full list of admin commands |
| `/admin_move ALICE 46.0619 11.1502` | Manually set a player's location (for stuck GPS) |
| `/admin_spawn opposition ALICE` | Spawn a fake player (for demo / testing) |
| `/admin_fake_capture ALICE TEST1` | Force a fake player to start capturing a node |

### Verification phase

When Opposition connects the two target nodes, the game does **not** end immediately. Instead a **3-minute verification phase** begins:

- Opposition cannot capture any more nodes.
- System can keep scanning Opposition QR codes via `/verify` to score identification points.
- After 3 minutes the game ends and final scores are computed.

This means the chain being built isn't a guaranteed Opposition win — System can still catch up on identification points.

---

## 9. Player commands cheat sheet

### Everyone
- `/start` — register / pick team
- `/help` — show commands
- `/map` — open the map Mini App
- `/myqr` — show your QR (Opposition: this is what System tries to scan)
- `/leave` — leave the game

### Opposition
- `/capture` — start capturing the nearest System node (you must be inside its circle)
- `/status` — your team's current state

### System
- `/defend` — interrupt a capture (must be inside the node's circle)
- `/verify` — scan an Opposition QR code and try to match it to an AGENT-ID
- `/ids` — list of AGENT-IDs you've personally identified
- `/team_ids` — your whole team's identification log
- `/score` — current score

---

## 10. Demo mode (no real players needed)

If you want to test the system or demo it without actual people walking around:

1. Start the bot as usual: `python3 bot.py`
2. Register yourself as System via `/start`.
3. Create a few nodes via `/admin_map` and run `/admin_start`.
4. In a second terminal, with the virtualenv active:
   ```bash
   python3 demo_scenario.py
   ```
5. This script spawns fake Opposition (`ALICE`, `CHARLIE`) and System (`BOB`, `DIANA`) players that walk to nodes, solve puzzles, get frozen, verify each other, and eventually trigger a chain win.
6. You'll receive real Telegram notifications as if Opposition were actually attacking.
7. The presentation mode of the map (open `<your-cloudflare-url>/presentation` in a browser) is a good spectator view for demos.

---

## 11. Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| No notifications in Telegram | Bot crashed, or `_bot` not initialised, or wrong `parse_mode` | The current `tg_send` already falls back to plain text on Markdown errors. Check the bot terminal for `[tg]` error lines. Make sure `bot.py` finished startup before captures begin. |
| `cloudflared` doesn't print a URL | Binary missing or rate-limited | Run `./cloudflared --version` to confirm it works. If rate-limited, wait 1–2 minutes and restart `bot.py`. As a fallback you can use `ngrok http 8001` and put the URL in `config.SERVER_URL` manually. |
| Player can't see nodes on the map | Location permission denied, or they registered for the wrong team | Ask them to fully close Telegram, reopen, and re-allow location. As a last resort run `/admin_reset` and have them `/start` again. |
| Capture never completes | Player is standing outside the radius, or a System player is inside the same node (freezes the timer) | Check `/admin_debug`. The radius is visible on the map. |
| Game shows "Game over" too early | You're running an older `server.py` without the verification phase | Make sure you have the patched `server.py` — the chain win now starts a 3-minute verification phase instead of ending immediately. |
| Database in a weird state | A crash mid-capture, or leftover state from a previous test | `/admin_reset` clears active state. To fully wipe everything (including nodes and players) stop the bot and delete `game.db`, then restart. |

---

## 12. Project layout

```
GPS-Strategy-Game/
├── bot.py                 ← entry point (starts bot + FastAPI + cloudflared)
├── server.py              ← FastAPI server: capture/verify/state APIs, WebSocket, victory check
├── database.py            ← SQLite schema and queries
├── config.py              ← your secrets and tunables (you create this from the template)
├── config_change_and_rename.py  ← template config
├── demo_scenario.py       ← fake-player walkthrough for demos
├── requirements.txt
├── handlers/
│   ├── common.py          ← /start, /map, /help, /myqr, /leave
│   ├── opposition.py      ← /capture, /status
│   ├── system.py          ← /defend, /verify, /ids, /score
│   └── admin.py           ← /admin_* commands
├── game/
│   ├── scheduler.py       ← background tasks: capture timer, radius growth, phases
│   ├── geo.py             ← haversine, connection graph, path check
│   └── puzzles/           ← Untangle, Sudoku, Mines, Magnets generators
├── map_trento.html        ← player map Mini App
├── admin_map.html         ← admin node editor
└── puzzle.html            ← puzzle UI shown during capture
```

---

## 13. Quick reference — one-page run sheet

```
1. Edit config.py            BOT_TOKEN, ADMIN_ID
2. python3 bot.py            (leave running, copy cloudflare URL)
3. /start                    in your bot — register as System
4. /admin_map                add 5–10 nodes
5. /admin_setnodes A B       pick targets
6. /admin_setmode A          chain-victory mode
7. Players join              /start, pick team, allow location
8. /admin_start              kick off phase 1
9. Play                      watch /admin_debug if anything looks off
10. After chain → 3-min verification phase → final score
```

