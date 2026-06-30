from aiogram import Router, F
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
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
        keyboard=[[KeyboardButton(text="📍 Send geolocation", request_location=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

def fmt_time(iso: str) -> str:
    try: return datetime.fromisoformat(iso).strftime("%H:%M %d/%m")
    except: return iso[:16]


# ── /finale ───────────────────────────────────────────────────────────────────

@router.message(Command("finale"))
async def cmd_finale(message: Message):
    """Open the final-scene UI (System only)."""
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("Only System players can open the final-scene UI.")
        return
    state = await db.get_game_state()
    if not state or not state.get("finale_stage"):
        await message.answer("Final scene is not active.")
        return
    url = (config.SERVER_URL or "").rstrip("/") + "/finale"
    if not url.startswith("https://"):
        await message.answer(f"Open in your browser: {url}")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎯 Open final scene", web_app=WebAppInfo(url=url))
    ]])
    await message.answer("Final scene ready. Open it together with the rest of System.", reply_markup=kb)


# ── /defend ───────────────────────────────────────────────────────────────────

@router.message(Command("defend"))
async def cmd_defend(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("This command is for System only.")
        return
    game_state = await db.get_game_state()
    if not game_state or not game_state["active"]:
        await message.answer("The game has not started yet.")
        return
    await message.answer("Send your geolocation to check for attacked nodes nearby.", reply_markup=geo_keyboard())


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
            f"⏸ Capture of *{node['name']}* is frozen!\n"
            f"{'🆔 Logged: `' + anon + '`' if new_id else '🆔 Already in database.'}"
        )
        if opp_id:
            try:
                await message.bot.send_message(
                    opp_id,
                    f"⛔️ Capture of *{node['name']}* is frozen — System is nearby.\nLeave or wait until they go away.",
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
            results.append(f"📡 *{node['name']}* — spotted `{anon}` again")

    if not results:
        nearby_any = find_nodes_containing_player(lat, lon, nodes_list)
        if nearby_any:
            names = ", ".join(n["node"]["name"] for n in nearby_any)
            await message.answer(f"You are near nodes: {names}\nNo attacks detected — all clear.")
        else:
            await message.answer("You are not inside any node's area.\nContinue patrolling.")
        return

    await message.answer("\n\n".join(results), parse_mode="Markdown")


# ── /ids ──────────────────────────────────────────────────────────────────────

@router.message(Command("ids"))
async def cmd_ids(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("This command is for System only.")
        return

    ids = await db.get_identifications(message.from_user.id)
    if not ids:
        await message.answer("No logs yet. Use /defend and look for attacked nodes.")
        return

    lines = [f"📋 *Your logs ({len(ids)}):*\n"]
    for i, row in enumerate(ids, 1):
        location = f"node #{row['node_id']}" if row["node_id"] else "?"
        anon = dict(row).get("anonymous_id") or "AGENT_????"
        lines.append(f"{i}. `{anon}` — {location} at {fmt_time(row['identified_at'])}")

    await message.answer("\n".join(lines), parse_mode="Markdown")


# ── /team_ids ─────────────────────────────────────────────────────────────────

@router.message(Command("team_ids", "teamids"))
async def cmd_team_ids(message: Message):
    """Team-wide knowledge of Opposition AGENT-IDs. Pulls from two sources
    of identification:
      - identifications: physical /defend tags (System walked up to an
        Opposition player and confirmed who they are)
      - verifications WHERE correct=1: correct QR-scan guesses (mid-game or
        finale). Same information from the team's perspective.
    Wrong QR guesses are excluded — they reveal nothing."""
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("This command is for System only.")
        return

    ids = await db.get_all_identifications()
    verifs = await db.get_all_verifications()

    agents: dict = {}

    for row in ids:
        anon = dict(row).get("anonymous_id") or "AGENT_????"
        ts = row["identified_at"]
        a = agents.setdefault(anon, {"count": 0, "last_seen": ts, "nodes": set(), "via": set()})
        a["count"] += 1
        a["via"].add("defend")
        if row["node_id"]:
            a["nodes"].add(row["node_id"])
        if ts and ts > a["last_seen"]:
            a["last_seen"] = ts

    for row in verifs:
        if not row["correct"]:
            continue  # wrong guesses reveal nothing
        anon = dict(row).get("guessed_anonymous_id") or "AGENT_????"
        ts = row["verified_at"]
        a = agents.setdefault(anon, {"count": 0, "last_seen": ts, "nodes": set(), "via": set()})
        a["count"] += 1
        a["via"].add("QR")
        if ts and (not a["last_seen"] or ts > a["last_seen"]):
            a["last_seen"] = ts

    if not agents:
        await message.answer("The team has not identified anyone yet.")
        return

    lines = ["📋 *System team logs:*\n"]
    for i, (anon, info) in enumerate(agents.items(), 1):
        nodes_str = ", ".join(f"#{n}" for n in info["nodes"]) if info["nodes"] else "?"
        via_str = "+".join(sorted(info["via"]))
        lines.append(
            f"{i}. `{anon}` — {info['count']}x ({via_str})\n"
            f"   nodes: {nodes_str}, last seen {fmt_time(info['last_seen'])}"
        )
    lines.append(f"\n*Unique agents: {len(agents)}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")


# ── /verify ───────────────────────────────────────────────────────────────────

class VerifyStates(StatesGroup):
    waiting_for_qr = State()
    waiting_for_guess = State()


@router.message(Command("verify"))
async def cmd_verify(message: Message, state: FSMContext):
    player = await db.get_player(message.from_user.id)
    if not player or player["team"] != "system":
        await message.answer("This command is for System only.")
        return
    await message.answer(
        "📷 Send a photo of the hacker's QR code or paste text from their /myqr.\n\n"
        "Ask the hacker to open /myqr and show their screen."
    )
    await state.set_state(VerifyStates.waiting_for_qr)


@router.message(VerifyStates.waiting_for_qr, F.photo)
async def verify_got_photo(message: Message, state: FSMContext):
    """Decode the QR from the uploaded photo. We prefer OpenCV (single pip
    wheel, no system deps), and fall back to pyzbar if OpenCV isn't there
    but pyzbar is — covers either install path."""
    try:
        import io
        from PIL import Image
        import numpy as np

        file = await message.bot.get_file(message.photo[-1].file_id)
        buf = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buf)
        buf.seek(0)
        img = Image.open(buf).convert("RGB")

        decoded_text = None

        # Path 1: OpenCV (recommended — installs from a single self-contained
        # pip wheel on every OS, no apt/brew step needed).
        try:
            import cv2
            arr = np.array(img)
            arr_bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            detector = cv2.QRCodeDetector()
            data, _pts, _qr = detector.detectAndDecode(arr_bgr)
            if data:
                decoded_text = data
        except ImportError:
            pass

        # Path 2: pyzbar fallback (only used if cv2 isn't installed but
        # pyzbar + libzbar are). Older deployments may still have this.
        if not decoded_text:
            try:
                from pyzbar.pyzbar import decode as qr_decode
                results = qr_decode(img)
                if results:
                    decoded_text = results[0].data.decode("utf-8")
            except ImportError:
                pass

        if decoded_text is None:
            await message.answer(
                "📸 Could not read the QR from this photo.\n"
                "Try a sharper, closer shot, or paste the QR text from /myqr."
            )
            return
        await _process_qr(message, state, decoded_text)
    except ImportError as e:
        # Neither OpenCV nor pyzbar is installed (Pillow / numpy also possible).
        # Tell the admin exactly what to do.
        await message.answer(
            "📸 QR auto-reading is not available on this server.\n\n"
            f"<i>(Reason: {e})</i>\n\n"
            "<b>To enable it, run:</b>\n"
            "<code>pip install -r requirements.txt</code>\n\n"
            "<b>Workaround:</b> ask the hacker to send you the QR text from /myqr "
            "and paste it here. Format:\n"
            "<code>GPSGAME:PLAYER:id:AGENT_XXXX</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"Error reading QR: {e}")


@router.message(VerifyStates.waiting_for_qr, F.text)
async def verify_got_text(message: Message, state: FSMContext):
    # If the player sent a command — exit FSM, do not intercept
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Verification canceled. Start /verify again when ready.")
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
        await message.answer("Invalid format. Expected GPSGAME:PLAYER:id:AGENT_XXXX")
        return

    await state.update_data(scanned_player_id=scanned_id, real_anon=real_anon)

    ids = await db.get_identifications(message.from_user.id)
    agent_logs = [r for r in ids if dict(r).get("anonymous_id") == real_anon]

    if agent_logs:
        hint = f"\n\nYour logs contain {len(agent_logs)} entry(ies) for this agent:"
        for r in agent_logs[:3]:
            hint += f"\n• node #{r['node_id']} at {fmt_time(r['identified_at'])}"
    else:
        hint = "\n\nNo logs found for this agent."

    # Without parse_mode — special characters in anonymous_id or usernames can break Markdown
    await message.answer(
        f"✅ QR decoded successfully.{hint}\n\n"
        f"What is this player's AGENT-ID?\nEnter AGENT_XXXX (e.g. AGENT_DC21) or type 'skip'."
    )
    await state.set_state(VerifyStates.waiting_for_guess)


@router.message(VerifyStates.waiting_for_guess, F.text)
async def verify_got_guess(message: Message, state: FSMContext):
    # If the player sent a command — exit FSM, do not intercept
    if message.text.startswith("/"):
        await state.clear()
        await message.answer("Verification canceled. Start /verify again when ready.")
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
        await message.answer(f"❌ {result.get('error', 'Error')}")
        return

    if result["correct"]:
        await message.answer(
            f"✅ Correct! It's {result['real_anonymous_id']}.\n+15 points for System team!"
        )
        # Notify opposition that they were identified (only for real players)
        if scanned_id and scanned_id > 0:
            try:
                await message.bot.send_message(
                    scanned_id,
                    "🚨 YOU HAVE BEEN IDENTIFIED.\n\n"
                    "System matched your QR code with your AGENT-ID "
                    f"({result['real_anonymous_id']}).\n"
                    "Your anonymity is compromised — they now know it was you.\n\n"
                    "+15 points for System team."
                )
            except Exception:
                pass
    else:
        await message.answer(
            f"❌ Incorrect.\nYou guessed: {result['guessed']}\nActual agent: {result['real_anonymous_id']}"
        )
        # Notify opposition that they escaped
        if scanned_id and scanned_id > 0:
            try:
                await message.bot.send_message(
                    scanned_id,
                    "🕵 YOU ESCAPED.\n\n"
                    "System tried to identify you but guessed the wrong AGENT-ID.\n"
                    "Your identity remains hidden — you are still anonymous.\n\n"
                    "No points awarded to System team."
                )
            except Exception:
                pass


# ── /score ────────────────────────────────────────────────────────────────────

@router.message(Command("score"))
async def cmd_score(message: Message):
    player = await db.get_player(message.from_user.id)
    if not player:
        await message.answer("Please register first — /start")
        return

    from game.scoring import compute_team_scores
    s = await compute_team_scores(
        points_per_node=config.POINTS_PER_NODE,
        points_per_agent=config.POINTS_PER_IDENTIFICATION,
    )

    await message.answer(
        f"📊 *Current Score:*\n\n"
        f"⚙️ System: *{s['sys_base']}* points\n"
        f"  • Nodes: {s['sys_nodes']}/{s['total_scorable']} "
        f"(anchors and finale hub excluded)\n"
        f"  • Agents identified: {s['unique_agents']}\n\n"
        f"🔴 Opposition: *{s['opp_base']}* points\n"
        f"  • Nodes: {s['opp_nodes']}/{s['total_scorable']}",
        parse_mode="Markdown"
    )