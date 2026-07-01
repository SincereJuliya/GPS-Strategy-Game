import aiosqlite
import random
from datetime import datetime, timedelta

DB_PATH = "game.db"


def _gen_agent_id() -> str:
    return "AGENT_" + "".join(random.choices("0123456789ABCDEF", k=4))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        await db.execute("""
            CREATE TABLE IF NOT EXISTS players (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                team TEXT,
                anonymous_id TEXT,
                last_location_lat REAL,
                last_location_lon REAL,
                last_location_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                lat REAL,
                lon REAL,
                node_type TEXT DEFAULT 'node',
                owner TEXT DEFAULT 'system',
                current_radius_m REAL DEFAULT 80,
                base_radius_m REAL DEFAULT 80,
                max_radius_m REAL DEFAULT 200,
                capture_started_at TEXT,
                capture_elapsed_sec REAL DEFAULT 0,
                capture_frozen INTEGER DEFAULT 0,
                freeze_started_at TEXT,
                capturing_player_id INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER,
                player_id INTEGER,
                started_at TEXT,
                completed_at TEXT,
                interrupted INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS identifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                system_player_id INTEGER,
                opp_player_id INTEGER,
                node_id INTEGER,
                lat REAL,
                lon REAL,
                identified_at TEXT,
                anonymous_id TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                system_player_id INTEGER,
                scanned_player_id INTEGER,
                guessed_anonymous_id TEXT,
                real_anonymous_id TEXT,
                correct INTEGER DEFAULT 0,
                verified_at TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS game_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                active INTEGER DEFAULT 0,
                current_phase INTEGER DEFAULT 0,
                phase_started_at TEXT,
                target_node_a INTEGER,
                target_node_b INTEGER
            )
        """)

        await db.execute("""
            INSERT OR IGNORE INTO game_state (id, active, current_phase)
            VALUES (1, 0, 0)
        """)

        # Migration: finale phase support (idempotent)
        # finale_stage: NULL = not in finale, 'rendezvous' or 'identification'
        # finale_stage_started_at: ISO timestamp of when the current stage started
        # rendezvous_node_id: node id of the meeting point
        # final_guess_submitted: 1 once System has submitted their final mapping
        for col, ddl in [
            ("verification_started_at", "TEXT"),  # legacy, kept for back-compat
            ("finale_stage", "TEXT"),
            ("finale_stage_started_at", "TEXT"),
            ("rendezvous_node_id", "INTEGER"),
            ("final_guess_submitted", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(f"ALTER TABLE game_state ADD COLUMN {col} {ddl}")
            except Exception:
                pass  # column already exists

        # Final guesses table: System's submitted AGENT-ID -> player mapping
        await db.execute("""
            CREATE TABLE IF NOT EXISTS final_guesses (
                anonymous_id TEXT PRIMARY KEY,
                guessed_player_id INTEGER,
                correct INTEGER DEFAULT 0,
                auto_identified INTEGER DEFAULT 0
            )
        """)

        # Puzzle sessions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS puzzle_sessions (
                id TEXT PRIMARY KEY,
                node_id INTEGER,
                player_id INTEGER,
                puzzle_type TEXT,
                puzzle_data TEXT,
                solution TEXT,
                started_at TEXT,
                status TEXT DEFAULT 'active',
                created_for_progress INTEGER DEFAULT 80
            )
        """)

        # Migrations for existing databases
        for col_sql in [
            "ALTER TABLE players ADD COLUMN anonymous_id TEXT",
            "ALTER TABLE identifications ADD COLUMN anonymous_id TEXT",
            "ALTER TABLE nodes ADD COLUMN capture_progress INTEGER DEFAULT 0",
            "ALTER TABLE nodes ADD COLUMN puzzles_solved TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN max_radius_m REAL DEFAULT 200",
            # verifications table — added with the finale rework. Old DBs
            # created before that have an empty table missing every column;
            # add them all idempotently so a SELECT * keeps working.
            "ALTER TABLE verifications ADD COLUMN scanned_player_id INTEGER",
            "ALTER TABLE verifications ADD COLUMN guessed_anonymous_id TEXT",
            "ALTER TABLE verifications ADD COLUMN real_anonymous_id TEXT",
            "ALTER TABLE verifications ADD COLUMN correct INTEGER DEFAULT 0",
            "ALTER TABLE verifications ADD COLUMN verified_at TEXT",
            # New puzzle-based capture flow: track when the current 3-min
            # attack window began, and when the last puzzle was solved (for
            # the between-puzzles cooldown). Both are ISO timestamps or NULL.
            "ALTER TABLE nodes ADD COLUMN attack_window_started_at TEXT",
            "ALTER TABLE nodes ADD COLUMN last_puzzle_solved_at TEXT",
            # Timed-capture v2: the deadline at which the node auto-captures
            # if the attacker is still in radius. Set on first puzzle open;
            # shortened by every solved puzzle; cleared on 100% capture or
            # when the attacker walks away.
            "ALTER TABLE nodes ADD COLUMN capture_deadline_at TEXT",
        ]:
            try:
                await db.execute(col_sql)
            except Exception:
                pass

        await db.commit()

    await _backfill_anonymous_ids()


async def _backfill_anonymous_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT telegram_id FROM players WHERE anonymous_id IS NULL") as cur:
            players = await cur.fetchall()
        for p in players:
            await db.execute(
                "UPDATE players SET anonymous_id = ? WHERE telegram_id = ?",
                (_gen_agent_id(), p["telegram_id"])
            )
        if players:
            await db.commit()


# ── Players ───────────────────────────────────────────────────────────────────

async def get_player(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM players WHERE telegram_id = ?", (telegram_id,)
        ) as cur:
            return await cur.fetchone()


async def register_player(telegram_id: int, username: str, team: str) -> str:
    anon_id = _gen_agent_id()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        while True:
            async with db.execute(
                "SELECT 1 FROM players WHERE anonymous_id = ?", (anon_id,)
            ) as cur:
                if not await cur.fetchone():
                    break
            anon_id = _gen_agent_id()
        await db.execute(
            "INSERT OR REPLACE INTO players (telegram_id, username, team, anonymous_id) VALUES (?, ?, ?, ?)",
            (telegram_id, username, team, anon_id)
        )
        await db.commit()
    return anon_id


async def update_player_location(telegram_id: int, lat: float, lon: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET last_location_lat=?, last_location_lon=?, last_location_at=? WHERE telegram_id=?",
            (lat, lon, datetime.now().isoformat(), telegram_id)
        )
        await db.commit()


async def get_all_players(team: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if team:
            async with db.execute("SELECT * FROM players WHERE team = ?", (team,)) as cur:
                return await cur.fetchall()
        async with db.execute("SELECT * FROM players") as cur:
            return await cur.fetchall()


# ── Nodes ─────────────────────────────────────────────────────────────────────

async def get_all_nodes():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM nodes") as cur:
            return await cur.fetchall()


async def add_node(name: str, lat: float, lon: float,
                   node_type: str = "node", radius: float = 80,
                   max_radius_m: float = None):
    """Create a node. ``max_radius_m`` is the per-node growth cap; if None,
    falls back to config.RADIUS_MAX_M (200m default)."""
    if max_radius_m is None:
        try:
            import config
            max_radius_m = float(getattr(config, "RADIUS_MAX_M", 200))
        except Exception:
            max_radius_m = 200.0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO nodes (name, lat, lon, node_type, owner, current_radius_m, base_radius_m, max_radius_m) "
            "VALUES (?, ?, ?, ?, 'system', ?, ?, ?)",
            (name, lat, lon, node_type, radius, radius, max_radius_m)
        )
        await db.commit()


async def set_node_max_radius(node_id: int, max_radius_m: float):
    """Update a node's growth cap. If current_radius_m is already above the
    new cap, the live radius is clamped down so the visual matches the rule."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET max_radius_m=?, current_radius_m=MIN(current_radius_m, ?) WHERE id=?",
            (max_radius_m, max_radius_m, node_id)
        )
        await db.commit()


async def update_node_owner(node_id: int, owner: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET owner=?, capture_started_at=NULL, capturing_player_id=NULL, capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL WHERE id=?",
            (owner, node_id)
        )
        await db.commit()


async def grow_node_radius(node_id: int, step_m: float, max_m: float = None):
    """Grow a node's current radius by ``step_m`` metres, capped at the node's
    own ``max_radius_m`` (per-node cap). The ``max_m`` argument is a legacy
    fallback used only if the node has no per-node cap set.
    Never shrinks: if a node is already above its cap (e.g. set via the
    demo's ``set_radius`` admin endpoint or because the cap was lowered later),
    this is a no-op rather than a shrink."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET current_radius_m = MAX(current_radius_m, "
            "MIN(current_radius_m + ?, COALESCE(max_radius_m, ?))) WHERE id = ?",
            (step_m, max_m if max_m is not None else 200, node_id)
        )
        await db.commit()


# ── Capture ───────────────────────────────────────────────────────────────────

async def start_node_capture(node_id: int, player_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET capture_started_at=?, capturing_player_id=?, capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL WHERE id=?",
            (datetime.now().isoformat(), player_id, node_id)
        )
        await db.commit()


async def freeze_node_capture(node_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)) as cur:
            node = await cur.fetchone()
        if not node or not node["capture_started_at"] or node["capture_frozen"]:
            return
        started = datetime.fromisoformat(node["capture_started_at"])
        new_elapsed = (datetime.now() - started).total_seconds() + (node["capture_elapsed_sec"] or 0)
        await db.execute(
            "UPDATE nodes SET capture_frozen=1, freeze_started_at=?, capture_elapsed_sec=? WHERE id=?",
            (datetime.now().isoformat(), new_elapsed, node_id)
        )
        await db.commit()


async def resume_node_capture(node_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT capture_elapsed_sec FROM nodes WHERE id = ?", (node_id,)) as cur:
            node = await cur.fetchone()
        if not node:
            return
        elapsed = node["capture_elapsed_sec"] or 0
        new_start = datetime.now() - timedelta(seconds=elapsed)
        await db.execute(
            "UPDATE nodes SET capture_frozen=0, freeze_started_at=NULL, capture_started_at=? WHERE id=?",
            (new_start.isoformat(), node_id)
        )
        await db.commit()


async def interrupt_node_capture(node_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET capture_started_at=NULL, capturing_player_id=NULL, capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL WHERE id=?",
            (node_id,)
        )
        await db.commit()


async def create_capture(node_id: int, player_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO captures (node_id, player_id, started_at) VALUES (?, ?, ?)",
            (node_id, player_id, datetime.now().isoformat())
        )
        await db.commit()


# ── Identifications ───────────────────────────────────────────────────────────

async def add_identification(system_player_id: int, opp_player_id: int,
                              node_id: int, lat: float, lon: float) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id FROM identifications WHERE system_player_id=? AND opp_player_id=? AND identified_at > datetime('now','-5 minutes')",
            (system_player_id, opp_player_id)
        ) as cur:
            if await cur.fetchone():
                return False

        async with db.execute(
            "SELECT anonymous_id FROM players WHERE telegram_id = ?", (opp_player_id,)
        ) as cur:
            opp = await cur.fetchone()

        anon_id = opp["anonymous_id"] if opp and opp["anonymous_id"] else "AGENT_????"

        await db.execute(
            "INSERT INTO identifications (system_player_id, opp_player_id, node_id, lat, lon, identified_at, anonymous_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (system_player_id, opp_player_id, node_id, lat, lon, datetime.now().isoformat(), anon_id)
        )
        await db.commit()
        return True


async def get_identifications(system_player_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM identifications WHERE system_player_id=? ORDER BY identified_at DESC",
            (system_player_id,)
        ) as cur:
            return await cur.fetchall()


async def get_all_identifications():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT i.*, p.username AS system_username FROM identifications i LEFT JOIN players p ON p.telegram_id=i.system_player_id ORDER BY i.identified_at DESC"
        ) as cur:
            return await cur.fetchall()


# ── Verifications ─────────────────────────────────────────────────────────────

async def add_verification(system_player_id: int, scanned_player_id: int,
                            guessed_anon_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT anonymous_id, team FROM players WHERE telegram_id = ?", (scanned_player_id,)
        ) as cur:
            scanned = await cur.fetchone()
        if not scanned:
            return {"ok": False, "error": "Player not found"}
        if scanned["team"] != "opposition":
            return {"ok": False, "error": "Can only verify Opposition players"}

        real_anon = scanned["anonymous_id"]
        correct = 1 if guessed_anon_id.upper().strip() == real_anon else 0

        async with db.execute(
            "SELECT id FROM verifications WHERE system_player_id=? AND scanned_player_id=?",
            (system_player_id, scanned_player_id)
        ) as cur:
            if await cur.fetchone():
                return {"ok": False, "error": "Already verified this player", "real_anonymous_id": real_anon}

        await db.execute(
            "INSERT INTO verifications (system_player_id, scanned_player_id, guessed_anonymous_id, real_anonymous_id, correct, verified_at) VALUES (?, ?, ?, ?, ?, ?)",
            (system_player_id, scanned_player_id, guessed_anon_id.upper().strip(), real_anon, correct, datetime.now().isoformat())
        )
        await db.commit()

    return {"ok": True, "correct": bool(correct), "real_anonymous_id": real_anon, "guessed": guessed_anon_id.upper().strip()}


async def get_all_verifications():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT v.*, sp.username AS system_username, hp.username AS opp_username FROM verifications v LEFT JOIN players sp ON sp.telegram_id=v.system_player_id LEFT JOIN players hp ON hp.telegram_id=v.scanned_player_id ORDER BY v.verified_at DESC"
        ) as cur:
            return await cur.fetchall()


# ── Game state ────────────────────────────────────────────────────────────────

async def get_game_state():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM game_state WHERE id = 1") as cur:
            return await cur.fetchone()


async def set_game_active(active: bool, phase: int = None):
    async with aiosqlite.connect(DB_PATH) as db:
        if phase is not None:
            await db.execute(
                "UPDATE game_state SET active=?, current_phase=?, phase_started_at=? WHERE id=1",
                (1 if active else 0, phase, datetime.now().isoformat())
            )
        else:
            await db.execute("UPDATE game_state SET active=? WHERE id=1", (1 if active else 0,))
        await db.commit()



# ─────────────────────────────────────────────────────────────────────────────
# PUZZLES
# ─────────────────────────────────────────────────────────────────────────────
import json as _json
import uuid as _uuid

async def create_puzzle_session(player_id: int, node_id: int, puzzle_type: str,
                                 puzzle_data: dict, solution, progress_target: int = 80) -> str:
    """Create a new puzzle session. Returns session_id."""
    session_id = str(_uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE puzzle_sessions SET status='closed' WHERE player_id=? AND status IN ('active','frozen')",
            (player_id,)
        )
        await db.execute("""
            INSERT INTO puzzle_sessions
            (id, node_id, player_id, puzzle_type, puzzle_data, solution, started_at, status, created_for_progress)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (session_id, node_id, player_id, puzzle_type,
              _json.dumps(puzzle_data), _json.dumps(solution),
              datetime.now().isoformat(), progress_target))
        await db.commit()
    return session_id


async def get_puzzle_session(session_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM puzzle_sessions WHERE id=?", (session_id,)) as cur:
            row = await cur.fetchone()
            if not row: return None
            d = dict(row)
            d["puzzle_data"] = _json.loads(d["puzzle_data"])
            if d["solution"]: d["solution"] = _json.loads(d["solution"])
            return d


async def close_puzzle_session(session_id: str, status: str = "closed"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE puzzle_sessions SET status=? WHERE id=?", (status, session_id))
        await db.commit()


async def freeze_puzzle_session(session_id: str, frozen: bool):
    status = "frozen" if frozen else "active"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE puzzle_sessions SET status=? WHERE id=? AND status IN ('active','frozen')",
            (status, session_id)
        )
        await db.commit()


async def update_node_capture_progress(node_id: int, progress: int, owner: str = None,
                                        radius_boost: float = 0):
    """Update a node's capture progress (and optionally owner) and set its
    current radius to a fraction of the per-node ``max_radius_m`` cap that
    matches the new progress value:

        current_radius_m = max(base_radius_m, max_radius_m * progress / 100)

    So a node sits at its base (capture-zone) radius until it's captured,
    grows to 80% of its cap after one puzzle solved (progress=80), and to
    the full cap after the second puzzle (progress=100). The clamp to
    base ensures the radius never visually shrinks below the capture zone
    when the admin has set a small max (e.g. anchor nodes where max=base).

    The ``radius_boost`` argument is accepted for backwards compatibility
    with older callers but is ignored — radius now follows progress, not
    accumulated boosts. Falls back to config.RADIUS_MAX_M for nodes that
    have no per-node cap set."""
    try:
        import config
        fallback_cap = float(getattr(config, "RADIUS_MAX_M", 200))
    except Exception:
        fallback_cap = 200.0
    fraction = max(0.0, min(progress, 100)) / 100.0
    # When the node reaches 100% the old capture-in-progress fields
    # (capture_started_at, capturing_player_id, capture_elapsed_sec,
    # capture_frozen, freeze_started_at) need to be cleared, otherwise the
    # player map keeps drawing the orange "capture in progress" outline
    # around a node that is already fully owned. These fields are debris
    # from the legacy timed-capture flow; the puzzle flow never resets them
    # on its own.
    fully_captured = progress >= 100
    async with aiosqlite.connect(DB_PATH) as db:
        if owner:
            if fully_captured:
                await db.execute(
                    "UPDATE nodes SET capture_progress=?, owner=?, "
                    "current_radius_m = MAX(base_radius_m, COALESCE(max_radius_m, ?) * ?), "
                    "capture_started_at=NULL, capturing_player_id=NULL, "
                    "capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL "
                    "WHERE id=?",
                    (progress, owner, fallback_cap, fraction, node_id)
                )
            else:
                await db.execute(
                    "UPDATE nodes SET capture_progress=?, owner=?, "
                    "current_radius_m = MAX(base_radius_m, COALESCE(max_radius_m, ?) * ?) "
                    "WHERE id=?",
                    (progress, owner, fallback_cap, fraction, node_id)
                )
        else:
            if fully_captured:
                await db.execute(
                    "UPDATE nodes SET capture_progress=?, "
                    "current_radius_m = MAX(base_radius_m, COALESCE(max_radius_m, ?) * ?), "
                    "capture_started_at=NULL, capturing_player_id=NULL, "
                    "capture_elapsed_sec=0, capture_frozen=0, freeze_started_at=NULL "
                    "WHERE id=?",
                    (progress, fallback_cap, fraction, node_id)
                )
            else:
                await db.execute(
                    "UPDATE nodes SET capture_progress=?, "
                    "current_radius_m = MAX(base_radius_m, COALESCE(max_radius_m, ?) * ?) "
                    "WHERE id=?",
                    (progress, fallback_cap, fraction, node_id)
                )
        await db.commit()


async def mark_puzzle_solved(node_id: int, puzzle_type: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT puzzles_solved FROM nodes WHERE id=?", (node_id,)) as cur:
            row = await cur.fetchone()
            current = row["puzzles_solved"] if row and row["puzzles_solved"] else ""
        solved = [p for p in current.split(",") if p]
        if puzzle_type not in solved:
            solved.append(puzzle_type)
        await db.execute(
            "UPDATE nodes SET puzzles_solved=? WHERE id=?", (",".join(solved), node_id)
        )
        await db.commit()


async def reset_node_puzzles(node_id: int):
    """When resetting a node, clears solved + progress."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE nodes SET puzzles_solved='', capture_progress=0 WHERE id=?",
            (node_id,)
        )
        await db.commit()