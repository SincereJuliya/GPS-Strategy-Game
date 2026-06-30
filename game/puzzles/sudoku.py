"""
Sudoku 4×4 — a simplified sudoku: digits 1–4 in a 4×4 grid divided into four 2×2 regions.
Each digit appears exactly once in every row, column, and region.
"""

import random


def _is_valid_sudoku(grid):
    """Validates a complete 4×4 sudoku solution."""
    # Rows
    for row in grid:
        if sorted(row) != [1, 2, 3, 4]: return False
    # Columns
    for c in range(4):
        col = [grid[r][c] for r in range(4)]
        if sorted(col) != [1, 2, 3, 4]: return False
    # 2×2 regions
    for box_r in [0, 2]:
        for box_c in [0, 2]:
            region = [grid[box_r + dr][box_c + dc] for dr in range(2) for dc in range(2)]
            if sorted(region) != [1, 2, 3, 4]: return False
    return True


def _generate_full_solution():
    """Generates a random complete 4×4 sudoku solution."""
    # Base template
    base = [
        [1, 2, 3, 4],
        [3, 4, 1, 2],
        [2, 1, 4, 3],
        [4, 3, 2, 1],
    ]
    # Shuffle digits randomly
    perm = list(range(1, 5))
    random.shuffle(perm)
    grid = [[perm[c - 1] for c in row] for row in base]
    return grid


def generate() -> dict:
    """Generates a 4×4 sudoku with 6–8 opened cells."""
    full = _generate_full_solution()
    n_clues = random.randint(6, 8)

    # Pick random cells that stay opened
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
                row.append(0)  # empty cell
        puzzle_grid.append(row)

    return {
        "puzzle_data": {
            "size": 4,
            "grid": puzzle_grid,  # 0 = empty, digit = hint
        },
        "solution": {
            "grid": full,
        },
    }


def validate(puzzle_data: dict, user_solution) -> bool:
    """
    user_solution = {"grid": [[1,2,3,4],...]} — the fully solved grid.
    Check that it matches the hints and forms a valid sudoku.
    """
    if not isinstance(user_solution, dict): return False
    user_grid = user_solution.get("grid", [])
    if not isinstance(user_grid, list) or len(user_grid) != 4: return False
    for row in user_grid:
        if not isinstance(row, list) or len(row) != 4: return False
        for v in row:
            if not isinstance(v, int) or v < 1 or v > 4: return False

    # Check hints
    original = puzzle_data["grid"]
    for r in range(4):
        for c in range(4):
            if original[r][c] != 0 and original[r][c] != user_grid[r][c]:
                return False

    return _is_valid_sudoku(user_grid)