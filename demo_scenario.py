"""
ДЕМО-СЦЕНАРИЙ — автоматически разыгрывает партию через фейковых игроков.

Использование:
    1. Запусти бот:           python3 bot.py
    2. В браузере открой:     <твой-cloudflare-url>/presentation
    3. В новом терминале:     python3 demo_scenario.py

СЮЖЕТ:
    NODE ALEX и NODE BEATRICE с самого начала принадлежат Opposition.
    Задача Opposition — захватить промежуточные ноды чтобы соединить их в цепочку.
    System пытается мешать через DEFEND.

Скрипт работает через HTTP API сервера, поэтому всё что он делает
видно на /presentation в реальном времени.

TIME_SCALE регулирует скорость:
    1.0  → нормально, рекомендую для записи видео
    0.5  → медленнее
    2.0  → быстрее
"""

import asyncio
import sys
import random
from datetime import datetime
from math import radians, sin, cos, sqrt, atan2

import aiohttp
import aiosqlite

DB_PATH = "game.db"
SERVER_URL = "http://localhost:8001"

# ── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

TIME_SCALE = 1.0      # 1.0 = нормально для наблюдения

WALK_STEPS = 8        # шагов плавного перемещения
WALK_DELAY = 1.5      # секунд между шагами

ACT_PAUSE = 5         # пауза между актами для драматизма

OPP_PLAYERS = ["ALICE", "CHARLIE"]
SYS_PLAYERS = ["BOB", "DIANA"]

# ── Helpers ──────────────────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


async def sleep_scaled(seconds: float):
    await asyncio.sleep(seconds / TIME_SCALE)


def log(text: str, emoji: str = "🎬"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {emoji} {text}")


def gen_anon():
    return "AGENT_" + "".join(random.choices("0123456789ABCDEF", k=4))


session: aiohttp.ClientSession = None


async def http_post(path: str, data: dict):
    try:
        async with session.post(f"{SERVER_URL}{path}", json=data,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
            return await r.json()
    except Exception as e:
        log(f"HTTP {path}: {e}", "❌")
        return {"ok": False}


# ── БД для создания фейков и чтения нод ──────────────────────────────────────

async def spawn_fake(name: str, team: str) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT MIN(telegram_id) FROM players") as cur:
            min_id = (await cur.fetchone())[0] or 0
        fake_id = min(min_id - 1, -1)
        anon = gen_anon()
        await conn.execute(
            "INSERT INTO players (telegram_id, username, team, anonymous_id) VALUES (?, ?, ?, ?)",
            (fake_id, f"FAKE_{name}", team, anon)
        )
        await conn.commit()
    return fake_id


async def cleanup_fakes():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM players WHERE telegram_id < 0")
        await conn.commit()


async def get_nodes():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM nodes ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def get_game_state():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM game_state WHERE id=1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}


# ── Действия через HTTP (видны на /presentation) ────────────────────────────

async def move_to(player_id, lat, lon):
    await http_post("/api/location", {"player_id": player_id, "lat": lat, "lon": lon})


async def smooth_walk(player_id, from_lat, from_lon, to_lat, to_lon,
                      steps=WALK_STEPS, step_delay=WALK_DELAY):
    for i in range(1, steps + 1):
        t = i / steps
        lat = from_lat + (to_lat - from_lat) * t
        lon = from_lon + (to_lon - from_lon) * t
        await move_to(player_id, lat, lon)
        await sleep_scaled(step_delay)


async def start_capture(player_id, node_id):
    await http_post("/api/admin/fake_capture", {"player_id": player_id, "node_id": node_id})


async def freeze_and_identify(system_player_id, node_id):
    await http_post("/api/admin/fake_defend",
                    {"player_id": system_player_id, "node_id": node_id})


async def complete_capture(node_id):
    await http_post("/api/admin/fake_complete_capture", {"node_id": node_id})


async def set_owner(node_id, owner):
    await http_post("/api/admin/set_owner", {"node_id": node_id, "owner": owner})


# ── СЦЕНАРИЙ ─────────────────────────────────────────────────────────────────

async def main():
    global session
    session = aiohttp.ClientSession()

    print("\n" + "=" * 60)
    print("  🎬 GPS STRATEGY — ДЕМО-СЦЕНАРИЙ")
    print("=" * 60)
    print(f"  TIME_SCALE: {TIME_SCALE}x")
    print(f"  Открой /presentation в браузере и смотри!")
    print("=" * 60 + "\n")

    try:
        async with session.get(f"{SERVER_URL}/api/game",
                               timeout=aiohttp.ClientTimeout(total=3)) as r:
            await r.json()
    except Exception:
        print("❌ Сервер недоступен на localhost:8001. Запусти python3 bot.py")
        await session.close()
        return

    nodes = await get_nodes()
    if len(nodes) < 3:
        print(f"❌ В БД только {len(nodes)} нод. Создай карту через /admin_map.")
        await session.close()
        return

    state = await get_game_state()
    target_a = next((n for n in nodes if n["id"] == state.get("target_node_a")), None)
    target_b = next((n for n in nodes if n["id"] == state.get("target_node_b")), None)
    if not target_a:
        target_a = next((n for n in nodes if "ALEX" in (n["name"] or "")), None)
    if not target_b:
        target_b = next((n for n in nodes if "BEATRICE" in (n["name"] or "")), None)

    if not target_a or not target_b:
        print("❌ Не найдены NODE ALEX и NODE BEATRICE.")
        await session.close()
        return

    other_nodes = [n for n in nodes if n["id"] not in (target_a["id"], target_b["id"])
                                       and n.get("node_type", "node") == "node"]
    other_nodes.sort(key=lambda n: haversine(target_a["lat"], target_a["lon"],
                                              n["lat"], n["lon"]))

    log(f"Карта: {len(nodes)} нод. Цель: соединить {target_a['name']} ↔ {target_b['name']}", "🗺")
    log(f"Промежуточные ноды по маршруту: {[n['name'] for n in other_nodes]}", "🗺")
    await sleep_scaled(2)

    # ═══ ПОДГОТОВКА ════════════════════════════════════════════════════════
    log("─" * 50, "")
    log("ПОДГОТОВКА: ALEX и BEATRICE отдаются Opposition", "⚙️")
    log("─" * 50, "")

    await set_owner(target_a["id"], "opposition")
    log(f"🔴 {target_a['name']} → Opposition", "✅")
    await sleep_scaled(2)
    await set_owner(target_b["id"], "opposition")
    log(f"🔴 {target_b['name']} → Opposition", "✅")
    await sleep_scaled(ACT_PAUSE)

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "UPDATE game_state SET active=1, current_phase=1, phase_started_at=? WHERE id=1",
            (datetime.now().isoformat(),)
        )
        await conn.commit()
    log("Игра активирована", "🚀")
    await sleep_scaled(2)

    # ═══ АКТ 1 — РЕГИСТРАЦИЯ ═══════════════════════════════════════════════
    log("─" * 50, "")
    log("АКТ 1: Игроки входят в игру", "🎭")
    log("─" * 50, "")

    await cleanup_fakes()

    opp_ids = {}
    for name in OPP_PLAYERS:
        pid = await spawn_fake(name, "opposition")
        opp_ids[name] = pid
        log(f"🔴 FAKE_{name} вошёл в Opposition", "👤")
        await sleep_scaled(2)

    sys_ids = {}
    for name in SYS_PLAYERS:
        pid = await spawn_fake(name, "system")
        sys_ids[name] = pid
        log(f"⚙️ FAKE_{name} вошёл в System", "👤")
        await sleep_scaled(2)

    await sleep_scaled(ACT_PAUSE)

    # Стартовые позиции
    center_lat = (target_a["lat"] + target_b["lat"]) / 2
    center_lon = (target_a["lon"] + target_b["lon"]) / 2

    log("Игроки занимают стартовые позиции", "📍")
    # Разнесённые позиции — Opposition по краям, System в стороне
    await move_to(opp_ids["ALICE"], target_a["lat"] + 0.0008, target_a["lon"] - 0.0008)
    await sleep_scaled(1)
    await move_to(opp_ids["CHARLIE"], target_b["lat"] - 0.0008, target_b["lon"] + 0.0008)
    await sleep_scaled(1)
    # System отдельно — севернее центра и южнее
    await move_to(sys_ids["BOB"], center_lat + 0.0015, center_lon - 0.0010)
    await sleep_scaled(1)
    await move_to(sys_ids["DIANA"], center_lat - 0.0015, center_lon + 0.0010)
    await sleep_scaled(ACT_PAUSE)

    if not other_nodes:
        log("Нет промежуточных нод — закрываю", "❌")
        await session.close()
        return

    # ═══ АКТ 2 — ALICE атакует первую ═══════════════════════════════════════
    first_node = other_nodes[0]
    log("─" * 50, "")
    log(f"АКТ 2: ALICE движется к {first_node['name']}", "🎭")
    log("─" * 50, "")

    log(f"🔴 ALICE идёт от {target_a['name']} к {first_node['name']}", "🏃")
    await smooth_walk(
        opp_ids["ALICE"],
        target_a["lat"] + 0.0005, target_a["lon"] - 0.0005,
        first_node["lat"], first_node["lon"],
        steps=WALK_STEPS, step_delay=WALK_DELAY
    )
    await sleep_scaled(2)

    log(f"🔴 ALICE начинает захват {first_node['name']}", "⚡")
    await start_capture(opp_ids["ALICE"], first_node["id"])
    log("🚨 Сервер послал пуш реальным System (если они есть в боте)", "📡")
    await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 3 — BOB реагирует ═════════════════════════════════════════════
    log("─" * 50, "")
    log(f"АКТ 3: BOB бежит на перехват", "🎭")
    log("─" * 50, "")

    log(f"⚙️ BOB бежит от центра к {first_node['name']}", "🏃")
    await smooth_walk(
        sys_ids["BOB"],
        center_lat + 0.0003, center_lon - 0.0003,
        first_node["lat"], first_node["lon"],
        steps=WALK_STEPS, step_delay=WALK_DELAY
    )
    await sleep_scaled(2)

    log(f"⚙️ BOB нажимает DEFEND", "🛡")
    await freeze_and_identify(sys_ids["BOB"], first_node["id"])
    log(f"🆔 ID атакующего залогирован — посмотри в /admin_replay", "📋")
    log(f"❄️ Захват ЗАМОРОЖЕН — нода фиолетовая на карте", "⏸")
    await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 4 — BOB уходит, ALICE стоит на месте, scheduler возобновляет ═══
    log("─" * 50, "")
    log(f"АКТ 4: BOB уходит — захват возобновляется автоматически", "🎭")
    log("─" * 50, "")

    log(f"⚙️ BOB возвращается в центр", "🏃")
    await smooth_walk(
        sys_ids["BOB"],
        first_node["lat"], first_node["lon"],
        center_lat + 0.0010, center_lon + 0.0005,
        steps=WALK_STEPS, step_delay=WALK_DELAY
    )

    log("ALICE остаётся на ноде — посмотри как scheduler через 30 сек возобновит захват", "⏳")
    await sleep_scaled(35)

    log("▶️ Scheduler возобновил захват — нода снова оранжевая", "✅")
    await sleep_scaled(2)

    log(f"⏱  Завершаем захват ускоренно", "⏱")
    await complete_capture(first_node["id"])
    log(f"✅ {first_node['name']} захвачен Opposition!", "🎉")
    await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 5 — CHARLIE с другой стороны ══════════════════════════════════
    if len(other_nodes) >= 2:
        second_node = other_nodes[-1]
        log("─" * 50, "")
        log(f"АКТ 5: CHARLIE атакует с фланга — {second_node['name']}", "🎭")
        log("─" * 50, "")

        log(f"🔴 CHARLIE идёт от {target_b['name']} к {second_node['name']}", "🏃")
        await smooth_walk(
            opp_ids["CHARLIE"],
            target_b["lat"] + 0.0005, target_b["lon"] - 0.0005,
            second_node["lat"], second_node["lon"],
            steps=WALK_STEPS, step_delay=WALK_DELAY
        )

        log(f"🔴 CHARLIE захватывает {second_node['name']}", "⚡")
        await start_capture(opp_ids["CHARLIE"], second_node["id"])
        await sleep_scaled(8)
        await complete_capture(second_node["id"])
        log(f"✅ {second_node['name']} захвачен", "🎉")
        await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 6 — ALICE достраивает цепочку ═════════════════════════════════
    middle_nodes = other_nodes[1:-1] if len(other_nodes) >= 3 else []
    if middle_nodes:
        log("─" * 50, "")
        log(f"АКТ 6: ALICE достраивает цепочку через {len(middle_nodes)} нод", "🎭")
        log("─" * 50, "")

        prev_lat, prev_lon = first_node["lat"], first_node["lon"]
        for node in middle_nodes:
            log(f"🔴 ALICE → {node['name']}", "🏃")
            await smooth_walk(
                opp_ids["ALICE"],
                prev_lat, prev_lon,
                node["lat"], node["lon"],
                steps=WALK_STEPS, step_delay=WALK_DELAY
            )
            log(f"⚡ Захват {node['name']}", "")
            await start_capture(opp_ids["ALICE"], node["id"])
            await sleep_scaled(6)
            await complete_capture(node["id"])
            log(f"✅ {node['name']} захвачен", "🎉")
            await sleep_scaled(3)
            prev_lat, prev_lon = node["lat"], node["lon"]

    # ═══ ФИНАЛ ══════════════════════════════════════════════════════════════
    log("─" * 50, "")
    log("ФИНАЛ: цепочка построена — scheduler проверит победу", "🏆")
    log("─" * 50, "")
    log("Ждём 30 сек пока scheduler выпишет победу...", "⏳")
    await sleep_scaled(30)

    log("─" * 50, "")
    log("🎬 СЦЕНАРИЙ ЗАВЕРШЁН", "✨")
    log("/admin_replay в боте — хронология событий", "📜")
    log("/score в боте — итоговый счёт", "📊")
    log("/admin_unspawn all — убрать фейков перед реальной игрой", "🗑")
    log("/admin_reset — сбросить ноды к System", "🔄")
    log("─" * 50, "")

    await session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Сценарий прерван")
        sys.exit(0)
