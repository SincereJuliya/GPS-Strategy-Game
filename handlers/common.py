from aiogram import Router, F
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ReplyKeyboardRemove, BufferedInputFile,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import aiosqlite
import io

import database as db

router = Router()

MAP_URL = "https://bullet-asked-disciplines-serves.trycloudflare.com/map"
# https://bullet-asked-disciplines-serves.trycloudflare.com                

class Registration(StatesGroup):
    waiting_for_team = State()


def _make_qr_bytes(data: str) -> bytes:
    import qrcode
    qr = qrcode.QRCode(box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if player:
        team_icon = "⚙️" if player["team"] == "system" else "🔴"
        map_url = MAP_URL + "?player_id=" + str(message.from_user.id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗺 Open map", web_app={"url": map_url})],
            [InlineKeyboardButton(text="🚪 Leave game", callback_data="leave_confirm")],
        ])
        await message.answer(
            f"You are in the game as {team_icon} *{player['team'].upper()}*\n\n/help — command list",
            reply_markup=kb, parse_mode="Markdown"
        )
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ System")], [KeyboardButton(text="🔴 Opposition")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(
        "Welcome to the game.\n\n"
        "⚙️ *System* — defend the network, catch hackers\n"
        "🔴 *Opposition* — capture nodes, build a network\n\n"
        "Choose your team:",
        reply_markup=kb, parse_mode="Markdown"
    )
    await state.set_state(Registration.waiting_for_team)


@router.message(Registration.waiting_for_team)
async def choose_team(message: Message, state: FSMContext):
    text = message.text.lower()
    if "system" in text:
        team = "system"
    elif "opposition" in text or "opps" in text or "oppositions" in text:
        team = "opposition"
    else:
        await message.answer("Choose System or Opposition.")
        return

    anon_id = await db.register_player(
        telegram_id=message.from_user.id,
        username=message.from_user.username or str(message.from_user.id),
        team=team
    )
    await state.clear()

    map_url = MAP_URL + "?player_id=" + str(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Open map", web_app={"url": map_url})],
    ])

    if team == "system":
        await message.answer(
            "✅ You joined *System*\n\nDefend nodes and identify hackers.\n\n/help — commands",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"✅ You joined *Opposition*\n\n"
            f"Your anonymous ID: `{anon_id}`\n\n"
            f"System only sees you as *{anon_id}* — no real name.\n"
            f"At the end of the game, they will try to match this ID with you personally.\n\n"
            f"The QR code below — show it only when System requests verification.\n\n"
            f"/help — commands",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        try:
            qr_bytes = _make_qr_bytes(f"GPSGAME:PLAYER:{message.from_user.id}:{anon_id}")
            await message.answer_photo(
                BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=f"🔲 QR code\nID: `{anon_id}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"QR generation failed: {e}")

    await message.answer("Ready? Open the map:", reply_markup=kb)


# ── /myqr ─────────────────────────────────────────────────────────────────────

@router.message(Command("myqr"))
async def cmd_myqr(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Please register first — /start")
        return
    if player["team"] != "opposition":
        await message.answer("QR code is for Opposition only.")
        return

    anon_id = player["anonymous_id"]
    try:
        qr_bytes = _make_qr_bytes(f"GPSGAME:PLAYER:{message.from_user.id}:{anon_id}")
        await message.answer_photo(
            BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=f"🔲 Your QR code\nID: `{anon_id}`\n\nShow it only when System requests verification.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"Error: {e}")


# ── /leave ────────────────────────────────────────────────────────────────────

@router.message(Command("leave"))
async def cmd_leave(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("You are not in the game.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, leave", callback_data="leave_yes"),
        InlineKeyboardButton(text="❌ Stay", callback_data="leave_no"),
    ]])
    await message.answer(
        f"You are in team *{player['team'].upper()}*.\n\nLeave and choose a team again?",
        reply_markup=kb, parse_mode="Markdown"
    )


@router.callback_query(F.data == "leave_confirm")
async def cb_leave_confirm(callback: CallbackQuery):
    player = await db.get_player(callback.from_user.id)
    if not player:
        await callback.answer("You are not in the game.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, leave", callback_data="leave_yes"),
        InlineKeyboardButton(text="❌ Stay", callback_data="leave_no"),
    ]])
    await callback.message.edit_text(
        f"You are in team *{player['team'].upper()}*.\n\nLeave and choose a team again?",
        reply_markup=kb, parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "leave_yes")
async def cb_leave_yes(callback: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM players WHERE telegram_id=?", (callback.from_user.id,))
        await conn.commit()
    await callback.message.edit_text("👋 You left the game.")
    await callback.answer()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ System")], [KeyboardButton(text="🔴 Opposition")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await callback.message.answer(
        "Choose a new team:\n\n⚙️ *System* — defend the network\n🔴 *Opposition* — capture nodes",
        reply_markup=kb, parse_mode="Markdown"
    )
    await state.set_state(Registration.waiting_for_team)


@router.callback_query(F.data == "leave_no")
async def cb_leave_no(callback: CallbackQuery):
    await callback.message.edit_text("You stay in the game 👍")
    await callback.answer()


# ── /map ──────────────────────────────────────────────────────────────────────

@router.message(Command("map"))
async def cmd_map(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Please register first — /start")
        return
    map_url = MAP_URL + "?player_id=" + str(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Open map", web_app={"url": map_url})],
    ])
    await message.answer("Open the map:", reply_markup=kb)


# ── /help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("/start — registration")
        return

    if player["team"] == "opposition":
        text = (
            "*🔴 Opposition — commands:*\n\n"
            "/capture — start capture (geolocation required)\n"
            "/status — your nodes and active captures\n"
            "/myqr — show your QR code\n"
            "/map — open map\n"
            "/leave — leave game\n\n"
            "*Key info:*\n"
            "• Capture nodes by standing in the radius for 3 min\n"
            "• Connect ALEX and BEATRICE via a chain to win\n"
            "• If System is nearby, the timer will freeze, not reset\n"
            "• If both leave for a long time, capture resets after 3 min"
        )
    else:
        text = (
            "*⚙️ System — commands:*\n\n"
            "/defend — check nearby attacks (geolocation required)\n"
            "/ids — your identification logs\n"
            "/team_ids — full team logs\n"
            "/verify — verify hacker by QR\n"
            "/score — current score\n"
            "/map — open map\n"
            "/leave — leave game\n\n"
            "*Key info:*\n"
            "• Within the attacked node radius, press DEFEND to freeze capture\n"
            "• In /ids you see only AGENT_XXXX, no real names\n"
            "• In the finale, scan QR via /verify — get +15 for a correct guess"
        )

    # Additional section for admin
    try:
        import config
        if message.from_user.id == config.ADMIN_ID:
            text += (
                "\n\n*🛠 Admin commands:*\n\n"
                "*Map & game:*\n"
                "`/admin_map` — map editor (create nodes)\n"
                "`/admin_setnodes` — quick Povo map (14 nodes)\n"
                "`/admin_nodes` — list of all nodes\n"
                "`/admin_addnode` — add node via command\n"
                "`/admin_start` — start game\n"
                "`/admin_reset` — reset nodes and score\n"
                "`/admin_debug` — state diagnostics\n\n"
                "*Video & review:*\n"
                "`/admin_presentation` — open /presentation for recording\n"
                "`/admin_replay` — timeline of all events\n\n"
                "*Fake players (for solo test):*\n"
                "`/admin_spawn opposition ALICE` — spawn fake player\n"
                "`/admin_move ALICE lat lon` — move fake player\n"
                "`/admin_fake_capture ALICE TEST1` — fake player starts capture\n"
                "`/admin_fake_defend BOB TEST1` — fake System freezes capture\n"
                "`/admin_fakes` — list of fake players\n"
                "`/admin_unspawn ALICE` — remove fake player\n"
                "`/admin_unspawn all` — remove all fake players\n\n"
                "*Full demo:* run `python3 demo_scenario.py` "
                "parallel to the bot — it will play a full scenario."
            )
    except Exception:
        pass

    await message.answer(text, parse_mode="Markdown")
