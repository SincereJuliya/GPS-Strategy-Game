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
    await message.answer("Send your geolocation to check for the nearest nodes.", reply_markup=geo_keyboard())


# ── Opposition Geolocation ──────────────────────────────────────────────────────

@router.message(F.location)
async def handle_location_opposition(message: Message, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "opposition":
        return

    lat = message.location.latitude
    lon = message.location.longitude

    # FIX: always update geolocation — needed for contested_checker and radius_grower
    await db.update_player_location(message.from_user.id, lat, lon)

    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("Game is not active.")
        return

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]

    # Capture is only possible if the player is INSIDE the node's circle (its current_radius_m)
    nearby = find_nodes_containing_player(
        lat, lon,
        [n for n in nodes_list if n["owner"] == "system" and n.get("node_type", "node") == "node"]
    )

    if not nearby:
        await message.answer(
            "You are not inside any System node area.\n"
            "Get closer to the node circle on the map to start capture."
        )
        return

    if len(nearby) == 1:
        await start_capture(message, player, nearby[0]["node"])
        return

    lines = ["*Multiple nodes nearby. Send a number:*\n"]
    for i, item in enumerate(nearby, 1):
        node = item["node"]
        status = "⚔️ under attack" if node["capture_started_at"] else "🔵 free"
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

    if node["capture_frozen"]:
        await message.answer(
            f"⏸ Node *{node['name']}* is currently contested — System is nearby.\n"
            f"Timer frozen. Wait for them to leave or go away yourself.",
            parse_mode="Markdown"
        )
        return

    if node["capture_started_at"]:
        from datetime import datetime
        started = datetime.fromisoformat(node["capture_started_at"])
        elapsed = int((datetime.now() - started).total_seconds())
        remaining = max(0, config.CAPTURE_TIME_SEC - elapsed)
        await message.answer(
            f"⚡️ Node *{node['name']}* is already being captured.\n"
            f"Time remaining: {remaining // 60}m {remaining % 60}s",
            parse_mode="Markdown"
        )
        return

    await db.start_node_capture(node_id, message.from_user.id)
    await db.create_capture(node_id, message.from_user.id)

    minutes = config.CAPTURE_TIME_SEC // 60
    await message.answer(
        f"⚡️ Capture of node *{node['name']}* started!\n\n"
        f"Hold your position for {minutes} min.\n"
        f"If System appears, the timer will freeze (not reset!).\n"
        f"Once they leave, the capture will resume.\n\n"
        f"/status — progress",
        parse_mode="Markdown"
    )

    system_players = await db.get_all_players("system")
    for sp in system_players:
        try:
            await message.bot.send_message(
                sp["telegram_id"],
                f"🚨 *Node under attack!*\n\n"
                f"Node *{node['name']}* is threatened.\n"
                f"You have {minutes} min!",
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
        if game_state and game_state["mode"] == "A":
            a = game_state["target_node_a"]
            b = game_state["target_node_b"]
            if a and b:
                path = check_path_exists(a, b, connections)
                if path:
                    lines.append("\n✅ Chain completed — VICTORY!")
                else:
                    lines.append("\n❌ Chain not completed yet")
    else:
        lines.append("You haven't captured any nodes yet.")

    await message.answer("\n".join(lines) if lines else "No activity.", parse_mode="Markdown")
