from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional
import aiosqlite
import asyncio
import json
from datetime import datetime, timedelta
from math import radians, sin, cos, sqrt, atan2

import config

DB_PATH = "game.db"
CAPTURE_TIME_SEC = 180
PHASE_DURATION_SEC = 20 * 60
LOCATION_FRESH_SEC = 90  # location is considered fresh for 90 sec (the map pings every 30)

# ── Finale (rendezvous + identification) ─────────────────────────────────────
# Pulled from config so admins can tune per-game; values below are fallbacks.
RENDEZVOUS_PHASE_SEC = getattr(config, "RENDEZVOUS_PHASE_SEC", 5 * 60)
IDENTIFICATION_PHASE_SEC = getattr(config, "IDENTIFICATION_PHASE_SEC", 5 * 60)
RENDEZVOUS_RADIUS_M = getattr(config, "RENDEZVOUS_RADIUS_M", 50)
FINAL_CORRECT_POINTS = getattr(config, "FINAL_CORRECT_POINTS", 15)
FINAL_AUTO_ID_POINTS = getattr(config, "FINAL_AUTO_ID_POINTS", 10)
FINAL_OPPOSITION_SURVIVAL_POINTS = getattr(config, "FINAL_OPPOSITION_SURVIVAL_POINTS", 20)
FINAL_WRONG_PENALTY = getattr(config, "FINAL_WRONG_PENALTY", 10)
POINTS_PER_NODE = getattr(config, "POINTS_PER_NODE", 10)
POINTS_PER_IDENTIFICATION = getattr(config, "POINTS_PER_IDENTIFICATION", 5)
FINAL_SWEEP_BONUS = getattr(config, "FINAL_SWEEP_BONUS", 50)
OPPOSITION_CHAIN_BONUS = getattr(config, "OPPOSITION_CHAIN_BONUS", 50)
CAPTURE_WINDOW_SEC = getattr(config, "CAPTURE_WINDOW_SEC", 180)
PUZZLE_COOLDOWN_SEC = getattr(config, "PUZZLE_COOLDOWN_SEC", 60)
PUZZLE_TIME_CUT_PERCENT = getattr(config, "PUZZLE_TIME_CUT_PERCENT", 80)

# Bot instance — injected from bot.py
_bot = None
def set_bot(b): global _bot; _bot = b

async def tg_send(chat_id: int, text: str, parse_mode: str = "Markdown"):
    """Send a Telegram message. Tries the requested parse_mode first; on
    failure (bad entities from underscores/asterisks/angle-brackets in
    names) falls back to plain text with formatting chars stripped, so the
    user still gets the notification.

    HTML mode is preferred for messages that interpolate names containing
    underscores (e.g. FINAL_SCENE_AUTO) — Markdown legacy treats _ as the
    italic delimiter, breaking on unmatched ones."""
    if not _bot:
        return
    try:
        await _bot.send_message(chat_id, text, parse_mode=parse_mode)
        return
    except Exception as e:
        print(f"[tg] {parse_mode} failed for {chat_id}: {e} — retrying plain")
    try:
        plain = (text.replace("*", "").replace("_", "").replace("`", "")
                     .replace("<b>", "").replace("</b>", "")
                     .replace("<i>", "").replace("</i>", "")
                     .replace("<code>", "").replace("</code>", ""))
        await _bot.send_message(chat_id, plain)
    except Exception as e:
        print(f"[tg] plain failed for {chat_id}: {e}")


async def tg_send_with_webapp(chat_id: int, text: str, button_text: str, web_app_url: str):
    """Send a Telegram message with an inline button that opens a WebApp.
    Falls back to plain tg_send if anything goes wrong (e.g. URL is not https,
    which Telegram requires for WebApp buttons)."""
    if not _bot:
        return
    if not web_app_url or not web_app_url.startswith("https://"):
        # WebApp buttons require HTTPS — fall back to a plain notification
        await tg_send(chat_id, text + f"\n\nOpen: {web_app_url or '(no URL)'}")
        return
    try:
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, web_app=WebAppInfo(url=web_app_url))
        ]])
        await _bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        print(f"[tg] webapp button failed for {chat_id}: {e} — falling back")
        await tg_send(chat_id, text)


# Position history for presentation mode (in memory, not DB)
# player_id -> [(lat, lon, timestamp_iso), ...]  up to 30 points per player
from collections import deque, defaultdict
_location_history = defaultdict(lambda: deque(maxlen=30))



# ── Helpers ───────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def is_fresh(ts_str: str, max_sec: int = LOCATION_FRESH_SEC) -> bool:
    if not ts_str: return False
    try: return (datetime.now() - datetime.fromisoformat(ts_str)).total_seconds() <= max_sec
    except: return False


# ── WebSocket manager ─────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self): self.connections = {}

    async def connect(self, ws, player_id):
        await ws.accept()
        self.connections[player_id] = ws

    def disconnect(self, player_id):
        self.connections.pop(player_id, None)

    async def send_to(self, player_id, data):
        ws = self.connections.get(player_id)
        if ws:
            try: await ws.send_text(json.dumps(data))
            except: self.connections.pop(player_id, None)

manager = ConnectionManager()


# ── Request models ────────────────────────────────────────────────────────────

class CaptureRequest(BaseModel):
    player_id: int
    node_id: Optional[int] = None
    lat: float
    lon: float

class DefendRequest(BaseModel):
    player_id: int
    lat: float
    lon: float
    node_id: Optional[int] = None

class LocationPingRequest(BaseModel):
    player_id: int
    lat: float
    lon: float

class AdminNodeRequest(BaseModel):
    name: str
    lat: float
    lon: float
    node_type: str = "node"
    radius: float = 80.0
    max_radius_m: Optional[float] = None  # per-node growth cap; None → server fallback

class VerifyRequest(BaseModel):
    system_player_id: int
    scanned_player_id: int
    guessed_anonymous_id: str


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_nodes():
    """Returns nodes + active_puzzle field (None / 'active' / 'frozen')."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM nodes") as cur:
            nodes = [dict(r) for r in await cur.fetchall()]
        active_puzzles = {}
        try:
            async with db.execute(
                "SELECT node_id, status FROM puzzle_sessions WHERE status IN ('active', 'frozen')"
            ) as cur:
                for s in await cur.fetchall():
                    existing = active_puzzles.get(s["node_id"])
                    if existing != "frozen":
                        active_puzzles[s["node_id"]] = s["status"]
        except Exception:
            pass
    for n in nodes:
        n["active_puzzle"] = active_puzzles.get(n["id"])
    return nodes

async def get_player(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM players WHERE telegram_id=?", (telegram_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

async def get_all_players(team: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM players WHERE team=?" if team else "SELECT * FROM players"
        args = (team,) if team else ()
        async with db.execute(q, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

async def get_game_state():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM game_state WHERE id=1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


# ── Fog of war ────────────────────────────────────────────────────────────────

def is_finale_visible(state) -> bool:
    """The finale hub is hidden from both teams until the chain is built
    and the finale phase starts."""
    return bool(state and state.get("finale_stage"))


def strip_finale_nodes(nodes, state):
    """Remove finale-type nodes from a list unless finale is active."""
    if is_finale_visible(state):
        return nodes
    return [n for n in nodes if n.get("node_type") != "finale"]


def filter_nodes_for_opposition(all_nodes, player_lat=None, player_lon=None):
    # Quick exit: if the deployment opted into full visibility, Opposition
    # sees the entire board — same as System. Skips all the fog-of-war
    # logic below. Toggle via OPPOSITION_SEES_ALL_NODES in config.py.
    if getattr(config, "OPPOSITION_SEES_ALL_NODES", True):
        return list(all_nodes)

    visible_ids = set()
    opp_nodes = [n for n in all_nodes if n["owner"] == "opposition"]
    regular_nodes = [n for n in all_nodes if n.get("node_type", "node") == "node"]

    # Targets are always visible
    for n in all_nodes:
        if n.get("name") and ("ALEX" in n["name"] or "BEATRICE" in n["name"]):
            visible_ids.add(n["id"])

    # Core is always visible
    for n in all_nodes:
        if n.get("node_type") == "core":
            visible_ids.add(n["id"])

    # The 2 starter nodes — geographically nearest to NODE ALEX (not by id)
    targets = [n for n in all_nodes if n.get("name") and "ALEX" in n["name"]]
    non_target = [n for n in regular_nodes if n["id"] not in visible_ids]
    if targets:
        anchor = targets[0]
        non_target_sorted = sorted(non_target, key=lambda n: haversine(anchor["lat"], anchor["lon"], n["lat"], n["lon"]))
    else:
        non_target_sorted = sorted(non_target, key=lambda n: n["id"])
    for n in non_target_sorted[:2]:
        visible_ids.add(n["id"])

    # Own captured nodes
    for n in opp_nodes:
        visible_ids.add(n["id"])

    # Nodes within range of own nodes
    for own in opp_nodes:
        r = own.get("current_radius_m") or own.get("base_radius_m") or 80
        for other in regular_nodes:
            if other["id"] not in visible_ids:
                if haversine(own["lat"], own["lon"], other["lat"], other["lon"]) <= r:
                    visible_ids.add(other["id"])

    # Node the player is standing on
    if player_lat is not None and player_lon is not None:
        for n in regular_nodes:
            if n["id"] not in visible_ids:
                if haversine(player_lat, player_lon, n["lat"], n["lon"]) <= (n.get("base_radius_m") or 80):
                    visible_ids.add(n["id"])

    # Hub next to a captured node
    for n in all_nodes:
        if n.get("node_type") == "hub" and n["id"] not in visible_ids:
            for own in opp_nodes:
                if haversine(n["lat"], n["lon"], own["lat"], own["lon"]) <= 400:
                    visible_ids.add(n["id"])
                    break

    return [n for n in all_nodes if n["id"] in visible_ids]


# ── Ally positions ────────────────────────────────────────────────────────────

async def get_allies(player_id: int, team: str) -> list:
    players = await get_all_players(team)
    allies = []
    for p in players:
        if p["telegram_id"] == player_id: continue
        if not is_fresh(p.get("last_location_at")): continue
        lat, lon = p.get("last_location_lat"), p.get("last_location_lon")
        if lat is None or lon is None: continue
        allies.append({"player_id": p["telegram_id"], "username": p.get("username", "?"), "lat": lat, "lon": lon})
    return allies


# ── Victory check ─────────────────────────────────────────────────────────────

async def check_victory_now():
    """Instant chain check fired after a puzzle solve. Uses the exact same
    geometry as scheduler.check_victory — mutual coverage between regular
    nodes, one-way coverage for targets (anchors) — so the puzzle path and
    the scheduler path can never disagree about whether the chain is closed."""
    from game.geo import find_connected_nodes, check_path_exists

    state = await get_game_state()
    if not state.get("active"): return
    if state.get("finale_stage"): return  # already in finale, don't re-trigger
    target_a, target_b = state.get("target_node_a"), state.get("target_node_b")
    if not target_a or not target_b: return

    nodes = await get_nodes()
    connections = find_connected_nodes(nodes)
    if check_path_exists(target_a, target_b, connections):
        await _start_finale()


def _finale_remaining(state: dict) -> Optional[int]:
    """Seconds left in the current finale stage (rendezvous or identification)."""
    if not state: return None
    started = state.get("finale_stage_started_at")
    stage = state.get("finale_stage")
    if not started or not stage: return None
    duration = RENDEZVOUS_PHASE_SEC if stage == "rendezvous" else IDENTIFICATION_PHASE_SEC
    try:
        t0 = datetime.fromisoformat(started)
        return max(0, int(duration - (datetime.now() - t0).total_seconds()))
    except Exception:
        return None


async def _pick_rendezvous_node():
    """Find the finale hub for the rendezvous point. Admin must have created
    one ahead of time via the admin map (node_type='finale'). Falls back to
    target_a if there is no finale node, so the game still works on old maps."""
    nodes = await get_nodes()
    finale = next((n for n in nodes if n.get("node_type") == "finale"), None)
    if finale:
        return finale

    # Auto-fallback: if the admin forgot to place a FINALE_SCENE node but
    # ALEX and BEATRICE exist, drop one at the midpoint between them with a
    # reasonable default radius. This matches what the + FINALE SCENE button
    # in /admin_map would have created on click. The game should not refuse
    # to start the finale just because the admin missed one button.
    alex     = next((n for n in nodes if "ALEX"     in (n.get("name") or "").upper()), None)
    beatrice = next((n for n in nodes if "BEATRICE" in (n.get("name") or "").upper()), None)
    if alex and beatrice:
        mid_lat = (alex["lat"] + beatrice["lat"]) / 2
        mid_lon = (alex["lon"] + beatrice["lon"]) / 2
        # Half the inter-anchor distance, floor 30 m — same heuristic the
        # admin map applies.
        try:
            dist = haversine(alex["lat"], alex["lon"], beatrice["lat"], beatrice["lon"])
        except Exception:
            dist = 200
        radius = max(30, int(dist / 2))
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO nodes (name,lat,lon,node_type,owner,"
                "current_radius_m,base_radius_m,max_radius_m) "
                "VALUES (?,?,?,?,?,?,?,?)",
                # No underscores in the auto-name — they would break
                # Markdown formatting in pushes like "Walk to *FINAL_SCENE_AUTO*"
                # (Telegram's legacy parser treats _ as italic).
                ("FINAL SCENE", mid_lat, mid_lon, "finale", "system",
                 radius, radius, radius)
            )
            await db.commit()
        print(f"[_pick_rendezvous_node] auto-created finale at midpoint, r={radius}m")
        nodes = await get_nodes()
        finale = next((n for n in nodes if n.get("node_type") == "finale"), None)
        if finale: return finale

    # Last-ditch: legacy behaviour — use target_a (the ALEX anchor)
    state = await get_game_state()
    target_a = state.get("target_node_a") if state else None
    if target_a:
        for n in nodes:
            if n["id"] == target_a: return n
    return nodes[0] if nodes else None


async def _start_finale():
    """Chain is complete — enter the rendezvous stage of the finale.
    Everyone is summoned to a meeting node. Opposition who fail to arrive
    by the end of RENDEZVOUS_PHASE_SEC are auto-identified."""
    state = await get_game_state()
    if state and state.get("finale_stage"):
        return  # already in finale, idempotent
    if not state or not state.get("active"):
        return

    rdv = await _pick_rendezvous_node()
    if not rdv:
        # Chain closed but no finale-hub node was placed. We can't run the
        # finale, but Opposition did achieve their win condition — so we
        # must end the game with the truthful narrative ('Opposition
        # prevails — all anonymous'), not the default 'System holds' that
        # would fire if we passed no breakdown at all.
        opp_players = await get_all_players("opposition")
        real_opp = [p for p in opp_players if p["telegram_id"] >= 0]
        total_opp = len(real_opp)
        print("[_start_finale] No finale-hub node found. "
              "Did the admin forget to place + FINALE SCENE? "
              "Ending game with chain-built narrative.")
        await _end_game(winner="opposition", finale_breakdown={
            "correct": 0, "auto": 0, "wrong": 0,
            "sys_final_points": 0, "opp_final_points": OPPOSITION_CHAIN_BONUS,
            "sweep_bonus": 0,
            "chain_bonus": OPPOSITION_CHAIN_BONUS,
            "wrong_penalty_total": 0,
            "total_opp": total_opp,
            "anonymous_left": total_opp,  # no finale ran → nobody identified there
            "chain_completed": True,
        })
        return

    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE game_state
            SET finale_stage='rendezvous',
                finale_stage_started_at=?,
                rendezvous_node_id=?
            WHERE id=1
        """, (now_iso, rdv["id"]))
        await db.commit()

    minutes = RENDEZVOUS_PHASE_SEC // 60
    # HTML, not Markdown — so node names like FINAL_SCENE_AUTO (with
    # underscores) don't break Telegram's entity parser.
    msg_opp = (
        f"🔗 <b>Chain complete — final scene begins.</b>\n\n"
        f"Walk to <b>{rdv['name']}</b> within {minutes} minutes.\n"
        f"If you do not arrive in the circle in time, you are automatically identified."
    )
    msg_sys = (
        f"🔗 <b>Opposition completed the chain — final scene begins.</b>\n\n"
        f"Walk to <b>{rdv['name']}</b> within {minutes} minutes.\n"
        f"After everyone gathers you will identify them collectively."
    )
    # Per-player logging so we can debug 'I never got the message' without
    # guessing. Counts who was sent vs skipped, and per-team summary.
    sent_sys = sent_opp = skipped = 0
    for p in await get_all_players():
        if p["telegram_id"] < 0:
            skipped += 1; continue  # fake
        team = p.get("team")
        text = msg_sys if team == "system" else msg_opp
        try:
            await tg_send(p["telegram_id"], text, parse_mode="HTML")
            if team == "system": sent_sys += 1
            else: sent_opp += 1
        except Exception as e:
            print(f"[_start_finale] send to {p['telegram_id']} ({team}) failed: {e}")
    print(f"[_start_finale] notified system={sent_sys} opposition={sent_opp} "
          f"(skipped fakes={skipped}) rdv={rdv['name']!r}")
    await broadcast_map_update()

    async def _advance_to_identification():
        s = await get_game_state()
        if s and s.get("active") and s.get("finale_stage") == "rendezvous":
            await _start_identification_stage()

    asyncio.create_task(_run_phase_with_warnings(
        RENDEZVOUS_PHASE_SEC, "Rendezvous", _advance_to_identification,
        expected_stage="rendezvous"
    ))


async def _run_phase_with_warnings(duration_sec: int, phase_label: str,
                                   on_finish, expected_stage: str,
                                   warn_thresholds=None):
    """Sleep for ``duration_sec`` and fire a push notification to every
    registered player when the remaining time crosses each threshold in
    ``warn_thresholds`` (seconds-remaining values, e.g. [150, 60]). On the
    way down, this is essentially a more granular timer that also sends
    reminders so players don't miss the deadline.

    ``expected_stage`` is the value ``game_state.finale_stage`` must hold
    for this task to keep running. If at any tick the stage no longer
    matches (admin reset, game manually ended, finale advanced early via
    scan_verify, etc.) the task exits silently without sending any further
    warnings and without invoking ``on_finish``. This prevents ghost
    notifications and double-finalisations after /admin_reset.

    Defaults: warnings at half-time and at 60 s remaining."""
    if warn_thresholds is None:
        warn_thresholds = [duration_sec // 2, 60]
    # Drop any threshold that's already past at start (e.g. duration < 120 s)
    warn_thresholds = sorted({t for t in warn_thresholds if 0 < t < duration_sec},
                             reverse=True)

    remaining = duration_sec
    fired = set()
    while remaining > 0:
        sleep_step = min(5, remaining)
        await asyncio.sleep(sleep_step)
        remaining -= sleep_step

        # State guard — bail out if the phase changed under us.
        s = await get_game_state()
        if not s or not s.get("active") or s.get("finale_stage") != expected_stage:
            return

        for t in warn_thresholds:
            if t in fired: continue
            if remaining <= t:
                fired.add(t)
                mins = t // 60
                secs = t % 60
                left = f"{mins}:{secs:02d}" if secs else f"{mins} min"
                msg = f"⏳ {phase_label}: {left} left"
                try:
                    for p in await get_all_players():
                        # Skip fakes (negative IDs) — Telegram returns
                        # "chat not found" for them and spams the log.
                        if p["telegram_id"] < 0: continue
                        await tg_send(p["telegram_id"], msg)
                except Exception as e:
                    print(f"[finale] warning push failed: {e}")
                break

    # Final guard before invoking on_finish — same reason as the loop guard.
    s = await get_game_state()
    if not s or not s.get("active") or s.get("finale_stage") != expected_stage:
        return
    try:
        await on_finish()
    except Exception as e:
        print(f"[finale] {phase_label} finish hook error: {e}")


async def _start_identification_stage():
    """Rendezvous time is up. Auto-identify any Opposition player who is not
    inside the meeting circle, then open the identification stage where
    System collectively maps remaining AGENT-IDs to players."""
    state = await get_game_state()
    if not state or not state.get("active"): return
    if state.get("finale_stage") != "rendezvous": return

    rdv_id = state.get("rendezvous_node_id")
    nodes = await get_nodes()
    rdv = next((n for n in nodes if n["id"] == rdv_id), None)
    if not rdv:
        # Rdv node disappeared between rendezvous start and identification —
        # most likely the admin deleted it during the finale. Chain was
        # already complete (we wouldn't be in finale otherwise), so award
        # the chain bonus and end honestly.
        opp_players = await get_all_players("opposition")
        real_opp = [p for p in opp_players if p["telegram_id"] >= 0]
        total_opp = len(real_opp)
        print("[_start_identification_stage] rdv node vanished mid-finale.")
        await _end_game(winner="opposition", finale_breakdown={
            "correct": 0, "auto": 0, "wrong": 0,
            "sys_final_points": 0, "opp_final_points": OPPOSITION_CHAIN_BONUS,
            "sweep_bonus": 0,
            "chain_bonus": OPPOSITION_CHAIN_BONUS,
            "wrong_penalty_total": 0,
            "total_opp": total_opp,
            "anonymous_left": total_opp,
            "chain_completed": True,
        })
        return

    # Check who is inside the rendezvous circle
    radius = max(rdv.get("current_radius_m") or 0, RENDEZVOUS_RADIUS_M)
    cutoff = (datetime.now() - timedelta(seconds=LOCATION_FRESH_SEC)).isoformat()

    no_shows = []
    opp_players = await get_all_players("opposition")
    for p in opp_players:
        if p["telegram_id"] < 0: continue  # skip fakes from auto-identification by default
        lat, lon = p.get("last_location_lat"), p.get("last_location_lon")
        last_at = p.get("last_location_at")
        present = False
        if lat and lon and last_at and last_at >= cutoff:
            dist = haversine(lat, lon, rdv["lat"], rdv["lon"])
            present = dist <= radius
        if not present:
            no_shows.append(p)

    # Auto-identify no-shows: pre-populate final_guesses with correct mapping
    async with aiosqlite.connect(DB_PATH) as db:
        for p in no_shows:
            anon = p.get("anonymous_id")
            if not anon: continue
            await db.execute("""
                INSERT OR REPLACE INTO final_guesses
                (anonymous_id, guessed_player_id, correct, auto_identified)
                VALUES (?, ?, 1, 1)
            """, (anon, p["telegram_id"]))
        await db.execute(
            "UPDATE game_state SET finale_stage='identification', finale_stage_started_at=? WHERE id=1",
            (datetime.now().isoformat(),)
        )
        await db.commit()

    minutes = IDENTIFICATION_PHASE_SEC // 60
    no_show_count = len(no_shows)
    msg_sys = (
        f"🎯 <b>Identification stage</b> — {minutes} minutes.\n\n"
        f"Take each Opposition player one by one, scan their QR, and guess their AGENT-ID.\n"
        + (f"\n{no_show_count} no-show(s) already auto-identified." if no_show_count else "")
    )
    msg_opp = (
        f"🎯 <b>Identification stage</b> — {minutes} minutes.\n\n"
        f"System is identifying you. Stay in the circle and be ready to show your QR."
    )
    finale_url = (getattr(config, "SERVER_URL", "") or "").rstrip("/") + "/finale"
    sent_sys = sent_opp = 0
    for p in await get_all_players():
        if p["telegram_id"] < 0: continue  # skip fakes
        team = p.get("team")
        try:
            if team == "system":
                await tg_send_with_webapp(p["telegram_id"], msg_sys, "🎯 Open final scene", finale_url)
                sent_sys += 1
            else:
                await tg_send(p["telegram_id"], msg_opp, parse_mode="HTML")
                sent_opp += 1
        except Exception as e:
            print(f"[_start_identification_stage] send to {p['telegram_id']} ({team}) failed: {e}")
    print(f"[_start_identification_stage] notified system={sent_sys} opposition={sent_opp}")
    await broadcast_map_update()

    async def _delayed_finish():
        s = await get_game_state()
        # Only finalize if we're still in identification — if scan_verify
        # already finalised earlier, finale_stage will have been cleared.
        if s and s.get("active") and s.get("finale_stage") == "identification":
            await _finalize_game()

    asyncio.create_task(_run_phase_with_warnings(
        IDENTIFICATION_PHASE_SEC, "Identification", _delayed_finish,
        expected_stage="identification"
    ))


async def _finalize_game():
    """Compute final scores using the final_guesses table and end the game.

    Defensive: wraps the whole flow so that any DB hiccup still ends the
    game. Previously a crash here left the game stuck in identification
    stage with no visible reason — the timer fired, _finalize_game raised,
    the exception bubbled into _run_phase_with_warnings, and the user just
    saw nothing happen. Now any failure ends the game with Opposition as a
    tie-breaker winner instead of silently freezing."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT anonymous_id, correct, auto_identified FROM final_guesses") as cur:
                guesses = await cur.fetchall()

        correct_count = sum(1 for _, c, auto in guesses if c and not auto)
        auto_count = sum(1 for _, c, auto in guesses if auto)
        wrong_count = sum(1 for _, c, auto in guesses if not c and not auto)

        sys_final_points = (
            correct_count * FINAL_CORRECT_POINTS
            + auto_count * FINAL_AUTO_ID_POINTS
            - wrong_count * FINAL_WRONG_PENALTY     # punish blind guessing
        )
        # Opposition's main win condition (closing the ALEX↔BEATRICE chain)
        # is what triggered the finale in the first place — award the chain
        # bonus unconditionally here, plus per-survivor anonymity points.
        opp_final_points = (
            wrong_count * FINAL_OPPOSITION_SURVIVAL_POINTS
            + OPPOSITION_CHAIN_BONUS
        )

        # Shared scoring helper — same numbers as /score.
        from game.scoring import compute_team_scores
        s = await compute_team_scores(
            points_per_node=POINTS_PER_NODE,
            points_per_agent=POINTS_PER_IDENTIFICATION,
        )

        # Sweep bonus: if System identified EVERY Opposition player by the
        # end of the game — via mid-game /defend, mid-game QR scan, correct
        # finale guess, or auto-identification of a no-show — give a flat
        # bonus on top. This rewards a perfect information sweep and
        # creates a meaningful System win condition beyond just out-pointing
        # Opposition on captures.
        opp_players = await get_all_players("opposition")
        # Only count real players when judging "did System ID everyone".
        # Fakes (negative IDs) make sweep trivially impossible in the demo,
        # which is fine in real play but inconvenient when testing.
        real_opp = [p for p in opp_players if p["telegram_id"] >= 0]
        sweep_bonus = 0
        if real_opp:
            # All identified = no opposition AGENT-ID is still open at the end.
            # final_guesses covers correct guesses and auto-id; s['unique_agents']
            # covers mid-game IDs from both tables. Easier: compare against the
            # full set of opposition AGENT-IDs.
            all_opp_anons = {p["anonymous_id"] for p in real_opp if p.get("anonymous_id")}
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT anonymous_id FROM final_guesses WHERE correct=1 OR auto_identified=1"
                ) as cur:
                    finale_locked = {r["anonymous_id"] for r in await cur.fetchall() if r["anonymous_id"]}
                async with db.execute(
                    "SELECT anonymous_id FROM identifications WHERE anonymous_id IS NOT NULL"
                ) as cur:
                    defend_locked = {r["anonymous_id"] for r in await cur.fetchall() if r["anonymous_id"]}
                async with db.execute(
                    "SELECT guessed_anonymous_id FROM verifications WHERE correct=1"
                ) as cur:
                    qr_locked = {r["guessed_anonymous_id"] for r in await cur.fetchall() if r["guessed_anonymous_id"]}
            identified_anons = finale_locked | defend_locked | qr_locked
            if all_opp_anons and all_opp_anons.issubset(identified_anons):
                sweep_bonus = FINAL_SWEEP_BONUS

        sys_total = s["sys_base"] + sys_final_points + sweep_bonus
        opp_total = s["opp_base"] + opp_final_points

        # Narrative classification: based on what really happened, not just
        # who scored more. Used by _end_game to pick a story-flavoured ending
        # message instead of dry "X wins, Y points".
        total_opp = len(real_opp)
        # all_opp_anons and identified_anons are already computed above for
        # the sweep check.
        anonymous_left = max(0, total_opp - len(all_opp_anons & identified_anons))

        winner = "system" if sys_total > opp_total else "opposition"
        await _end_game(winner=winner, finale_breakdown={
            "correct": correct_count, "auto": auto_count, "wrong": wrong_count,
            "sys_final_points": sys_final_points + sweep_bonus,
            "opp_final_points": opp_final_points,
            "sweep_bonus": sweep_bonus,
            "chain_bonus": OPPOSITION_CHAIN_BONUS,
            "wrong_penalty_total": wrong_count * FINAL_WRONG_PENALTY,
            "total_opp": total_opp,
            "anonymous_left": anonymous_left,
            "chain_completed": True,
        })
    except Exception as e:
        print(f"[_finalize_game] FAILED: {e} — ending game in fallback mode")
        import traceback; traceback.print_exc()
        # Even on failure the game must end — otherwise it stays
        # finale_stage='identification' forever and players can't restart.
        try:
            await _end_game(winner="opposition")
        except Exception as e2:
            print(f"[_finalize_game] fallback _end_game also failed: {e2}")


def _narrative_ending(finale_breakdown: Optional[dict]) -> tuple:
    """Pick a story-shaped headline + paragraph for the game-over message.

    Three outcomes the game wants to tell, in the user's own framing:

      A. Chain NOT built — Opposition's network was cut off before it spanned
         the city. The central System held; order is preserved.

      B. Chain built, ALL Opposition stayed anonymous — total Opposition
         triumph. The network spanned the city and the agents vanished.
         Hope for reform is alive.

      C. Chain built, at least one Opposition was identified — the goals were
         reached but at a cost. The fight was tense, and some agents were
         caught. (Captured, of course, not killed.)

    Returns (headline_text, paragraph_text) — caller formats it into the
    Telegram message."""
    # No finale info ⇒ chain was never closed.
    if not finale_breakdown or not finale_breakdown.get("chain_completed"):
        return (
            "⚙️ The Central System holds",
            "The Opposition's network was cut before it could span the city. "
            "Surveillance held the line. The System remains intact, its order "
            "preserved — for now."
        )

    anonymous_left = finale_breakdown.get("anonymous_left", 0)
    total_opp = finale_breakdown.get("total_opp", 0)

    if total_opp > 0 and anonymous_left == total_opp:
        # Chain built and not a single Opposition was identified anywhere.
        return (
            "🔴 The Opposition prevails",
            "The chain is closed. Every agent vanished back into the crowd "
            "before the System could put a face to them. The central System "
            "could not unmask the network that broke it. "
            "Hope for reform is alive."
        )

    # Chain built; at least one Opposition was identified.
    caught = total_opp - anonymous_left
    if anonymous_left == 0:
        survivor_line = "Every operative behind it has been captured."
    elif anonymous_left == 1:
        survivor_line = (
            f"{caught} operative(s) were unmasked and captured; "
            "one slipped back into the crowd."
        )
    else:
        survivor_line = (
            f"{caught} operative(s) were unmasked and captured; "
            f"{anonymous_left} slipped back into the crowd."
        )
    return (
        "⚖️ Victory, with sacrifices",
        "The Opposition completed the chain — proof their network can span "
        "the city. But the fight was tense, and the System struck back. "
        f"{survivor_line} (Captured, of course. Not killed.) "
        "The objectives stand."
    )


async def _end_game(winner: str, finale_breakdown: Optional[dict] = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE game_state
            SET active=0, finale_stage=NULL, finale_stage_started_at=NULL,
                rendezvous_node_id=NULL, verification_started_at=NULL
            WHERE id=1
        """)
        await db.commit()

    # Shared scoring helper — same numbers as /score.
    from game.scoring import compute_team_scores
    s = await compute_team_scores(
        points_per_node=POINTS_PER_NODE,
        points_per_agent=POINTS_PER_IDENTIFICATION,
    )
    sys_n, hak_n, unique_ids = s["sys_nodes"], s["opp_nodes"], s["unique_agents"]
    sys_base, opp_base = s["sys_base"], s["opp_base"]

    sys_total = sys_base + (finale_breakdown["sys_final_points"] if finale_breakdown else 0)
    opp_total = opp_base + (finale_breakdown["opp_final_points"] if finale_breakdown else 0)

    # Story-driven ending — what really happened, not just who scored more.
    headline, paragraph = _narrative_ending(finale_breakdown)

    msg = (
        f"🏁 *Game over!*\n\n"
        f"*{headline}*\n\n"
        f"{paragraph}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"⚙️ System: *{sys_total}* points\n"
        f"🔴 Opposition: *{opp_total}* points"
    )
    if finale_breakdown:
        msg += (
            f"\n\n_Final scene:_"
            f"\n• Correct guesses: {finale_breakdown['correct']} × +{FINAL_CORRECT_POINTS}"
            f"\n• Auto-identified (no-shows): {finale_breakdown['auto']} × +{FINAL_AUTO_ID_POINTS}"
            f"\n• Survived anonymity: {finale_breakdown['wrong']} × +{FINAL_OPPOSITION_SURVIVAL_POINTS}"
        )
        if finale_breakdown.get("wrong_penalty_total"):
            msg += f"\n• Wrong-guess penalty: −{finale_breakdown['wrong_penalty_total']} System"
        if finale_breakdown.get("chain_bonus"):
            msg += f"\n• 🔗 *Chain bonus:* +{finale_breakdown['chain_bonus']} Opposition (closed ALEX↔BEATRICE)"
        if finale_breakdown.get("sweep_bonus"):
            msg += f"\n• 🎯 *Sweep bonus:* +{finale_breakdown['sweep_bonus']} System (all Opposition identified)"
    for p in await get_all_players():
        if p["telegram_id"] < 0: continue  # skip fakes
        await tg_send(p["telegram_id"], msg)
    await broadcast_map_update()


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def broadcast_map_update():
    all_nodes = await get_nodes()
    state = await get_game_state()
    phase_remaining_sec = _calc_remaining(state)

    for player_id, ws in list(manager.connections.items()):
        player = await get_player(player_id)
        if not player: continue
        team = player["team"]
        visible = all_nodes if team == "system" else filter_nodes_for_opposition(
            all_nodes, player.get("last_location_lat"), player.get("last_location_lon")
        )
        visible = strip_finale_nodes(visible, state)
        allies = await get_allies(player_id, team)
        try:
            await ws.send_text(json.dumps({
                "type": "map_update", "nodes": visible, "allies": allies,
                "phase": state.get("current_phase", 0), "active": state.get("active", 0),
                "phase_remaining_sec": phase_remaining_sec,
                "finale_stage": state.get("finale_stage"),
                "finale_remaining_sec": _finale_remaining(state),
                "rendezvous_node_id": state.get("rendezvous_node_id"),
            }))
        except: pass


def _calc_remaining(state: dict) -> Optional[int]:
    if state.get("active") and state.get("phase_started_at"):
        try:
            started = datetime.fromisoformat(state["phase_started_at"])
            return max(0, int(PHASE_DURATION_SEC - (datetime.now() - started).total_seconds()))
        except: pass
    return None


# ── App ───────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    # radius_grower removed — now only in scheduler.py (bot) to avoid duplication
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/map", response_class=HTMLResponse)
async def get_map(request: Request):
    """Serve the map with API_BASE injection — fix for iOS Telegram Mini App."""
    with open("map_trento.html", "r", encoding="utf-8") as f:
        html = f.read()
    # API_BASE = current URL the request arrived on (cloudflare tunnel)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    api_base = f"{scheme}://{host}"
    html = html.replace("{{API_BASE}}", api_base)
    return HTMLResponse(content=html)

@app.get("/admin")
async def get_admin(): return FileResponse("admin_map.html")

@app.get("/api/nodes")
async def api_nodes():
    """Public node list — finale hub is hidden until the finale phase starts.
    Use /api/admin/nodes from the admin map to see everything."""
    state = await get_game_state()
    return strip_finale_nodes(await get_nodes(), state)


@app.get("/api/admin/nodes")
async def api_admin_nodes():
    """Admin view — returns every node including the hidden finale hub."""
    return await get_nodes()

@app.get("/api/nodes/{player_id}")
async def api_nodes_for_player(player_id: int, lat: float = None, lon: float = None):
    player = await get_player(player_id)
    if not player: return []
    all_nodes = await get_nodes()
    if lat is not None and lon is not None:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE players SET last_location_lat=?,last_location_lon=?,last_location_at=? WHERE telegram_id=?",
                (lat, lon, datetime.now().isoformat(), player_id)
            )
            await db.commit()
    state = await get_game_state()
    if player["team"] == "system":
        return strip_finale_nodes(all_nodes, state)
    return strip_finale_nodes(filter_nodes_for_opposition(all_nodes, lat, lon), state)


@app.post("/api/location")
async def api_location(req: LocationPingRequest):
    """Background geo ping from the map every 30 sec — keeps the location fresh."""
    player = await get_player(req.player_id)
    if not player: return {"ok": False}
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET last_location_lat=?,last_location_lon=?,last_location_at=? WHERE telegram_id=?",
            (req.lat, req.lon, now_iso, req.player_id)
        )
        await db.commit()

    # Store in history for presentation mode
    _location_history[req.player_id].append((req.lat, req.lon, now_iso))

    return {"ok": True}


@app.get("/presentation")
async def get_presentation(request: Request):
    """Map in presentation mode — for video recording.
    Shows all players with names and movement trails, no interaction buttons."""
    with open("map_trento.html", "r", encoding="utf-8") as f:
        html = f.read()

    # IMPORTANT: inject API_BASE like in /map — otherwise fetch and WebSocket do not work
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    api_base = f"{scheme}://{host}"
    html = html.replace("{{API_BASE}}", api_base)

    # Enable presentation mode
    html = html.replace(
        "var PLAYER_ID = urlParams.get('player_id')",
        "var PRESENTATION_MODE = true;\n  var PLAYER_ID = urlParams.get('player_id') || 'admin'"
    )
    return HTMLResponse(content=html)


@app.get("/api/presentation/players")
async def api_presentation_players():
    """Returns all players with location + trails for presentation mode."""
    players = await get_all_players()
    result = []
    for p in players:
        if not is_fresh(p.get("last_location_at"), max_sec=300): continue
        lat, lon = p.get("last_location_lat"), p.get("last_location_lon")
        if lat is None or lon is None: continue

        # Pull the trail from history
        history = list(_location_history.get(p["telegram_id"], []))
        trail = [{"lat": h[0], "lon": h[1], "ts": h[2]} for h in history]

        result.append({
            "player_id": p["telegram_id"],
            "username": p.get("username", "?"),
            "team": p["team"],
            "anonymous_id": p.get("anonymous_id"),
            "lat": lat, "lon": lon,
            "trail": trail,
        })
    return result
    return {"ok": True}


@app.get("/api/allies/{player_id}")
async def api_allies(player_id: int):
    player = await get_player(player_id)
    if not player: return []
    return await get_allies(player_id, player["team"])


@app.get("/api/game")
async def api_game():
    state = await get_game_state()
    nodes = await get_nodes()
    regular = [n for n in nodes if n.get("node_type", "node") == "node"]
    sys_n = len([n for n in regular if n["owner"] == "system"])
    hak_n = len([n for n in regular if n["owner"] == "opposition"])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(DISTINCT anonymous_id) FROM identifications WHERE anonymous_id IS NOT NULL") as cur:
            row = await cur.fetchone(); unique_ids = row[0] if row else 0

    # Check chain progress ALEX↔BEATRICE
    from game.geo import find_connected_nodes, check_path_exists
    nodes_list = [dict(n) for n in nodes]
    connections = find_connected_nodes(nodes_list)
    target_a = state.get("target_node_a")
    target_b = state.get("target_node_b")
    chain_built = False
    if target_a and target_b:
        chain_built = check_path_exists(target_a, target_b, connections)

    return {
        "phase": state.get("current_phase", 0),
        "active": state.get("active", 0),
        "system_nodes": sys_n, "opp_nodes": hak_n, "total_nodes": len(regular),
        "system_score": sys_n * 10 + unique_ids * 15,
        "opp_score": hak_n * 10,
        "total_ids": unique_ids,
        "phase_remaining_sec": _calc_remaining(state),
        "connections": connections,
        "chain_built": chain_built,
        "target_a": target_a,
        "target_b": target_b,
        "finale_stage": state.get("finale_stage"),
        "finale_remaining_sec": _finale_remaining(state),
        "rendezvous_node_id": state.get("rendezvous_node_id"),
    }


@app.get("/api/player/{telegram_id}")
async def api_player(telegram_id: int):
    player = await get_player(telegram_id)
    if not player: raise HTTPException(404, "Not found")
    return player


@app.post("/api/capture")
async def api_capture(req: CaptureRequest):
    player = await get_player(req.player_id)
    if not player:
        return {"ok": False, "message": "Player not found — try /start in bot"}
    if player["team"] != "opposition":
        return {"ok": False, "message": "Only Opposition can capture nodes"}

    state = await get_game_state()
    if not state or not state.get("active"):
        return {"ok": False, "message": "Game is not active yet — wait for admin to start"}

    if state.get("finale_stage"):
        return {"ok": False, "message": "Final scene in progress — capture is locked."}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET last_location_lat=?,last_location_lon=?,last_location_at=? WHERE telegram_id=?",
            (req.lat, req.lon, datetime.now().isoformat(), req.player_id)
        )
        await db.commit()

    nodes = await get_nodes()
    # Finale hub is non-capturable
    nodes = [n for n in nodes if n.get("node_type") != "finale"]
    if req.node_id:
        candidates = [n for n in nodes if n["id"] == req.node_id and n["owner"] == "system" and n.get("node_type", "node") == "node"]
        if not candidates:
            return {"ok": False, "message": "Node not found or already captured"}
        node = candidates[0]
        dist = haversine(req.lat, req.lon, node["lat"], node["lon"])
        if dist > (node.get("base_radius_m") or 80):
            return {"ok": False, "message": f"Too far ({round(dist)}m). Get within {round(node.get('base_radius_m') or 80)}m"}
    else:
        candidates = [
            n for n in nodes
            if n["owner"] == "system" and n.get("node_type", "node") == "node"
            and haversine(req.lat, req.lon, n["lat"], n["lon"]) <= (n.get("base_radius_m") or 80)
        ]
        if not candidates:
            return {"ok": False, "message": "No System nodes nearby — get closer"}
        node = min(candidates, key=lambda n: haversine(req.lat, req.lon, n["lat"], n["lon"]))

    if node.get("capture_frozen"):
        # FIX: check whether System is really still nearby
        # If System has left — let Opposition resume the capture
        system_players_check = await get_all_players("system")
        system_still_here = False
        for sp in system_players_check:
            if not is_fresh(sp.get("last_location_at")): continue
            slat, slon = sp.get("last_location_lat"), sp.get("last_location_lon")
            if slat is None or slon is None: continue
            if haversine(slat, slon, node["lat"], node["lon"]) <= (node.get("base_radius_m") or 80):
                system_still_here = True
                break

        if system_still_here:
            return {"ok": False, "message": "Capture frozen — System is here. Wait or leave"}

        # System left — resume capture from accumulated elapsed
        # capturing_player_id transfers to whoever pressed (in case it is a different Opposition player)
        elapsed = node.get("capture_elapsed_sec") or 0
        new_start = (datetime.now() - timedelta(seconds=elapsed)).isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE nodes SET capture_frozen=0, freeze_started_at=NULL, capture_started_at=?, capturing_player_id=? WHERE id=?",
                (new_start, req.player_id, node["id"])
            )
            await db.commit()
        await broadcast_map_update()
        remaining_sec = max(0, CAPTURE_TIME_SEC - int(elapsed))
        return {
            "ok": True,
            "resumed": True,
            "node_id": node["id"],
            "node_name": node["name"],
            "capture_time_sec": remaining_sec,
            "message": f"Capture resumed — {remaining_sec//60}m {remaining_sec%60}s left"
        }

    if node["capture_started_at"]:
        started = datetime.fromisoformat(node["capture_started_at"])
        remaining = max(0, CAPTURE_TIME_SEC - int((datetime.now() - started).total_seconds()))
        return {"ok": False, "message": f"Already being captured ({remaining//60}m {remaining%60}s left)"}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET capture_started_at=?,capturing_player_id=?,capture_elapsed_sec=0,capture_frozen=0 WHERE id=?",
            (datetime.now().isoformat(), req.player_id, node["id"])
        )
        await db.execute(
            "INSERT INTO captures (node_id,player_id,started_at) VALUES (?,?,?)",
            (node["id"], req.player_id, datetime.now().isoformat())
        )
        await db.commit()

    await broadcast_map_update()

    # Notify System via Telegram
    for sp in await get_all_players("system"):
        await tg_send(sp["telegram_id"],
            f"🚨 *Node under attack!*\n\nNode *{node['name']}* is in danger.\nYou have {CAPTURE_TIME_SEC//60} min!")

    return {"ok": True, "node_id": node["id"], "node_name": node["name"], "capture_time_sec": CAPTURE_TIME_SEC}


@app.post("/api/defend")
async def api_defend(req: DefendRequest):
    player = await get_player(req.player_id)
    if not player:
        return {"ok": False, "message": "Player not found — try /start in bot"}
    if player["team"] != "system":
        return {"ok": False, "message": "Only System can defend nodes"}

    state = await get_game_state()
    if not state or not state.get("active"):
        return {"ok": False, "message": "Game is not active"}

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET last_location_lat=?,last_location_lon=?,last_location_at=? WHERE telegram_id=?",
            (req.lat, req.lon, datetime.now().isoformat(), req.player_id)
        )
        await db.commit()

    nodes = await get_nodes()

    if req.node_id:
        target = next((n for n in nodes if n["id"] == req.node_id), None)
        if not target: return {"ok": False, "message": "Node not found"}
        dist = haversine(req.lat, req.lon, target["lat"], target["lon"])
        if dist > (target.get("base_radius_m") or 80):
            return {"ok": False, "message": f"Too far ({round(dist)}m). Get closer."}
        attacked = [target] if target["capture_started_at"] and target["owner"] == "system" and not target.get("capture_frozen") else []
    else:
        attacked = [
            n for n in nodes
            if n["capture_started_at"] and n["owner"] == "system" and not n.get("capture_frozen")
            and haversine(req.lat, req.lon, n["lat"], n["lon"]) <= (n.get("base_radius_m") or 80)
        ]

    if not attacked:
        frozen = [n for n in nodes if n.get("capture_frozen") and haversine(req.lat, req.lon, n["lat"], n["lon"]) <= (n.get("base_radius_m") or 80)]
        if frozen:
            return {"ok": True, "results": [{"node": n["name"], "identified": False, "frozen": True} for n in frozen]}
        return {"ok": False, "message": "No attacked nodes nearby"}

    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        for node in attacked:
            opp_id = node["capturing_player_id"]
            started = datetime.fromisoformat(node["capture_started_at"])
            elapsed = (datetime.now() - started).total_seconds() + (node.get("capture_elapsed_sec") or 0)
            await db.execute(
                "UPDATE nodes SET capture_frozen=1,freeze_started_at=?,capture_elapsed_sec=? WHERE id=?",
                (datetime.now().isoformat(), elapsed, node["id"])
            )
            identified = False
            if opp_id:
                async with db.execute(
                    "SELECT id FROM identifications WHERE system_player_id=? AND opp_player_id=? AND identified_at > datetime('now','-5 minutes')",
                    (req.player_id, opp_id)
                ) as cur:
                    recent = await cur.fetchone()
                if not recent:
                    async with db.execute("SELECT anonymous_id FROM players WHERE telegram_id=?", (opp_id,)) as cur:
                        opp = await cur.fetchone()
                    anon = opp["anonymous_id"] if opp and opp["anonymous_id"] else "AGENT_????"
                    await db.execute(
                        "INSERT INTO identifications (system_player_id,opp_player_id,node_id,lat,lon,identified_at,anonymous_id) VALUES (?,?,?,?,?,?,?)",
                        (req.player_id, opp_id, node["id"], req.lat, req.lon, datetime.now().isoformat(), anon)
                    )
                    identified = True
                # Notify the attacker that the timer is frozen
                await tg_send(opp_id,
                    f"⛔️ Capture of *{node['name']}* is frozen — System is nearby.\nLeave or wait until they go.")
            results.append({"node": node["name"], "identified": identified, "frozen": True})
        await db.commit()

    await broadcast_map_update()
    return {"ok": True, "results": results}


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.post("/api/admin/node")
async def admin_add_node(req: AdminNodeRequest):
    name = req.name.strip().upper()
    if not name: return {"ok": False, "message": "Name required"}
    if req.node_type not in ("node", "hub", "core", "finale"):
        return {"ok": False, "message": "Invalid type"}

    # Enforce a single finale hub — a second one would silently confuse
    # _pick_rendezvous_node, which just takes the first match.
    if req.node_type == "finale":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM nodes WHERE node_type='finale'") as cur:
                row = await cur.fetchone()
                if row and row[0] > 0:
                    return {"ok": False, "message": "A finale hub already exists. Delete it first."}

    # Minimum radius is taken from config (default 5m for small maps)
    min_radius = getattr(config, "MIN_NODE_RADIUS_M", 5)
    max_radius = getattr(config, "MAX_NODE_RADIUS_M", 1000)
    radius = max(float(min_radius), min(req.radius, float(max_radius)))

    # Per-node growth cap: explicit if provided, otherwise fall back to config.
    # Clamped: cannot be smaller than the base radius (else the node could
    # never grow at all and would even shrink current_radius_m on edit),
    # and capped at MAX_NODE_RADIUS_M so admins cannot accidentally pin a
    # cap of 50 km.
    fallback_cap = float(getattr(config, "RADIUS_MAX_M", 200))
    requested_cap = req.max_radius_m if req.max_radius_m is not None else fallback_cap
    cap = max(radius, min(float(requested_cap), float(max_radius)))

    target_set = None  # for the response

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO nodes (name,lat,lon,node_type,owner,current_radius_m,base_radius_m,max_radius_m) VALUES (?,?,?,?,?,?,?,?)",
            (name, req.lat, req.lon, req.node_type, "system", radius, radius, cap)
        )
        new_node_id = cursor.lastrowid

        # Auto-assign target_a/b if the name contains ALEX or BEATRICE
        if "ALEX" in name:
            await db.execute("UPDATE game_state SET target_node_a = ? WHERE id = 1", (new_node_id,))
            target_set = "A (ALEX)"
        elif "BEATRICE" in name:
            await db.execute("UPDATE game_state SET target_node_b = ? WHERE id = 1", (new_node_id,))
            target_set = "B (BEATRICE)"

        await db.commit()

    await broadcast_map_update()
    return {"ok": True, "name": name, "target_set": target_set, "node_id": new_node_id, "max_radius_m": cap}


@app.delete("/api/admin/node/{node_id}")
async def admin_delete_node(node_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name FROM nodes WHERE id=?", (node_id,)) as cur:
            node = await cur.fetchone()
        if not node: return {"ok": False, "message": "Node not found"}
        await db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        await db.execute("DELETE FROM captures WHERE node_id=?", (node_id,))
        await db.execute("DELETE FROM identifications WHERE node_id=?", (node_id,))
        await db.commit()
    await broadcast_map_update()
    return {"ok": True, "deleted": node["name"]}


@app.post("/api/admin/reset")
async def admin_reset():
    """Reset all per-run game state so the next start (or demo run) begins
    from a clean slate. Specifically: nodes go back to System with capture
    progress and puzzle history wiped; the game_state row is deactivated
    and finale fields cleared; per-run side tables (final_guesses,
    identifications, verifications, puzzle_sessions) are emptied.

    Map structure is preserved: node positions, base/max radii, and target
    assignments are not touched, so the admin doesn't have to re-seed the
    map between runs.

    Any in-flight finale timer (_run_phase_with_warnings) self-cancels on
    its next tick because it checks finale_stage against the expected
    value, which is now NULL."""
    # Snapshot stage before wiping so we know whether to notify players.
    pre = await get_game_state()
    was_in_finale = bool(pre and pre.get("finale_stage"))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE nodes SET owner='system', capture_started_at=NULL,
            capturing_player_id=NULL, capture_elapsed_sec=0, capture_frozen=0,
            freeze_started_at=NULL, current_radius_m=base_radius_m,
            capture_progress=0, puzzles_solved='',
            attack_window_started_at=NULL, last_puzzle_solved_at=NULL
        """)
        await db.execute("""
            UPDATE game_state
            SET active=0, current_phase=0, verification_started_at=NULL,
                finale_stage=NULL, finale_stage_started_at=NULL,
                rendezvous_node_id=NULL, final_guess_submitted=0
            WHERE id=1
        """)
        await db.execute("DELETE FROM final_guesses")
        await db.execute("DELETE FROM identifications")
        await db.execute("DELETE FROM verifications")
        await db.execute("DELETE FROM puzzle_sessions")
        await db.commit()
    await broadcast_map_update()

    # If the game was mid-finale, players were staring at a countdown — tell
    # them explicitly that it's been cancelled, otherwise they keep waiting
    # for the rendezvous/identification timer to do something.
    if was_in_finale:
        try:
            for p in await get_all_players():
                if p["telegram_id"] < 0: continue  # skip fakes
                await tg_send(p["telegram_id"],
                              "🔄 Admin reset the game. Wait for the next round.")
        except Exception as e:
            print(f"[admin_reset] notify failed: {e}")

    return {"ok": True}


# ── Verify API ────────────────────────────────────────────────────────────────

@app.get("/api/events")
async def api_events():
    """Recent events for the LIVE EVENTS log panel on /presentation.

    Reads from the tables that the puzzle-based capture flow actually
    writes to:
      - puzzle_sessions (status='solved'): a fake/real Opposition solved a
        puzzle (one solve → 80%, second → 100%).
      - identifications: System physically tagged an Opposition player
        next to a node (via /defend).
      - verifications: System guessed an AGENT-ID through a QR scan,
        either mid-game (admin/fake_verify) or in the finale.
      - captures (legacy): the old timed-capture table; still pulled in
        case any legacy fake_capture row exists, so live events on a
        mid-upgrade DB don't look broken.

    Sorted newest-first, capped at 20 rows."""
    events = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Puzzle solves — the main "Opposition is attacking / captured" signal.
        # started_at is used as an approximate timestamp for the solve, since
        # the schema has no separate solved_at column.
        try:
            async with db.execute(
                """SELECT s.started_at AS ts, p.username, p.anonymous_id,
                          n.name AS node_name, s.puzzle_type,
                          s.created_for_progress AS target_progress
                   FROM puzzle_sessions s
                   LEFT JOIN players p ON p.telegram_id = s.player_id
                   LEFT JOIN nodes n ON n.id = s.node_id
                   WHERE s.status='solved'
                   ORDER BY s.started_at DESC LIMIT 15"""
            ) as cur:
                for r in await cur.fetchall():
                    pct = r["target_progress"] or 80
                    arrow = "→ 100%" if pct >= 100 else "→ 80%"
                    events.append({
                        "ts": r["ts"], "type": "capture",
                        "text": f"🧩 {r['username'] or '?'} solved {r['puzzle_type']} on {r['node_name'] or '?'} {arrow}"
                    })
        except Exception:
            pass

        # Identifications (System tagged Opposition next to a node)
        try:
            async with db.execute(
                """SELECT i.identified_at AS ts, sp.username AS sys, i.anonymous_id,
                          n.name AS node_name
                   FROM identifications i
                   LEFT JOIN players sp ON sp.telegram_id = i.system_player_id
                   LEFT JOIN nodes n ON n.id = i.node_id
                   ORDER BY i.identified_at DESC LIMIT 15"""
            ) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "ts": r["ts"], "type": "ident",
                        "text": f"🆔 {r['sys'] or '?'} identified {r['anonymous_id']} at {r['node_name'] or '?'}"
                    })
        except Exception:
            pass

        # Verifications (QR guesses — both mid-game and finale)
        try:
            async with db.execute(
                """SELECT v.verified_at AS ts, sp.username AS sys,
                          v.guessed_anonymous_id AS guess, v.correct
                   FROM verifications v
                   LEFT JOIN players sp ON sp.telegram_id = v.system_player_id
                   ORDER BY v.verified_at DESC LIMIT 15"""
            ) as cur:
                for r in await cur.fetchall():
                    mark = "✓" if r["correct"] else "✗"
                    events.append({
                        "ts": r["ts"], "type": "ident",
                        "text": f"📷 {r['sys'] or '?'} guessed {r['guess']} {mark}"
                    })
        except Exception:
            pass

        # Legacy captures table (timed-capture flow, may be empty)
        try:
            async with db.execute(
                """SELECT c.started_at AS ts, p.username, n.name AS node_name
                   FROM captures c
                   LEFT JOIN players p ON p.telegram_id = c.player_id
                   LEFT JOIN nodes n ON n.id = c.node_id
                   ORDER BY c.started_at DESC LIMIT 15"""
            ) as cur:
                for r in await cur.fetchall():
                    events.append({
                        "ts": r["ts"], "type": "capture",
                        "text": f"⚡ {r['username'] or '?'} started capturing {r['node_name'] or '?'}"
                    })
        except Exception:
            pass

    # Drop rows with no timestamp (shouldn't happen but defensive), then
    # sort newest-first and cap.
    events = [e for e in events if e.get("ts")]
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:20]


@app.post("/api/admin/fake_capture")
async def api_fake_capture(req: dict):
    """Fake player starts a capture — used by demo_scenario via HTTP."""
    player_id = req.get("player_id")
    node_id = req.get("node_id")
    if not player_id or not node_id:
        return {"ok": False, "message": "player_id and node_id required"}
    p = await get_player(player_id)
    n_list = [x for x in await get_nodes() if x["id"] == node_id]
    if not p or not n_list:
        return {"ok": False, "message": "Player or node not found"}
    node = n_list[0]
    if node["owner"] != "system" or node["capture_started_at"]:
        return {"ok": False, "message": "Node not available"}

    import database as db_module
    # Place the fake on the node
    await db_module.update_player_location(player_id, node["lat"], node["lon"])
    _location_history[player_id].append((node["lat"], node["lon"], datetime.now().isoformat()))
    # Start the capture
    await db_module.start_node_capture(node_id, player_id)
    await db_module.create_capture(node_id, player_id)
    await broadcast_map_update()

    # Send notification to real System players
    sys_players = await get_all_players("system")
    for sp in sys_players:
        if sp["telegram_id"] < 0: continue
        await tg_send(sp["telegram_id"], f"🚨 Node *{node['name']}* is under attack!")
    return {"ok": True}


@app.post("/api/admin/fake_defend")
async def api_fake_defend(req: dict):
    """Fake System player freezes a capture — used by demo via HTTP."""
    player_id = req.get("player_id")
    node_id = req.get("node_id")
    if not player_id or not node_id:
        return {"ok": False, "message": "player_id and node_id required"}
    n_list = [x for x in await get_nodes() if x["id"] == node_id]
    if not n_list: return {"ok": False, "message": "Node not found"}
    node = n_list[0]
    if not node["capture_started_at"]:
        return {"ok": False, "message": "Not under attack"}

    import database as db_module
    await db_module.update_player_location(player_id, node["lat"], node["lon"])
    _location_history[player_id].append((node["lat"], node["lon"], datetime.now().isoformat()))
    await db_module.freeze_node_capture(node_id)

    opp_id = node["capturing_player_id"]
    if opp_id:
        await db_module.add_identification(
            system_player_id=player_id, opp_player_id=opp_id,
            node_id=node_id, lat=node["lat"], lon=node["lon"]
        )
        if opp_id > 0:
            await tg_send(opp_id, f"⛔ Capture of *{node['name']}* is frozen — System is nearby.")
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/fake_complete_capture")
async def api_fake_complete(req: dict):
    """Instantly complete a capture — node goes to Opposition. For demo."""
    node_id = req.get("node_id")
    if not node_id: return {"ok": False}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET owner='opposition', capture_started_at=NULL, capturing_player_id=NULL, capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL WHERE id=?",
            (node_id,)
        )
        await db.commit()
    await broadcast_map_update()
    try:
        await check_victory_now()
    except Exception as e:
        print(f"[fake_complete_capture] check_victory_now: {e}")
    return {"ok": True}


@app.post("/api/admin/set_owner")
async def api_set_owner(req: dict):
    """Set node owner (for scenario setup — e.g. give ALEX/BEATRICE to opposition immediately)."""
    node_id = req.get("node_id")
    owner = req.get("owner", "system")
    if not node_id: return {"ok": False}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE nodes SET owner=? WHERE id=?", (owner, node_id))
        await db.commit()
    await broadcast_map_update()
    if owner == "opposition":
        try:
            await check_victory_now()
        except Exception as e:
            print(f"[set_owner] check_victory_now: {e}")
    return {"ok": True}


@app.post("/api/admin/set_radius")
async def api_set_radius(req: dict):
    """Set a node's current radius directly — debug / demo override that
    simulates a long hold without waiting for the scheduler. To keep the
    per-node invariant ``current_radius_m <= max_radius_m`` honest, this
    also raises ``max_radius_m`` to the new value when the override pushes
    current past the cap. Without this, the admin map and player map would
    render `current` overflowing the cap circle, which is both visually
    confusing and physically impossible during real gameplay."""
    node_id = req.get("node_id")
    radius = req.get("radius")
    if not node_id or radius is None: return {"ok": False}
    radius = float(radius)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET current_radius_m=?, "
            "max_radius_m = MAX(COALESCE(max_radius_m, ?), ?) "
            "WHERE id=?",
            (radius, radius, radius, node_id)
        )
        await db.commit()
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/set_max_radius")
async def api_set_max_radius(req: dict):
    """Edit a node's per-node growth cap (max_radius_m). If the current radius
    is above the new cap, current_radius_m is clamped down to match — keeps the
    visual and the rule consistent."""
    node_id = req.get("node_id")
    max_radius_m = req.get("max_radius_m")
    if not node_id or max_radius_m is None:
        return {"ok": False, "message": "node_id and max_radius_m required"}
    # Clamp to global bounds so admin slip-ups don't break the map render.
    min_radius = float(getattr(config, "MIN_NODE_RADIUS_M", 5))
    max_radius = float(getattr(config, "MAX_NODE_RADIUS_M", 1000))
    cap = max(min_radius, min(float(max_radius_m), max_radius))
    import database as _db
    await _db.set_node_max_radius(int(node_id), cap)
    await broadcast_map_update()
    return {"ok": True, "max_radius_m": cap}


@app.get("/api/admin/suggest_max_radius")
async def api_suggest_max_radius(lat: float, lon: float, exclude_node_id: Optional[int] = None):
    """Suggest reasonable min (capture zone) and max (growth cap) radii for a
    new node at (lat, lon).

    Strategy, in order:
      1. If both ALEX and BEATRICE exist on the map, base the suggestion on
         the target-to-target distance D_AB. For a chain through two
         intermediates each circle needs to span roughly D_AB/3, so we use
         that as the default cap — but never less than the distance from the
         clicked point to the nearest target (otherwise the chain can't
         possibly close through this node).
      2. If only the second-nearest-neighbour heuristic is available, use
         that (legacy behaviour: cover the 2nd-nearest neighbour + 20 m).
      3. If the map is empty, fall back to config.RADIUS_MAX_M.

    The min radius (base / capture zone) is fixed at a sensible small default
    so the admin doesn't have to think about it. It can still be overridden
    in the form."""
    fallback_cap = float(getattr(config, "RADIUS_MAX_M", 200))
    min_radius = float(getattr(config, "MIN_NODE_RADIUS_M", 5))
    max_radius = float(getattr(config, "MAX_NODE_RADIUS_M", 1000))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name, lat, lon FROM nodes") as cur:
            rows = await cur.fetchall()

    # Locate ALEX and BEATRICE targets if present.
    alex = next((r for r in rows
                 if "ALEX" in (r["name"] or "")
                 and (exclude_node_id is None or r["id"] != exclude_node_id)), None)
    beatrice = next((r for r in rows
                     if "BEATRICE" in (r["name"] or "")
                     and (exclude_node_id is None or r["id"] != exclude_node_id)), None)

    d_ab = None
    d_to_alex = d_to_beatrice = None
    if alex and beatrice:
        d_ab = haversine(alex["lat"], alex["lon"], beatrice["lat"], beatrice["lon"])
        d_to_alex = haversine(lat, lon, alex["lat"], alex["lon"])
        d_to_beatrice = haversine(lat, lon, beatrice["lat"], beatrice["lon"])

    # Distances to all OTHER nodes (excluding the targets themselves so they
    # don't double-count when we fall through to the neighbour heuristic).
    other_dists = []
    for r in rows:
        if exclude_node_id is not None and r["id"] == exclude_node_id:
            continue
        if alex and r["id"] == alex["id"]: continue
        if beatrice and r["id"] == beatrice["id"]: continue
        other_dists.append(haversine(lat, lon, r["lat"], r["lon"]))
    other_dists.sort()

    # Pick a strategy.
    if d_ab is not None:
        # Target-based: 1/3 of full distance, but never below "reach the
        # nearest target" (otherwise this intermediate can't link to anyone).
        d_to_nearest_target = min(d_to_alex, d_to_beatrice)
        suggestion = max(d_ab / 3, d_to_nearest_target + 20)
        strategy = "targets"
    elif len(other_dists) >= 2:
        suggestion = other_dists[1] + 20
        strategy = "neighbours"
    elif len(other_dists) == 1:
        suggestion = other_dists[0] + 20
        strategy = "neighbours"
    else:
        suggestion = fallback_cap
        strategy = "fallback"

    suggestion = max(min_radius, min(suggestion, max_radius))

    # Suggested min (capture zone) — small fixed default; admins rarely need
    # to tune it per node. 20m is comfortable indoor and outdoor.
    suggested_min = max(min_radius, min(20.0, suggestion))

    return {
        "ok": True,
        "suggested_max_radius_m": round(suggestion, 1),
        "suggested_min_radius_m": round(suggested_min, 1),
        "strategy": strategy,
        "d_alex_beatrice_m": round(d_ab, 1) if d_ab is not None else None,
        "d_to_alex_m": round(d_to_alex, 1) if d_to_alex is not None else None,
        "d_to_beatrice_m": round(d_to_beatrice, 1) if d_to_beatrice is not None else None,
        "nearest_other_distances_m": [round(d, 1) for d in other_dists[:3]],
        "fallback": fallback_cap,
    }


@app.post("/api/admin/fake_interrupt_capture")
async def api_fake_interrupt(req: dict):
    """Reset a node capture (simulate everyone leaving for > 3 minutes) — for demo."""
    node_id = req.get("node_id")
    if not node_id: return {"ok": False}
    import database as db_module
    await db_module.interrupt_node_capture(node_id)
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/fake_verify")
async def api_fake_verify(req: dict):
    """Simulate QR verification: System guesses Opposition AGENT-ID. For demo."""
    sys_id = req.get("system_player_id")
    opp_id = req.get("scanned_player_id")
    guessed = req.get("guessed_anonymous_id")
    if not (sys_id and opp_id and guessed):
        return {"ok": False, "error": "missing params"}
    import database as db_module
    result = await db_module.add_verification(sys_id, opp_id, guessed)
    # Push a notification to the real Opposition player if there is one
    if opp_id > 0 and result.get("ok"):
        if result.get("correct"):
            await tg_send(opp_id, "🚨 YOU HAVE BEEN IDENTIFIED.\n\nSystem matched your QR to an AGENT-ID.")
        else:
            await tg_send(opp_id, "🕵 YOU GOT AWAY.\n\nSystem guessed the wrong AGENT-ID.")
    await broadcast_map_update()
    return result


@app.post("/api/verify")
async def api_verify(req: VerifyRequest):
    system = await get_player(req.system_player_id)
    if not system or system["team"] != "system":
        return {"ok": False, "error": "Only System can verify"}
    import database as db_module
    return await db_module.add_verification(
        req.system_player_id, req.scanned_player_id, req.guessed_anonymous_id
    )


@app.get("/api/player/{telegram_id}/qr-data")
async def api_qr_data(telegram_id: int):
    player = await get_player(telegram_id)
    if not player: raise HTTPException(404, "Not found")
    anon_id = player.get("anonymous_id") or "AGENT_????"
    return {"qr_string": f"GPSGAME:PLAYER:{telegram_id}:{anon_id}", "anonymous_id": anon_id, "team": player["team"]}


# ── Finale API ────────────────────────────────────────────────────────────────

@app.get("/api/finale/state")
async def api_finale_state():
    """Snapshot of the final-scene UI for System: every Opposition player,
    every AGENT-ID, what's already pinned (by mid-game verifications or
    auto-identification), and what System still has to assign.
    Also reports who is currently inside the rendezvous circle, so the
    System UI can highlight who showed up."""
    state = await get_game_state()
    if not state or not state.get("finale_stage"):
        return {"ok": False, "message": "Not in finale"}

    rdv_id = state.get("rendezvous_node_id")
    nodes = await get_nodes()
    rdv = next((n for n in nodes if n["id"] == rdv_id), None)
    rdv_radius = max(rdv.get("current_radius_m") or 0, RENDEZVOUS_RADIUS_M) if rdv else RENDEZVOUS_RADIUS_M
    cutoff = (datetime.now() - timedelta(seconds=LOCATION_FRESH_SEC)).isoformat()

    opp_players = await get_all_players("opposition")
    # AGENT-IDs already pinned during the game come from two tables:
    #   - identifications: System physically tagged Opposition next to a node
    #     (column opp_player_id, anonymous_id). Every row IS a correct ID.
    #   - verifications: QR-scan guess via /api/admin/fake_verify. We only
    #     count rows with correct=1 (scanned_player_id, guessed_anonymous_id).
    # The previous code queried `identifications` for `scanned_player_id` —
    # a column that table doesn't have — and crashed the finale UI.
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT opp_player_id AS player_id, anonymous_id AS guessed "
            "FROM identifications WHERE anonymous_id IS NOT NULL"
        ) as cur:
            id_rows = await cur.fetchall()
        async with db.execute(
            "SELECT scanned_player_id AS player_id, guessed_anonymous_id AS guessed "
            "FROM verifications WHERE correct=1"
        ) as cur:
            verif_rows = await cur.fetchall()
        async with db.execute(
            "SELECT anonymous_id, guessed_player_id, correct, auto_identified "
            "FROM final_guesses"
        ) as cur:
            final_rows = await cur.fetchall()

    verified_during_game = {}
    for r in list(id_rows) + list(verif_rows):
        verified_during_game[r["player_id"]] = r["guessed"]
    final_guesses = {r["anonymous_id"]: dict(r) for r in final_rows}

    # Index final_guesses by which player they reference, so we can attach
    # the per-row result to each Opposition player in the response.
    final_by_player = {}
    for anon, g in final_guesses.items():
        pid = g.get("guessed_player_id")
        if pid: final_by_player[pid] = {"correct": bool(g["correct"]), "guessed": anon, "auto": bool(g.get("auto_identified"))}

    out_players = []
    for p in opp_players:
        anon = p.get("anonymous_id")
        present = False
        if rdv and p.get("last_location_lat") and p.get("last_location_lon") and (p.get("last_location_at") or "") >= cutoff:
            present = haversine(p["last_location_lat"], p["last_location_lon"], rdv["lat"], rdv["lon"]) <= rdv_radius
        pinned_anon = verified_during_game.get(p["telegram_id"])
        auto_id = anon in final_guesses and final_guesses[anon]["auto_identified"]
        # final_result: non-null once a manual scan_verify happened for this player
        fr = final_by_player.get(p["telegram_id"])
        final_result = None
        if fr and not fr["auto"]:
            final_result = {"correct": fr["correct"], "guessed": fr["guessed"]}
        out_players.append({
            "player_id": p["telegram_id"],
            "username": p.get("username") or "",
            "anonymous_id": anon,
            "verified_during_game": pinned_anon,  # AGENT-ID locked in mid-game
            "auto_identified": bool(auto_id),     # no-show, automatically pinned
            "present_at_rendezvous": present,
            "final_result": final_result,        # manual scan result for this row
        })

    # AGENT-IDs that still need a guess (open = not yet locked anywhere)
    all_anons = [p.get("anonymous_id") for p in opp_players if p.get("anonymous_id")]
    locked_anons = (
        set(verified_during_game.values())
        | {a for a, g in final_guesses.items() if g["auto_identified"]}
        | {a for a, g in final_guesses.items() if g["correct"]}
    )
    open_anons = [a for a in all_anons if a not in locked_anons]

    return {
        "ok": True,
        "stage": state.get("finale_stage"),
        "remaining_sec": _finale_remaining(state),
        "rendezvous_node": rdv,
        "rendezvous_radius_m": rdv_radius,
        "players": out_players,
        "open_anonymous_ids": open_anons,
    }


class FinaleScanRequest(BaseModel):
    system_player_id: int
    qr_string: str
    guessed_anonymous_id: str


@app.post("/api/finale/scan_verify")
async def api_finale_scan_verify(req: FinaleScanRequest):
    """A System player scans an Opposition QR and guesses their AGENT-ID.
    Called repeatedly during the identification stage — once per Opposition
    player physically in the lineup. After everyone open is processed the
    game ends automatically."""
    state = await get_game_state()
    if not state or not state.get("active"):
        return {"ok": False, "message": "Game not active"}
    if state.get("finale_stage") != "identification":
        return {"ok": False, "message": "Not in identification stage"}

    # Authz: only System players may verify
    system = await get_player(req.system_player_id)
    if not system or system["team"] != "system":
        return {"ok": False, "message": "Only System can verify"}

    # Parse QR: format is "GPSGAME:PLAYER:<telegram_id>:<AGENT-ID>"
    parts = (req.qr_string or "").split(":")
    if len(parts) < 3 or parts[0] != "GPSGAME" or parts[1] != "PLAYER":
        return {"ok": False, "message": "Invalid QR"}
    try:
        scanned_pid = int(parts[2])
    except ValueError:
        return {"ok": False, "message": "Invalid QR"}

    scanned = await get_player(scanned_pid)
    if not scanned or scanned["team"] != "opposition":
        return {"ok": False, "message": "Scanned player is not Opposition"}

    correct_anon = scanned.get("anonymous_id")
    is_correct = (req.guessed_anonymous_id == correct_anon)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Reject duplicate guesses for an AGENT-ID that is already locked in
        # (either by a no-show auto-id or by an earlier correct scan).
        async with db.execute(
            "SELECT correct, auto_identified FROM final_guesses WHERE anonymous_id=?",
            (req.guessed_anonymous_id,)
        ) as cur:
            existing = await cur.fetchone()
        if existing and (existing["auto_identified"] or existing["correct"]):
            return {"ok": False, "message": "That AGENT-ID is already locked in"}

        await db.execute("""
            INSERT OR REPLACE INTO final_guesses
            (anonymous_id, guessed_player_id, correct, auto_identified)
            VALUES (?, ?, ?, 0)
        """, (req.guessed_anonymous_id, scanned_pid, 1 if is_correct else 0))
        await db.commit()

    # If every open Opposition has now been resolved, end the game.
    fst = await api_finale_state()
    if fst.get("ok") and not fst.get("open_anonymous_ids"):
        await _finalize_game()

    return {
        "ok": True,
        "correct": is_correct,
        "scanned_username": scanned.get("username") or "",
        "actual_anonymous_id": correct_anon if is_correct else None,
        "guessed_anonymous_id": req.guessed_anonymous_id,
    }


@app.get("/finale", response_class=HTMLResponse)
async def finale_page(request: Request):
    """Final-scene UI for System: shows opposition players and lets them
    assign remaining AGENT-IDs collectively."""
    try:
        with open("finale.html", encoding="utf-8") as f:
            html = f.read()
    except FileNotFoundError:
        return HTMLResponse("<h1>finale.html missing</h1>", status_code=500)
    # Use the public-facing host/scheme (cloudflare tunnel) so that fetches
    # from a Telegram Mini App go to the right place. request.base_url alone
    # resolves to http://localhost:8001 because uvicorn doesn't see through
    # the proxy by default — same fix as the /map route.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    api_base = f"{scheme}://{host}"
    html = html.replace("__API_BASE__", api_base)
    return HTMLResponse(html)


@app.websocket("/ws/{player_id}")
async def websocket_endpoint(ws: WebSocket, player_id: int):
    await manager.connect(ws, player_id)
    player = await get_player(player_id)
    all_nodes = await get_nodes()
    state = await get_game_state()

    if not player: visible = []
    elif player["team"] == "system": visible = all_nodes
    else: visible = filter_nodes_for_opposition(all_nodes, player.get("last_location_lat"), player.get("last_location_lon"))

    allies = await get_allies(player_id, player["team"]) if player else []
    await ws.send_text(json.dumps({
        "type": "map_update", "nodes": visible, "allies": allies,
        "team": player["team"] if player else "unknown",
        "phase": state.get("current_phase", 0), "active": state.get("active", 0),
        "phase_remaining_sec": _calc_remaining(state),
    }))
    try:
        while True: await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        manager.disconnect(player_id)


# ── Background: radius grower ─────────────────────────────────────────────────
# capture_checker and contested_checker were removed — they live only in scheduler.py (bot).
# Duplication on a single SQLite caused race conditions.

# radius_grower removed — now only in scheduler.py


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)


# ─────────────────────────────────────────────────────────────────────────────
# PUZZLES
# ─────────────────────────────────────────────────────────────────────────────

from game import puzzles as puzzle_module
import database as _db_mod


class PuzzleStartReq(BaseModel):
    player_id: int
    node_id: int
    puzzle_type: str


class PuzzleSubmitReq(BaseModel):
    session_id: str
    solution: dict


class PuzzleHeartbeatReq(BaseModel):
    session_id: str
    lat: float
    lon: float


@app.post("/api/puzzle/start")
async def api_puzzle_start(req: PuzzleStartReq):
    player = await _db_mod.get_player(req.player_id)
    if not player or player["team"] != "opposition":
        return {"ok": False, "error": "Only Opposition can capture nodes"}

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM nodes WHERE id=?", (req.node_id,)) as cur:
            node = await cur.fetchone()
    if not node: return {"ok": False, "error": "Node not found"}
    node = dict(node)

    if not player["last_location_lat"]:
        return {"ok": False, "error": "Open the map first to share location"}
    dist = haversine(player["last_location_lat"], player["last_location_lon"],
                     node["lat"], node["lon"])
    if dist > (node["current_radius_m"] or 80) + 15:
        return {"ok": False, "error": f"Too far ({int(dist)}m, need within {int(node['current_radius_m'] or 80)}m)"}

    progress = node.get("capture_progress") or 0
    if progress >= 100:
        return {"ok": False, "error": "Node already fully captured"}
    progress_target = 100 if progress >= 80 else 80

    solved = (node.get("puzzles_solved") or "").split(",")
    solved = [s for s in solved if s]
    if req.puzzle_type in solved:
        return {"ok": False, "error": f"You already solved {req.puzzle_type} for this node — try another type"}

    # ── Capture-window and cooldown checks ────────────────────────────────
    # Timed-capture model:
    #   • Opening the first puzzle sets capture_deadline_at = now + WINDOW.
    #     From then on the node auto-captures at that timestamp if an
    #     Opposition player is still in the radius (scheduler tick handles
    #     this). Solving puzzles cuts the deadline forward (see submit).
    #   • Cooldown between puzzles (PUZZLE_COOLDOWN_SEC) is enforced here.
    #   • If the previous deadline already expired without capture (attacker
    #     walked away), the scheduler cleared it; we start a fresh cycle.
    now = datetime.now()
    deadline_iso = node.get("capture_deadline_at")
    deadline = None
    if deadline_iso:
        try: deadline = datetime.fromisoformat(deadline_iso)
        except Exception: deadline = None

    # Cooldown check — only applies inside an active cycle.
    if deadline and deadline > now:
        last_solve_iso = node.get("last_puzzle_solved_at")
        if last_solve_iso:
            try:
                last_solve = datetime.fromisoformat(last_solve_iso)
                elapsed = (now - last_solve).total_seconds()
                if elapsed < PUZZLE_COOLDOWN_SEC:
                    wait = int(PUZZLE_COOLDOWN_SEC - elapsed)
                    return {"ok": False, "error": f"Cooldown — wait {wait}s before the next puzzle"}
            except Exception:
                pass

    # Fresh cycle if no active deadline. Store the attacking player so the
    # scheduler can check radius presence at expiry.
    starting_fresh = (not deadline or deadline <= now)
    if starting_fresh:
        new_deadline = now + timedelta(seconds=CAPTURE_WINDOW_SEC)
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "UPDATE nodes SET capture_deadline_at=?, capturing_player_id=?, "
                "last_puzzle_solved_at=NULL, attack_window_started_at=? "
                "WHERE id=?",
                (new_deadline.isoformat(), req.player_id, now.isoformat(), req.node_id)
            )
            await conn.commit()
        deadline = new_deadline

    try:
        gen = puzzle_module.generate(req.puzzle_type)
    except Exception:
        return {"ok": False, "error": "Unknown puzzle type"}

    session_id = await _db_mod.create_puzzle_session(
        req.player_id, req.node_id, req.puzzle_type,
        gen["puzzle_data"], gen["solution"], progress_target
    )

    # Notify all System players. player is a sqlite3.Row (no .get()) — index
    # by column name and tolerate NULL anonymous_id (shouldn't happen for an
    # Opposition player but be safe).
    try:
        anon = player["anonymous_id"] or "AGENT_????"
    except (KeyError, IndexError):
        anon = "AGENT_????"
    sys_players = await get_all_players("system")
    notif_text = (
        "🚨 *Hack started!*\n\n"
        f"Agent *{anon}* is trying to capture node *{node['name']}* via puzzle *{req.puzzle_type}*.\n\n"
        "Run to the node and stand inside its circle to freeze the hack!\n"
        f"Progress: {progress}% → target {progress_target}%"
    )
    for sp in sys_players:
        if sp["telegram_id"] < 0: continue
        await tg_send(sp["telegram_id"], notif_text)

    await broadcast_map_update()

    # Remaining seconds until auto-capture — used by the puzzle UI countdown.
    window_remaining = max(0, int((deadline - now).total_seconds()))

    return {
        "ok": True, "session_id": session_id, "puzzle_type": req.puzzle_type,
        "puzzle_data": gen["puzzle_data"], "progress_target": progress_target,
        "window_remaining_sec": window_remaining,
        "cooldown_sec": PUZZLE_COOLDOWN_SEC,
    }


@app.post("/api/puzzle/submit")
async def api_puzzle_submit(req: PuzzleSubmitReq):
    sess = await _db_mod.get_puzzle_session(req.session_id)
    if not sess: return {"ok": False, "error": "Session not found"}
    if sess["status"] != "active":
        return {"ok": False, "error": f"Session is {sess['status']}"}

    puzzle_data_v = dict(sess["puzzle_data"])
    if sess["puzzle_type"] == "mines" and sess["solution"]:
        puzzle_data_v["_correct_mines"] = sess["solution"].get("mines", [])

    # DEBUG: log what we received
    print(f"[puzzle/submit] type={sess['puzzle_type']}")
    print(f"  user_solution={req.solution}")
    print(f"  puzzle_data keys: {list(puzzle_data_v.keys())}")

    try:
        ok = puzzle_module.validate(sess["puzzle_type"], puzzle_data_v, req.solution)
        print(f"  validate returned: {ok}")
    except Exception as e:
        import traceback
        print(f"  validate threw exception: {e}")
        traceback.print_exc()
        ok = False

    if not ok:
        return {"ok": True, "correct": False, "message": "Wrong solution — try again"}

    # Enforce the capture window at submit time. If the deadline has passed,
    # the scheduler either already auto-captured the node or cancelled the
    # attack — either way, this solve is stale.
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT capture_deadline_at FROM nodes WHERE id=?", (sess["node_id"],)
        ) as cur:
            nrow = await cur.fetchone()
    deadline = None
    if nrow and nrow["capture_deadline_at"]:
        try:
            deadline = datetime.fromisoformat(nrow["capture_deadline_at"])
            if datetime.now() >= deadline:
                await _db_mod.close_puzzle_session(req.session_id, "closed")
                return {"ok": True, "correct": False,
                        "message": "Attack window expired — tap the node again to start over."}
        except Exception:
            pass

    progress = sess["created_for_progress"]
    await _db_mod.update_node_capture_progress(sess["node_id"], progress, owner="opposition")
    await _db_mod.mark_puzzle_solved(sess["node_id"], sess["puzzle_type"])
    await _db_mod.close_puzzle_session(req.session_id, "solved")

    # Update the node's capture-timing bookkeeping:
    #   • At 100% (second puzzle): capture is done. Clear the deadline
    #     and the attacker slot; no auto-tick needed anymore.
    #   • At 80% (first puzzle): cut the deadline by PUZZLE_TIME_CUT_PERCENT
    #     of the REMAINING time — the accelerator that solving a puzzle
    #     gives you over just standing there. Record last_puzzle_solved_at
    #     for the cooldown check on the next puzzle open.
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as conn:
        if progress >= 100:
            await conn.execute(
                "UPDATE nodes SET last_puzzle_solved_at=?, capture_deadline_at=NULL, "
                "capturing_player_id=NULL WHERE id=?",
                (now.isoformat(), sess["node_id"])
            )
        elif deadline:
            remaining = max(0, (deadline - now).total_seconds())
            new_remaining = remaining * (100 - PUZZLE_TIME_CUT_PERCENT) / 100
            new_deadline = now + timedelta(seconds=new_remaining)
            await conn.execute(
                "UPDATE nodes SET last_puzzle_solved_at=?, capture_deadline_at=? WHERE id=?",
                (now.isoformat(), new_deadline.isoformat(), sess["node_id"])
            )
        else:
            # No deadline for some reason — just record the solve time so
            # cooldown works if the player somehow starts another cycle.
            await conn.execute(
                "UPDATE nodes SET last_puzzle_solved_at=? WHERE id=?",
                (now.isoformat(), sess["node_id"])
            )
        await conn.commit()

    try:
        await broadcast_map_update()
    except Exception:
        pass

    # Push 'Node lost' to System the moment the node falls — same as fake.
    if progress >= 100:
        try:
            await _notify_node_captured(sess["node_id"], attacker_player_id=sess.get("player_id"))
        except Exception as e:
            print(f"[puzzle/submit] notify: {e}")

    # Instant chain check — saves up to 30s vs waiting for the scheduler
    try:
        await check_victory_now()
    except Exception as e:
        print(f"[puzzle/submit] check_victory_now: {e}")

    return {"ok": True, "correct": True, "progress": progress,
            "message": f"Node captured at {progress}%! Radius now at {progress}% of cap."}


@app.post("/api/puzzle/heartbeat")
async def api_puzzle_heartbeat(req: PuzzleHeartbeatReq):
    sess = await _db_mod.get_puzzle_session(req.session_id)
    if not sess: return {"ok": False, "error": "Session not found"}
    if sess["status"] not in ("active", "frozen"):
        return {"ok": False, "error": f"Session is {sess['status']}"}

    await _db_mod.update_player_location(sess["player_id"], req.lat, req.lon)

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM nodes WHERE id=?", (sess["node_id"],)) as cur:
            node = await cur.fetchone()
    if not node:
        await _db_mod.close_puzzle_session(req.session_id, "closed")
        return {"ok": False, "error": "Node disappeared"}
    node = dict(node)
    dist = haversine(req.lat, req.lon, node["lat"], node["lon"])
    if dist > (node["current_radius_m"] or 80) + 15:
        await _db_mod.close_puzzle_session(req.session_id, "expired")
        if sess["player_id"] > 0:
            await tg_send(sess["player_id"],
                f"❌ You left the circle of *{node['name']}*. Puzzle closed, progress lost.")
        await broadcast_map_update()
        return {"ok": True, "in_range": False, "message": "Left node circle"}

    sys_players = await _db_mod.get_all_players(team="system")
    frozen = False
    nearby_system = []
    for sp in sys_players:
        sp = dict(sp)
        if not sp.get("last_location_lat"): continue
        if haversine(sp["last_location_lat"], sp["last_location_lon"],
                     node["lat"], node["lon"]) <= (node["current_radius_m"] or 80) + 15:
            frozen = True
            nearby_system.append(sp)

    was_frozen = (sess["status"] == "frozen")

    if frozen and not was_frozen:
        await _db_mod.freeze_puzzle_session(req.session_id, True)
        if sess["player_id"] > 0:
            await tg_send(sess["player_id"],
                f"❄️ Puzzle is *frozen* — System is near *{node['name']}*.\nYou cannot Submit while they are there.")
        opp_player = await _db_mod.get_player(sess["player_id"])
        opp_anon = "AGENT_????"
        if opp_player and dict(opp_player).get("anonymous_id"):
            opp_anon = dict(opp_player)["anonymous_id"]
        notif_block = (
            "🛡 *You are blocking a hack!*\n\n"
            f"Agent *{opp_anon}* was trying to capture *{node['name']}*.\n"
            "Their puzzle is frozen — stay here."
        )
        for sp in nearby_system:
            if sp["telegram_id"] < 0: continue
            await tg_send(sp["telegram_id"], notif_block)
        await broadcast_map_update()
    elif not frozen and was_frozen:
        await _db_mod.freeze_puzzle_session(req.session_id, False)
        if sess["player_id"] > 0:
            await tg_send(sess["player_id"],
                f"▶️ Puzzle unfreezing — System left *{node['name']}*. Submit before they come back!")
        await broadcast_map_update()

    return {"ok": True, "in_range": True, "frozen": frozen,
            "status": "frozen" if frozen else "active"}


@app.get("/puzzle/{node_id}")
async def get_puzzle_page(node_id: int, request: Request):
    with open("puzzle.html", "r", encoding="utf-8") as f:
        html = f.read()
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    api_base = f"{scheme}://{host}"
    html = html.replace("{{API_BASE}}", api_base)
    html = html.replace("{{NODE_ID}}", str(node_id))
    return HTMLResponse(content=html)



async def _notify_node_captured(node_id: int, attacker_player_id: Optional[int] = None):
    """Push 'Node fell to Opposition' to every System player. Called when a
    puzzle solve takes a node from 80% to 100%. Without this, defenders have
    no idea the node was lost unless they happen to look at the map.

    Skipped for anchor nodes (ALEX/BEATRICE) because they're Opposition by
    design from the start of the game — losing them is not an event System
    should be told about. Also skipped for finale-type nodes.

    Fakes are skipped (negative IDs) and the attacker themselves is omitted
    even on System (defensive — shouldn't happen but harmless)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT name, node_type FROM nodes WHERE id=?", (node_id,)) as cur:
            n = await cur.fetchone()
    if not n:
        return
    name = n["name"] or ""
    if n["node_type"] == "finale":
        return
    upper_name = name.upper()
    if "ALEX" in upper_name or "BEATRICE" in upper_name:
        return  # anchors — not a real loss

    anon = None
    if attacker_player_id:
        ap = await get_player(attacker_player_id)
        if ap and ap.get("anonymous_id"):
            anon = ap["anonymous_id"]
        else:
            # Attacker row vanished between puzzle start and solve — most
            # likely /admin_unspawn or /admin_reset hit mid-session. Log it
            # so the admin sees something happened.
            print(f"[_notify_node_captured] node={node_id} attacker_player_id={attacker_player_id} "
                  f"could not be resolved in players table (deleted? race?)")
    elif attacker_player_id is None:
        # Caller passed no attacker at all — usually a manual admin trigger
        # without player_id. Not a bug, just worth knowing about.
        print(f"[_notify_node_captured] node={node_id} called without attacker_player_id")
    # If we couldn't resolve the attacker, just say so honestly instead of
    # printing "AGENT_????" — that reads like a bug.
    attacker_line = f"Last attacker: *{anon}*" if anon else "Attacker unknown."
    text = (
        f"🚨 *Node lost!*\n\n"
        f"*{name}* fell to Opposition.\n"
        f"{attacker_line}"
    )
    for sp in await get_all_players("system"):
        if sp["telegram_id"] < 0: continue
        await tg_send(sp["telegram_id"], text)


@app.post("/api/admin/fake_solve_puzzle")
async def api_fake_solve_puzzle(req: dict):
    """Simulates a fake solving a puzzle — instantly increases node progress.

    Two things this endpoint must do beyond just updating the node:
      1. Leave an audit row in puzzle_sessions so /api/events (the LIVE
         EVENTS panel) reports the solve. fake_start_puzzle creates a
         session only for the FIRST puzzle on a node; subsequent solves
         had no session and were invisible in the event log.
      2. When the node reaches 100%, push a 'Node captured' notification
         to System — symmetric to the 'Hack started' push on attack."""
    node_id = req.get("node_id")
    puzzle_type = req.get("puzzle_type", "untangle")
    player_id = req.get("player_id")  # optional — used for attribution
    if not node_id:
        return {"ok": False, "error": "node_id required"}
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT capture_progress, puzzles_solved FROM nodes WHERE id=?", (node_id,)
        ) as cur:
            node = await cur.fetchone()
    if not node:
        return {"ok": False, "error": "Node not found"}
    current = node["capture_progress"] or 0
    if current >= 100:
        return {"ok": False, "error": "Already 100%"}
    new_progress = 100 if current >= 80 else 80
    await _db_mod.update_node_capture_progress(
        node_id, new_progress, owner="opposition"
    )
    await _db_mod.mark_puzzle_solved(node_id, puzzle_type)

    # Close any in-flight puzzle session AND insert an audit row for this
    # specific solve, so /api/events catches every solve regardless of
    # whether fake_start_puzzle was called beforehand. Without the explicit
    # INSERT here, only the first puzzle on a node would ever appear in
    # the live events log.
    import uuid
    new_session_id = str(uuid.uuid4())
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as conn:
        # Close any leftover open sessions on this node so the orange
        # "puzzle in progress" ring disappears from the map.
        await conn.execute(
            "UPDATE puzzle_sessions SET status='solved' "
            "WHERE node_id=? AND status IN ('active','frozen')",
            (node_id,)
        )
        # Audit row for THIS solve. created_for_progress mirrors the new
        # progress so /api/events can label it 80% vs 100%.
        await conn.execute(
            "INSERT INTO puzzle_sessions "
            "(id, node_id, player_id, puzzle_type, puzzle_data, solution, "
            " started_at, status, created_for_progress) "
            "VALUES (?, ?, ?, ?, '{}', '{}', ?, 'solved', ?)",
            (new_session_id, node_id, player_id, puzzle_type, now_iso, new_progress)
        )
        await conn.commit()

    await broadcast_map_update()

    # Telegram push to System when the node hits 100%.
    if new_progress >= 100:
        try:
            await _notify_node_captured(node_id, attacker_player_id=player_id)
        except Exception as e:
            print(f"[fake_solve_puzzle] notify: {e}")

    try:
        await check_victory_now()
    except Exception as e:
        print(f"[fake_solve_puzzle] check_victory_now: {e}")
    return {"ok": True, "progress": new_progress, "puzzle_type": puzzle_type}


@app.post("/api/admin/fake_start_puzzle")
async def api_fake_start_puzzle(req: dict):
    """Creates a real puzzle_session for a fake — used to demonstrate freezing."""
    player_id = req.get("player_id")
    node_id = req.get("node_id")
    puzzle_type = req.get("puzzle_type", "untangle")
    if not player_id or not node_id:
        return {"ok": False, "error": "player_id and node_id required"}
    try:
        gen = puzzle_module.generate(puzzle_type)
    except Exception:
        return {"ok": False, "error": "Unknown puzzle type"}
    session_id = await _db_mod.create_puzzle_session(
        player_id, node_id, puzzle_type, gen["puzzle_data"], gen["solution"], 80
    )

    # Notify real System players — demo behaves like a regular hack
    nodes = [x for x in await get_nodes() if x["id"] == node_id]
    fake_player = await get_player(player_id)
    if nodes and fake_player:
        node = nodes[0]
        anon = fake_player.get("anonymous_id") or "AGENT_????"
        notif_text = (
            "🚨 *Hack started!*\n\n"
            f"Agent *{anon}* is trying to capture node *{node['name']}* via puzzle *{puzzle_type}*.\n\n"
            "Run to the node and stand inside its circle to freeze the hack!"
        )
        for sp in await get_all_players("system"):
            if sp["telegram_id"] < 0: continue
            await tg_send(sp["telegram_id"], notif_text)
    await broadcast_map_update()
    return {"ok": True, "session_id": session_id}


@app.post("/api/admin/fake_freeze_puzzle")
async def api_fake_freeze_puzzle(req: dict):
    """Freeze or unfreeze an active puzzle session."""
    session_id = req.get("session_id")
    frozen = bool(req.get("frozen", True))
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    await _db_mod.freeze_puzzle_session(session_id, frozen)
    await broadcast_map_update()
    return {"ok": True, "status": "frozen" if frozen else "active"}