from aiogram import Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.filters import Command
import aiosqlite

import database as db
import config

router = Router()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == config.ADMIN_ID


@router.message(Command("admin_qr"))
async def cmd_admin_qr(message: Message):
    """Send the QR code of any Opposition player to the admin.
    Usage:
        /admin_qr ALICE       — match by FAKE_<name> or real username substring
        /admin_qr AGENT_44C4  — match by anonymous AGENT-ID
    Useful when System lost the QR or for testing the finale scan flow."""
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Usage: `/admin_qr <name | AGENT_XXXX>`", parse_mode="Markdown")
        return
    query = parts[1].strip().upper()

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        # Match on AGENT-ID exact, FAKE_<name>, or any username substring —
        # admin doesn't need to remember whether the player is a fake or real.
        async with conn.execute(
            "SELECT telegram_id, username, anonymous_id, team FROM players "
            "WHERE team='opposition' AND "
            "(UPPER(anonymous_id)=? OR UPPER(username)=? OR UPPER(username) LIKE ?)",
            (query, query, f"%{query}%")
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await message.answer(f"No Opposition player matches `{query}`.", parse_mode="Markdown")
        return
    if len(rows) > 1:
        names = ", ".join(f"{r['username']} ({r['anonymous_id']})" for r in rows)
        await message.answer(f"Multiple matches — be more specific:\n{names}")
        return

    p = rows[0]
    if not p["anonymous_id"]:
        await message.answer(f"*{p['username']}* has no AGENT-ID assigned.", parse_mode="Markdown")
        return

    # Build the same QR string the player sees via /myqr, so finale scan_verify
    # accepts it identically.
    from handlers.common import _make_qr_bytes
    qr_data = f"GPSGAME:PLAYER:{p['telegram_id']}:{p['anonymous_id']}"
    try:
        qr_bytes = _make_qr_bytes(qr_data)
        await message.answer_photo(
            BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=(f"🔲 QR for *{p['username']}*\n"
                     f"AGENT-ID: `{p['anonymous_id']}`\n"
                     f"Telegram ID: `{p['telegram_id']}`"),
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"QR generation failed: {e}")


@router.message(Command("admin_addnode"))
async def cmd_add_node(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.replace("/admin_addnode", "").strip().split(";")
    if len(parts) < 3:
        await message.answer(
            "Format: /admin_addnode Name;lat;lon\n"
            "Or with type: /admin_addnode Name;lat;lon;type;radius\n"
            "Types: node (default), hub, core"
        )
        return
    name = parts[0].strip()
    try:
        lat = float(parts[1].strip())
        lon = float(parts[2].strip())
        node_type = parts[3].strip() if len(parts) > 3 else "node"
        radius = float(parts[4].strip()) if len(parts) > 4 else 80
    except ValueError:
        await message.answer("Invalid coordinates.")
        return

    await db.add_node(name, lat, lon, node_type, radius)
    await message.answer(f"✅ *{name}* added\nType: {node_type} | Radius: {radius}m", parse_mode="Markdown")


@router.message(Command("admin_nodes"))
async def cmd_list_nodes(message: Message):
    if not is_admin(message.from_user.id): return
    nodes = await db.get_all_nodes()
    if not nodes:
        await message.answer("No nodes found.")
        return
    lines = ["*All nodes:*\n"]
    for n in nodes:
        ntype = n["node_type"] if "node_type" in n.keys() else "node"
        icon = "⬛" if ntype == "core" else "🔷" if ntype == "hub" else "🔵" if n["owner"] == "system" else "🔴"
        frozen = " ⏸" if n["capture_frozen"] else ""
        capture = " ⚔️" if n["capture_started_at"] else ""
        lines.append(f"{icon} #{n['id']} *{n['name']}* [{ntype}] — {n['owner']}{capture}{frozen}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("admin_start"))
async def cmd_start_game(message: Message):
    if not is_admin(message.from_user.id): return
    nodes = await db.get_all_nodes()
    if not nodes:
        await message.answer("Add nodes before starting.")
        return

    # Anchors belong to Opposition by design — they represent established
    # Opposition cells at the city's edges. Without this, ALEX and BEATRICE
    # would start owned by System (the default for any newly placed node),
    # and Opposition could never close the chain. The demo did this by hand
    # via set_owner; the real /admin_start must do it automatically.
    # Also set capture_progress=100 so the anchor's effective radius is at
    # max from the very first second (they don't have to be "captured").
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "UPDATE nodes SET owner='opposition', capture_progress=100, "
            "current_radius_m=base_radius_m "
            "WHERE UPPER(name) LIKE '%ALEX%' OR UPPER(name) LIKE '%BEATRICE%'"
        )
        await conn.commit()

    await db.set_game_active(True, phase=1)
    all_players = await db.get_all_players()
    for p in all_players:
        if p["telegram_id"] < 0: continue  # skip fakes
        try:
            await message.bot.send_message(
                p["telegram_id"],
                f"🚀 *The game has started! Phase 1 of {config.PHASE_COUNT}*\n\nOpen the map: /map",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    await message.answer(f"✅ Game started. Players: {len(all_players)}\nAnchors (ALEX/BEATRICE) handed to Opposition.")


@router.message(Command("admin_reset"))
async def cmd_reset(message: Message):
    if not is_admin(message.from_user.id): return
    await db.set_game_active(False, phase=0)
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("""
            UPDATE nodes SET
                owner = 'system',
                capture_started_at = NULL,
                capturing_player_id = NULL,
                capture_elapsed_sec = 0,
                capture_frozen = 0,
                freeze_started_at = NULL,
                current_radius_m = base_radius_m
        """)
        # Clear all event logs so that /admin_replay and /presentation panel are empty
        await conn.execute("DELETE FROM captures")
        await conn.execute("DELETE FROM identifications")
        await conn.execute("DELETE FROM verifications")
        # Reset puzzles
        await conn.execute("DELETE FROM puzzle_sessions")
        await conn.execute("UPDATE nodes SET capture_progress=0, puzzles_solved=''")
        await conn.commit()
    await message.answer("✅ Game reset. All nodes returned to System, logs and puzzles cleared.")


@router.message(Command("admin_setnodes"))
async def cmd_set_povo_nodes(message: Message):
    """Quick command — FBK/Povo test nodes."""
    if not is_admin(message.from_user.id): return

    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM nodes")
        await conn.commit()

    nodes = [
        ("FBK CORE",      46.0619, 11.1502, "core", 150),
        ("HUB NORD",      46.0645, 11.1498, "hub",  120),
        ("HUB SUD",       46.0595, 11.1508, "hub",  120),
        ("HUB EST",       46.0618, 11.1535, "hub",  120),
        ("HUB OVEST",     46.0620, 11.1468, "hub",  120),
        ("POVO CENTRO",   46.0650, 11.1510, "node", 80),
        ("VIA SOMMARIVE", 46.0628, 11.1495, "node", 80),
        ("UNITN POVO",    46.0635, 11.1520, "node", 80),
        ("PARCO POVO",    46.0605, 11.1490, "node", 80),
        ("FERMATA BUS",   46.0598, 11.1515, "node", 80),
        ("LABORATORI",    46.0612, 11.1540, "node", 80),
        ("CAFFETTERIA",   46.0625, 11.1478, "node", 80),
        ("NODE ALEX",     46.0640, 11.1460, "node", 80),
        ("NODE BEATRICE", 46.0600, 11.1550, "node", 80),
    ]
    for name, lat, lon, ntype, radius in nodes:
        await db.add_node(name, lat, lon, ntype, radius)

    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT id FROM nodes WHERE name = 'NODE ALEX'") as cur:
            alex = await cur.fetchone()
        async with conn.execute("SELECT id FROM nodes WHERE name = 'NODE BEATRICE'") as cur:
            beatrice = await cur.fetchone()
        if alex and beatrice:
            await conn.execute(
                "UPDATE game_state SET target_node_a = ?, target_node_b = ? WHERE id = 1",
                (alex[0], beatrice[0])
            )
            await conn.commit()

    await message.answer(f"✅ Added {len(nodes)} nodes for FBK/Povo\nTargets: NODE ALEX → NODE BEATRICE")


@router.message(Command("admin_debug"))
async def cmd_debug(message: Message):
    if not is_admin(message.from_user.id): return

    from datetime import datetime

    nodes = await db.get_all_nodes()
    players = await db.get_all_players()
    game_state = await db.get_game_state()
    all_ids = await db.get_all_identifications()
    all_verifs = await db.get_all_verifications()

    gs_text = f"🎮 Game: {'active' if game_state and game_state['active'] else 'not active'}"
    if game_state and game_state["active"]:
        gs_text += f" | Phase {game_state['current_phase']}/{config.PHASE_COUNT}"

    node_lines = ["*Nodes:*"]
    for n in nodes:
        n = dict(n)
        if n["capture_started_at"] and not n["capture_frozen"]:
            started = datetime.fromisoformat(n["capture_started_at"])
            elapsed = int((datetime.now() - started).total_seconds())
            remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
            cap_info = f"⚔️ {elapsed}s ({remaining}s remaining)"
        elif n["capture_frozen"]:
            cap_info = f"⏸ frozen ({int(n['capture_elapsed_sec'] or 0)}s)"
        else:
            cap_info = "—"
        owner_icon = "🔵" if n["owner"] == "system" else "🔴"
        node_lines.append(f"{owner_icon} *{n['name']}* r={int(n['current_radius_m'])}m | {cap_info}")

    player_lines = ["*Players:*"]
    system_count = sum(1 for p in players if p["team"] == "system")
    opposition_count = sum(1 for p in players if p["team"] == "opposition")
    player_lines.append(f"⚙️ System: {system_count} | 🔴 Opposition: {opposition_count}")

    # FIX: unique agents instead of COUNT(*)
    unique_agents = len(set(r["anonymous_id"] for r in all_ids if dict(r).get("anonymous_id")))
    correct_verifs = len([v for v in all_verifs if v["correct"]])
    id_lines = [
        f"*Agents spotted:* {unique_agents}",
        f"*Correct verifications:* {correct_verifs}/{len(all_verifs)}"
    ]

    text = (
        gs_text + "\n\n" +
        "\n".join(node_lines) + "\n\n" +
        "\n".join(player_lines) + "\n\n" +
        "\n".join(id_lines)
    )
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("admin_map"))
async def cmd_admin_map(message: Message):
    """Open interactive map editor."""
    if not is_admin(message.from_user.id): return

    server_url = getattr(config, 'SERVER_URL', None)
    if not server_url:
        await message.answer(
            "Add to config.py:\n`SERVER_URL = 'https://your-cloudflare-url'`",
            parse_mode="Markdown"
        )
        return

    admin_url = server_url.rstrip('/') + '/admin'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Open Map Editor", url=admin_url)]
    ])
    await message.answer(
        "*Admin Map Editor*\n\n"
        "• Click on the map to add nodes\n"
        "• Click on a node to delete it\n"
        "• See game status in real-time\n"
        "• The RESET ALL button resets the game\n\n"
        f"`{admin_url}`",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@router.message(Command("admin_replay"))
async def cmd_admin_replay(message: Message):
    """Timeline of all game events — for review after the test."""
    if not is_admin(message.from_user.id): return
    from datetime import datetime as _dt
    import aiosqlite as _ai

    events = []

    async with _ai.connect(db.DB_PATH) as conn:
        conn.row_factory = _ai.Row

        # Captures
        async with conn.execute(
            """SELECT c.*, p.username, p.anonymous_id, n.name AS node_name
               FROM captures c
               LEFT JOIN players p ON p.telegram_id = c.player_id
               LEFT JOIN nodes n ON n.id = c.node_id"""
        ) as cur:
            for row in await cur.fetchall():
                events.append({
                    "time": row["started_at"],
                    "type": "capture_start",
                    "text": f"⚡ @{row['username'] or '?'} started capturing *{row['node_name'] or '?'}*"
                })

        # Identifications
        async with conn.execute(
            """SELECT i.*, p.username AS sys_username, n.name AS node_name
               FROM identifications i
               LEFT JOIN players p ON p.telegram_id = i.system_player_id
               LEFT JOIN nodes n ON n.id = i.node_id"""
        ) as cur:
            for row in await cur.fetchall():
                events.append({
                    "time": row["identified_at"],
                    "type": "ident",
                    "text": f"🆔 @{row['sys_username'] or '?'} logged `{row['anonymous_id']}` at *{row['node_name'] or '?'}*"
                })

        # Verifications
        async with conn.execute(
            """SELECT v.*, sp.username AS sys_username, hp.username AS opp_username
               FROM verifications v
               LEFT JOIN players sp ON sp.telegram_id = v.system_player_id
               LEFT JOIN players hp ON hp.telegram_id = v.scanned_player_id"""
        ) as cur:
            for row in await cur.fetchall():
                check = "✅" if row["correct"] else "❌"
                events.append({
                    "time": row["verified_at"],
                    "type": "verify",
                    "text": f"{check} @{row['sys_username'] or '?'} verified @{row['opp_username'] or '?'}: guessed {row['guessed_anonymous_id']}, actual was {row['real_anonymous_id']}"
                })

    if not events:
        await message.answer("📜 No events found. Play a game first.")
        return

    # Sort by time
    events.sort(key=lambda e: e["time"])

    # Format by time phases
    def fmt(iso):
        try: return _dt.fromisoformat(iso).strftime("%H:%M:%S")
        except: return iso[:8]

    lines = [f"📜 Game Timeline ({len(events)} events)\n"]
    for e in events:
        lines.append(f"{fmt(e['time'])}  {e['text']}")

    # Telegram limit 4096 chars — split if necessary
    text = "\n".join(lines)
    # Without Markdown — special characters in username/anonymous_id can break the parser
    if len(text) > 4000:
        chunks = []
        cur = lines[0] + "\n"
        for line in lines[1:]:
            if len(cur) + len(line) > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        if cur: chunks.append(cur)
        for chunk in chunks:
            await message.answer(chunk)
    else:
        await message.answer(text)


@router.message(Command("admin_presentation"))
async def cmd_admin_presentation(message: Message):
    """Open the map in presentation mode for video recording."""
    if not is_admin(message.from_user.id): return

    server_url = getattr(config, 'SERVER_URL', None)
    if not server_url:
        await message.answer("Add SERVER_URL to config.py")
        return

    pres_url = server_url.rstrip('/') + '/presentation'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Open Presentation Mode", url=pres_url)]
    ])
    await message.answer(
        "*Presentation Mode* — for video recording:\n\n"
        "• ALL players are visible with names and teams\n"
        "• Movement trajectories (latest positions)\n"
        "• Large labels\n"
        "• No buttons — spectator mode\n\n"
        "Open in a PC browser and record via OBS or built-in screen recording.\n\n"
        f"`{pres_url}`",
        reply_markup=kb, parse_mode="Markdown"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FAKE PLAYERS SIMULATION — for solo testing via bot
# ─────────────────────────────────────────────────────────────────────────────

import random as _rand
from datetime import datetime as _dt


def _gen_anon():
    return "AGENT_" + "".join(_rand.choices("0123456789ABCDEF", k=4))


async def _find_fake_by_name(name: str):
    name = name.upper().strip()
    if not name.startswith("FAKE_"):
        name = "FAKE_" + name
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM players WHERE username = ? AND telegram_id < 0", (name,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _find_node_by_name(name: str):
    name = name.upper().strip()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM nodes WHERE UPPER(name) = ?", (name,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


@router.message(Command("admin_spawn"))
async def cmd_spawn(message: Message):
    """Create a fake player: /admin_spawn <team> <name>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "*Usage:*\n"
            "`/admin_spawn opposition ALICE`\n"
            "`/admin_spawn system BOB`",
            parse_mode="Markdown"
        )
        return

    team_input = parts[1].lower()
    name = parts[2].upper()

    if team_input in ("opp", "opposition", "opps"):
        team = "opposition"
    elif team_input in ("sys", "system"):
        team = "system"
    else:
        await message.answer("Team must be either 'opposition' or 'system'")
        return

    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute("SELECT MIN(telegram_id) FROM players") as cur:
            min_id = (await cur.fetchone())[0] or 0
        fake_id = min(min_id - 1, -1)

    anon = _gen_anon()
    full_name = f"FAKE_{name}"

    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO players (telegram_id, username, team, anonymous_id) VALUES (?, ?, ?, ?)",
            (fake_id, full_name, team, anon)
        )
        await conn.commit()

    team_icon = "⚙️" if team == "system" else "🔴"
    await message.answer(
        f"✅ Fake player *{full_name}* created\n"
        f"Team: {team_icon} {team}\n"
        f"ID: `{fake_id}` · Anon: `{anon}`\n\n"
        f"Set their coordinates:\n"
        f"`/admin_move {name} <lat> <lon>`",
        parse_mode="Markdown"
    )


@router.message(Command("admin_move"))
async def cmd_move(message: Message):
    """Move a fake player: /admin_move <name> <lat> <lon>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 4:
        await message.answer("Usage: `/admin_move ALICE 46.0619 11.1502`", parse_mode="Markdown")
        return

    name = parts[1]
    try:
        lat = float(parts[2])
        lon = float(parts[3])
    except ValueError:
        await message.answer("Invalid coordinates.")
        return

    fake = await _find_fake_by_name(name)
    if not fake:
        await message.answer(f"Fake FAKE_{name.upper()} not found. Create one via /admin_spawn")
        return

    await db.update_player_location(fake["telegram_id"], lat, lon)
    try:
        import server as srv
        srv._location_history[fake["telegram_id"]].append((lat, lon, _dt.now().isoformat()))
    except Exception:
        pass

    await message.answer(
        f"📍 *{fake['username']}* moved to `{lat:.5f}, {lon:.5f}`",
        parse_mode="Markdown"
    )


@router.message(Command("admin_fake_capture"))
async def cmd_fake_capture(message: Message):
    """Fake Opposition fully captures a node, bypassing the puzzle UI:
    /admin_fake_capture <fake> <node>
    Internally this drives the same code path as solving two puzzles —
    progress jumps 0 → 80 → 100, owner becomes opposition, current radius
    snaps to the node's per-node max_radius_m. Useful for solo testing
    without spinning up the puzzle WebApp."""
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: `/admin_fake_capture ALICE TEST1`", parse_mode="Markdown")
        return

    fake = await _find_fake_by_name(parts[1])
    if not fake or fake["team"] != "opposition":
        await message.answer("Fake not found or not on Opposition team.")
        return

    node = await _find_node_by_name(parts[2])
    if not node:
        await message.answer("Node not found.")
        return
    if node["owner"] == "opposition" and (node.get("capture_progress") or 0) >= 100:
        await message.answer(f"Node *{node['name']}* is already fully captured.", parse_mode="Markdown")
        return

    # Park the fake at the node so any subsequent location-based check
    # (defend, finale rendezvous, etc.) sees them where they "captured" from.
    await db.update_player_location(fake["telegram_id"], node["lat"], node["lon"])

    # Drive progress 0 → 80 → 100 through the puzzle path, marking two
    # puzzles solved so puzzles_solved reflects reality.
    await db.update_node_capture_progress(node["id"], 80, owner="opposition")
    await db.mark_puzzle_solved(node["id"], "untangle")
    await db.update_node_capture_progress(node["id"], 100, owner="opposition")
    await db.mark_puzzle_solved(node["id"], "sudoku")

    await message.answer(
        f"⚡️ *{fake['username']}* captured *{node['name']}* (100%).",
        parse_mode="Markdown"
    )

    # Instant chain check — same path as a real puzzle submit.
    try:
        from server import check_victory_now, broadcast_map_update
        await broadcast_map_update()
        await check_victory_now()
    except Exception as e:
        print(f"[admin_fake_capture] post-capture hook: {e}")

    system_players = await db.get_all_players("system")
    for sp in system_players:
        if sp["telegram_id"] < 0: continue
        try:
            await message.bot.send_message(
                sp["telegram_id"],
                f"🚨 *{node['name']}* fell to Opposition.",
                parse_mode="Markdown"
            )
        except Exception:
            pass


@router.message(Command("admin_finale_debug"))
async def cmd_finale_debug(message: Message):
    """Diagnose the Final Scene UI: what stage is the game in, how many
    Opposition players the API will list, and whether anonymous_id is
    actually set on them. Run this when the /finale lineup looks empty
    despite fakes being spawned."""
    if not is_admin(message.from_user.id): return

    state = await db.get_game_state()
    stage = state["finale_stage"] if state else None

    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT telegram_id, username, team, anonymous_id "
            "FROM players WHERE team='opposition'"
        ) as cur:
            opp = [dict(r) for r in await cur.fetchall()]

    lines = [
        "*Final Scene diagnostic:*",
        f"game.active           = {bool(state['active']) if state else 'n/a'}",
        f"game.finale_stage     = {stage or 'none'}",
        f"opposition players    = {len(opp)}",
    ]
    if not opp:
        lines.append("\n❌ No opposition players in DB — the /finale lineup will be empty.")
        lines.append("Did /admin_unspawn all run, or were no fakes spawned?")
    else:
        lines.append("")
        for p in opp:
            anon = p["anonymous_id"] or "❌MISSING"
            lines.append(f"  • {p['username']} (id={p['telegram_id']}) → {anon}")
        missing = [p for p in opp if not p["anonymous_id"]]
        if missing:
            lines.append("\n⚠️ Some players have no anonymous_id — they will show up but cannot be guessed.")
    if not stage:
        lines.append("\n⚠️ Game is not in finale. /finale will show 'Not in finale' instead of the lineup.")
        lines.append("Trigger finale via /admin_check_chain or by completing the chain organically.")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("admin_check_chain"))
async def cmd_check_chain(message: Message):
    """Force an immediate chain check. If a path from ALEX to BEATRICE
    through captured Opposition nodes exists right now, the finale is
    started without waiting for the next scheduler tick (~30 s). Otherwise
    the admin gets a diagnostic with the current opposition mesh.

    Use this when you've just captured the last needed node and don't want
    to wait for the periodic scheduler to notice."""
    if not is_admin(message.from_user.id): return

    from server import check_victory_now
    from game.geo import find_connected_nodes, check_path_exists

    state = await db.get_game_state()
    if not state:
        await message.answer("Game state missing — start the game first.")
        return
    if not state["active"]:
        await message.answer("Game is not active.")
        return
    if state["finale_stage"]:
        await message.answer(f"Finale already running (stage: *{state['finale_stage']}*).", parse_mode="Markdown")
        return

    target_a, target_b = state["target_node_a"], state["target_node_b"]
    if not target_a or not target_b:
        await message.answer("Targets ALEX/BEATRICE not set.")
        return

    nodes = await db.get_all_nodes()
    node_list = [dict(n) for n in nodes]
    connections = find_connected_nodes(node_list)

    if check_path_exists(target_a, target_b, connections):
        await check_victory_now()
        await message.answer("✅ Chain complete — finale started.")
        return

    # Diagnostic: tell the admin what's actually connected so they know which
    # link is missing.
    by_id = {n["id"]: n for n in node_list}
    if not connections:
        await message.answer("❌ No Opposition links exist yet.")
        return
    lines = ["❌ Chain not closed yet. Current Opposition links:"]
    for a, b in connections:
        na, nb = by_id.get(a, {}).get("name", f"#{a}"), by_id.get(b, {}).get("name", f"#{b}")
        lines.append(f"  • {na} ↔ {nb}")
    await message.answer("\n".join(lines))


@router.message(Command("admin_fake_defend"))
async def cmd_fake_defend(message: Message):
    """Fake System freezes capture: /admin_fake_defend <fake> <node>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: `/admin_fake_defend BOB TEST1`", parse_mode="Markdown")
        return

    fake = await _find_fake_by_name(parts[1])
    if not fake or fake["team"] != "system":
        await message.answer("Fake not found or not on System team.")
        return

    node = await _find_node_by_name(parts[2])
    if not node or not node["capture_started_at"]:
        await message.answer("Node is not being attacked.")
        return

    await db.update_player_location(fake["telegram_id"], node["lat"], node["lon"])
    await db.freeze_node_capture(node["id"])

    opp_id = node["capturing_player_id"]
    if opp_id:
        new_id = await db.add_identification(
            system_player_id=fake["telegram_id"],
            opp_player_id=opp_id,
            node_id=node["id"], lat=node["lat"], lon=node["lon"]
        )
        opp_player = await db.get_player(opp_id)
        anon = opp_player["anonymous_id"] if opp_player else "AGENT_????"
        await message.answer(
            f"🛡 *{fake['username']}* froze *{node['name']}*\n"
            f"🆔 Logged: `{anon}` {'(new)' if new_id else '(already in DB)'}",
            parse_mode="Markdown"
        )
    else:
        await message.answer(f"🛡 *{node['name']}* frozen", parse_mode="Markdown")


@router.message(Command("admin_fakes"))
async def cmd_list_fakes(message: Message):
    """Show all fake players."""
    if not is_admin(message.from_user.id): return
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM players WHERE telegram_id < 0 ORDER BY telegram_id DESC"
        ) as cur:
            fakes = [dict(r) for r in await cur.fetchall()]

    if not fakes:
        await message.answer("No fakes found. `/admin_spawn opposition ALICE`", parse_mode="Markdown")
        return

    lines = [f"*Fakes ({len(fakes)}):*\n"]
    for f in fakes:
        icon = "⚙️" if f["team"] == "system" else "🔴"
        loc = ""
        if f.get("last_location_lat"):
            loc = f" @ `{f['last_location_lat']:.5f},{f['last_location_lon']:.5f}`"
        lines.append(f"{icon} *{f['username']}* — `{f['anonymous_id']}`{loc}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("admin_unspawn"))
async def cmd_unspawn(message: Message):
    """Remove a fake player: /admin_unspawn ALICE or /admin_unspawn all"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Usage: `/admin_unspawn ALICE` or `/admin_unspawn all`", parse_mode="Markdown")
        return

    target = parts[1].lower()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        if target == "all":
            await conn.execute("DELETE FROM players WHERE telegram_id < 0")
            await conn.commit()
            await message.answer("🗑 All fakes removed")
        else:
            fake = await _find_fake_by_name(target)
            if not fake:
                await message.answer("Fake not found.")
                return
            await conn.execute("DELETE FROM players WHERE telegram_id = ?", (fake["telegram_id"],))
            await conn.commit()
            await message.answer(f"🗑 {fake['username']} removed")


@router.message(Command("admin_help"))
async def cmd_admin_help(message: Message):
    """Admin commands only — without Opposition/System sections."""
    if not is_admin(message.from_user.id): return

    text = (
        "*🛠 Admin Commands:*\n\n"
        "*Map editor:*\n"
        "`/admin_map` — visual node editor (recommended)\n"
        "`/admin_addnode` Name;lat;lon — add a node from chat\n"
        "`/admin_nodes` — list of all nodes\n"
        "`/admin_setnodes` — quick seed: 14-node Povo map\n\n"
        "*Game flow:*\n"
        "`/admin_start` — start the game (push to everyone)\n"
        "`/admin_reset` — reset captures, scores, finale state\n"
        "`/admin_check_chain` — force immediate chain check\n"
        "                   (skip the 30 s scheduler wait,\n"
        "                    or trigger the finale manually)\n\n"
        "*Diagnostics:*\n"
        "`/admin_debug` — general state diagnostics\n"
        "`/admin_finale_debug` — why is the /finale lineup empty?\n"
        "`/admin_replay` — chronological event log\n"
        "`/admin_presentation` — open /presentation in a browser\n\n"
        "*Real players (during game):*\n"
        "`/admin_qr <name|AGENT_ID>` — fetch any Opposition's QR\n\n"
        "*Fake players (solo testing):*\n"
        "`/admin_spawn opposition ALICE` — create fake Opposition\n"
        "`/admin_spawn system BOB` — create fake System\n"
        "`/admin_fakes` — list fakes\n"
        "`/admin_move ALICE 46.06 11.15` — set fake location\n"
        "`/admin_fake_capture ALICE NODE_NAME` — fake fully captures node\n"
        "`/admin_fake_defend BOB NODE_NAME` — fake System freezes capture\n"
        "`/admin_unspawn ALICE` — remove one fake\n"
        "`/admin_unspawn all` — remove all fakes\n\n"
        "*Auto scenario:*\n"
        "Run in parallel: `python3 demo_scenario.py`\n"
        "Watches on /presentation"
    )
    await message.answer(text, parse_mode="Markdown")