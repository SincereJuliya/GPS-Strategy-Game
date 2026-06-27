"""
DEMO SCENARIO WITH PUZZLES — demonstrates capture through solving puzzles.

Usage:
    1. Start the bot:        python3 bot.py
    2. Open in browser:      <your-cloudflare-url>/presentation
    3. In a new terminal:    python3 demo_scenario.py

INCLUDED MECHANICS:
    • Player registration with AGENT-ID
    • Capture via PUZZLES (Untangle, Sudoku, Mines, Magnets)
    • Progress 0% → 80% (one puzzle) → 100% (second puzzle)
    • Puzzle freeze when System is nearby
    • Unfreezing after System leaves
    • Mesh connection between captured nodes
    • QR verification: System guesses AGENT-ID
    • Opposition victory via ALEX ↔ BEATRICE chain
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


# ── Actions via HTTP ───────────────────────────────────────────────────

async def move_to(pid, lat, lon):
    await http_post("/api/location", {"player_id": pid, "lat": lat, "lon": lon})


async def smooth_walk(pid, fr_lat, fr_lon, to_lat, to_lon, steps=WALK_STEPS, delay=WALK_DELAY):
    for i in range(1, steps + 1):
        t = i / steps
        await move_to(pid, fr_lat + (to_lat - fr_lat) * t, fr_lon + (to_lon - fr_lon) * t)
        await sleep_scaled(delay)


async def set_owner(node_id, owner):
    await http_post("/api/admin/set_owner", {"node_id": node_id, "owner": owner})


async def fake_solve_puzzle(node_id, puzzle_type):
    """Simulates a fake player solving a puzzle — instantly updates progress."""
    res = await http_post("/api/admin/fake_solve_puzzle",
                            {"node_id": node_id, "puzzle_type": puzzle_type})
    # Debug log — to see what the server returned
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
    print(f"  Open /presentation in your browser to watch!")
    print("=" * 60 + "\n")

    try:
        async with session.get(f"{SERVER_URL}/api/game",
                               timeout=aiohttp.ClientTimeout(total=3)) as r: await r.json()
    except Exception:
        print("❌ Server unavailable. Run python3 bot.py")
        await session.close(); return

    nodes = await get_nodes()
    if len(nodes) < 3:
        print(f"❌ Only {len(nodes)} nodes in DB. Need at least 3 (ALEX, BEATRICE + 1 intermediate).")
        await session.close(); return

    state = await get_game_state()
    target_a = next((n for n in nodes if n["id"] == state.get("target_node_a")), None) \
               or next((n for n in nodes if "ALEX" in (n["name"] or "")), None)
    target_b = next((n for n in nodes if n["id"] == state.get("target_node_b")), None) \
               or next((n for n in nodes if "BEATRICE" in (n["name"] or "")), None)
    if not target_a or not target_b:
        print("❌ NODE ALEX and NODE BEATRICE are required.")
        await session.close(); return

    other_nodes = [n for n in nodes if n["id"] not in (target_a["id"], target_b["id"])
                                       and n.get("node_type", "node") == "node"]
    other_nodes.sort(key=lambda n: haversine(target_a["lat"], target_a["lon"], n["lat"], n["lon"]))

    log(f"Map: {len(nodes)} nodes. Goal: connect {target_a['name']} ↔ {target_b['name']}", "🗺")
    log(f"Intermediate nodes: {[n['name'] for n in other_nodes]}", "🗺")
    log(f"Capture is now done via PUZZLES: 1 puzzle → 80%, 2 puzzles → 100%", "🧩")
    await sleep_scaled(2)

    # ═══ PREPARATION ════════════════════════════════════════════════════════
    log("─" * 50, ""); log("PREPARATION: ALEX and BEATRICE belong to Opposition immediately (anchors)", "⚙️"); log("─" * 50, "")
    await set_owner(target_a["id"], "opposition")
    await fake_solve_puzzle(target_a["id"], "untangle")
    await fake_solve_puzzle(target_a["id"], "sudoku")  # 100% instantly
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

    # ═══ ACT 1 — REGISTRATION ═══════════════════════════════════════════════
    log("─" * 50, ""); log("ACT 1: Players enter the game", "🎭"); log("─" * 50, "")
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

    # ═══ ACT 2 — APPROACH AND PUZZLE START ═══════════════════════════════════
    log("─" * 50, ""); log(f"ACT 2: ALICE approaches {first_node['name']} and opens a puzzle", "🎭"); log("─" * 50, "")
    log(f"🔴 ALICE moves inside the circle of {first_node['name']}...", "running_man")
    await smooth_walk(opp_ids["ALICE"], target_a["lat"] + 0.0030, target_a["lon"] - 0.0030,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log(f"🔴 ALICE is inside the circle — opening Untangle puzzle", "🧩")

    # Creating a real puzzle_session to demonstrate freezing
    res = await fake_start_puzzle(opp_ids["ALICE"], first_node["id"], "untangle")
    alice_session = res.get("session_id") if res.get("ok") else None
    log(f"📡 Puzzle session created: {alice_session[:8] if alice_session else 'fail'}", "")

    # Starting ALICE's pinger
    alice_pinger, alice_stop = start_pinger(opp_ids["ALICE"], first_node["lat"], first_node["lon"])
    log("🚨 Server sends 'Node under attack' push notification to real System players", "📡")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 3 — BOB ARRIVES → PUZZLE FROZEN ════════════════════════════════
    log("─" * 50, ""); log("ACT 3: BOB intercepts → puzzle frozen", "🎭"); log("─" * 50, "")
    log(f"⚙️ BOB runs inside the circle of {first_node['name']}", "running_man")
    await smooth_walk(sys_ids["BOB"], center_lat + 0.0015, center_lon - 0.0010,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log("⚙️ BOB is inside the circle → ALICE's puzzle is FROZEN (cannot Submit)", "🛡")
    if alice_session:
        await fake_freeze_puzzle(alice_session, True)
    bob_pinger, bob_stop = start_pinger(sys_ids["BOB"], first_node["lat"], first_node["lon"])
    log("📋 System logs the attacker's AGENT_ID", "📡")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 4 — BOB LEAVES → UNFREEZE ══════════════════════════════════════
    log("─" * 50, ""); log("ACT 4: BOB leaves → puzzle unfreezes", "🎭"); log("─" * 50, "")
    await stop_pinger(bob_pinger, bob_stop)
    bob_far_lat = first_node["lat"] + 0.003
    bob_far_lon = first_node["lon"] - 0.003
    log(f"⚙️ BOB leaves THE CIRCLE (~330m away)", "running_man")
    await smooth_walk(sys_ids["BOB"], first_node["lat"], first_node["lon"],
                      bob_far_lat, bob_far_lon, steps=WALK_STEPS, delay=WALK_DELAY)
    log("⏳ ALICE remains in the circle → puzzle unfreezes", "⏳")
    if alice_session:
        await fake_freeze_puzzle(alice_session, False)
    await sleep_scaled(3)
    log("▶️ ALICE can continue solving the puzzle", "✅")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 5 — ALICE SOLVES PUZZLE 1 → NODE 80% ════════════════════════════════
    log("─" * 50, ""); log(f"ACT 5: ALICE solves Untangle → {first_node['name']} becomes 80%", "🎭"); log("─" * 50, "")
    log("🧩 ALICE untangles the graph nodes...", "🕸")
    await sleep_scaled(4)
    res = await fake_solve_puzzle(first_node["id"], "untangle")
    if res.get("ok"):
        log(f"✅ Untangle puzzle solved! {first_node['name']} → Opposition 80% (+30m radius)", "🎉")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 6 — ALICE SOLVES SECOND PUZZLE → NODE 100% ══════════════════════════
    log("─" * 50, ""); log(f"ACT 6: ALICE solves Sudoku → {first_node['name']} becomes 100%", "🎭"); log("─" * 50, "")
    log("🔢 ALICE solves 4×4 Sudoku (reinforcing the node)...", "")
    await sleep_scaled(4)
    res = await fake_solve_puzzle(first_node["id"], "sudoku")
    if res.get("ok"):
        log(f"✅ Sudoku puzzle solved! {first_node['name']} → 100% (+30m radius)", "🎉")
    await stop_pinger(alice_pinger, alice_stop)
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 7 — CHARLIE FROM THE OTHER SIDE ══════════════════════════════════════
    if len(other_nodes) >= 2:
        second_node = other_nodes[-1]
        log("─" * 50, ""); log(f"ACT 7: CHARLIE attacks from the flank on {second_node['name']}", "🎭"); log("─" * 50, "")
        await smooth_walk(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030,
                          second_node["lat"], second_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
        log(f"🔴 CHARLIE solves Mines on {second_node['name']}", "💣")
        await sleep_scaled(4)
        await fake_solve_puzzle(second_node["id"], "mines")
        log(f"✅ Mines solved → {second_node['name']} 80%", "🎉")
        await sleep_scaled(3)
        log(f"🔴 CHARLIE solves Magnets to reinforce", "🧲")
        await sleep_scaled(4)
        await fake_solve_puzzle(second_node["id"], "magnets")
        log(f"✅ Magnets solved → {second_node['name']} 100%", "🎉")
        await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 8 — QR VERIFICATION ════════════════════════════════════════════
    log("─" * 50, ""); log("ACT 8: QR verification — System tries to identify ALICE", "🎭"); log("─" * 50, "")
    log("⚙️ BOB scans ALICE's QR and tries to guess the AGENT-ID", "📷")
    await sleep_scaled(2)
    correct_anon = opp_anons["ALICE"]
    log(f"⚙️ BOB inputs: {correct_anon} (correct)", "🧠")
    res = await verify_player(sys_ids["BOB"], opp_ids["ALICE"], correct_anon)
    if res.get("correct"):
        log(f"✅ CORRECT! ALICE = {correct_anon}. +15 points to System", "🎯")
    await sleep_scaled(ACT_PAUSE)

    log("⚙️ DIANA tries to verify CHARLIE — but makes a mistake", "📷")
    fake_guess = "AGENT_XXXX"
    log(f"⚙️ DIANA inputs: {fake_guess}", "🧠")
    res = await verify_player(sys_ids["DIANA"], opp_ids["CHARLIE"], fake_guess)
    if res.get("ok") and not res.get("correct"):
        log(f"❌ Incorrect. Correct was: {opp_anons['CHARLIE']}", "🕵")
        log("CHARLIE remains anonymous", "✨")
    await sleep_scaled(ACT_PAUSE)

    # ═══ FINAL ═════════════════════════════════════════════════════════════
    log("─" * 50, ""); log("FINAL: victory check", "🏆"); log("─" * 50, "")
    log("Scheduler will check ALEX ↔ BEATRICE chain...", "⏳")
    await sleep_scaled(30)

    log("─" * 50, ""); log("🎬 SCENARIO COMPLETED", "✨")
    log("/admin_replay — full chronology", "📜")
    log("/admin_reset — reset nodes + CLEAR LOGS", "🔄")
    log("/admin_unspawn all — remove fakes", "🗑")
    await session.close()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Interrupted"); sys.exit(0)