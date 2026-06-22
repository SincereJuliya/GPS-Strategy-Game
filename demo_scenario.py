"""
DEMO SCENARIO — shows ALL game mechanics using fake players.

Usage:
    1. Start the bot:         python3 bot.py
    2. Open in browser:       <your-cloudflare-url>/presentation
    3. In a new terminal:     python3 demo_scenario.py

INCLUDED MECHANICS:
    • Player registration with AGENT-ID
    • Node capture (3 minutes hold time)
    • "Node attacked" notification by real System
    • DEFEND → capture freeze + identification
    • Contested logging: System stands still, AGENT logs in repeatedly
    • Auto-resume after System leaves
    • Capture complete → node transitions to Opposition
    • Real-time radius growth
    • Capture reset if everyone leaves for a long time
    • Mesh connection between captured nodes (green lines)
    • QR verification: System guesses AGENT-ID
    • Opposition victory via ALEX ↔ BEATRICE chain

TIME_SCALE adjusts the speed (1.0 = real time).
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

# The demo relies on the built-in scheduler just like the real game:
# - capture completes after CAPTURE_TIME_SEC
# - radius grows over RADIUS_GROWTH_INTERVAL_SEC intervals
# The demo only moves fakes and logs the process

# Parameters from config.py
try:
    import sys as _sys
    _sys.path.insert(0, ".")
    import config as _game_config
    REAL_CAPTURE_TIME = int(getattr(_game_config, "CAPTURE_TIME_SEC", 90))
    GROW_INTERVAL = int(getattr(_game_config, "RADIUS_GROWTH_INTERVAL_SEC", 10))
except Exception:
    REAL_CAPTURE_TIME = 90
    GROW_INTERVAL = 10


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


async def get_node_radius(node_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT current_radius_m FROM nodes WHERE id=?", (node_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ── Actions via HTTP ───────────────────────────────────────────────────

async def move_to(pid, lat, lon):
    await http_post("/api/location", {"player_id": pid, "lat": lat, "lon": lon})


async def smooth_walk(pid, fr_lat, fr_lon, to_lat, to_lon, steps=WALK_STEPS, delay=WALK_DELAY):
    for i in range(1, steps + 1):
        t = i / steps
        await move_to(pid, fr_lat + (to_lat - fr_lat) * t, fr_lon + (to_lon - fr_lon) * t)
        await sleep_scaled(delay)


async def start_capture(pid, node_id):
    await http_post("/api/admin/fake_capture", {"player_id": pid, "node_id": node_id})


async def freeze_and_identify(pid, node_id):
    await http_post("/api/admin/fake_defend", {"player_id": pid, "node_id": node_id})


async def complete_capture(node_id):
    """OPTIONAL: instantly complete capture (used only in Act 6 for reset)."""
    await http_post("/api/admin/fake_complete_capture", {"node_id": node_id})


async def set_owner(node_id, owner):
    await http_post("/api/admin/set_owner", {"node_id": node_id, "owner": owner})


async def interrupt_capture(node_id):
    await http_post("/api/admin/fake_interrupt_capture", {"node_id": node_id})


async def verify_player(sys_id, opp_id, guessed_anon):
    return await http_post("/api/admin/fake_verify", {
        "system_player_id": sys_id,
        "scanned_player_id": opp_id,
        "guessed_anonymous_id": guessed_anon,
    })


async def _background_pinger(player_id, lat, lon, stop_event, interval=8):
    """Background task — constantly pings player geolocation so the scheduler can see them."""
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
    """Starts background pinging. Returns (task, stop_event) to stop it later."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(_background_pinger(player_id, lat, lon, stop_event))
    return task, stop_event


async def stop_pinger(task, stop_event):
    """Stops background pinging."""
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=2)
    except Exception:
        task.cancel()


async def hold_position_for_capture(node, player_id):
    """
    Player stands near the node → the scheduler itself will:
    1. Complete the capture after CAPTURE_TIME_SEC seconds
    2. Start growing the radius over RADIUS_GROWTH_INTERVAL_SEC intervals
    We just wait and log what happens.
    """
    total_wait = REAL_CAPTURE_TIME + 40  # + buffer for scheduler checks
    log(f"⏳ {node['name']}: scheduler itself will complete capture in {REAL_CAPTURE_TIME} sec", "⌛")
    elapsed = 0
    while elapsed < total_wait:
        # Update player position every 10 sec (geoping so the scheduler sees them)
        await move_to(player_id, node["lat"], node["lon"])
        # Check status
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT owner, current_radius_m FROM nodes WHERE id=?",
                                    (node["id"],)) as cur:
                row = await cur.fetchone()
        if row and row["owner"] == "opposition":
            log(f"  ✅ {node['name']} transferred to Opposition! Radius: {int(row['current_radius_m'])}m", "🎉")
            break
        if elapsed % 20 == 0:
            log(f"  ⏱  {elapsed} sec / {REAL_CAPTURE_TIME} sec of capture elapsed...", "")
        await sleep_scaled(10)
        elapsed += 10


async def hold_for_radius_growth(node, player_id, hold_seconds=60):
    """
    Player stands near the captured node → the scheduler grows the radius automatically.
    We log the process every GROW_INTERVAL seconds.
    """
    log(f"📍 {node['name']}: {player_id} holds position — scheduler grows the radius", "🌐")
    elapsed = 0
    while elapsed < hold_seconds:
        await move_to(player_id, node["lat"], node["lon"])
        await sleep_scaled(GROW_INTERVAL + 2)
        elapsed += GROW_INTERVAL + 2
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT current_radius_m FROM nodes WHERE id=?",
                                    (node["id"],)) as cur:
                r = (await cur.fetchone())[0]
        log(f"  📈 Radius of {node['name']}: {int(r)}m", "")


# ── SCENARIO ─────────────────────────────────────────────────────────────────

async def main():
    global session
    session = aiohttp.ClientSession()

    print("\n" + "=" * 60)
    print("  🎬 GPS STRATEGY — FULL DEMO")
    print("=" * 60)
    print(f"  TIME_SCALE: {TIME_SCALE}x")
    print(f"  Open /presentation in your browser and watch!")
    print("=" * 60 + "\n")

    try:
        async with session.get(f"{SERVER_URL}/api/game",
                               timeout=aiohttp.ClientTimeout(total=3)) as r: await r.json()
    except Exception:
        print("❌ Server is unavailable on localhost:8001. Run python3 bot.py first")
        await session.close(); return

    nodes = await get_nodes()
    if len(nodes) < 4:
        print(f"❌ Only {len(nodes)} nodes in DB. Need at least 4 (ALEX, BEATRICE + 2 intermediate nodes).")
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

    # Radius for intermediate nodes. With the new logic: ALEX/BEATRICE do not grow,
    # the ADJACENT regular node must reach their center with its radius.
    # Therefore, we calculate distances from ALEX to the first node, and from BEATRICE to the last node.
    # We take the maximum of all adjacent distances.
    chain_path = [target_a] + other_nodes + [target_b]
    max_gap = max(haversine(chain_path[i]["lat"], chain_path[i]["lon"],
                            chain_path[i+1]["lat"], chain_path[i+1]["lon"])
                  for i in range(len(chain_path)-1))
    CHAIN_RADIUS = max(int(max_gap * 1.15), 60)

    log(f"Map: {len(nodes)} nodes. Goal: connect {target_a['name']} ↔ {target_b['name']}", "🗺")
    log(f"Intermediate nodes: {[n['name'] for n in other_nodes]}", "🗺")
    log(f"Max gap: {int(max_gap)}m → regular nodes radius after capture: {CHAIN_RADIUS}m", "📏")
    log(f"ALEX and BEATRICE remain with base radius — they are anchor nodes", "⚓")
    await sleep_scaled(2)

    # ═══ PREPARATION ════════════════════════════════════════════════════════
    log("─" * 50, ""); log("PREPARATION: ALEX and BEATRICE belong to Opposition right away (anchors)", "⚙️"); log("─" * 50, "")
    await set_owner(target_a["id"], "opposition")
    log(f"🔴 {target_a['name']} → Opposition (anchor, radius does not grow)", "⚓")
    await sleep_scaled(2)
    await set_owner(target_b["id"], "opposition")
    log(f"🔴 {target_b['name']} → Opposition (anchor, radius does not grow)", "⚓")
    await sleep_scaled(ACT_PAUSE)

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE game_state SET active=1, current_phase=1, phase_started_at=? WHERE id=1",
                          (datetime.now().isoformat(),))
        await conn.commit()
    log("Game activated", "🚀"); await sleep_scaled(2)

    # ═══ ACT 1 — REGISTRATION ═══════════════════════════════════════════════
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

    # Starting positions (Opposition is far from their nodes to prevent uncontrolled radius growth)
    center_lat = (target_a["lat"] + target_b["lat"]) / 2
    center_lon = (target_a["lon"] + target_b["lon"]) / 2
    log("Players take starting positions", "📍")
    await move_to(opp_ids["ALICE"],   target_a["lat"] + 0.0030, target_a["lon"] - 0.0030); await sleep_scaled(1)
    await move_to(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030); await sleep_scaled(1)
    await move_to(sys_ids["BOB"],     center_lat + 0.0015, center_lon - 0.0010); await sleep_scaled(1)
    await move_to(sys_ids["DIANA"],   center_lat - 0.0015, center_lon + 0.0010); await sleep_scaled(ACT_PAUSE)

    first_node = other_nodes[0]

    # ═══ ACT 2 — CAPTURE ════════════════════════════════════════════════════
    log("─" * 50, ""); log(f"ACT 2: ALICE attacks {first_node['name']}", "🎭"); log("─" * 50, "")
    log(f"🔴 ALICE moves into the circle of {first_node['name']}...", "RUN")
    await smooth_walk(opp_ids["ALICE"], target_a["lat"] + 0.0030, target_a["lon"] - 0.0030,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log(f"🔴 ALICE is inside the circle — starts capturing {first_node['name']} (holding)", "⚡")
    await start_capture(opp_ids["ALICE"], first_node["id"])
    log("🚨 Server sends 'Node attacked' push notification to real System players", "📡")

    # 🔁 IMPORTANT: launch background pinging for ALICE — while it runs, the geolocation is always fresh
    # and the scheduler will not reset the capture while ALICE "stands" on the node
    alice_pinger, alice_stop = start_pinger(opp_ids["ALICE"], first_node["lat"], first_node["lon"])
    log("📡 Background ping for ALICE is running — geolocation will stay updated", "")

    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 3 — DEFENSE + CONTESTED LOGGING ══════════════════════════════
    log("─" * 50, ""); log("ACT 3: BOB intercepts + repeated logging", "🎭"); log("─" * 50, "")
    log(f"⚙️ BOB runs inside the circle of {first_node['name']}", "RUN")
    await smooth_walk(sys_ids["BOB"], center_lat + 0.0015, center_lon - 0.0010,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log("⚙️ BOB is inside the circle → DEFEND → capture is FROZEN, AGENT is logged", "🛡")
    await freeze_and_identify(sys_ids["BOB"], first_node["id"])

    # Ping BOB too — so the scheduler sees him within radius and contested logging works
    bob_pinger, bob_stop = start_pinger(sys_ids["BOB"], first_node["lat"], first_node["lon"])
    await sleep_scaled(ACT_PAUSE)

    log("⚙️ BOB remains near the frozen node — scheduler repeatedly logs AGENT every 30 sec", "📡")
    log("⏳ Waiting 35 sec to see the repeated identification...", "⏳")
    await sleep_scaled(35)
    log("📋 AGENT recorded again — System accumulates target data", "✅")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 4 — SYSTEM LEAVES, AUTO-RESUME ══════════════════════════════
    log("─" * 50, ""); log("ACT 4: BOB leaves the circle → capture resumes", "🎭"); log("─" * 50, "")
    log(f"⚙️ BOB exits the node CIRCLE (~330m) — freeze will be lifted", "RUN")

    # Stop BOB's ping since he is leaving
    await stop_pinger(bob_pinger, bob_stop)

    # Move 330m away from first_node — definitely outside of any radius
    bob_far_lat = first_node["lat"] + 0.003
    bob_far_lon = first_node["lon"] - 0.003
    await smooth_walk(sys_ids["BOB"], first_node["lat"], first_node["lon"],
                      bob_far_lat, bob_far_lon, steps=WALK_STEPS, delay=WALK_DELAY)
    log("⏳ ALICE remains INSIDE the circle (bg-ping works) → waiting 35 sec for unfreeze...", "⏳")
    await sleep_scaled(35)
    log("▶️ Scheduler unfroze the capture — node is orange again, timer is ticking", "✅")
    await sleep_scaled(2)

    # ═══ ACT 5 — CAPTURE COMPLETION + RADIUS GROWTH ══════════════════════════
    log("─" * 50, ""); log("ACT 5: natural capture completion (scheduler)", "🎭"); log("─" * 50, "")
    await hold_position_for_capture(first_node, opp_ids["ALICE"])
    log("─" * 50, ""); log("ACT 5b: ALICE holds position → scheduler grows radius", "🎭"); log("─" * 50, "")
    await hold_for_radius_growth(first_node, opp_ids["ALICE"], hold_seconds=40)

    # Stop ALICE's bg-ping — she will move to other nodes
    await stop_pinger(alice_pinger, alice_stop)
    await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 6 — RESET: Opposition left for a long time ════════════════════════════════
    if len(other_nodes) >= 3:
        sacrifice_node = other_nodes[1]
        log("─" * 50, ""); log(f"ACT 6: Automatic capture reset demonstration", "🎭"); log("─" * 50, "")
        log("Reset condition: Opposition left node radius for longer than ABANDON_TIMEOUT_SEC", "📖")

        log(f"🔴 CHARLIE starts capturing {sacrifice_node['name']}", "⚡")
        await move_to(opp_ids["CHARLIE"], sacrifice_node["lat"], sacrifice_node["lon"])
        await start_capture(opp_ids["CHARLIE"], sacrifice_node["id"])
        await sleep_scaled(5)

        log(f"⚙️ DIANA freezes the capture", "🛡")
        await move_to(sys_ids["DIANA"], sacrifice_node["lat"], sacrifice_node["lon"])
        await freeze_and_identify(sys_ids["DIANA"], sacrifice_node["id"])
        await sleep_scaled(5)

        log("⚙️ DIANA leaves (System does not affect reset — only freezing)", "RUN")
        await smooth_walk(sys_ids["DIANA"], sacrifice_node["lat"], sacrifice_node["lon"],
                          center_lat + 0.002, center_lon, steps=4, delay=1)

        log("🔴 CHARLIE also goes FAR AWAY — outside node radius", "RUN")
        # Move ~330m away — definitely outside any radius
        far_lat = sacrifice_node["lat"] + 0.003
        far_lon = sacrifice_node["lon"] + 0.003
        await smooth_walk(opp_ids["CHARLIE"], sacrifice_node["lat"], sacrifice_node["lon"],
                          far_lat, far_lon, steps=5, delay=1)

        # Wait until scheduler notices Opposition left and resets the capture
        # ABANDON_TIMEOUT_SEC from config (defaulting to 30 sec) + buffer for check loop
        try:
            abandon_timeout = int(getattr(_game_config, "ABANDON_TIMEOUT_SEC", 180))
        except Exception:
            abandon_timeout = 180
        total_wait = abandon_timeout + 35  # buffer for scheduler cycle
        log(f"⏳ Waiting {abandon_timeout} sec (ABANDON_TIMEOUT_SEC) + 35 sec for scheduler cycle...", "⌛")
        for elapsed in range(0, total_wait, 10):
            # Feed geolocation to ensure CHARLIE is definitely "tracked" far away
            await move_to(opp_ids["CHARLIE"], far_lat, far_lon)
            # Check node state
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT owner, capture_started_at FROM nodes WHERE id=?",
                    (sacrifice_node["id"],)
                ) as cur:
                    row = await cur.fetchone()
            if row and row["capture_started_at"] is None and row["owner"] == "system":
                log(f"✅ Scheduler reset the capture — node {sacrifice_node['name']} is blue again", "🔄")
                break
            log(f"  ⏱  {elapsed}/{total_wait} sec, node is still in process...", "")
            await sleep_scaled(10)
        await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 7 — CHARLIE from the other side ══════════════════════════════════════
    if len(other_nodes) >= 2:
        second_node = other_nodes[-1]
        log("─" * 50, ""); log(f"ACT 7: CHARLIE attacks from the flank {second_node['name']}", "🎭"); log("─" * 50, "")
        await smooth_walk(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030,
                          second_node["lat"], second_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
        log(f"🔴 CHARLIE captures {second_node['name']}", "⚡")
        await start_capture(opp_ids["CHARLIE"], second_node["id"])
        await sleep_scaled(2)
        await hold_position_for_capture(second_node, opp_ids["CHARLIE"])
        await hold_for_radius_growth(second_node, opp_ids["CHARLIE"], hold_seconds=30)
        await sleep_scaled(ACT_PAUSE)

    # ═══ ACT 8 — QR VERIFICATION ════════════════════════════════════════════
    log("─" * 50, ""); log("ACT 8: QR verification — System tries to identify ALICE", "🎭"); log("─" * 50, "")
    log("⚙️ BOB 'scans QR' of ALICE and tries to guess her AGENT-ID", "📷")
    await sleep_scaled(2)
    # Guesses correctly
    correct_anon = opp_anons["ALICE"]
    log(f"⚙️ BOB enters guess: {correct_anon} (correct)", "🧠")
    res = await verify_player(sys_ids["BOB"], opp_ids["ALICE"], correct_anon)
    if res.get("correct"):
        log(f"✅ CORRECT! ALICE = {correct_anon}. +15 points to System", "🎯")
    else:
        log(f"❌ Failed to verify (might have already been verified)", "⚠️")
    await sleep_scaled(ACT_PAUSE)

    log("⚙️ DIANA tries to verify CHARLIE — but makes a mistake", "📷")
    await sleep_scaled(2)
    fake_guess = "AGENT_XXXX"
    log(f"⚙️ DIANA enters incorrect guess: {fake_guess}", "🧠")
    res = await verify_player(sys_ids["DIANA"], opp_ids["CHARLIE"], fake_guess)
    if res.get("ok") and not res.get("correct"):
        log(f"❌ Incorrect. Correct was: {opp_anons['CHARLIE']}", "🕵")
        log("CHARLIE remained anonymous — Opposition maintained cover", "✨")
    await sleep_scaled(ACT_PAUSE)

    # ═══ FINALE ══════════════════════════════════════════════════════════════
    log("─" * 50, ""); log("FINALE: chain built — checking victory", "🏆"); log("─" * 50, "")
    log("Scheduler will check the chain and grant victory...", "⏳")
    await sleep_scaled(30)

    log("─" * 50, ""); log("🎬 SCENARIO COMPLETE", "✨")
    log("/admin_replay — full chronology", "📜")
    log("/score — final score", "📊")
    log("/admin_reset — reset nodes + CLEAR LOGS", "🔄")
    log("/admin_unspawn all — remove fake players", "🗑")
    log("─" * 50, "")
    await session.close()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Interrupted"); sys.exit(0)