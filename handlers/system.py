from aiogram import Router, F
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime

import database as db
from game.geo import find_nodes_in_radius, find_nodes_containing_player
import config

router = Router()


def geo_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Отправить геолокацию", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def fmt_time(iso: str) -> str:
    try: return datetime.fromisoformat(iso).strftime("%H:%M %d/%m")
    except: return iso[:16]


# ── /defend ───────────────────────────────────────────────────────────────────

@router.message(Command("defend"))
async def cmd_defend(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("Эта команда только для System.")
        return
    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("Игра ещё не началась.")
        return
    await message.answer("Отправь геолокацию — проверим атакованные ноды рядом.", reply_markup=geo_keyboard())


@router.message(F.location)
async def handle_location_system(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        return

    lat = message.location.latitude
    lon = message.location.longitude
    await db.update_player_location(message.from_user.id, lat, lon)

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]
    results = []

    attacked = [n for n in nodes_list if n["capture_started_at"] and n["owner"] == "system" and not n["capture_frozen"]]
    nearby_attacked = find_nodes_containing_player(lat, lon, attacked)

    for item in nearby_attacked:
        node = item["node"]
        opp_id = node["capturing_player_id"]
        await db.freeze_node_capture(node["id"])
        new_id = await db.add_identification(
            system_player_id=message.from_user.id,
            opp_player_id=opp_id,
            node_id=node["id"], lat=lat, lon=lon
        )
        opp_player = await db.get_player(opp_id) if opp_id else None
        anon = opp_player["anonymous_id"] if opp_player and opp_player["anonymous_id"] else "AGENT_????"
        results.append(
            f"⏸ Захват *{node['name']}* заморожен!\n"
            f"{'🆔 Залогирован: `' + anon + '`' if new_id else '🆔 Уже в базе.'}"
        )
        if opp_id:
            try:
                await message.bot.send_message(
                    opp_id,
                    f"⛔️ Захват *{node['name']}* заморожен — System рядом.\nУходи или жди пока они уйдут.",
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    frozen = [n for n in nodes_list if n["capture_frozen"] and n["owner"] == "system"]
    nearby_frozen = find_nodes_containing_player(lat, lon, frozen)

    for item in nearby_frozen:
        node = item["node"]
        opp_id = node["capturing_player_id"]
        if not opp_id: continue
        new_id = await db.add_identification(
            system_player_id=message.from_user.id,
            opp_player_id=opp_id,
            node_id=node["id"], lat=lat, lon=lon
        )
        if new_id:
            opp_player = await db.get_player(opp_id)
            anon = opp_player["anonymous_id"] if opp_player else "AGENT_????"
            results.append(f"📡 *{node['name']}* — снова замечен `{anon}`")

    if not results:
        nearby_any = find_nodes_containing_player(lat, lon, nodes_list)
        if nearby_any:
            names = ", ".join(n["node"]["name"] for n in nearby_any)
            await message.answer(f"Ты рядом с нодами: {names}\nАтак не обнаружено — всё чисто.")
        else:
            await message.answer("Ты не внутри круга ни одной ноды.\nПродолжай патрулирование.")
        return

    await message.answer("\n\n".join(results), parse_mode="Markdown")


# ── /ids ──────────────────────────────────────────────────────────────────────

@router.message(Command("ids"))
async def cmd_ids(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("Эта команда только для System.")
        return

    ids = await db.get_identifications(message.from_user.id)
    if not ids:
        await message.answer("Логов ещё нет. Используй /defend и выходи на атакованные ноды.")
        return

    lines = [f"📋 *Твои логи ({len(ids)}):*\n"]
    for i, row in enumerate(ids, 1):
        location = f"нода #{row['node_id']}" if row["node_id"] else "?"
        anon = dict(row).get("anonymous_id") or "AGENT_????"
        lines.append(f"{i}. `{anon}` — {location} в {fmt_time(row['identified_at'])}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


# ── /team_ids ─────────────────────────────────────────────────────────────────

@router.message(Command("team_ids", "teamids"))
async def cmd_team_ids(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("Эта команда только для System.")
        return

    ids = await db.get_all_identifications()
    if not ids:
        await message.answer("Команда ещё никого не встретила.")
        return

    agents: dict = {}
    for row in ids:
        anon = dict(row).get("anonymous_id") or "AGENT_????"
        if anon not in agents:
            agents[anon] = {"count": 0, "last_seen": row["identified_at"], "nodes": set()}
        agents[anon]["count"] += 1
        if row["node_id"]:
            agents[anon]["nodes"].add(row["node_id"])
        if row["identified_at"] > agents[anon]["last_seen"]:
            agents[anon]["last_seen"] = row["identified_at"]

    lines = ["📋 *Логи команды System:*\n"]
    for i, (anon, info) in enumerate(agents.items(), 1):
        nodes_str = ", ".join(f"#{n}" for n in info["nodes"]) if info["nodes"] else "?"
        lines.append(
            f"{i}. `{anon}` — {info['count']}x\n"
            f"   ноды: {nodes_str}, последний раз {fmt_time(info['last_seen'])}"
        )
    lines.append(f"\n*Уникальных агентов: {len(agents)}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")


# ── /verify ───────────────────────────────────────────────────────────────────

class VerifyStates(StatesGroup):
    waiting_for_qr = State()
    waiting_for_guess = State()


@router.message(Command("verify"))
async def cmd_verify(message: Message, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("Эта команда только для System.")
        return
    await message.answer(
        "📷 Отправь фото QR-кода хакера или вставь текст из его /myqr.\n\n"
        "Попроси хакера открыть /myqr и показать экран."
    )
    await state.set_state(VerifyStates.waiting_for_qr)


@router.message(VerifyStates.waiting_for_qr, F.photo)
async def verify_got_photo(message: Message, state: FSMContext):
    try:
        from pyzbar.pyzbar import decode as qr_decode
        from PIL import Image
        import io
        file = await message.bot.get_file(message.photo[-1].file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buf)
        buf.seek(0)
        decoded = qr_decode(Image.open(buf))
        if not decoded:
            await message.answer("QR не прочитался. Попробуй ещё раз или введи текст вручную.")
            return
        await _process_qr(message, state, decoded[0].data.decode("utf-8"))
    except ImportError:
        await message.answer(
            "Авточтение QR недоступно.\nВведи данные вручную: `GPSGAME:PLAYER:id:AGENT_XXXX`",
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(f"Ошибка: {e}")


@router.message(VerifyStates.waiting_for_qr, F.text)
async def verify_got_text(message: Message, state: FSMContext):
    # Если игрок отправил команду — выходим из FSM, не перехватываем
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Верификация отменена. Запусти /verify заново когда будешь готов.")
        return
    await _process_qr(message, state, message.text.strip())


async def _process_qr(message: Message, state: FSMContext, qr_text: str):
    try:
        parts = qr_text.split(":")
        if len(parts) < 4 or parts[0] != "GPSGAME" or parts[1] != "PLAYER":
            raise ValueError()
        scanned_id = int(parts[2])
        real_anon = parts[3]
    except Exception:
        await message.answer("Неверный формат. Ожидается GPSGAME:PLAYER:id:AGENT_XXXX")
        return

    await state.update_data(scanned_player_id=scanned_id, real_anon=real_anon)

    ids = await db.get_identifications(message.from_user.id)
    agent_logs = [r for r in ids if dict(r).get("anonymous_id") == real_anon]

    if agent_logs:
        hint = f"\n\nВ твоих логах {len(agent_logs)} запись(ей) с этим агентом:"
        for r in agent_logs[:3]:
            hint += f"\n• нода #{r['node_id']} в {fmt_time(r['identified_at'])}"
    else:
        hint = "\n\nВ твоих логах нет записей с этим агентом."

    # Без parse_mode — спецсимволы в anonymous_id, узернеймах могут ломать Markdown
    await message.answer(
        f"✅ QR прочитан.{hint}\n\n"
        f"Какой AGENT-ID у этого игрока?\nВведи AGENT_XXXX (например AGENT_DC21) или skip."
    )
    await state.set_state(VerifyStates.waiting_for_guess)


@router.message(VerifyStates.waiting_for_guess, F.text)
async def verify_got_guess(message: Message, state: FSMContext):
    # Если игрок отправил команду — выходим из FSM, не перехватываем
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Верификация отменена. Запусти /verify заново когда будешь готов.")
        return
    data = await state.get_data()
    scanned_id = data.get("scanned_player_id")
    guessed = message.text.strip()
    if guessed.lower() == "skip":
        guessed = data.get("real_anon", "")

    result = await db.add_verification(
        system_player_id=message.from_user.id,
        scanned_player_id=scanned_id,
        guessed_anon_id=guessed
    )
    await state.clear()

    if not result.get("ok"):
        await message.answer(f"❌ {result.get('error', 'Ошибка')}")
        return

    if result["correct"]:
        await message.answer(
            f"✅ Верно! Это {result['real_anonymous_id']}.\n+15 очков команде System!"
        )
        # Уведомляем оппозицию что её вычислили (только реальным игрокам)
        if scanned_id and scanned_id > 0:
            try:
                await message.bot.send_message(
                    scanned_id,
                    "🚨 ТЕБЯ ВЫЧИСЛИЛИ.\n\n"
                    "System сопоставила твой QR с твоим AGENT-ID "
                    f"({result['real_anonymous_id']}).\n"
                    "Твоя анонимность раскрыта — теперь они знают что это был именно ты.\n\n"
                    "+15 очков команде System."
                )
            except Exception:
                pass
    else:
        await message.answer(
            f"❌ Неверно.\nТы думал: {result['guessed']}\nНа самом деле: {result['real_anonymous_id']}"
        )
        # Уведомляем оппозицию что она ускользнула
        if scanned_id and scanned_id > 0:
            try:
                await message.bot.send_message(
                    scanned_id,
                    "🕵 ТЫ УСКОЛЬЗНУЛ.\n\n"
                    "System пыталась тебя вычислить, но угадала неверный AGENT-ID.\n"
                    "Твоя личность остаётся в тени — ты всё ещё анонимен.\n\n"
                    "Очки команде System не засчитаны."
                )
            except Exception:
                pass


# ── /score ────────────────────────────────────────────────────────────────────

@router.message(Command("score"))
async def cmd_score(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Сначала зарегистрируйся — /start")
        return

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]
    system_nodes = len([n for n in nodes_list if n["owner"] == "system"])
    opp_nodes = len([n for n in nodes_list if n["owner"] == "opposition"])
    total = len(nodes_list)

    all_ids = await db.get_all_identifications()
    # Уникальные агенты (не пары, не COUNT(*))
    unique_agents = len(set(r["anonymous_id"] for r in all_ids if dict(r).get("anonymous_id")))

    all_verifs = await db.get_all_verifications()
    correct_verifs = len([v for v in all_verifs if v["correct"]])

    system_score = (
        system_nodes * config.POINTS_PER_NODE
        + unique_agents * config.POINTS_PER_IDENTIFICATION
        + correct_verifs * 15
    )
    opp_score = opp_nodes * config.POINTS_PER_NODE

    await message.answer(
        f"📊 *Текущий счёт:*\n\n"
        f"⚙️ System: *{system_score}* очков\n"
        f"  • Ноды: {system_nodes}/{total}\n"
        f"  • Агентов в логах: {unique_agents}\n"
        f"  • Верных верификаций: {correct_verifs}\n\n"
        f"🔴 Opposition: *{opp_score}* очков\n"
        f"  • Ноды: {opp_nodes}/{total}",
        parse_mode="Markdown"
    )
