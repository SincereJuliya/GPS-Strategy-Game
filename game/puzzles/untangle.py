"""
Untangle puzzle.
"""

import random
import math


def _segments_intersect_by_id(edge1, edge2, pos_map):
    """Do two edges intersect. Shared nodes (by ID) are NOT counted as an intersection."""
    # If the edges share a node (by ID) — not an intersection
    if set(edge1) & set(edge2):
        return False

    p1 = pos_map[edge1[0]]; p2 = pos_map[edge1[1]]
    p3 = pos_map[edge2[0]]; p4 = pos_map[edge2[1]]

    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])

    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)


def generate() -> dict:
    """
    Generates a GUARANTEED planar graph (cycle + optional chord).
    Then shuffles positions — the player must untangle it back.
    """
    n = random.randint(5, 7)

    # First — a simple cycle (guaranteed planar)
    edges = set()
    for i in range(n):
        edges.add((i, (i + 1) % n))

    # Add 1 safe chord — between non-adjacent nodes of the cycle
    if n >= 5:
        a = 0
        b = n // 2  # opposite node
        edge = (min(a, b), max(a, b))
        edges.add(edge)

    # Shuffle positions
    shuffled = []
    for _ in range(n):
        shuffled.append((
            random.uniform(60, 340),
            random.uniform(60, 340),
        ))

    return {
        "puzzle_data": {
            "nodes": [{"id": i, "x": shuffled[i][0], "y": shuffled[i][1]} for i in range(n)],
            "edges": [list(e) for e in edges],
        },
        "solution": None,
    }


def validate(puzzle_data: dict, user_solution) -> bool:
    """
    user_solution = {"positions": [{"id": 0, "x": ..., "y": ...}, ...]}
    Check: no intersections between edges with no shared nodes.
    """
    if not isinstance(user_solution, dict): return False
    positions = user_solution.get("positions", [])
    if len(positions) != len(puzzle_data["nodes"]):
        return False

    pos_map = {}
    for p in positions:
        node_id = p.get("id")
        if node_id is None: return False
        try:
            pos_map[int(node_id)] = (float(p["x"]), float(p["y"]))
        except (KeyError, ValueError, TypeError):
            return False

    edges = puzzle_data["edges"]
    for i, e1 in enumerate(edges):
        e1_tuple = (int(e1[0]), int(e1[1]))
        for e2 in edges[i+1:]:
            e2_tuple = (int(e2[0]), int(e2[1]))
            if _segments_intersect_by_id(e1_tuple, e2_tuple, pos_map):
                return False
    return True