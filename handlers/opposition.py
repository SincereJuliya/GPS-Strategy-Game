from aiogram import Router, F
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from game.geo import find_nodes_in_radius, find_nodes_containing_player, find_connected_nodes, check_path_exists
import config

router = Router()


def geo_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Send geolocation", request_location=True)]],
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
        await message.answer("This command is for Opposition only.")
        return
    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("The game has not started yet.")
        return
    await message.answer("Send geolocation — we will check nearby nodes.", reply_markup=geo_keyboard())


# ── Geolocation from Opposition ──────────────────────────────────────────────────

@router.message(F.location)
async def handle_location_opposition(message: Message, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        return

    lat = message.location.latitude
    lon = message.location.longitude

    # FIX: always update geolocation — required for contested_checker and radius_grower
    await db.update_player_location(message.from_user.id, lat, lon)

    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("The game is not active.")
        return

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]

    # Capture is possible only if the player is INSIDE the node circle (its current_radius_m)
    nearby = find_nodes_containing_player(
        lat, lon,
        [n for n in nodes_list if n["owner"] == "system" and n.get("node_type", "node") == "node"]
    )

    if not nearby:
        await message.answer(
            "You are not inside the circle of any System node.\n"
            "Get closer to the node zone itself (circle on the map) to start capturing."
        )
        return

    if len(nearby) == 1:
        await start_capture(message, player, nearby[0]["node"])
        return

    lines = ["*Multiple nodes nearby. Send a number:*\n"]
    for i, item in enumerate(nearby, 1):
        node = item["node"]
        status = "⚔️ under attack" if node["capture_started_at"] else "🔵 vacant"
        lines.append(f"{i}. *{node['name']}* — {item['distance_m']}m ({status})")

    await state.set_state(NodeSelect.waiting_for_choice)
    await state.update_data(nearby=nearby)
    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(NodeSelect.waiting_for_choice)
async def handle_node_choice(message: Message, state: FSMContext):
    # Commands clear the FSM
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("Selection canceled.")
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
        await message.answer(f"Enter a number from 1 to {len(nearby)}.")
        return

    await state.clear()
    node = nearby[choice]["node"]
    await start_capture(message, player, node)


# ── Capture Logic ────────────────────────────────────────────────────────────

async def start_capture(message: Message, player, node: dict):
    node_id = node["id"]

    # Progress check — if already 100% captured by this (or another opposition), nothing to do
    if node.get("capture_progress") == 100 and node.get("owner") == "opposition":
        await message.answer(
            f"✅ Node *{node['name']}* is already fully captured (100%).",
            parse_mode="Markdown"
        )
        return

    # Send a link to the puzzle page
    puzzle_url = f"{config.SERVER_URL}/puzzle/{node_id}"

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import WebAppInfo
    kb = InlineKeyboardBuilder()
    kb.button(text="🧩 Open puzzle", web_app=WebAppInfo(url=puzzle_url))

    progress = node.get("capture_progress") or 0
    progress_msg = (
        f"Node is not captured yet. Solve the first puzzle → node is 80% yours."
        if progress == 0 else
        f"Node captured at {progress}%. Solve one more puzzle (of another type) → 100%."
    )

    await message.answer(
        f"🔴 Node *{node['name']}* is within radius.\n\n{progress_msg}\n\n"
        f"Open the puzzle and solve it. If you leave the circle, progress will be lost.\n"
        f"If System arrives, the puzzle will freeze.",
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )

    # Notify System about the attack
    system_players = await db.get_all_players("system")
    for sp in system_players:
        try:
            await message.bot.send_message(
                sp["telegram_id"],
                f"🚨 *Node under attack!*\n\nNode *{node['name']}* is being hacked.\n"
                f"Run and step into its circle to prevent it!",
                parse_mode="Markdown"
            )
        except Exception:
            pass


# ── /status ───────────────────────────────────────────────────────────────────

@router.message(Command("status"))
async def cmd_status(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        await message.answer("This command is for Opposition.")
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
        lines.append("⚔️ *Active captures:*\n")
        from datetime import datetime
        for node in active_captures:
            if node["capture_frozen"]:
                elapsed = int(node["capture_elapsed_sec"] or 0)
                lines.append(f"• *{node['name']}* — ⏸ FROZEN ({elapsed // 60}m {elapsed % 60}s elapsed)")
            else:
                started = datetime.fromisoformat(node["capture_started_at"])
                elapsed = int((datetime.now() - started).total_seconds())
                remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
                lines.append(
                    f"• *{node['name']}* — ▶️ {elapsed // 60}m {elapsed % 60}s "
                    f"({remaining // 60}m {remaining % 60}s remaining)"
                )
        lines.append("")

    if opp_nodes:
        lines.append(f"🔴 *Your nodes ({len(opp_nodes)}):*\n")
        for node in opp_nodes:
            lines.append(f"• *{node['name']}* — radius {int(node['current_radius_m'])}m")

        connections = find_connected_nodes(nodes_list)
        lines.append(f"\n🔗 Connections: {len(connections)}")

        game_state = await db.get_game_state()
        if game_state:
            a = game_state["target_node_a"]
            b = game_state["target_node_b"]
            if a and b:
                path = check_path_exists(a, b, connections)
                if path:
                    lines.append("\n✅ Chain completed — VICTORY!")
                else:
                    lines.append("\n❌ Chain not built yet")
    else:
        lines.append("You have not captured any nodes yet.")

    await message.answer("\n".join(lines) if lines else "No activity.", parse_mode="Markdown")