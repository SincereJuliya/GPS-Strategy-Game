from aiogram import Router
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
import aiosqlite

import database as db
import config

router = Router()


def is_admin(telegram_id: int) -> bool:
    return telegram_id == config.ADMIN_ID


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
        await message.answer("Add nodes before starting the game.")
        return
    await db.set_game_active(True, phase=1)
    all_players = await db.get_all_players()
    for p in all_players:
        try:
            await message.bot.send_message(
                p["telegram_id"],
                f"🚀 *The game has started! Phase 1 of {config.PHASE_COUNT}*\n\nOpen the map: /map",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    await message.answer(f"✅ Game started. Total players: {len(all_players)}")


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
        # Чистим все логи событий чтобы /admin_replay и панель /presentation были пустые
        await conn.execute("DELETE FROM captures")
        await conn.execute("DELETE FROM identifications")
        await conn.execute("DELETE FROM verifications")
        await conn.commit()
    await message.answer("✅ Game reset. All nodes returned to System, logs cleared.")


@router.message(Command("admin_setnodes"))
async def cmd_set_povo_nodes(message: Message):
    """Быстрая команда — тестовые ноды FBK/Povo."""
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

    gs_text = f"🎮 Game: {'active' if game_state and game_state['active'] else 'inactive'}"
    if game_state and game_state["active"]:
        gs_text += f" | Phase {game_state['current_phase']}/{config.PHASE_COUNT}"

    node_lines = ["*Nodes:*"]
    for n in nodes:
        n = dict(n)
        if n["capture_started_at"] and not n["capture_frozen"]:
            started = datetime.fromisoformat(n["capture_started_at"])
            elapsed = int((datetime.now() - started).total_seconds())
            remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
            cap_info = f"⚔️ {elapsed}s (left {remaining}s)"
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

    # FIX: уникальные агенты вместо COUNT(*)
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


@router.message(Command("admin_setmode"))
async def cmd_set_mode(message: Message):
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 2 or parts[1] not in ("A", "B"):
        await message.answer("Usage: /admin_setmode A or /admin_setmode B")
        return
    mode = parts[1]
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("UPDATE game_state SET mode = ? WHERE id = 1", (mode,))
        await conn.commit()
    await message.answer(f"✅ Mode set to: {mode}")


@router.message(Command("admin_map"))
async def cmd_admin_map(message: Message):
    """Открыть интерактивный редактор карты."""
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
        "• View real-time game status\n"
        "• RESET ALL button resets the entire game\n\n"
        f"`{admin_url}`",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@router.message(Command("admin_replay"))
async def cmd_admin_replay(message: Message):
    """Хронология всех событий игры — для разбора после теста."""
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
                    "text": f"🆔 @{row['sys_username'] or '?'} recorded `{row['anonymous_id']}` at *{row['node_name'] or '?'}*"
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
                    "text": f"{check} @{row['sys_username'] or '?'} verified @{row['opp_username'] or '?'}: guessed {row['guessed_anonymous_id']}, was {row['real_anonymous_id']}"
                })

    if not events:
        await message.answer("📜 No events found. Please play a game first.")
        return

    # Сортируем по времени
    events.sort(key=lambda e: e["time"])

    # Форматируем по фазам времени
    def fmt(iso):
        try: return _dt.fromisoformat(iso).strftime("%H:%M:%S")
        except: return iso[:8]

    lines = [f"📜 Game Timeline ({len(events)} events)\n"]
    for e in events:
        lines.append(f"{fmt(e['time'])}  {e['text']}")

    # Telegram limit 4096 chars — режем при необходимости
    text = "\n".join(lines)
    # Без Markdown — спецсимволы в username/anonymous_id могут ломать парсер
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
    """Открыть карту в режиме презентации для записи видео."""
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
        "*Presentation Mode* — for recording video:\n\n"
        "• ALL players are visible with names and teams\n"
        "• Movement trajectories (latest positions)\n"
        "• Large labels\n"
        "• No buttons — spectator mode\n\n"
        "Open in a desktop browser and record via OBS or built-in screen recorder.\n\n"
        f"`{pres_url}`",
        reply_markup=kb, parse_mode="Markdown"
    )


# ─────────────────────────────────────────────────────────────────────────────
# СИМУЛЯЦИЯ ФЕЙКОВЫХ ИГРОКОВ — для одиночного теста через бот
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
    """Создать фейкового игрока: /admin_spawn <team> <name>"""
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
        f"✅ Created fake player *{full_name}*\n"
        f"Team: {team_icon} {team}\n"
        f"ID: `{fake_id}` · Anon: `{anon}`\n\n"
        f"Set their coordinates:\n"
        f"`/admin_move {name} <lat> <lon>`",
        parse_mode="Markdown"
    )


@router.message(Command("admin_move"))
async def cmd_move(message: Message):
    """Передвинуть фейка: /admin_move <name> <lat> <lon>"""
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
        await message.answer(f"Fake FAKE_{name.upper()} not found. Create it via /admin_spawn")
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
    """Фейк-Opposition начинает захват: /admin_fake_capture <fake> <node>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: `/admin_fake_capture ALICE TEST1`", parse_mode="Markdown")
        return

    fake = await _find_fake_by_name(parts[1])
    if not fake or fake["team"] != "opposition":
        await message.answer("Fake player not found or is not in Opposition.")
        return

    node = await _find_node_by_name(parts[2])
    if not node:
        await message.answer("Node not found.")
        return
    if node["owner"] != "system" or node["capture_started_at"]:
        await message.answer(f"Node *{node['name']}* is not available for capture.", parse_mode="Markdown")
        return

    await db.update_player_location(fake["telegram_id"], node["lat"], node["lon"])
    await db.start_node_capture(node["id"], fake["telegram_id"])
    await db.create_capture(node["id"], fake["telegram_id"])

    await message.answer(
        f"⚡️ *{fake['username']}* started capturing *{node['name']}*\n"
        f"In {config.CAPTURE_TIME_SEC // 60} min the node will transfer to Opposition.",
        parse_mode="Markdown"
    )

    system_players = await db.get_all_players("system")
    for sp in system_players:
        if sp["telegram_id"] < 0: continue
        try:
            await message.bot.send_message(
                sp["telegram_id"],
                f"🚨 *Node under attack!* *{node['name']}* is at risk.",
                parse_mode="Markdown"
            )
        except Exception:
            pass


@router.message(Command("admin_fake_defend"))
async def cmd_fake_defend(message: Message):
    """Фейк-System замораживает захват: /admin_fake_defend <fake> <node>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: `/admin_fake_defend BOB TEST1`", parse_mode="Markdown")
        return

    fake = await _find_fake_by_name(parts[1])
    if not fake or fake["team"] != "system":
        await message.answer("Fake player not found or is not in System.")
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
        await message.answer(f"🛡 *{node['name']}* has been frozen", parse_mode="Markdown")


@router.message(Command("admin_fakes"))
async def cmd_list_fakes(message: Message):
    """Показать всех фейков."""
    if not is_admin(message.from_user.id): return
    async with aiosqlite.connect(db.DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM players WHERE telegram_id < 0 ORDER BY telegram_id DESC"
        ) as cur:
            fakes = [dict(r) for r in await cur.fetchall()]

    if not fakes:
        await message.answer("No fake players found. Create one using `/admin_spawn opposition ALICE`", parse_mode="Markdown")
        return

    lines = [f"*Fake Players ({len(fakes)}):*\n"]
    for f in fakes:
        icon = "⚙️" if f["team"] == "system" else "🔴"
        loc = ""
        if f.get("last_location_lat"):
            loc = f" @ `{f['last_location_lat']:.5f},{f['last_location_lon']:.5f}`"
        lines.append(f"{icon} *{f['username']}* — `{f['anonymous_id']}`{loc}")
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("admin_unspawn"))
async def cmd_unspawn(message: Message):
    """Удалить фейка: /admin_unspawn ALICE  или  /admin_unspawn all"""
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
            await message.answer("🗑 All fake players removed")
        else:
            fake = await _find_fake_by_name(target)
            if not fake:
                await message.answer("Fake player not found.")
                return
            await conn.execute("DELETE FROM players WHERE telegram_id = ?", (fake["telegram_id"],))
            await conn.commit()
            await message.answer(f"🗑 {fake['username']} removed")


@router.message(Command("admin_help"))
async def cmd_admin_help(message: Message):
    """Только админ-команды — без Opposition/System секций."""
    if not is_admin(message.from_user.id): return

    text = (
        "*🛠 Admin Commands:*\n\n"
        "*Map & Game:*\n"
        "`/admin_map` — map editor (click to create nodes)\n"
        "`/admin_setnodes` — quick Povo map setup (14 nodes)\n"
        "`/admin_nodes` — list of all nodes\n"
        "`/admin_addnode` Name;lat;lon — manually add a node\n"
        "`/admin_start` — start the game (broadcast alert to all)\n"
        "`/admin_reset` — reset nodes and scores\n"
        "`/admin_setmode A|B` — set game mode\n"
        "`/admin_debug` — state diagnostics\n\n"
        "*Video & Analysis:*\n"
        "`/admin_presentation` — link to /presentation page\n"
        "`/admin_replay` — full event timeline history\n\n"
        "*Fake Players (Single Testing):*\n"
        "`/admin_spawn opposition ALICE` — create a fake opposition player\n"
        "`/admin_spawn system BOB` — create a fake system player\n"
        "`/admin_move ALICE 46.06 11.15` — move fake player\n"
        "`/admin_fake_capture ALICE TEST1` — fake player starts capture\n"
        "`/admin_fake_defend BOB TEST1` — fake system player freezes capture\n"
        "`/admin_fakes` — list all fake players\n"
        "`/admin_unspawn ALICE` — remove one fake player\n"
        "`/admin_unspawn all` — remove all fake players\n\n"
        "*Auto-Script:*\n"
        "Run in parallel: `python3 demo_scenario.py`\n"
        "Will open the complete scenario on /presentation"
    )
    await message.answer(text, parse_mode="Markdown")