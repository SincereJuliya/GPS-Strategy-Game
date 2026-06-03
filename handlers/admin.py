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
            "Формат: /admin_addnode Имя;lat;lon\n"
            "Или с типом: /admin_addnode Имя;lat;lon;тип;радиус\n"
            "Типы: node (по умолчанию), hub, core"
        )
        return
    name = parts[0].strip()
    try:
        lat = float(parts[1].strip())
        lon = float(parts[2].strip())
        node_type = parts[3].strip() if len(parts) > 3 else "node"
        radius = float(parts[4].strip()) if len(parts) > 4 else 80
    except ValueError:
        await message.answer("Неверные координаты.")
        return

    await db.add_node(name, lat, lon, node_type, radius)
    await message.answer(f"✅ *{name}* добавлена\nТип: {node_type} | Радиус: {radius}м", parse_mode="Markdown")


@router.message(Command("admin_nodes"))
async def cmd_list_nodes(message: Message):
    if not is_admin(message.from_user.id): return
    nodes = await db.get_all_nodes()
    if not nodes:
        await message.answer("Нод нет.")
        return
    lines = ["*Все ноды:*\n"]
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
        await message.answer("Добавь ноды перед стартом.")
        return
    await db.set_game_active(True, phase=1)
    all_players = await db.get_all_players()
    for p in all_players:
        try:
            await message.bot.send_message(
                p["telegram_id"],
                f"🚀 *Игра началась! Фаза 1 из {config.PHASE_COUNT}*\n\nОткрой карту: /map",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    await message.answer(f"✅ Игра запущена. Игроков: {len(all_players)}")


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
        await conn.commit()
    await message.answer("✅ Игра сброшена. Все ноды возвращены System.")


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

    await message.answer(f"✅ Добавлено {len(nodes)} нод для FBK/Povo\nЦели: NODE ALEX → NODE BEATRICE")


@router.message(Command("admin_debug"))
async def cmd_debug(message: Message):
    if not is_admin(message.from_user.id): return

    from datetime import datetime

    nodes = await db.get_all_nodes()
    players = await db.get_all_players()
    game_state = await db.get_game_state()
    all_ids = await db.get_all_identifications()
    all_verifs = await db.get_all_verifications()

    gs_text = f"🎮 Игра: {'активна' if game_state and game_state['active'] else 'не активна'}"
    if game_state and game_state["active"]:
        gs_text += f" | Фаза {game_state['current_phase']}/{config.PHASE_COUNT}"

    node_lines = ["*Ноды:*"]
    for n in nodes:
        n = dict(n)
        if n["capture_started_at"] and not n["capture_frozen"]:
            started = datetime.fromisoformat(n["capture_started_at"])
            elapsed = int((datetime.now() - started).total_seconds())
            remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
            cap_info = f"⚔️ {elapsed}с (осталось {remaining}с)"
        elif n["capture_frozen"]:
            cap_info = f"⏸ заморожен ({int(n['capture_elapsed_sec'] or 0)}с)"
        else:
            cap_info = "—"
        owner_icon = "🔵" if n["owner"] == "system" else "🔴"
        node_lines.append(f"{owner_icon} *{n['name']}* r={int(n['current_radius_m'])}м | {cap_info}")

    player_lines = ["*Игроки:*"]
    system_count = sum(1 for p in players if p["team"] == "system")
    opposition_count = sum(1 for p in players if p["team"] == "opposition")
    player_lines.append(f"⚙️ System: {system_count} | 🔴 Opposition: {opposition_count}")

    # FIX: уникальные агенты вместо COUNT(*)
    unique_agents = len(set(r["anonymous_id"] for r in all_ids if dict(r).get("anonymous_id")))
    correct_verifs = len([v for v in all_verifs if v["correct"]])
    id_lines = [
        f"*Агентов замечено:* {unique_agents}",
        f"*Верификаций верных:* {correct_verifs}/{len(all_verifs)}"
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
        await message.answer("Использование: /admin_setmode A или /admin_setmode B")
        return
    mode = parts[1]
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("UPDATE game_state SET mode = ? WHERE id = 1", (mode,))
        await conn.commit()
    await message.answer(f"✅ Режим установлен: {mode}")


@router.message(Command("admin_map"))
async def cmd_admin_map(message: Message):
    """Открыть интерактивный редактор карты."""
    if not is_admin(message.from_user.id): return

    server_url = getattr(config, 'SERVER_URL', None)
    if not server_url:
        await message.answer(
            "Добавь в config.py:\n`SERVER_URL = 'https://твой-cloudflare-url'`",
            parse_mode="Markdown"
        )
        return

    admin_url = server_url.rstrip('/') + '/admin'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть редактор карты", url=admin_url)]
    ])
    await message.answer(
        "*Admin Map Editor*\n\n"
        "• Тыкай на карту — добавляй ноды\n"
        "• Кликай на ноду — удаляй\n"
        "• Видишь состояние игры в реальном времени\n"
        "• Кнопка RESET ALL сбрасывает игру\n\n"
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

        # Захваты
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
                    "text": f"⚡ @{row['username'] or '?'} начал захват *{row['node_name'] or '?'}*"
                })

        # Идентификации
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
                    "text": f"🆔 @{row['sys_username'] or '?'} зафиксировал `{row['anonymous_id']}` на *{row['node_name'] or '?'}*"
                })

        # Верификации
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
                    "text": f"{check} @{row['sys_username'] or '?'} верифицировал @{row['opp_username'] or '?'}: думал {row['guessed_anonymous_id']}, было {row['real_anonymous_id']}"
                })

    if not events:
        await message.answer("📜 События не найдены. Сначала сыграйте партию.")
        return

    # Сортируем по времени
    events.sort(key=lambda e: e["time"])

    # Форматируем по фазам времени
    def fmt(iso):
        try: return _dt.fromisoformat(iso).strftime("%H:%M:%S")
        except: return iso[:8]

    lines = [f"📜 Хронология игры ({len(events)} событий)\n"]
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
        await message.answer("Добавь SERVER_URL в config.py")
        return

    pres_url = server_url.rstrip('/') + '/presentation'
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Открыть режим презентации", url=pres_url)]
    ])
    await message.answer(
        "*Режим презентации* — для записи видео:\n\n"
        "• Видны ВСЕ игроки с именами и командами\n"
        "• Траектории движения (последние позиции)\n"
        "• Крупные подписи\n"
        "• Без кнопок — режим наблюдателя\n\n"
        "Открой в браузере на компе и запиши через OBS или встроенную запись экрана.\n\n"
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
            "*Использование:*\n"
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
        await message.answer("Команда должна быть 'opposition' или 'system'")
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
        f"✅ Создан фейк *{full_name}*\n"
        f"Team: {team_icon} {team}\n"
        f"ID: `{fake_id}` · Anon: `{anon}`\n\n"
        f"Поставь его на координаты:\n"
        f"`/admin_move {name} <lat> <lon>`",
        parse_mode="Markdown"
    )


@router.message(Command("admin_move"))
async def cmd_move(message: Message):
    """Передвинуть фейка: /admin_move <name> <lat> <lon>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 4:
        await message.answer("Использование: `/admin_move ALICE 46.0619 11.1502`", parse_mode="Markdown")
        return

    name = parts[1]
    try:
        lat = float(parts[2])
        lon = float(parts[3])
    except ValueError:
        await message.answer("Неверные координаты.")
        return

    fake = await _find_fake_by_name(name)
    if not fake:
        await message.answer(f"Фейк FAKE_{name.upper()} не найден. Создай через /admin_spawn")
        return

    await db.update_player_location(fake["telegram_id"], lat, lon)
    try:
        import server as srv
        srv._location_history[fake["telegram_id"]].append((lat, lon, _dt.now().isoformat()))
    except Exception:
        pass

    await message.answer(
        f"📍 *{fake['username']}* перемещён в `{lat:.5f}, {lon:.5f}`",
        parse_mode="Markdown"
    )


@router.message(Command("admin_fake_capture"))
async def cmd_fake_capture(message: Message):
    """Фейк-Opposition начинает захват: /admin_fake_capture <fake> <node>"""
    if not is_admin(message.from_user.id): return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Использование: `/admin_fake_capture ALICE TEST1`", parse_mode="Markdown")
        return

    fake = await _find_fake_by_name(parts[1])
    if not fake or fake["team"] != "opposition":
        await message.answer("Фейк не найден или не в Opposition.")
        return

    node = await _find_node_by_name(parts[2])
    if not node:
        await message.answer("Нода не найдена.")
        return
    if node["owner"] != "system" or node["capture_started_at"]:
        await message.answer(f"Нода *{node['name']}* недоступна для захвата.", parse_mode="Markdown")
        return

    await db.update_player_location(fake["telegram_id"], node["lat"], node["lon"])
    await db.start_node_capture(node["id"], fake["telegram_id"])
    await db.create_capture(node["id"], fake["telegram_id"])

    await message.answer(
        f"⚡️ *{fake['username']}* начал захват *{node['name']}*\n"
        f"Через {config.CAPTURE_TIME_SEC // 60} мин нода перейдёт к Opposition.",
        parse_mode="Markdown"
    )

    system_players = await db.get_all_players("system")
    for sp in system_players:
        if sp["telegram_id"] < 0: continue
        try:
            await message.bot.send_message(
                sp["telegram_id"],
                f"🚨 *Нода атакована!* *{node['name']}* под угрозой.",
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
        await message.answer("Использование: `/admin_fake_defend BOB TEST1`", parse_mode="Markdown")
        return

    fake = await _find_fake_by_name(parts[1])
    if not fake or fake["team"] != "system":
        await message.answer("Фейк не найден или не в System.")
        return

    node = await _find_node_by_name(parts[2])
    if not node or not node["capture_started_at"]:
        await message.answer("Нода не атакуется.")
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
            f"🛡 *{fake['username']}* заморозил *{node['name']}*\n"
            f"🆔 Залогирован: `{anon}` {'(новый)' if new_id else '(уже в базе)'}",
            parse_mode="Markdown"
        )
    else:
        await message.answer(f"🛡 *{node['name']}* заморожена", parse_mode="Markdown")


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
        await message.answer("Фейков нет. `/admin_spawn opposition ALICE`", parse_mode="Markdown")
        return

    lines = [f"*Фейки ({len(fakes)}):*\n"]
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
        await message.answer("Использование: `/admin_unspawn ALICE` или `/admin_unspawn all`", parse_mode="Markdown")
        return

    target = parts[1].lower()
    async with aiosqlite.connect(db.DB_PATH) as conn:
        if target == "all":
            await conn.execute("DELETE FROM players WHERE telegram_id < 0")
            await conn.commit()
            await message.answer("🗑 Все фейки удалены")
        else:
            fake = await _find_fake_by_name(target)
            if not fake:
                await message.answer("Фейк не найден.")
                return
            await conn.execute("DELETE FROM players WHERE telegram_id = ?", (fake["telegram_id"],))
            await conn.commit()
            await message.answer(f"🗑 {fake['username']} удалён")


@router.message(Command("admin_help"))
async def cmd_admin_help(message: Message):
    """Только админ-команды — без Opposition/System секций."""
    if not is_admin(message.from_user.id): return

    text = (
        "*🛠 Admin-команды:*\n\n"
        "*Карта и игра:*\n"
        "`/admin_map` — редактор карты (создать ноды кликом)\n"
        "`/admin_setnodes` — быстрая карта Povo (14 нод)\n"
        "`/admin_nodes` — список всех нод\n"
        "`/admin_addnode` Имя;lat;lon — добавить ноду\n"
        "`/admin_start` — запустить игру (всем пуш)\n"
        "`/admin_reset` — сбросить ноды и счёт\n"
        "`/admin_setmode A|B` — режим игры\n"
        "`/admin_debug` — диагностика состояния\n\n"
        "*Видео и разбор:*\n"
        "`/admin_presentation` — ссылка на /presentation\n"
        "`/admin_replay` — хронология всех событий\n\n"
        "*Фейковые игроки (одиночный тест):*\n"
        "`/admin_spawn opposition ALICE` — создать фейка-оппозицию\n"
        "`/admin_spawn system BOB` — создать фейка-системы\n"
        "`/admin_move ALICE 46.06 11.15` — переместить\n"
        "`/admin_fake_capture ALICE TEST1` — фейк захватывает\n"
        "`/admin_fake_defend BOB TEST1` — фейк-System замораживает\n"
        "`/admin_fakes` — список фейков\n"
        "`/admin_unspawn ALICE` — удалить одного\n"
        "`/admin_unspawn all` — удалить всех\n\n"
        "*Авто-сценарий:*\n"
        "Запусти параллельно: `python3 demo_scenario.py`\n"
        "Откроет полный сценарий на /presentation"
    )
    await message.answer(text, parse_mode="Markdown")
