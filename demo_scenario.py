"""
PUZZLE DEMO SCENARIO — showcases node capture via solving puzzles.

Usage:
    1. Start the bot:           python3 bot.py
    2. Open in a browser:       <your-cloudflare-url>/presentation
    3. In a new terminal:       python3 demo_scenario.py

MECHANICS COVERED:
    • Player registration with AGENT-ID
    • Capture via PUZZLES (Untangle, Sudoku, Mines, Magnets)
    • Progress 0% → 80% (one puzzle) → 100% (second puzzle)
    • Puzzle freezing when System is nearby
    • Unfreezing after System leaves
    • Mesh connectivity between captured nodes
    • QR verification: System guesses AGENT-ID
    • Opposition wins by completing chain ALEX ↔ BEATRICE
"""

import asyncio
import sys
import random
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2

import aiohttp
import aiosqlite

DB_PATH = "game.db"
SERVER_URL = "http://localhost:8001"

TIME_SCALE = 1.0
WALK_STEPS = 8
WALK_DELAY = 1.5
ACT_PAUSE = 5
OPP_PLAYERS = ["ALICE", "CHARLIE"]
SYS_PLAYERS = ["BOB", "DIANA"]


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


async def sleep_scaled(s): await asyncio.sleep(s / TIME_SCALE)


def log(text, emoji="🎬"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {emoji} {text}")


def gen_anon():
    return "AGENT_" + "".join(random.choices("0123456789ABCDEF", k=4))


session: aiohttp.ClientSession = None


async def http_post(path, data):
    try:
        async with session.post(f"{SERVER_URL}{path}", json=data,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
            return await r.json()
    except Exception as e:
        log(f"HTTP {path}: {e}", "❌")
        return {"ok": False}


# ── DB helpers ───────────────────────────────────────────────────────────────

async def spawn_fake(name, team):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT MIN(telegram_id) FROM players") as cur:
            min_id = (await cur.fetchone())[0] or 0
        fake_id = min(min_id - 1, -1)
        anon = gen_anon()
        await conn.execute(
            "INSERT INTO players (telegram_id, username, team, anonymous_id) VALUES (?, ?, ?, ?)",
            (fake_id, f"FAKE_{name}", team, anon)
        )
        await conn.commit()
    return fake_id, anon


async def cleanup_fakes():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM players WHERE telegram_id < 0")
        await conn.commit()


async def get_nodes():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM nodes ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_game_state():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM game_state WHERE id=1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


async def get_last_location(player_id):
    """Return the player's last known (lat, lon) so the demo can resume
    movement from where they actually are — not from a hard-coded start
    point that only happens to be near a specific map (Trento)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute(
            "SELECT last_location_lat, last_location_lon FROM players WHERE telegram_id=?",
            (player_id,)
        ) as cur:
            row = await cur.fetchone()
    if row and row[0] is not None and row[1] is not None:
        return float(row[0]), float(row[1])
    return None


# ── HTTP actions ──────────────────────────────────────────────────────────

async def move_to(pid, lat, lon):
    await http_post("/api/location", {"player_id": pid, "lat": lat, "lon": lon})


async def smooth_walk(pid, fr_lat, fr_lon, to_lat, to_lon, steps=WALK_STEPS, delay=WALK_DELAY):
    for i in range(1, steps + 1):
        t = i / steps
        await move_to(pid, fr_lat + (to_lat - fr_lat) * t, fr_lon + (to_lon - fr_lon) * t)
        await sleep_scaled(delay)


async def set_owner(node_id, owner):
    await http_post("/api/admin/set_owner", {"node_id": node_id, "owner": owner})


async def fake_solve_puzzle(node_id, puzzle_type, player_id=None):
    """Simulates a fake solving a puzzle — instantly updates progress.
    player_id is optional but recommended for non-anchor solves so that the
    server can correctly attribute the capture in the 'Node lost' push."""
    payload = {"node_id": node_id, "puzzle_type": puzzle_type}
    if player_id is not None:
        payload["player_id"] = player_id
    res = await http_post("/api/admin/fake_solve_puzzle", payload)
    log(f"  → fake_solve_puzzle(node={node_id}, type={puzzle_type}) → {res}", "🔍")
    return res


async def fake_start_puzzle(player_id, node_id, puzzle_type):
    """Creates a real puzzle_session to demonstrate freezing."""
    return await http_post("/api/admin/fake_start_puzzle",
                            {"player_id": player_id, "node_id": node_id, "puzzle_type": puzzle_type})


async def fake_freeze_puzzle(session_id, frozen):
    return await http_post("/api/admin/fake_freeze_puzzle",
                            {"session_id": session_id, "frozen": frozen})


async def verify_player(sys_id, opp_id, guessed_anon):
    return await http_post("/api/admin/fake_verify", {
        "system_player_id": sys_id,
        "scanned_player_id": opp_id,
        "guessed_anonymous_id": guessed_anon,
    })


async def http_get(path):
    try:
        async with session.get(f"{SERVER_URL}{path}",
                               timeout=aiohttp.ClientTimeout(total=5)) as r:
            return await r.json()
    except Exception as e:
        log(f"GET {path}: {e}", "❌")
        return {"ok": False}


async def create_finale_node(lat, lon, name="FINAL_SCENE", radius=80):
    """Create a finale-hub node if there isn't one already.
    Returns the node dict (existing or newly created)."""
    nodes = await get_nodes()
    existing = next((n for n in nodes if n.get("node_type") == "finale"), None)
    if existing:
        return existing
    res = await http_post("/api/admin/node", {
        "name": name, "lat": lat, "lon": lon,
        "node_type": "finale", "radius": radius,
        "max_radius_m": radius,  # finale hub does not grow — pin cap to its base radius
    })
    # Re-fetch and find it
    nodes = await get_nodes()
    return next((n for n in nodes if n.get("node_type") == "finale"), None)


async def get_finale_state():
    return await http_get("/api/finale/state")


async def submit_finale_scan(system_player_id, qr_string, guessed_anon):
    return await http_post("/api/finale/scan_verify", {
        "system_player_id": system_player_id,
        "qr_string": qr_string,
        "guessed_anonymous_id": guessed_anon,
    })


async def _background_pinger(player_id, lat, lon, stop_event, interval=8):
    try:
        while not stop_event.is_set():
            await move_to(player_id, lat, lon)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass


def start_pinger(player_id, lat, lon):
    stop_event = asyncio.Event()
    task = asyncio.create_task(_background_pinger(player_id, lat, lon, stop_event))
    return task, stop_event


async def stop_pinger(task, stop_event):
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=2)
    except Exception:
        task.cancel()


# ── SCENARIO ─────────────────────────────────────────────────────────────────

async def main():
    global session
    session = aiohttp.ClientSession()

    print("\n" + "=" * 60)
    print("  🎬 GPS STRATEGY — PUZZLE DEMO")
    print("=" * 60)
    print(f"  Open /presentation in a browser and watch!")
    print("=" * 60 + "\n")

    try:
        async with session.get(f"{SERVER_URL}/api/game",
                               timeout=aiohttp.ClientTimeout(total=3)) as r: await r.json()
    except Exception:
        print("❌ Server unreachable. Run python3 bot.py")
        await session.close(); return

    nodes = await get_nodes()
    if len(nodes) < 3:
        print(f"❌ Only {len(nodes)} nodes in DB. At least 3 required (ALEX, BEATRICE + 1 intermediate).")
        await session.close(); return

    state = await get_game_state()
    target_a = next((n for n in nodes if n["id"] == state.get("target_node_a")), None) \
               or next((n for n in nodes if "ALEX" in (n["name"] or "")), None)
    target_b = next((n for n in nodes if n["id"] == state.get("target_node_b")), None) \
               or next((n for n in nodes if "BEATRICE" in (n["name"] or "")), None)
    if not target_a or not target_b:
        print("❌ Nodes NODE ALEX and NODE BEATRICE are required.")
        await session.close(); return

    other_nodes = [n for n in nodes if n["id"] not in (target_a["id"], target_b["id"])
                                       and n.get("node_type", "node") == "node"]
    other_nodes.sort(key=lambda n: haversine(target_a["lat"], target_a["lon"], n["lat"], n["lon"]))

    log(f"Map: {len(nodes)} nodes. Goal: connect {target_a['name']} ↔ {target_b['name']}", "🗺")
    log(f"Intermediate nodes: {[n['name'] for n in other_nodes]}", "🗺")
    log(f"Capture now goes through PUZZLES: 1 puzzle → 80%, 2 puzzles → 100%", "🧩")
    await sleep_scaled(2)

    # ═══ SETUP ═══════════════════════════════════════════════════════════
    log("─" * 50, ""); log("SETUP: ALEX and BEATRICE start owned by Opposition (anchors)", "⚙️"); log("─" * 50, "")

    # Full server-side reset so a previous run's capture progress, puzzle
    # history, and finale state don't poison this one. Without this, a second
    # demo run silently fails because every fake_solve_puzzle call hits
    # "Already 100%" — the nodes were already maxed out by the first run.
    # Map structure (positions, caps, target assignments) is preserved.
    await http_post("/api/admin/reset", {})
    log("Server state reset to clean slate", "↺")

    # Ensure a finale hub exists — place it roughly between ALEX and BEATRICE
    mid_lat = (target_a["lat"] + target_b["lat"]) / 2
    mid_lon = (target_a["lon"] + target_b["lon"]) / 2
    finale_node = await create_finale_node(mid_lat, mid_lon, name="FINAL_SCENE", radius=80)
    if finale_node:
        log(f"🎬 Finale hub: *{finale_node['name']}* (hidden until chain is built)", "🕶")
    await set_owner(target_a["id"], "opposition")
    await fake_solve_puzzle(target_a["id"], "untangle")
    await fake_solve_puzzle(target_a["id"], "sudoku")  # instantly 100%
    log(f"🔴 {target_a['name']} → Opposition 100% (anchor)", "⚓")
    await sleep_scaled(2)
    await set_owner(target_b["id"], "opposition")
    await fake_solve_puzzle(target_b["id"], "untangle")
    await fake_solve_puzzle(target_b["id"], "sudoku")
    log(f"🔴 {target_b['name']} → Opposition 100% (anchor)", "⚓")
    await sleep_scaled(ACT_PAUSE)

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE game_state SET active=1, current_phase=1, phase_started_at=? WHERE id=1",
                          (datetime.now().isoformat(),))
        await conn.commit()
    log("Game activated", "🚀"); await sleep_scaled(2)

    # ═══ ACT 1 — REGISTRATION ═══════════════════════════════════════════
    log("─" * 50, ""); log("ACT 1: Players join the game", "🎭"); log("─" * 50, "")
    await cleanup_fakes()
    opp_ids, opp_anons = {}, {}
    for name in OPP_PLAYERS:
        pid, anon = await spawn_fake(name, "opposition")
        opp_ids[name] = pid; opp_anons[name] = anon
        log(f"🔴 FAKE_{name} joined Opposition (ID: {anon})", "👤")
        await sleep_scaled(2)
    sys_ids = {}
    for name in SYS_PLAYERS:
        pid, _ = await spawn_fake(name, "system")
        sys_ids[name] = pid
        log(f"⚙️ FAKE_{name} joined System", "👤")
        await sleep_scaled(2)
    await sleep_scaled(ACT_PAUSE)

    # Starting positions — far from their starting nodes
    center_lat = (target_a["lat"] + target_b["lat"]) / 2
    center_lon = (target_a["lon"] + target_b["lon"]) / 2
    log("Players take their starting positions", "📍")
    await move_to(opp_ids["ALICE"],   target_a["lat"] + 0.0030, target_a["lon"] - 0.0030); await sleep_scaled(1)
    await move_to(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030); await sleep_scaled(1)
    await move_to(sys_ids["BOB"],     center_lat + 0.0015, center_lon - 0.0010); await sleep_scaled(1)
    await move_to(sys_ids["DIANA"],   center_lat - 0.0015, center_lon + 0.0010); await sleep_scaled(ACT_PAUSE)

    first_node = other_nodes[0]

    # ═══ ACT 2 — APPROACH AND PUZZLE START ═══════════════════════════════
    log("─" * 50, ""); log(f"ACT 2: ALICE approaches {first_node['name']} and opens a puzzle", "🎭"); log("─" * 50, "")
    log(f"🔴 ALICE moves into the circle of {first_node['name']}...", "🏃")
    await smooth_walk(opp_ids["ALICE"], target_a["lat"] + 0.0030, target_a["lon"] - 0.0030,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log(f"🔴 ALICE is inside the circle — opens the Untangle puzzle", "🧩")

    # Create a real puzzle_session to demonstrate freezing
    res = await fake_start_puzzle(opp_ids["ALICE"], first_node["id"], "untangle")
    alice_session = res.get("session_id") if res.get("ok") else None
    log(f"📡 Puzzle session created: {alice_session[:8] if alice_session else 'fail'}", "")

    # Start pinging ALICE
    alice_pinger, alice_stop = start_pinger(opp_ids["ALICE"], first_node["lat"], first_node["lon"])
    log("🚨 Server pushes 'Node under attack' to real System players", "📡")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 3 — BOB ARRIVES → PUZZLE FROZEN ═════════════════════════════
    log("─" * 50, ""); log("ACT 3: BOB intercepts → puzzle is frozen", "🎭"); log("─" * 50, "")
    log(f"⚙️ BOB runs into the circle of {first_node['name']}", "🏃")
    await smooth_walk(sys_ids["BOB"], center_lat + 0.0015, center_lon - 0.0010,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log("⚙️ BOB inside the circle → ALICE's puzzle is FROZEN (cannot Submit)", "🛡")
    if alice_session:
        await fake_freeze_puzzle(alice_session, True)
    bob_pinger, bob_stop = start_pinger(sys_ids["BOB"], first_node["lat"], first_node["lon"])
    log("📋 System logs the attacker's AGENT_ID", "📡")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 4 — BOB LEAVES → UNFREEZE ════════════════════════════════════
    log("─" * 50, ""); log("ACT 4: BOB leaves → puzzle unfreezes", "🎭"); log("─" * 50, "")
    await stop_pinger(bob_pinger, bob_stop)
    bob_far_lat = first_node["lat"] + 0.003
    bob_far_lon = first_node["lon"] - 0.003
    log(f"⚙️ BOB leaves the CIRCLE (~330m)", "🏃")
    await smooth_walk(sys_ids["BOB"], first_node["lat"], first_node["lon"],
                      bob_far_lat, bob_far_lon, steps=WALK_STEPS, delay=WALK_DELAY)
    log("⏳ ALICE stays in the circle → puzzle unfreezes", "⏳")
    if alice_session:
        await fake_freeze_puzzle(alice_session, False)
    await sleep_scaled(3)
    log("▶️ ALICE can continue solving the puzzle", "✅")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 5 — ALICE SOLVES PUZZLE 1 → NODE 80% ══════════════════════════
    log("─" * 50, ""); log(f"ACT 5: ALICE solves Untangle → {first_node['name']} becomes 80%", "🎭"); log("─" * 50, "")
    log("🧩 ALICE untangles the graph nodes...", "🕸")
    await sleep_scaled(4)
    res = await fake_solve_puzzle(first_node["id"], "untangle", player_id=opp_ids["ALICE"])
    if res.get("ok"):
        log(f"✅ Untangle solved! {first_node['name']} → Opposition 80% — radius grows to 80% of cap", "🎉")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 6 — ALICE SOLVES SECOND PUZZLE → NODE 100% ════════════════════
    log("─" * 50, ""); log(f"ACT 6: ALICE solves Sudoku → {first_node['name']} becomes 100%", "🎭"); log("─" * 50, "")
    log("🔢 ALICE solves Sudoku 4×4 (reinforces the node)...", "")
    await sleep_scaled(4)
    res = await fake_solve_puzzle(first_node["id"], "sudoku", player_id=opp_ids["ALICE"])
    if res.get("ok"):
        log(f"✅ Sudoku solved! {first_node['name']} → 100% — radius reaches its cap", "🎉")
    # Stop ALICE's ping loop. She stays where she is — radius is set by
    # progress now, not by holding ground, so no need to move her away.
    await stop_pinger(alice_pinger, alice_stop)
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 7 — CHARLIE FROM THE OTHER SIDE ═══════════════════════════════
    if len(other_nodes) >= 2:
        second_node = other_nodes[-1]
        log("─" * 50, ""); log(f"ACT 7: CHARLIE attacks {second_node['name']} from the flank", "🎭"); log("─" * 50, "")
        await smooth_walk(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030,
                          second_node["lat"], second_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
        log(f"🔴 CHARLIE solves Mines on {second_node['name']}", "💣")
        await sleep_scaled(4)
        await fake_solve_puzzle(second_node["id"], "mines", player_id=opp_ids["CHARLIE"])
        log(f"✅ Mines solved → {second_node['name']} 80% — radius grows to 80% of cap", "🎉")
        await sleep_scaled(3)
        log(f"🔴 CHARLIE solves Magnets to reinforce", "🧲")
        await sleep_scaled(4)
        await fake_solve_puzzle(second_node["id"], "magnets", player_id=opp_ids["CHARLIE"])
        log(f"✅ Magnets solved → {second_node['name']} 100% — radius reaches its cap", "🎉")
        await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 8 — QR VERIFICATION ════════════════════════════════════════
    log("─" * 50, ""); log("ACT 8: QR verification — System tries to identify ALICE", "🎭"); log("─" * 50, "")
    log("⚙️ BOB scans ALICE's QR and tries to guess the AGENT-ID", "📷")
    await sleep_scaled(2)
    correct_anon = opp_anons["ALICE"]
    log(f"⚙️ BOB inputs: {correct_anon} (correct)", "🧠")
    res = await verify_player(sys_ids["BOB"], opp_ids["ALICE"], correct_anon)
    if res.get("correct"):
        log(f"✅ CORRECT! ALICE = {correct_anon}. +15 points for System", "🎯")
    await sleep_scaled(ACT_PAUSE)

    log("⚙️ DIANA tries to verify CHARLIE — but gets it wrong", "📷")
    fake_guess = "AGENT_XXXX"
    log(f"⚙️ DIANA inputs: {fake_guess}", "🧠")
    res = await verify_player(sys_ids["DIANA"], opp_ids["CHARLIE"], fake_guess)
    if res.get("ok") and not res.get("correct"):
        log(f"❌ Wrong. Correct: {opp_anons['CHARLIE']}", "🕵")
        log("CHARLIE remained anonymous", "✨")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 9 — CHAIN COMPLETION ══════════════════════════════════════════
    log("─" * 50, ""); log("ACT 9: Opposition completes the chain", "🎭"); log("─" * 50, "")
    # All captured intermediates already sit at their per-node max_radius_m
    # (because progress=100). The chain forms automatically whenever the
    # admin's caps are wide enough to overlap ALEX, the intermediates, and
    # BEATRICE. The /api/admin/suggest_max_radius default (≈ D_AB / 3) is
    # tuned exactly for this — two intermediates can bridge the targets.
    log("Capture the last node: /admin_move ALICE 46.06220 11.15191 and then /admin_fake_capture ALICE J; scheduler will detect the chain shortly", "⏳")

    # Wait until the server enters the finale stage
    rdv_node = None
    for _ in range(40):  # up to ~80s
        await sleep_scaled(2)
        fst = await get_finale_state()
        if fst.get("ok") and fst.get("stage"):
            rdv_node = fst.get("rendezvous_node")
            log(f"🔗 Finale triggered. Rendezvous at *{rdv_node['name']}* — {fst['stage']} stage", "🎬")
            break
    if not rdv_node:
        log("Finale did not start in time — check server logs", "⚠️")
        await session.close(); return

    # ═══ ACT 10 — RENDEZVOUS ═══════════════════════════════════════════════
    log("─" * 50, ""); log("ACT 10: All players walk to the finale node", "🎭"); log("─" * 50, "")

    # ALICE arrives; CHARLIE is a no-show (will be auto-identified).
    # Start each walk from the player's actual last known location so the demo
    # works on any map, not only Trento.
    log("🔴 ALICE walks toward the rendezvous", "🏃")
    a_from = await get_last_location(opp_ids["ALICE"]) or (rdv_node["lat"], rdv_node["lon"])
    await smooth_walk(opp_ids["ALICE"], a_from[0], a_from[1],
                      rdv_node["lat"], rdv_node["lon"], steps=4, delay=1)
    log(f"🔴 ALICE inside the rendezvous circle of *{rdv_node['name']}*", "📍")
    # CHARLIE doesn't move; their last_location stays far from rendezvous.
    log("🔴 CHARLIE stays away — will be auto-identified", "🙈")

    # Both System players arrive
    for sname in SYS_PLAYERS:
        log(f"⚙️ {sname} walks to the rendezvous", "🏃")
        s_from = await get_last_location(sys_ids[sname]) or (rdv_node["lat"], rdv_node["lon"])
        await smooth_walk(sys_ids[sname], s_from[0], s_from[1],
                          rdv_node["lat"], rdv_node["lon"], steps=3, delay=1)

    # Keep the location of everyone in the circle fresh while we wait for the
    # rendezvous timer (server's LOCATION_FRESH_SEC is 90s; otherwise everyone
    # would be flagged as a no-show).
    pingers = []
    pingers.append(start_pinger(opp_ids["ALICE"], rdv_node["lat"], rdv_node["lon"]))
    for sname in SYS_PLAYERS:
        pingers.append(start_pinger(sys_ids[sname], rdv_node["lat"], rdv_node["lon"]))

    # Wait out the rendezvous timer (server's RENDEZVOUS_PHASE_SEC)
    log("⏳ Waiting for the rendezvous timer to expire — CHARLIE auto-identifies", "⌛")
    for _ in range(180):  # up to 6 min poll
        await sleep_scaled(2)
        fst = await get_finale_state()
        if fst.get("ok") and fst.get("stage") == "identification":
            log("🎯 Identification stage opened", "🔓")
            break

    # ═══ ACT 11 — FINAL IDENTIFICATION ═════════════════════════════════════
    log("─" * 50, ""); log("ACT 11: System scans each Opposition QR and guesses their AGENT-ID", "🎭"); log("─" * 50, "")
    fst = await get_finale_state()
    if not fst.get("ok"):
        log(f"Finale state error: {fst}", "⚠️")
        await session.close(); return

    open_anons = fst.get("open_anonymous_ids", [])
    players = fst.get("players", [])
    log(f"Open AGENT-IDs left to assign: {open_anons}", "📋")

    # For each unresolved Opposition who showed up, BOB (System) scans their QR
    # (we synthesise the same QR string the server publishes) and guesses correctly.
    for p in players:
        if p.get("verified_during_game") or p.get("auto_identified"):
            continue
        if not p.get("present_at_rendezvous"):
            continue
        qr_string = f"GPSGAME:PLAYER:{p['player_id']}:{p['anonymous_id']}"
        log(f"⚙️ BOB scans {p['username']} → guessing {p['anonymous_id']}", "📷")
        res = await submit_finale_scan(sys_ids["BOB"], qr_string, p["anonymous_id"])
        if res.get("ok"):
            mark = "✓" if res.get("correct") else "✗"
            log(f"  {mark} server confirmed: correct={res.get('correct')}", "🎯")
        else:
            log(f"  scan failed: {res}", "⚠️")
        await sleep_scaled(1)

    # Stop the background location pingers we started for ALICE/BOB/DIANA
    for task, ev in pingers:
        await stop_pinger(task, ev)

    # The server triggers _finalize_game automatically once all open IDs are
    # resolved, so just wait briefly for the end-of-game message.
    await sleep_scaled(3)

    log("─" * 50, ""); log("🎬 SCENARIO COMPLETE", "✨")
    log("Check the bot for final scores", "📜")
    log("/admin_replay — full chronology", "📜")
    log("/admin_reset — reset nodes + CLEAR LOGS", "🔄")
    log("/admin_unspawn all — remove fakes", "🗑")
    await session.close()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Interrupted"); sys.exit(0)