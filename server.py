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
LOCATION_FRESH_SEC = 90  # геолокация живая 90 сек (карта пингует каждые 30)

# Bot instance — инжектируется из bot.py
_bot = None
def set_bot(b): global _bot; _bot = b

async def tg_send(chat_id: int, text: str):
    if _bot:
        try: await _bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception as e: print(f"[tg] {e}")


# История позиций для режима презентации (в памяти, не БД)
# player_id -> [(lat, lon, timestamp_iso), ...]  максимум 30 точек на игрока
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

class VerifyRequest(BaseModel):
    system_player_id: int
    scanned_player_id: int
    guessed_anonymous_id: str


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_nodes():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM nodes") as cur:
            return [dict(r) for r in await cur.fetchall()]

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

def filter_nodes_for_opposition(all_nodes, player_lat=None, player_lon=None):
    visible_ids = set()
    opp_nodes = [n for n in all_nodes if n["owner"] == "opposition"]
    regular_nodes = [n for n in all_nodes if n.get("node_type", "node") == "node"]

    # Цели всегда видны
    for n in all_nodes:
        if n.get("name") and ("ALEX" in n["name"] or "BEATRICE" in n["name"]):
            visible_ids.add(n["id"])

    # Core всегда виден
    for n in all_nodes:
        if n.get("node_type") == "core":
            visible_ids.add(n["id"])

    # Стартовые 2 ноды — географически ближайшие к NODE ALEX (не по id)
    targets = [n for n in all_nodes if n.get("name") and "ALEX" in n["name"]]
    non_target = [n for n in regular_nodes if n["id"] not in visible_ids]
    if targets:
        anchor = targets[0]
        non_target_sorted = sorted(non_target, key=lambda n: haversine(anchor["lat"], anchor["lon"], n["lat"], n["lon"]))
    else:
        non_target_sorted = sorted(non_target, key=lambda n: n["id"])
    for n in non_target_sorted[:2]:
        visible_ids.add(n["id"])

    # Свои захваченные
    for n in opp_nodes:
        visible_ids.add(n["id"])

    # Ноды в радиусе своих
    for own in opp_nodes:
        r = own.get("current_radius_m") or own.get("base_radius_m") or 80
        for other in regular_nodes:
            if other["id"] not in visible_ids:
                if haversine(own["lat"], own["lon"], other["lat"], other["lon"]) <= r:
                    visible_ids.add(other["id"])

    # Нода где стоит игрок
    if player_lat is not None and player_lon is not None:
        for n in regular_nodes:
            if n["id"] not in visible_ids:
                if haversine(player_lat, player_lon, n["lat"], n["lon"]) <= (n.get("base_radius_m") or 80):
                    visible_ids.add(n["id"])

    # Hub рядом с захваченной нодой
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
    state = await get_game_state()
    if not state.get("active") or state.get("mode") != "A": return
    target_a, target_b = state.get("target_node_a"), state.get("target_node_b")
    if not target_a or not target_b: return

    nodes = await get_nodes()
    opp_nodes = [n for n in nodes if n["owner"] == "opposition"]
    connections = []
    for i, a in enumerate(opp_nodes):
        for b in opp_nodes[i+1:]:
            if haversine(a["lat"], a["lon"], b["lat"], b["lon"]) <= (a.get("current_radius_m") or 80) + (b.get("current_radius_m") or 80):
                connections.append((a["id"], b["id"]))
    if not connections: return

    graph = {}
    for a, b in connections:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)

    visited, queue = set(), [target_a]
    while queue:
        cur = queue.pop(0)
        if cur == target_b:
            await _end_game(winner="opposition")
            return
        if cur not in visited:
            visited.add(cur)
            queue.extend(graph.get(cur, []))


async def _end_game(winner: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE game_state SET active=0 WHERE id=1")
        await db.commit()
    nodes = await get_nodes()
    sys_n = len([n for n in nodes if n["owner"] == "system"])
    hak_n = len([n for n in nodes if n["owner"] == "opposition"])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(DISTINCT anonymous_id) FROM identifications WHERE anonymous_id IS NOT NULL") as cur:
            row = await cur.fetchone(); unique_ids = row[0] if row else 0
    winner_text = "🔴 Opposition победили — цепочка построена!" if winner == "opposition" else "⚙️ System победила!"
    msg = f"🏁 *Игра завершена!*\n\n{winner_text}\n\n⚙️ System: {sys_n*10+unique_ids*15} очков\n🔴 Opposition: {hak_n*10} очков"
    for p in await get_all_players():
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
        allies = await get_allies(player_id, team)
        try:
            await ws.send_text(json.dumps({
                "type": "map_update", "nodes": visible, "allies": allies,
                "phase": state.get("current_phase", 0), "active": state.get("active", 0),
                "phase_remaining_sec": phase_remaining_sec,
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
    # radius_grower убран — теперь только в scheduler.py (бот) во избежание дублирования
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/map", response_class=HTMLResponse)
async def get_map(request: Request):
    """Отдаём карту с инжекцией API_BASE — фикс для iOS Telegram Mini App."""
    with open("map_trento.html", "r", encoding="utf-8") as f:
        html = f.read()
    # API_BASE = текущий URL по которому пришёл запрос (cloudflare tunnel)
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    api_base = f"{scheme}://{host}"
    html = html.replace("{{API_BASE}}", api_base)
    return HTMLResponse(content=html)

@app.get("/admin")
async def get_admin(): return FileResponse("admin_map.html")

@app.get("/api/nodes")
async def api_nodes(): return await get_nodes()

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
    if player["team"] == "system": return all_nodes
    return filter_nodes_for_opposition(all_nodes, lat, lon)


@app.post("/api/location")
async def api_location(req: LocationPingRequest):
    """Фоновый геопинг с карты каждые 30 сек — держит геолокацию живой."""
    player = await get_player(req.player_id)
    if not player: return {"ok": False}
    now_iso = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET last_location_lat=?,last_location_lon=?,last_location_at=? WHERE telegram_id=?",
            (req.lat, req.lon, now_iso, req.player_id)
        )
        await db.commit()

    # Сохраняем в историю для режима презентации
    _location_history[req.player_id].append((req.lat, req.lon, now_iso))

    return {"ok": True}


@app.get("/presentation")
async def get_presentation(request: Request):
    """Карта в режиме презентации — для записи видео.
    Видны все игроки с именами, траектории движения, без кнопок взаимодействия."""
    with open("map_trento.html", "r", encoding="utf-8") as f:
        html = f.read()

    # ВАЖНО: подставляем API_BASE как в /map — иначе fetch и WebSocket не работают
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    api_base = f"{scheme}://{host}"
    html = html.replace("{{API_BASE}}", api_base)

    # Включаем режим презентации
    html = html.replace(
        "var PLAYER_ID = urlParams.get('player_id')",
        "var PRESENTATION_MODE = true;\n  var PLAYER_ID = urlParams.get('player_id') || 'admin'"
    )
    return HTMLResponse(content=html)


@app.get("/api/presentation/players")
async def api_presentation_players():
    """Возвращает всех игроков с геолокацией + траектории для режима презентации."""
    players = await get_all_players()
    result = []
    for p in players:
        if not is_fresh(p.get("last_location_at"), max_sec=300): continue
        lat, lon = p.get("last_location_lat"), p.get("last_location_lon")
        if lat is None or lon is None: continue

        # Достаём траекторию из истории
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

    # Проверяем прогресс цепочки ALEX↔BEATRICE
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

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET last_location_lat=?,last_location_lon=?,last_location_at=? WHERE telegram_id=?",
            (req.lat, req.lon, datetime.now().isoformat(), req.player_id)
        )
        await db.commit()

    nodes = await get_nodes()

    # Всегда проверяем расстояние — даже если node_id передан явно
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
        # FIX: проверяем реально ли System всё ещё рядом
        # Если System ушли — разрешаем оппозиции возобновить захват
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

        # System ушёл — возобновляем захват с накопленного elapsed
        # capturing_player_id переходит к нажавшему (на случай если это другой игрок оппозиции)
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
            "message": f"Захват возобновлён — осталось {remaining_sec//60}м {remaining_sec%60}с"
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

    # Уведомляем System через Telegram
    for sp in await get_all_players("system"):
        await tg_send(sp["telegram_id"],
            f"🚨 *Нода атакована!*\n\nНода *{node['name']}* под угрозой.\nУ тебя {CAPTURE_TIME_SEC//60} мин!")

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
                # Уведомляем хакера что таймер заморожен
                await tg_send(opp_id,
                    f"⛔️ Захват *{node['name']}* заморожен — System рядом.\nУходи или жди пока они уйдут.")
            results.append({"node": node["name"], "identified": identified, "frozen": True})
        await db.commit()

    await broadcast_map_update()
    return {"ok": True, "results": results}


# ── Admin API ─────────────────────────────────────────────────────────────────

@app.post("/api/admin/node")
async def admin_add_node(req: AdminNodeRequest):
    name = req.name.strip().upper()
    if not name: return {"ok": False, "message": "Name required"}
    if req.node_type not in ("node", "hub", "core"): return {"ok": False, "message": "Invalid type"}
    # Минимальный радиус берём из config (дефолт 5м для маленьких карт)
    min_radius = getattr(config, "MIN_NODE_RADIUS_M", 5)
    max_radius = getattr(config, "MAX_NODE_RADIUS_M", 1000)
    radius = max(float(min_radius), min(req.radius, float(max_radius)))

    target_set = None  # для ответа

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO nodes (name,lat,lon,node_type,owner,current_radius_m,base_radius_m) VALUES (?,?,?,?,?,?,?)",
            (name, req.lat, req.lon, req.node_type, "system", radius, radius)
        )
        new_node_id = cursor.lastrowid

        # Автоматически назначаем target_a/b если имя содержит ALEX или BEATRICE
        if "ALEX" in name:
            await db.execute("UPDATE game_state SET target_node_a = ? WHERE id = 1", (new_node_id,))
            target_set = "A (ALEX)"
        elif "BEATRICE" in name:
            await db.execute("UPDATE game_state SET target_node_b = ? WHERE id = 1", (new_node_id,))
            target_set = "B (BEATRICE)"

        await db.commit()

    await broadcast_map_update()
    return {"ok": True, "name": name, "target_set": target_set, "node_id": new_node_id}


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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE nodes SET owner='system', capture_started_at=NULL,
            capturing_player_id=NULL, capture_elapsed_sec=0, capture_frozen=0,
            freeze_started_at=NULL, current_radius_m=base_radius_m
        """)
        await db.execute("UPDATE game_state SET active=0, current_phase=0 WHERE id=1")
        await db.commit()
    await broadcast_map_update()
    return {"ok": True}


# ── Verify API ────────────────────────────────────────────────────────────────

@app.get("/api/events")
async def api_events():
    """Последние события для панели логов на /presentation."""
    events = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Захваты
        async with db.execute(
            """SELECT c.started_at AS ts, p.username, p.anonymous_id, n.name AS node_name, 'capture' AS type
               FROM captures c
               LEFT JOIN players p ON p.telegram_id = c.player_id
               LEFT JOIN nodes n ON n.id = c.node_id
               ORDER BY c.started_at DESC LIMIT 15"""
        ) as cur:
            for r in await cur.fetchall():
                events.append({
                    "ts": r["ts"], "type": "capture",
                    "text": f"⚡ {r['username'] or '?'} начал захват {r['node_name'] or '?'}"
                })
        # Идентификации
        async with db.execute(
            """SELECT i.identified_at AS ts, sp.username AS sys, i.anonymous_id, n.name AS node_name
               FROM identifications i
               LEFT JOIN players sp ON sp.telegram_id = i.system_player_id
               LEFT JOIN nodes n ON n.id = i.node_id
               ORDER BY i.identified_at DESC LIMIT 15"""
        ) as cur:
            for r in await cur.fetchall():
                events.append({
                    "ts": r["ts"], "type": "ident",
                    "text": f"🆔 {r['sys'] or '?'} зафиксировал {r['anonymous_id']} на {r['node_name'] or '?'}"
                })
        # Захваты завершённые (owner = opposition)
        async with db.execute(
            "SELECT name, id FROM nodes WHERE owner='opposition'"
        ) as cur:
            for r in await cur.fetchall():
                # без timestamp точного — добавим как "сейчас"
                pass

    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:20]


@app.post("/api/admin/fake_capture")
async def api_fake_capture(req: dict):
    """Фейк начинает захват — для demo_scenario через HTTP."""
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
    # Ставим фейка на ноду
    await db_module.update_player_location(player_id, node["lat"], node["lon"])
    _location_history[player_id].append((node["lat"], node["lon"], datetime.now().isoformat()))
    # Стартуем захват
    await db_module.start_node_capture(node_id, player_id)
    await db_module.create_capture(node_id, player_id)
    await broadcast_map_update()

    # Шлём настоящим System
    sys_players = await get_all_players("system")
    for sp in sys_players:
        if sp["telegram_id"] < 0: continue
        await tg_send(sp["telegram_id"], f"🚨 Нода *{node['name']}* атакована!")
    return {"ok": True}


@app.post("/api/admin/fake_defend")
async def api_fake_defend(req: dict):
    """Фейк-System замораживает захват — для demo через HTTP."""
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
            await tg_send(opp_id, f"⛔ Захват *{node['name']}* заморожен — System рядом.")
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/fake_complete_capture")
async def api_fake_complete(req: dict):
    """Мгновенно завершить захват — нода переходит к Opposition. Для demo."""
    node_id = req.get("node_id")
    if not node_id: return {"ok": False}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET owner='opposition', capture_started_at=NULL, capturing_player_id=NULL, capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL WHERE id=?",
            (node_id,)
        )
        await db.commit()
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/set_owner")
async def api_set_owner(req: dict):
    """Установить владельца ноды (для подготовки сценария — ALEX/BEATRICE сразу opposition)."""
    node_id = req.get("node_id")
    owner = req.get("owner", "system")
    if not node_id: return {"ok": False}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE nodes SET owner=? WHERE id=?", (owner, node_id))
        await db.commit()
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/set_radius")
async def api_set_radius(req: dict):
    """Установить радиус ноды напрямую — для демо чтобы имитировать долгое удержание."""
    node_id = req.get("node_id")
    radius = req.get("radius")
    if not node_id or radius is None: return {"ok": False}
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE nodes SET current_radius_m=? WHERE id=?", (radius, node_id))
        await db.commit()
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/fake_interrupt_capture")
async def api_fake_interrupt(req: dict):
    """Сбросить захват ноды (имитация что все ушли > 3 минут) — для демо."""
    node_id = req.get("node_id")
    if not node_id: return {"ok": False}
    import database as db_module
    await db_module.interrupt_node_capture(node_id)
    await broadcast_map_update()
    return {"ok": True}


@app.post("/api/admin/fake_verify")
async def api_fake_verify(req: dict):
    """Симулировать QR-верификацию: System угадывает AGENT-ID Opposition. Для демо."""
    sys_id = req.get("system_player_id")
    opp_id = req.get("scanned_player_id")
    guessed = req.get("guessed_anonymous_id")
    if not (sys_id and opp_id and guessed):
        return {"ok": False, "error": "missing params"}
    import database as db_module
    result = await db_module.add_verification(sys_id, opp_id, guessed)
    # Шлём пуш реальной оппозиции если она есть
    if opp_id > 0 and result.get("ok"):
        if result.get("correct"):
            await tg_send(opp_id, "🚨 ТЕБЯ ВЫЧИСЛИЛИ.\n\nSystem сопоставила твой QR с AGENT-ID.")
        else:
            await tg_send(opp_id, "🕵 ТЫ УСКОЛЬЗНУЛ.\n\nSystem угадала неверный AGENT-ID.")
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


# ── WebSocket ─────────────────────────────────────────────────────────────────

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
# capture_checker и contested_checker убраны — только в scheduler.py (боте).
# Дублирование на одной SQLite вызывало race conditions.

# radius_grower удалён — теперь только в scheduler.py


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8001, reload=True)
