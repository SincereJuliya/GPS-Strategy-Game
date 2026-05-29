from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timedelta


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в метрах."""
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
    """Возвращает список нод в радиусе от игрока."""
    result = []
    for node in nodes:
        dist = haversine(player_lat, player_lon, node["lat"], node["lon"])
        if dist <= radius_m:
            result.append({"node": node, "distance_m": round(dist)})
    return result


def find_connected_nodes(nodes: list) -> list:
    """
    Пары opposition-нод, радиусы которых перекрываются.
    Возвращает [(node_id_a, node_id_b), ...]
    """
    opp_nodes = [n for n in nodes if n["owner"] == "opposition"]
    connections = []
    for i, a in enumerate(opp_nodes):
        for b in opp_nodes[i + 1:]:
            dist = haversine(a["lat"], a["lon"], b["lat"], b["lon"])
            if dist <= (a["current_radius_m"] + b["current_radius_m"]):
                connections.append((a["id"], b["id"]))
    return connections


def check_path_exists(node_a_id: int, node_b_id: int, connections: list) -> bool:
    """
    BFS — есть ли путь между двумя нодами через граф связей.
    Режим A: opposition должны соединить NODE ALEX и NODE BEATRICE.
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
    Fog of war для opposition:
    - Нода видима если она захвачена opposition (своя)
    - Нода видима если она попадает в радиус хотя бы одной opposition-ноды
    - Core-нода всегда видима (чтобы знали цель, но не могли взаимодействовать)

    Вызывается в FastAPI-эндпоинте /api/map?player_id=...
    Передавать:
        all_nodes  — все ноды из БД (list of dicts)
        opp_nodes — ноды с owner == 'opposition' (уже захваченные)

    Возвращает список видимых нод.
    """
    visible = set()

    # Своё всегда видно
    for n in opp_nodes:
        visible.add(n["id"])

    # Ноды в радиусе своих нод
    for own in opp_nodes:
        for other in all_nodes:
            if other["id"] in visible:
                continue
            dist = haversine(own["lat"], own["lon"], other["lat"], other["lon"])
            if dist <= own["current_radius_m"]:
                visible.add(other["id"])

    # Core всегда видна (exists to be seen, not taken)
    for n in all_nodes:
        if n.get("node_type") == "core":
            visible.add(n["id"])

    return [n for n in all_nodes if n["id"] in visible]


def is_location_fresh(last_location_at: str, fresh_sec: int = 600) -> bool:
    """Проверяет, свежая ли геолокация игрока (не старше fresh_sec секунд)."""
    if not last_location_at:
        return False
    try:
        ts = datetime.fromisoformat(last_location_at)
        return (datetime.now() - ts).total_seconds() <= fresh_sec
    except Exception:
        return False