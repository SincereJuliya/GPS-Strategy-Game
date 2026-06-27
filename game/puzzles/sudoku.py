"""
Sudoku 4×4 — упрощённый судоку: цифры 1-4 в сетке 4×4, разделённой на 4 региона 2×2.
В каждой строке, столбце и регионе каждая цифра встречается ровно один раз.
"""

import random


def _is_valid_sudoku(grid):
    """Проверяет полное решение судоку 4×4."""
    # Строки
    for row in grid:
        if sorted(row) != [1, 2, 3, 4]: return False
    # Столбцы
    for c in range(4):
        col = [grid[r][c] for r in range(4)]
        if sorted(col) != [1, 2, 3, 4]: return False
    # Регионы 2×2
    for box_r in [0, 2]:
        for box_c in [0, 2]:
            region = [grid[box_r + dr][box_c + dc] for dr in range(2) for dc in range(2)]
            if sorted(region) != [1, 2, 3, 4]: return False
    return True


def _generate_full_solution():
    """Генерирует случайное полное решение судоку 4×4."""
    # Базовый шаблон
    base = [
        [1, 2, 3, 4],
        [3, 4, 1, 2],
        [2, 1, 4, 3],
        [4, 3, 2, 1],
    ]
    # Перемешиваем цифры случайно
    perm = list(range(1, 5))
    random.shuffle(perm)
    grid = [[perm[c - 1] for c in row] for row in base]
    return grid


def generate() -> dict:
    """Генерирует судоку 4×4 с 6-8 открытыми клетками."""
    full = _generate_full_solution()
    n_clues = random.randint(6, 8)

    # Выбираем случайные клетки которые остаются открытыми
    cells = [(r, c) for r in range(4) for c in range(4)]
    random.shuffle(cells)
    visible_cells = set(cells[:n_clues])

    puzzle_grid = []
    for r in range(4):
        row = []
        for c in range(4):
            if (r, c) in visible_cells:
                row.append(full[r][c])
            else:
                row.append(0)  # пустая клетка
        puzzle_grid.append(row)

    return {
        "puzzle_data": {
            "size": 4,
            "grid": puzzle_grid,  # 0 = пусто, число = подсказка
        },
        "solution": {
            "grid": full,
        },
    }


def validate(puzzle_data: dict, user_solution) -> bool:
    """
    user_solution = {"grid": [[1,2,3,4],...]} — полная решённая сетка.
    Проверяем что соответствует подсказкам и образует валидное судоку.
    """
    if not isinstance(user_solution, dict): return False
    user_grid = user_solution.get("grid", [])
    if not isinstance(user_grid, list) or len(user_grid) != 4: return False
    for row in user_grid:
        if not isinstance(row, list) or len(row) != 4: return False
        for v in row:
            if not isinstance(v, int) or v < 1 or v > 4: return False

    # Проверяем подсказки
    original = puzzle_data["grid"]
    for r in range(4):
        for c in range(4):
            if original[r][c] != 0 and original[r][c] != user_grid[r][c]:
                return False

    return _is_valid_sudoku(user_grid)