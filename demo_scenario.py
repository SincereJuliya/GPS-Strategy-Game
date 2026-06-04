"""
ДЕМО-СЦЕНАРИЙ — показывает ВСЕ механики игры через фейковых игроков.

Использование:
    1. Запусти бот:           python3 bot.py
    2. В браузере открой:     <твой-cloudflare-url>/presentation
    3. В новом терминале:     python3 demo_scenario.py

ВКЛЮЧЁННЫЕ МЕХАНИКИ:
    • Регистрация игроков с AGENT-ID
    • Захват ноды (3 минуты удержания)
    • Уведомление "Нода атакована" реальным System
    • DEFEND → заморозка захвата + идентификация
    • Контестед-логирование: System стоит, AGENT повторно логируется
    • Авто-возобновление после ухода System
    • Захват завершён → нода переходит к Opposition
    • Рост радиуса в реальном времени
    • Сброс захвата если все ушли надолго
    • Mesh-связь между захваченными нодами (зелёные линии)
    • QR-верификация: System угадывает AGENT-ID
    • Победа Opposition через цепочку ALEX ↔ BEATRICE

TIME_SCALE регулирует скорость (1.0 = реальное время).
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

TIME_SCALE = 1.0
WALK_STEPS = 8
WALK_DELAY = 1.5
ACT_PAUSE = 5
OPP_PLAYERS = ["ALICE", "ROMA"]
SYS_PLAYERS = ["BOB", "DIANA"]

# Демо полагается на встроенный scheduler как настоящая игра:
# - захват завершится через CAPTURE_TIME_SEC
# - радиус будет расти через RADIUS_GROWTH_INTERVAL_SEC
# Демо лишь двигает фейков и логирует процесс

# Параметры из config.py
try:
    import sys as _sys
    _sys.path.insert(0, ".")
    import config as _game_config
    REAL_CAPTURE_TIME = int(getattr(_game_config, "CAPTURE_TIME_SEC", 90))
    GROW_INTERVAL = int(getattr(_game_config, "RADIUS_GROWTH_INTERVAL_SEC", 10))
except Exception:
    REAL_CAPTURE_TIME = 90
    GROW_INTERVAL = 10


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1)*cos(lat2)*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


async def sleep_scaled(s): await asyncio.sleep(s / TIME_SCALE)


def log(text, emoji="🎬"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {emoji} {text}")


def gen_anon():
    return "AGENT_" + "".join(random.choices("0123456789ABCDEF", k=4))


session: aiohttp.ClientSession = None


async def http_post(path, data):
    try:
        async with session.post(f"{SERVER_URL}{path}", json=data,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
            return await r.json()
    except Exception as e:
        log(f"HTTP {path}: {e}", "❌")
        return {"ok": False}


# ── БД helpers ───────────────────────────────────────────────────────────────

async def spawn_fake(name, team):
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
    return fake_id, anon


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


async def get_node_radius(node_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT current_radius_m FROM nodes WHERE id=?", (node_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ── Действия через HTTP ───────────────────────────────────────────────────

async def move_to(pid, lat, lon):
    await http_post("/api/location", {"player_id": pid, "lat": lat, "lon": lon})


async def smooth_walk(pid, fr_lat, fr_lon, to_lat, to_lon, steps=WALK_STEPS, delay=WALK_DELAY):
    for i in range(1, steps + 1):
        t = i / steps
        await move_to(pid, fr_lat + (to_lat - fr_lat) * t, fr_lon + (to_lon - fr_lon) * t)
        await sleep_scaled(delay)


async def start_capture(pid, node_id):
    await http_post("/api/admin/fake_capture", {"player_id": pid, "node_id": node_id})


async def freeze_and_identify(pid, node_id):
    await http_post("/api/admin/fake_defend", {"player_id": pid, "node_id": node_id})


async def complete_capture(node_id):
    """ОПЦИОНАЛЬНО: мгновенно завершить захват (используется только в Акте 6 для сброса)."""
    await http_post("/api/admin/fake_complete_capture", {"node_id": node_id})


async def set_owner(node_id, owner):
    await http_post("/api/admin/set_owner", {"node_id": node_id, "owner": owner})


async def interrupt_capture(node_id):
    await http_post("/api/admin/fake_interrupt_capture", {"node_id": node_id})


async def verify_player(sys_id, opp_id, guessed_anon):
    return await http_post("/api/admin/fake_verify", {
        "system_player_id": sys_id,
        "scanned_player_id": opp_id,
        "guessed_anonymous_id": guessed_anon,
    })


async def _background_pinger(player_id, lat, lon, stop_event, interval=8):
    """Фоновый таск — постоянно пингует геолокацию игрока, чтобы scheduler видел его."""
    try:
        while not stop_event.is_set():
            await move_to(player_id, lat, lon)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    except Exception:
        pass


def start_pinger(player_id, lat, lon):
    """Запускает фоновый пинг. Возвращает (task, stop_event) для остановки."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(_background_pinger(player_id, lat, lon, stop_event))
    return task, stop_event


async def stop_pinger(task, stop_event):
    """Останавливает фоновый пинг."""
    stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=2)
    except Exception:
        task.cancel()


async def hold_position_for_capture(node, player_id):
    """
    Игрок стоит у ноды → scheduler сам:
    1. Завершит захват через CAPTURE_TIME_SEC секунд
    2. Начнёт растить радиус через RADIUS_GROWTH_INTERVAL_SEC интервалы
    Мы просто ждём и логируем что происходит.
    """
    total_wait = REAL_CAPTURE_TIME + 40  # + буфер на проверку scheduler
    log(f"⏳ {node['name']}: scheduler сам завершит захват за {REAL_CAPTURE_TIME} сек", "⌛")
    elapsed = 0
    while elapsed < total_wait:
        # Каждые 10 сек обновляем игроку позицию (геопинг чтобы scheduler видел его)
        await move_to(player_id, node["lat"], node["lon"])
        # Проверяем статус
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            async with conn.execute("SELECT owner, current_radius_m FROM nodes WHERE id=?",
                                    (node["id"],)) as cur:
                row = await cur.fetchone()
        if row and row["owner"] == "opposition":
            log(f"  ✅ {node['name']} перешла к Opposition! Радиус: {int(row['current_radius_m'])}м", "🎉")
            break
        if elapsed % 20 == 0:
            log(f"  ⏱  Прошло {elapsed} сек / {REAL_CAPTURE_TIME} сек захвата...", "")
        await sleep_scaled(10)
        elapsed += 10


async def hold_for_radius_growth(node, player_id, hold_seconds=60):
    """
    Игрок стоит у захваченной ноды → scheduler растит радиус автоматически.
    Логируем процесс каждые GROW_INTERVAL секунд.
    """
    log(f"📍 {node['name']}: {player_id} удерживает позицию — scheduler растит радиус", "🌐")
    elapsed = 0
    while elapsed < hold_seconds:
        await move_to(player_id, node["lat"], node["lon"])
        await sleep_scaled(GROW_INTERVAL + 2)
        elapsed += GROW_INTERVAL + 2
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT current_radius_m FROM nodes WHERE id=?",
                                    (node["id"],)) as cur:
                r = (await cur.fetchone())[0]
        log(f"  📈 Радиус {node['name']}: {int(r)}м", "")


# ── СЦЕНАРИЙ ─────────────────────────────────────────────────────────────────

async def main():
    global session
    session = aiohttp.ClientSession()

    print("\n" + "=" * 60)
    print("  🎬 GPS STRATEGY — ПОЛНОЕ ДЕМО")
    print("=" * 60)
    print(f"  TIME_SCALE: {TIME_SCALE}x")
    print(f"  Открой /presentation в браузере и смотри!")
    print("=" * 60 + "\n")

    try:
        async with session.get(f"{SERVER_URL}/api/game",
                               timeout=aiohttp.ClientTimeout(total=3)) as r: await r.json()
    except Exception:
        print("❌ Сервер недоступен на localhost:8001. Запусти python3 bot.py")
        await session.close(); return

    nodes = await get_nodes()
    if len(nodes) < 4:
        print(f"❌ В БД только {len(nodes)} нод. Нужно минимум 4 (ALEX, BEATRICE + 2 промежуточные).")
        await session.close(); return

    state = await get_game_state()
    target_a = next((n for n in nodes if n["id"] == state.get("target_node_a")), None) \
               or next((n for n in nodes if "ALEX" in (n["name"] or "")), None)
    target_b = next((n for n in nodes if n["id"] == state.get("target_node_b")), None) \
               or next((n for n in nodes if "BEATRICE" in (n["name"] or "")), None)
    if not target_a or not target_b:
        print("❌ Нужны ноды NODE ALEX и NODE BEATRICE.")
        await session.close(); return

    other_nodes = [n for n in nodes if n["id"] not in (target_a["id"], target_b["id"])
                                       and n.get("node_type", "node") == "node"]
    other_nodes.sort(key=lambda n: haversine(target_a["lat"], target_a["lon"], n["lat"], n["lon"]))

    # Радиус для промежуточных нод. С новой логикой: ALEX/BEATRICE не растут,
    # нужно чтобы СОСЕДНЯЯ обычная нода своим радиусом доставала их центр.
    # Поэтому считаем дистанции от ALEX до первой, и от BEATRICE до последней.
    # Берём максимум всех соседних дистанций.
    chain_path = [target_a] + other_nodes + [target_b]
    max_gap = max(haversine(chain_path[i]["lat"], chain_path[i]["lon"],
                            chain_path[i+1]["lat"], chain_path[i+1]["lon"])
                  for i in range(len(chain_path)-1))
    CHAIN_RADIUS = max(int(max_gap * 1.15), 60)

    log(f"Карта: {len(nodes)} нод. Цель: соединить {target_a['name']} ↔ {target_b['name']}", "🗺")
    log(f"Промежуточные ноды: {[n['name'] for n in other_nodes]}", "🗺")
    log(f"Макс gap: {int(max_gap)}м → радиус обычных нод после захвата {CHAIN_RADIUS}м", "📏")
    log(f"ALEX и BEATRICE остаются с базовым радиусом — они якорные", "⚓")
    await sleep_scaled(2)

    # ═══ ПОДГОТОВКА ════════════════════════════════════════════════════════
    log("─" * 50, ""); log("ПОДГОТОВКА: ALEX и BEATRICE сразу у Opposition (якоря)", "⚙️"); log("─" * 50, "")
    await set_owner(target_a["id"], "opposition")
    log(f"🔴 {target_a['name']} → Opposition (якорная, радиус не растёт)", "⚓")
    await sleep_scaled(2)
    await set_owner(target_b["id"], "opposition")
    log(f"🔴 {target_b['name']} → Opposition (якорная, радиус не растёт)", "⚓")
    await sleep_scaled(ACT_PAUSE)

    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE game_state SET active=1, current_phase=1, phase_started_at=? WHERE id=1",
                          (datetime.now().isoformat(),))
        await conn.commit()
    log("Игра активирована", "🚀"); await sleep_scaled(2)

    # ═══ АКТ 1 — РЕГИСТРАЦИЯ ═══════════════════════════════════════════════
    log("─" * 50, ""); log("АКТ 1: Игроки входят в игру", "🎭"); log("─" * 50, "")
    await cleanup_fakes()
    opp_ids, opp_anons = {}, {}
    for name in OPP_PLAYERS:
        pid, anon = await spawn_fake(name, "opposition")
        opp_ids[name] = pid; opp_anons[name] = anon
        log(f"🔴 FAKE_{name} вошёл в Opposition (ID: {anon})", "👤")
        await sleep_scaled(2)
    sys_ids = {}
    for name in SYS_PLAYERS:
        pid, _ = await spawn_fake(name, "system")
        sys_ids[name] = pid
        log(f"⚙️ FAKE_{name} вошёл в System", "👤")
        await sleep_scaled(2)
    await sleep_scaled(ACT_PAUSE)

    # Стартовые позиции (Opposition далеко от своих нод чтобы радиусы не росли неконтролируемо)
    center_lat = (target_a["lat"] + target_b["lat"]) / 2
    center_lon = (target_a["lon"] + target_b["lon"]) / 2
    log("Игроки занимают стартовые позиции", "📍")
    await move_to(opp_ids["ALICE"],   target_a["lat"] + 0.0030, target_a["lon"] - 0.0030); await sleep_scaled(1)
    await move_to(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030); await sleep_scaled(1)
    await move_to(sys_ids["BOB"],     center_lat + 0.0015, center_lon - 0.0010); await sleep_scaled(1)
    await move_to(sys_ids["DIANA"],   center_lat - 0.0015, center_lon + 0.0010); await sleep_scaled(ACT_PAUSE)

    first_node = other_nodes[0]

    # ═══ АКТ 2 — ЗАХВАТ ════════════════════════════════════════════════════
    log("─" * 50, ""); log(f"АКТ 2: ALICE атакует {first_node['name']}", "🎭"); log("─" * 50, "")
    log(f"🔴 ALICE движется внутрь круга {first_node['name']}...", "🏃")
    await smooth_walk(opp_ids["ALICE"], target_a["lat"] + 0.0030, target_a["lon"] - 0.0030,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log(f"🔴 ALICE внутри круга — начинает захват {first_node['name']} (удержание)", "⚡")
    await start_capture(opp_ids["ALICE"], first_node["id"])
    log("🚨 Сервер шлёт пуш 'Нода атакована' реальным System", "📡")

    # 🔁 ВАЖНО: запускаем фоновый пинг ALICE — пока он работает, геолокация всегда свежая
    # и scheduler не сбросит захват пока ALICE "стоит" на ноде
    alice_pinger, alice_stop = start_pinger(opp_ids["ALICE"], first_node["lat"], first_node["lon"])
    log("📡 Фоновый пинг ALICE запущен — геолокация будет всегда свежая", "")

    await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 3 — ЗАЩИТА + КОНТЕСТЕД-ЛОГИРОВАНИЕ ══════════════════════════════
    log("─" * 50, ""); log("АКТ 3: BOB перехват + повторное логирование", "🎭"); log("─" * 50, "")
    log(f"⚙️ BOB бежит внутрь круга {first_node['name']}", "🏃")
    await smooth_walk(sys_ids["BOB"], center_lat + 0.0015, center_lon - 0.0010,
                      first_node["lat"], first_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
    await sleep_scaled(2)
    log("⚙️ BOB внутри круга → DEFEND → захват ЗАМОРОЖЕН, AGENT залогирован", "🛡")
    await freeze_and_identify(sys_ids["BOB"], first_node["id"])

    # BOB тоже пингуем — чтобы scheduler видел его в радиусе и контестед-логирование работало
    bob_pinger, bob_stop = start_pinger(sys_ids["BOB"], first_node["lat"], first_node["lon"])
    await sleep_scaled(ACT_PAUSE)

    log("⚙️ BOB остаётся у замороженной ноды — scheduler повторно логирует AGENT каждые 30 сек", "📡")
    log("⏳ Ждём 35 сек чтобы увидеть повторную идентификацию...", "⏳")
    await sleep_scaled(35)
    log("📋 AGENT повторно записан — System накапливает данные о цели", "✅")
    await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 4 — УХОД SYSTEM, АВТО-ВОЗОБНОВЛЕНИЕ ══════════════════════════════
    log("─" * 50, ""); log("АКТ 4: BOB уходит из круга → захват возобновляется", "🎭"); log("─" * 50, "")
    log(f"⚙️ BOB выходит ИЗ КРУГА ноды (~330м) — заморозка снимется", "🏃")

    # Останавливаем BOB-пинг т.к. он уходит
    await stop_pinger(bob_pinger, bob_stop)

    # Уходим на 330м от first_node — точно вне любого радиуса
    bob_far_lat = first_node["lat"] + 0.003
    bob_far_lon = first_node["lon"] - 0.003
    await smooth_walk(sys_ids["BOB"], first_node["lat"], first_node["lon"],
                      bob_far_lat, bob_far_lon, steps=WALK_STEPS, delay=WALK_DELAY)
    log("⏳ ALICE остаётся ВНУТРИ круга (фон-пинг работает) → ждём 35 сек разморозки...", "⏳")
    await sleep_scaled(35)
    log("▶️ Scheduler разморозил захват — нода снова оранжевая, таймер тикает", "✅")
    await sleep_scaled(2)

    # ═══ АКТ 5 — ЗАВЕРШЕНИЕ ЗАХВАТА + РОСТ РАДИУСА ══════════════════════════
    log("─" * 50, ""); log("АКТ 5: естественное завершение захвата (scheduler)", "🎭"); log("─" * 50, "")
    await hold_position_for_capture(first_node, opp_ids["ALICE"])
    log("─" * 50, ""); log("АКТ 5b: ALICE удерживает → scheduler растит радиус", "🎭"); log("─" * 50, "")
    await hold_for_radius_growth(first_node, opp_ids["ALICE"], hold_seconds=40)

    # Останавливаем фон-пинг ALICE — она пойдёт к другим нодам
    await stop_pinger(alice_pinger, alice_stop)
    await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 6 — СБРОС: Opposition ушла надолго ════════════════════════════════
    if len(other_nodes) >= 3:
        sacrifice_node = other_nodes[1]
        log("─" * 50, ""); log(f"АКТ 6: Демонстрация автоматического сброса захвата", "🎭"); log("─" * 50, "")
        log("Условие сброса: Opposition ушла из радиуса ноды дольше чем ABANDON_TIMEOUT_SEC", "📖")

        log(f"🔴 CHARLIE начинает захват {sacrifice_node['name']}", "⚡")
        await move_to(opp_ids["CHARLIE"], sacrifice_node["lat"], sacrifice_node["lon"])
        await start_capture(opp_ids["CHARLIE"], sacrifice_node["id"])
        await sleep_scaled(5)

        log(f"⚙️ DIANA замораживает захват", "🛡")
        await move_to(sys_ids["DIANA"], sacrifice_node["lat"], sacrifice_node["lon"])
        await freeze_and_identify(sys_ids["DIANA"], sacrifice_node["id"])
        await sleep_scaled(5)

        log("⚙️ DIANA уходит (System не влияет на сброс — только на заморозку)", "🏃")
        await smooth_walk(sys_ids["DIANA"], sacrifice_node["lat"], sacrifice_node["lon"],
                          center_lat + 0.002, center_lon, steps=4, delay=1)

        log("🔴 CHARLIE тоже уходит ДАЛЕКО — за пределы радиуса ноды", "🏃")
        # Уходим на ~330м — точно за пределы любого радиуса
        far_lat = sacrifice_node["lat"] + 0.003
        far_lon = sacrifice_node["lon"] + 0.003
        await smooth_walk(opp_ids["CHARLIE"], sacrifice_node["lat"], sacrifice_node["lon"],
                          far_lat, far_lon, steps=5, delay=1)

        # Ждём пока scheduler заметит что Opposition ушла и сбросит захват
        # ABANDON_TIMEOUT_SEC из config (у тебя 30 сек) + буфер на цикл проверки
        try:
            abandon_timeout = int(getattr(_game_config, "ABANDON_TIMEOUT_SEC", 180))
        except Exception:
            abandon_timeout = 180
        total_wait = abandon_timeout + 35  # буфер на цикл scheduler
        log(f"⏳ Ждём {abandon_timeout} сек (ABANDON_TIMEOUT_SEC) + 35 сек на цикл scheduler...", "⌛")
        for elapsed in range(0, total_wait, 10):
            # Подкачиваем геолокацию чтобы CHARLIE точно "был зафиксирован" вдалеке
            await move_to(opp_ids["CHARLIE"], far_lat, far_lon)
            # Проверяем состояние ноды
            async with aiosqlite.connect(DB_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    "SELECT owner, capture_started_at FROM nodes WHERE id=?",
                    (sacrifice_node["id"],)
                ) as cur:
                    row = await cur.fetchone()
            if row and row["capture_started_at"] is None and row["owner"] == "system":
                log(f"✅ Scheduler сбросил захват — нода {sacrifice_node['name']} снова синяя", "🔄")
                break
            log(f"  ⏱  {elapsed}/{total_wait} сек, нода всё ещё в процессе...", "")
            await sleep_scaled(10)
        await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 7 — CHARLIE с другой стороны ══════════════════════════════════════
    if len(other_nodes) >= 2:
        second_node = other_nodes[-1]
        log("─" * 50, ""); log(f"АКТ 7: CHARLIE атакует с фланга {second_node['name']}", "🎭"); log("─" * 50, "")
        await smooth_walk(opp_ids["CHARLIE"], target_b["lat"] - 0.0030, target_b["lon"] + 0.0030,
                          second_node["lat"], second_node["lon"], steps=WALK_STEPS, delay=WALK_DELAY)
        log(f"🔴 CHARLIE захватывает {second_node['name']}", "⚡")
        await start_capture(opp_ids["CHARLIE"], second_node["id"])
        await sleep_scaled(2)
        await hold_position_for_capture(second_node, opp_ids["CHARLIE"])
        await hold_for_radius_growth(second_node, opp_ids["CHARLIE"], hold_seconds=30)
        await sleep_scaled(ACT_PAUSE)

    # ═══ АКТ 8 — QR-ВЕРИФИКАЦИЯ ════════════════════════════════════════════
    log("─" * 50, ""); log("АКТ 8: QR-верификация — System пытается вычислить ALICE", "🎭"); log("─" * 50, "")
    log("⚙️ BOB 'сканирует QR' ALICE и пробует угадать её AGENT-ID", "📷")
    await sleep_scaled(2)
    # Угадывает правильно
    correct_anon = opp_anons["ALICE"]
    log(f"⚙️ BOB вводит догадку: {correct_anon} (правильно)", "🧠")
    res = await verify_player(sys_ids["BOB"], opp_ids["ALICE"], correct_anon)
    if res.get("correct"):
        log(f"✅ ВЕРНО! ALICE = {correct_anon}. +15 очков System", "🎯")
    else:
        log(f"❌ Не получилось верифицировать (возможно уже было)", "⚠️")
    await sleep_scaled(ACT_PAUSE)

    log("⚙️ DIANA пытается верифицировать CHARLIE — но ошибается", "📷")
    await sleep_scaled(2)
    fake_guess = "AGENT_XXXX"
    log(f"⚙️ DIANA вводит неверную догадку: {fake_guess}", "🧠")
    res = await verify_player(sys_ids["DIANA"], opp_ids["CHARLIE"], fake_guess)
    if res.get("ok") and not res.get("correct"):
        log(f"❌ Неверно. Правильно было: {opp_anons['CHARLIE']}", "🕵")
        log("CHARLIE остался анонимен — Opposition сохранила тень", "✨")
    await sleep_scaled(ACT_PAUSE)

    # ═══ ФИНАЛ ══════════════════════════════════════════════════════════════
    log("─" * 50, ""); log("ФИНАЛ: цепочка построена — проверка победы", "🏆"); log("─" * 50, "")
    log("Scheduler проверит цепочку и выпишет победу...", "⏳")
    await sleep_scaled(30)

    log("─" * 50, ""); log("🎬 СЦЕНАРИЙ ЗАВЕРШЁН", "✨")
    log("/admin_replay — полная хронология", "📜")
    log("/score — итоговый счёт", "📊")
    log("/admin_reset — сбросить ноды + ОЧИСТИТЬ ЛОГИ", "🔄")
    log("/admin_unspawn all — убрать фейков", "🗑")
    log("─" * 50, "")
    await session.close()


if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⛔ Прерван"); sys.exit(0)
