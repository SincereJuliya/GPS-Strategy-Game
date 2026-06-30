"""Single source of truth for team scoring.

Both the live ``/score`` command and the end-of-game finalisation call into
``compute_team_scores`` here so they can never report different numbers
for the same DB state.

Scoring rules:
  - Each captured non-anchor regular node = ``POINTS_PER_NODE`` (default 10)
    to its current owner. ALEX_NODE and BEATRICE_NODE are anchors — they
    start owned by Opposition by design and are never "earned" in
    gameplay, so they don't count toward node-based scoring. The finale
    hub is a meta-node (meeting place for the final scene), not a board
    piece, and is also excluded.
  - Each unique Opposition AGENT-ID the team has identified = ``POINTS_PER_AGENT``
    (default 15) to System. "Identified" means either:
      • the team physically tagged the player at a node (/defend writes to
        ``identifications``), or
      • the team correctly guessed the AGENT-ID via a QR scan
        (``verifications`` with ``correct=1``).
    Same data the ``/team_ids`` command displays.
  - Finale points (correct guesses, auto-id'd no-shows, opposition survival)
    are added on top by the caller — they only exist at end of game.
"""

import aiosqlite

import database as db


# Node names that mark the chain anchors. Any node whose name contains one
# of these substrings is treated as an anchor and excluded from node-based
# scoring. Match the same convention used by the admin map and chain
# detection logic.
_ANCHOR_KEYWORDS = ("ALEX", "BEATRICE")


def _is_anchor_or_meta(node: dict) -> bool:
    if node.get("node_type") == "finale":
        return True
    name = (node.get("name") or "").upper()
    return any(kw in name for kw in _ANCHOR_KEYWORDS)


async def compute_team_scores(points_per_node: int = 10,
                              points_per_agent: int = 15) -> dict:
    """Return the base (in-game) scoreboard. Caller adds finale points if
    relevant. ``unique_agents`` merges /defend identifications and correct
    QR-verifications so the same agent is never double-counted."""
    nodes = await db.get_all_nodes()
    nodes_list = [dict(n) for n in nodes]
    scorable = [n for n in nodes_list if not _is_anchor_or_meta(n)]
    sys_nodes = sum(1 for n in scorable if n["owner"] == "system")
    opp_nodes = sum(1 for n in scorable if n["owner"] == "opposition")

    async with aiosqlite.connect(db.DB_PATH) as conn:
        async with conn.execute(
            "SELECT anonymous_id FROM identifications WHERE anonymous_id IS NOT NULL"
        ) as cur:
            id_set = {r[0] for r in await cur.fetchall() if r[0]}
        async with conn.execute(
            "SELECT guessed_anonymous_id FROM verifications WHERE correct=1"
        ) as cur:
            verif_set = {r[0] for r in await cur.fetchall() if r[0]}
    unique_agents = len(id_set | verif_set)

    sys_base = sys_nodes * points_per_node + unique_agents * points_per_agent
    opp_base = opp_nodes * points_per_node

    return {
        "sys_nodes": sys_nodes,
        "opp_nodes": opp_nodes,
        "total_scorable": len(scorable),
        "unique_agents": unique_agents,
        "sys_base": sys_base,
        "opp_base": opp_base,
    }
