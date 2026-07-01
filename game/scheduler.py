import asyncio
from datetime import datetime, timedelta

import database as db
import config
from game.geo import (
    find_nodes_in_radius,
    find_connected_nodes,
    check_path_exists,
    is_location_fresh,
)

# All parameters are loaded from config.py — defaults are used if missing
LOCATION_FRESH_SEC = getattr(config, "LOCATION_FRESH_SEC", 90)


# ── Node capture ─────────────────────────────────────────────────────────────

async def check_captures(bot):
    """Every 10 sec, look at every node with an active capture deadline. When
    the deadline passes, either finalize the capture (attacker still in the
    radius) or cancel the attack (attacker walked away).

    Uses the new timed-capture model: opening a puzzle sets a 3-minute
    deadline; solving puzzles cuts it forward. When the deadline arrives,
    the node auto-captures at 100% — no puzzle solve needed — provided the
    attacker is still standing there."""
    from game.geo import haversine
    while True:
        await asyncio.sleep(10)
        try:
            game_state = await db.get_game_state()
            if not game_state or not game_state["active"]:
                continue

            nodes = await db.get_all_nodes()
            for node in nodes:
                node = dict(node)
                deadline_iso = node.get("capture_deadline_at")
                if not deadline_iso:
                    continue
                try:
                    deadline = datetime.fromisoformat(deadline_iso)
                except Exception:
                    continue
                if datetime.now() < deadline:
                    continue

                # Deadline reached. Verify the attacker is still in the
                # radius with a fresh location. If not, cancel silently —
                # walking away breaks the capture, this is the
                # anti-camping rule.
                attacker_id = node.get("capturing_player_id")
                attacker = None
                if attacker_id:
                    attacker = await db.get_player(attacker_id)

                in_radius = False
                if attacker:
                    lat = attacker["last_location_lat"]
                    lon = attacker["last_location_lon"]
                    ts = attacker["last_location_at"]
                    if lat is not None and lon is not None and is_location_fresh(ts, LOCATION_FRESH_SEC):
                        dist = haversine(lat, lon, node["lat"], node["lon"])
                        if dist <= (node["current_radius_m"] or 80) + 15:
                            in_radius = True

                if not in_radius:
                    # Attacker walked away. Cancel the attack; keep
                    # whatever progress was already locked in (e.g. 80%
                    # from a solved first puzzle).
                    import aiosqlite
                    async with aiosqlite.connect(db.DB_PATH) as conn:
                        await conn.execute(
                            "UPDATE nodes SET capture_deadline_at=NULL, "
                            "capturing_player_id=NULL WHERE id=?",
                            (node["id"],)
                        )
                        await conn.commit()
                    print(f"[scheduler] capture of {node['name']!r} cancelled — attacker left the radius")
                    continue

                # Attacker still there → auto-capture at 100%.
                await db.update_node_capture_progress(node["id"], 100, owner="opposition")
                import aiosqlite
                async with aiosqlite.connect(db.DB_PATH) as conn:
                    await conn.execute(
                        "UPDATE nodes SET capture_deadline_at=NULL, "
                        "capturing_player_id=NULL WHERE id=?",
                        (node["id"],)
                    )
                    await conn.commit()
                print(f"[scheduler] {node['name']!r} auto-captured for Opposition (timer expired)")

                # Notify System that they lost a node — reuse the same
                # helper that fires on puzzle-driven captures.
                try:
                    import server as _srv
                    await _srv._notify_node_captured(node["id"], attacker_player_id=attacker_id)
                except Exception as e:
                    print(f"[scheduler] notify failed: {e}")

                # Instant chain check — the timer path shouldn't lag the
                # puzzle path by up to 30 s.
                try:
                    import server as _srv
                    await _srv.check_victory_now()
                except Exception as e:
                    print(f"[scheduler] chain check failed: {e}")

        except Exception as e:
            print(f"[scheduler] check_captures error: {e}")


# ── Radius growth ────────────────────────────────────────────────────────────
# Passive growth over time has been removed by design — a node's current
# radius is now determined entirely by puzzle progress (see
# database.update_node_capture_progress). The fields RADIUS_GROWTH_STEP_M
# and RADIUS_GROWTH_INTERVAL_SEC in config are no longer read; they can be
# left in place for backwards compatibility but have no effect.


# ── Contested: auto-unfreeze ─────────────────────────────────────────────────

async def check_contested(bot):
    """
    Every 30 sec checks frozen nodes.

    Logic:
    - System nearby → keep frozen, log repeated identification
    - System left, Opposition in radius → automatically resume capture
    - System left, Opposition also left:
        * First 3 minutes — node stays frozen (Opposition can return and press CAPTURE)
        * After 3 minutes — capture is reset, node becomes blue again
    """
    # ABANDON_TIMEOUT_SEC can be set in config.py for fast testing (e.g. 30 for demo)
    ABANDON_TIMEOUT_SEC = getattr(config, "ABANDON_TIMEOUT_SEC", 180)

    while True:
        await asyncio.sleep(30)
        try:
            game_state = await db.get_game_state()
            if not game_state or not game_state["active"]:
                continue

            nodes = await db.get_all_nodes()
            system_players = await db.get_all_players("system")

            for node in nodes:
                node = dict(node)
                if not node["capture_frozen"] or not node["capture_started_at"]:
                    continue

                # Which System players are inside the node radius
                from game.geo import haversine
                system_nearby = []
                for sp in system_players:
                    sp = dict(sp)
                    if not is_location_fresh(sp.get("last_location_at"), LOCATION_FRESH_SEC):
                        continue
                    lat, lon = sp.get("last_location_lat"), sp.get("last_location_lon")
                    if lat is None or lon is None: continue
                    # System inside the node's own radius (current_radius_m)
                    if haversine(lat, lon, node["lat"], node["lon"]) <= (node["current_radius_m"] or 80):
                        system_nearby.append(sp)

                # Capturing Opposition player inside node radius?
                opp_id = node["capturing_player_id"]
                opp_nearby = False
                if opp_id:
                    opp = await db.get_player(opp_id)
                    if opp:
                        opp = dict(opp)
                        if is_location_fresh(opp.get("last_location_at"), LOCATION_FRESH_SEC):
                            olat = opp.get("last_location_lat")
                            olon = opp.get("last_location_lon")
                            if olat is not None and olon is not None:
                                if haversine(olat, olon, node["lat"], node["lon"]) <= (node.get("base_radius_m") or 80):
                                    opp_nearby = True

                if system_nearby:
                    # System still nearby — keep frozen, log again
                    if not opp_id: continue
                    for sp in system_nearby:
                        new_id = await db.add_identification(
                            system_player_id=sp["telegram_id"],
                            opp_player_id=opp_id,
                            node_id=node["id"],
                            lat=sp.get("last_location_lat", 0),
                            lon=sp.get("last_location_lon", 0)
                        )
                        if new_id:
                            try:
                                await bot.send_message(
                                    sp["telegram_id"],
                                    f"📡 Agent at *{node['name']}* logged again.",
                                    parse_mode="Markdown"
                                )
                            except Exception:
                                pass

                elif opp_nearby:
                    # System left, Opposition holds position — automatically resume
                    await db.resume_node_capture(node["id"])
                    if opp_id:
                        try:
                            await bot.send_message(
                                opp_id,
                                f"▶️ Capture of *{node['name']}* resumed — System left.",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass

                else:
                    # Nobody nearby — check how long it has been frozen
                    freeze_started = node.get("freeze_started_at")
                    if not freeze_started: continue
                    try:
                        freeze_dt = datetime.fromisoformat(freeze_started)
                        abandoned_sec = (datetime.now() - freeze_dt).total_seconds()
                    except Exception:
                        continue

                    if abandoned_sec >= ABANDON_TIMEOUT_SEC:
                        # 3 minutes — nobody returned, reset capture
                        await db.interrupt_node_capture(node["id"])
                        if opp_id:
                            try:
                                await bot.send_message(
                                    opp_id,
                                    f"❌ Capture of *{node['name']}* cancelled — you did not return within 3 minutes.",
                                    parse_mode="Markdown"
                                )
                            except Exception:
                                pass
                    # Otherwise keep frozen — Opposition can return and press CAPTURE

        except Exception as e:
            print(f"[scheduler] check_contested error: {e}")


# ── Opposition victory condition ─────────────────────────────────────────────

async def check_victory(bot):
    """Every 30 sec checks whether Opposition connected NODE ALEX and NODE BEATRICE.
    On chain completion we hand off to the server's finale flow (rendezvous +
    identification) instead of ending the game right away."""
    # Local import avoids a top-level circular dependency between server and scheduler
    from server import _start_finale

    while True:
        await asyncio.sleep(30)
        try:
            game_state = await db.get_game_state()
            if not game_state or not game_state["active"]: continue
            # Already in finale? Server task handles the rest. game_state is
            # an aiosqlite.Row (not a dict), so use bracket indexing — the
            # column exists after the migration so this is always safe.
            if game_state["finale_stage"]: continue

            target_a = game_state["target_node_a"]
            target_b = game_state["target_node_b"]
            if not target_a or not target_b: continue

            nodes = await db.get_all_nodes()
            nodes_list = [dict(n) for n in nodes]
            connections = find_connected_nodes(nodes_list)

            if check_path_exists(target_a, target_b, connections):
                await _start_finale()

        except Exception as e:
            print(f"[scheduler] check_victory error: {e}")


# ── Phase changes ────────────────────────────────────────────────────────────

async def phase_timer(bot):
    """Checks for phase changes every minute."""
    while True:
        await asyncio.sleep(60)
        try:
            game_state = await db.get_game_state()
            if not game_state or not game_state["active"]: continue

            started = datetime.fromisoformat(game_state["phase_started_at"])
            elapsed = (datetime.now() - started).total_seconds()

            if elapsed >= config.PHASE_DURATION_SEC:
                current_phase = game_state["current_phase"]
                if current_phase >= config.PHASE_COUNT:
                    await end_game(bot)
                else:
                    next_phase = current_phase + 1
                    await db.set_game_active(True, next_phase)
                    all_players = await db.get_all_players()
                    for p in all_players:
                        try:
                            await bot.send_message(
                                p["telegram_id"],
                                f"⏱ *Phase {next_phase} of {config.PHASE_COUNT} started!*\n"
                                f"Next {config.PHASE_DURATION_SEC // 60} minutes.",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass

        except Exception as e:
            print(f"[scheduler] phase_timer error: {e}")


# ── End game ─────────────────────────────────────────────────────────────────

async def end_game(bot, winner: str = None):
    await db.set_game_active(False)

    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]
    system_nodes = len([n for n in nodes_list if n["owner"] == "system"])
    opp_nodes = len([n for n in nodes_list if n["owner"] == "opposition"])
    total = len(nodes_list)

    all_ids = await db.get_all_identifications()
    # Unique agents — consistent with the server
    unique_agents = len(set(r["anonymous_id"] for r in all_ids if dict(r).get("anonymous_id")))

    all_verifs = await db.get_all_verifications()
    correct_verifs = len([v for v in all_verifs if v["correct"]])

    system_score = system_nodes * config.POINTS_PER_NODE + unique_agents * config.POINTS_PER_IDENTIFICATION + correct_verifs * 15
    opp_score = opp_nodes * config.POINTS_PER_NODE

    if winner == "opposition":
        winner_text = "🔴 Opposition won — the chain is complete!"
    elif winner == "system":
        winner_text = "⚙️ System won!"
    else:
        winner_text = "⚙️ System" if system_score >= opp_score else "🔴 Opposition"
        winner_text += " won on points"

    result_text = (
        f"🏁 *Game over!*\n\n{winner_text}\n\n"
        f"⚙️ System: {system_score} points\n"
        f"  Nodes: {system_nodes}/{total}\n"
        f"  Agents: {unique_agents}\n"
        f"  Verifications: {correct_verifs}\n\n"
        f"🔴 Opposition: {opp_score} points\n"
        f"  Nodes: {opp_nodes}/{total}"
    )

    all_players = await db.get_all_players()
    for p in all_players:
        try:
            await bot.send_message(p["telegram_id"], result_text, parse_mode="Markdown")
        except Exception:
            pass


# ── Startup ──────────────────────────────────────────────────────────────────

def start_schedulers(bot):
    loop = asyncio.get_event_loop()
    loop.create_task(check_captures(bot))
    loop.create_task(check_contested(bot))
    loop.create_task(check_victory(bot))
    loop.create_task(phase_timer(bot))