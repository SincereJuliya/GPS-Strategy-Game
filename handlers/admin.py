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
    import bot as main_bot
    for p in all_players:
        try:
            await main_bot.bot.send_message(
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
    unique_agents = len(set(r["anonymous_id"] for r in all_ids if r.get("anonymous_id")))
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
