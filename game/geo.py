from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timedelta


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points in meters."""
    R = 6371000
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def is_in_radius(player_lat: float, player_lon: float,
                 target_lat: float, target_lon: float,
                 radius_m: float) -> bool:
    return haversine(player_lat, player_lon, target_lat, target_lon) <= radius_m


def find_nodes_in_radius(player_lat: float, player_lon: float,
                         nodes: list, radius_m: float) -> list:
    """Returns nodes within the player's radius."""
    result = []
    for node in nodes:
        dist = haversine(player_lat, player_lon, node["lat"], node["lon"])
        if dist <= radius_m:
            result.append({"node": node, "distance_m": round(dist)})
    return result


def find_nodes_containing_player(player_lat: float, player_lon: float,
                                 nodes: list, tolerance_m: float = 10) -> list:
    """
    Returns nodes whose radius contains the player.
    Radius is taken from the node itself (current_radius_m).
    tolerance_m compensates for GPS inaccuracy.
    """
    result = []
    for node in nodes:
        dist = haversine(player_lat, player_lon, node["lat"], node["lon"])
        node_radius = node.get("current_radius_m") or node.get("base_radius_m") or 20
        if dist <= node_radius + tolerance_m:
            result.append({"node": node, "distance_m": round(dist)})
    return result


def _is_target_node(node) -> bool:
    """Identifies anchor nodes (NODE ALEX / NODE BEATRICE) by name."""
    name = (node.get("name") or "").upper()
    return "ALEX" in name or "BEATRICE" in name


def find_connected_nodes(nodes: list) -> list:
    """
    Pairs of opposition nodes connected by a mesh link.

    Connection logic:
    - regular ↔ regular: mutual coverage (each center inside the other's radius)
    - regular ↔ target (ALEX/BEATRICE): one-way — regular only needs
      to cover the target center (target is an anchor, does not grow)
    - target ↔ target: mutual coverage (in case ALEX and BEATRICE are close)

    Returns [(node_id_a, node_id_b), ...]
    """
    opp_nodes = [n for n in nodes if n["owner"] == "opposition"]
    connections = []
    for i, a in enumerate(opp_nodes):
        for b in opp_nodes[i + 1:]:
            dist = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            a_is_target = _is_target_node(a)
            b_is_target = _is_target_node(b)

            if a_is_target and b_is_target:
                # Both anchors — mutual coverage (rare case)
                connected = dist <= a["current_radius_m"] and dist <= b["current_radius_m"]
            elif a_is_target:
                # a is an anchor. b only needs to cover a's center
                connected = dist <= b["current_radius_m"]
            elif b_is_target:
                # b is an anchor. a only needs to cover b's center
                connected = dist <= a["current_radius_m"]
            else:
                # Both regular nodes — mutual coverage
                connected = dist <= a["current_radius_m"] and dist <= b["current_radius_m"]

            if connected:
                connections.append((a["id"], b["id"]))
    return connections


def check_path_exists(node_a_id: int, node_b_id: int, connections: list) -> bool:
    """
    BFS — checks whether a path exists between two nodes in the graph.
    Mode A: opposition must connect NODE ALEX and NODE BEATRICE.
    """
    if not connections:
        return False

    graph = {}
    for a, b in connections:
        graph.setdefault(a, set()).add(b)
        graph.setdefault(b, set()).add(a)

    visited = set()
    queue = [node_a_id]
    while queue:
        current = queue.pop(0)
        if current == node_b_id:
            return True
        if current in visited:
            continue
        visited.add(current)
        queue.extend(graph.get(current, []))
    return False


def get_visible_nodes_for_opposition(all_nodes: list, opp_nodes: list) -> list:
    """
    Fog of war for opposition:
    - A node is visible if captured by opposition
    - A node is visible if it is within the radius of any opposition node
    - Core nodes are always visible (to know the objective, but not interact)

    Called from the FastAPI endpoint /api/map?player_id=...

    Pass:
        all_nodes  — all nodes from the DB (list of dicts)
        opp_nodes — nodes with owner == 'opposition' (already captured)

    Returns a list of visible nodes.
    """
    visible = set()

    # Own nodes are always visible
    for n in opp_nodes:
        visible.add(n["id"])

    # Nodes inside the radius of owned nodes
    for own in opp_nodes:
        for other in all_nodes:
            if other["id"] in visible:
                continue
            dist = haversine(own["lat"], own["lon"], other["lat"], other["lon"])
            if dist <= own["current_radius_m"]:
                visible.add(other["id"])

    # Core is always visible (exists to be seen, not taken)
    for n in all_nodes:
        if n.get("node_type") == "core":
            visible.add(n["id"])

    return [n for n in all_nodes if n["id"] in visible]


def is_location_fresh(last_location_at: str, fresh_sec: int = 600) -> bool:
    """Checks whether the player's location is fresh (not older than fresh_sec seconds)."""
    if not last_location_at:
        return False
    try:
        ts = datetime.fromisoformat(last_location_at)
        return (datetime.now() - ts).total_seconds() <= fresh_sec
    except Exception:
        return False