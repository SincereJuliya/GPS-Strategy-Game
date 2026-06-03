from aiogram import Router, F
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from game.geo import find_nodes_in_radius, find_connected_nodes, check_path_exists
import config

router = Router()


def geo_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )


class NodeSelect(StatesGroup):
    waiting_for_choice = State()


# ── /capture ──────────────────────────────────────────────────────────────────

@router.message(Command("capture"))
async def cmd_capture(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        await message.answer("Эта команда только для Opposition.")
        return
    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("Игра ещё не началась.")
        return
    await message.answer("Отправь геолокацию — проверим ближайшие ноды.", reply_markup=geo_keyboard())


# ── Геолокация от Opposition ──────────────────────────────────────────────────────

@router.message(F.location)
async def handle_location_opposition(message: Message, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        return

    lat = message.location.latitude
    lon = message.location.longitude

    # FIX: всегда обновляем геолокацию — нужно для contested_checker и radius_grower
    await db.update_player_location(message.from_user.id, lat, lon)

    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("Игра не активна.")
        return

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]

    nearby = find_nodes_in_radius(
        lat, lon,
        [n for n in nodes_list if n["owner"] == "system" and n.get("node_type", "node") == "node"],
        config.CAPTURE_RADIUS_M
    )

    if not nearby:
        await message.answer(
            f"Нет System-нод в радиусе {config.CAPTURE_RADIUS_M}м.\n"
            "Подойди ближе к ноде."
        )
        return

    if len(nearby) == 1:
        await start_capture(message, player, nearby[0]["node"])
        return

    lines = ["*Несколько нод рядом. Отправь номер:*\n"]
    for i, item in enumerate(nearby, 1):
        node = item["node"]
        status = "⚔️ атакуется" if node["capture_started_at"] else "🔵 свободна"
        lines.append(f"{i}. *{node['name']}* — {item['distance_m']}м ({status})")

    await state.set_state(NodeSelect.waiting_for_choice)
    await state.update_data(nearby=nearby)
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(NodeSelect.waiting_for_choice)
async def handle_node_choice(message: Message, state: FSMContext):
    # Команды освобождают FSM
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Выбор отменён.")
        return
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        await state.clear()
        return

    data = await state.get_data()
    nearby = data.get("nearby", [])

    try:
        choice = int(message.text.strip()) - 1
        if choice < 0 or choice >= len(nearby):
            raise ValueError
    except ValueError:
        await message.answer(f"Введи число от 1 до {len(nearby)}.")
        return

    await state.clear()
    node = nearby[choice]["node"]
    await start_capture(message, player, node)


# ── Логика захвата ────────────────────────────────────────────────────────────

async def start_capture(message: Message, player, node: dict):
    node_id = node["id"]

    if node["capture_frozen"]:
        await message.answer(
            f"⏸ Нода *{node['name']}* сейчас оспаривается — System рядом.\n"
            f"Таймер заморожен. Жди пока они уйдут или уходи сам.",
            parse_mode="Markdown"
        )
        return

    if node["capture_started_at"]:
        from datetime import datetime
        started = datetime.fromisoformat(node["capture_started_at"])
        elapsed = int((datetime.now() - started).total_seconds())
        remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
        await message.answer(
            f"⚡️ Нода *{node['name']}* уже захватывается.\n"
            f"Осталось: {remaining // 60}м {remaining % 60}с",
            parse_mode="Markdown"
        )
        return

    await db.start_node_capture(node_id, message.from_user.id)
    await db.create_capture(node_id, message.from_user.id)

    minutes = config.CAPTURE_TIME_SEC // 60
    await message.answer(
        f"⚡️ Захват ноды *{node['name']}* начат!\n\n"
        f"Держи позицию {minutes} мин.\n"
        f"Если появится System — таймер заморозится (не сбросится!).\n"
        f"Когда уйдут — захват возобновится.\n\n"
        f"/status — прогресс",
        parse_mode="Markdown"
    )

    system_players = await db.get_all_players("system")
    for sp in system_players:
        try:
            await message.bot.send_message(
                sp["telegram_id"],
                f"🚨 *Нода атакована!*\n\n"
                f"Нода *{node['name']}* под угрозой.\n"
                f"У тебя {minutes} мин!",
                parse_mode="Markdown"
            )
        except Exception:
            pass


# ── /status ───────────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        await message.answer("Эта команда для Opposition.")
        return

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]

    opp_nodes = [n for n in nodes_list if n["owner"] == "opposition"]
    active_captures = [
        n for n in nodes_list
        if n["owner"] == "system" and n["capture_started_at"]
           and n["capturing_player_id"] == message.from_user.id
    ]

    lines = []

    if active_captures:
        lines.append("⚔️ *Активные захваты:*\n")
        from datetime import datetime
        for node in active_captures:
            if node["capture_frozen"]:
                elapsed = int(node["capture_elapsed_sec"] or 0)
                lines.append(f"• *{node['name']}* — ⏸ ЗАМОРОЖЕН (пройдено {elapsed // 60}м {elapsed % 60}с)")
            else:
                started = datetime.fromisoformat(node["capture_started_at"])
                elapsed = int((datetime.now() - started).total_seconds())
                remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
                lines.append(
                    f"• *{node['name']}* — ▶️ {elapsed // 60}м {elapsed % 60}с "
                    f"(осталось {remaining // 60}м {remaining % 60}с)"
                )
        lines.append("")

    if opp_nodes:
        lines.append(f"🔴 *Ваши ноды ({len(opp_nodes)}):*\n")
        for node in opp_nodes:
            lines.append(f"• *{node['name']}* — радиус {int(node['current_radius_m'])}м")

        connections = find_connected_nodes(nodes_list)
        lines.append(f"\n🔗 Связей: {len(connections)}")

        game_state = await db.get_game_state()
        if game_state and game_state["mode"] == "A":
            a = game_state["target_node_a"]
            b = game_state["target_node_b"]
            if a and b:
                path = check_path_exists(a, b, connections)
                if path:
                    lines.append("\n✅ Цепочка завершена — ПОБЕДА!")
                else:
                    lines.append("\n❌ Цепочка ещё не построена")
    else:
        lines.append("Ты ещё не захватил ни одной ноды.")

    await message.answer("\n".join(lines) if lines else "Нет активности.", parse_mode="Markdown")
