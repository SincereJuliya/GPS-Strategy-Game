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
            [InlineKeyboardButton(text="🗺 Открыть карту", web_app={"url": map_url})],
            [InlineKeyboardButton(text="🚪 Выйти из игры", callback_data="leave_confirm")],
        ])
        await message.answer(
            f"Ты в игре как {team_icon} *{player['team'].upper()}*\n\n/help — список команд",
            reply_markup=kb, parse_mode="Markdown"
        )
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ System")], [KeyboardButton(text="🔴 Opposition")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(
        "Добро пожаловать в игру.\n\n"
        "⚙️ *System* — защищай сеть, лови хакеров\n"
        "🔴 *Opposition* — захватывай ноды, строй сеть\n\n"
        "Выбери команду:",
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
        await message.answer("Выбери System или Opposition.")
        return

    anon_id = await db.register_player(
        telegram_id=message.from_user.id,
        username=message.from_user.username or str(message.from_user.id),
        team=team
    )
    await state.clear()

    map_url = MAP_URL + "?player_id=" + str(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть карту", web_app={"url": map_url})],
    ])

    if team == "system":
        await message.answer(
            "✅ Ты вступил в *System*\n\nЗащищай ноды и идентифицируй хакеров.\n\n/help — команды",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
    else:
        await message.answer(
            f"✅ Ты вступил в *Opposition*\n\n"
            f"Твой анонимный ID: `{anon_id}`\n\n"
            f"System видит тебя только как *{anon_id}* — без имени.\n"
            f"В конце игры они попытаются сопоставить этот ID с тобой лично.\n\n"
            f"QR-код ниже — показывай только когда System требует верификацию.\n\n"
            f"/help — команды",
            reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown"
        )
        try:
            qr_bytes = _make_qr_bytes(f"GPSGAME:PLAYER:{message.from_user.id}:{anon_id}")
            await message.answer_photo(
                BufferedInputFile(qr_bytes, filename="qr.png"),
                caption=f"🔲 QR-код\nID: `{anon_id}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await message.answer(f"QR не сгенерировался: {e}")

    await message.answer("Готов? Открывай карту:", reply_markup=kb)


# ── /myqr ─────────────────────────────────────────────────────────────────────

@router.message(Command("myqr"))
async def cmd_myqr(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Сначала зарегистрируйся — /start")
        return
    if player["team"] != "opposition":
        await message.answer("QR-код только для Opposition.")
        return

    anon_id = player["anonymous_id"]
    try:
        qr_bytes = _make_qr_bytes(f"GPSGAME:PLAYER:{message.from_user.id}:{anon_id}")
        await message.answer_photo(
            BufferedInputFile(qr_bytes, filename="qr.png"),
            caption=f"🔲 Твой QR-код\nID: `{anon_id}`\n\nПоказывай только когда System требует верификацию.",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


# ── /leave ────────────────────────────────────────────────────────────────────

@router.message(Command("leave"))
async def cmd_leave(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Ты не в игре.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выйти", callback_data="leave_yes"),
        InlineKeyboardButton(text="❌ Остаться", callback_data="leave_no"),
    ]])
    await message.answer(
        f"Ты в команде *{player['team'].upper()}*.\n\nВыйти и выбрать команду заново?",
        reply_markup=kb, parse_mode="Markdown"
    )


@router.callback_query(F.data == "leave_confirm")
async def cb_leave_confirm(callback: CallbackQuery):
    player = await db.get_player(callback.from_user.id)
    if not player:
        await callback.answer("Ты не в игре.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, выйти", callback_data="leave_yes"),
        InlineKeyboardButton(text="❌ Остаться", callback_data="leave_no"),
    ]])
    await callback.message.edit_text(
        f"Ты в команде *{player['team'].upper()}*.\n\nВыйти и выбрать команду заново?",
        reply_markup=kb, parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data == "leave_yes")
async def cb_leave_yes(callback: CallbackQuery, state: FSMContext):
    async with aiosqlite.connect(db.DB_PATH) as conn:
        await conn.execute("DELETE FROM players WHERE telegram_id=?", (callback.from_user.id,))
        await conn.commit()
    await callback.message.edit_text("👋 Ты вышел из игры.")
    await callback.answer()
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⚙️ System")], [KeyboardButton(text="🔴 Opposition")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await callback.message.answer(
        "Выбери новую команду:\n\n⚙️ *System* — защищай сеть\n🔴 *Opposition* — захватывай ноды",
        reply_markup=kb, parse_mode="Markdown"
    )
    await state.set_state(Registration.waiting_for_team)


@router.callback_query(F.data == "leave_no")
async def cb_leave_no(callback: CallbackQuery):
    await callback.message.edit_text("Ты остаёшься в игре 👍")
    await callback.answer()


# ── /map ──────────────────────────────────────────────────────────────────────

@router.message(Command("map"))
async def cmd_map(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Сначала зарегистрируйся — /start")
        return
    map_url = MAP_URL + "?player_id=" + str(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть карту", web_app={"url": map_url})],
    ])
    await message.answer("Открывай карту:", reply_markup=kb)


# ── /help ─────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("/start — регистрация")
        return

    if player["team"] == "opposition":
        text = (
            "*🔴 Opposition — команды:*\n\n"
            "/capture — начать захват (нужна геолокация)\n"
            "/status — твои ноды и активные захваты\n"
            "/myqr — показать свой QR-код\n"
            "/map — открыть карту\n"
            "/leave — выйти из игры\n\n"
            "*Главное:*\n"
            "• Захватывай ноды стоя в радиусе 3 мин\n"
            "• Соедини ALEX и BEATRICE через цепочку для победы\n"
            "• Если System рядом — таймер замёрзнет, не сбросится\n"
            "• Если оба ушли надолго — захват сбросится через 3 мин"
        )
    else:
        text = (
            "*⚙️ System — команды:*\n\n"
            "/defend — проверить атаки рядом (нужна геолокация)\n"
            "/ids — твои логи идентификаций\n"
            "/team_ids — логи всей команды\n"
            "/verify — верифицировать хакера по QR\n"
            "/score — текущий счёт\n"
            "/map — открыть карту\n"
            "/leave — выйти из игры\n\n"
            "*Главное:*\n"
            "• В радиусе атакованной ноды жми DEFEND — заморозит захват\n"
            "• В /ids видишь только AGENT_XXXX, не имена\n"
            "• В финале сканируй QR через /verify — за верное угадывание +15"
        )

    # Дополнительная секция для админа
    try:
        import config
        if message.from_user.id == config.ADMIN_ID:
            text += (
                "\n\n*🛠 Admin-команды:*\n\n"
                "*Карта и игра:*\n"
                "`/admin_map` — редактор карты (создать ноды)\n"
                "`/admin_setnodes` — быстрая карта Povo (14 нод)\n"
                "`/admin_nodes` — список всех нод\n"
                "`/admin_addnode` — добавить ноду через команду\n"
                "`/admin_start` — запустить игру\n"
                "`/admin_reset` — сбросить ноды и счёт\n"
                "`/admin_setmode A|B` — режим игры\n"
                "`/admin_debug` — диагностика состояния\n\n"
                "*Видео и разбор:*\n"
                "`/admin_presentation` — открыть /presentation для записи\n"
                "`/admin_replay` — хронология всех событий\n\n"
                "*Фейковые игроки (для одиночного теста):*\n"
                "`/admin_spawn opposition ALICE` — создать фейка\n"
                "`/admin_move ALICE lat lon` — переместить фейка\n"
                "`/admin_fake_capture ALICE TEST1` — фейк начинает захват\n"
                "`/admin_fake_defend BOB TEST1` — фейк-System замораживает\n"
                "`/admin_fakes` — список фейков\n"
                "`/admin_unspawn ALICE` — удалить фейка\n"
                "`/admin_unspawn all` — удалить всех\n\n"
                "*Полное демо:* запусти `python3 demo_scenario.py` "
                "параллельно с ботом — оно проиграет полный сценарий."
            )
    except Exception:
        pass

    await message.answer(text, parse_mode="Markdown")
