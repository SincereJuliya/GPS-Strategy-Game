"""
Magnets puzzle (simplified version) — 4×4 grid divided into domino pairs.
Each domino contains a magnet (+/-) or is empty.
Rules:
- + and + cannot be neighbors
- - and - cannot be neighbors
- Each row/column has a specified count of + and - required.

Simplified implementation: we provide a pre-made solution and a few clues,
the player must fill the rest to satisfy the rules.
"""

import random


def _check_neighbours(grid, size):
    """Check that identical poles are not adjacent."""
    for r in range(size):
        for c in range(size):
            if grid[r][c] == "0": continue
            # Check right and bottom neighbors
            if c + 1 < size and grid[r][c+1] == grid[r][c]:
                return False
            if r + 1 < size and grid[r+1][c] == grid[r][c]:
                return False
    return True


def _check_counts(grid, size, row_plus, row_minus, col_plus, col_minus):
    """Check that the counts of + and - match the specified totals."""
    for r in range(size):
        p = sum(1 for c in range(size) if grid[r][c] == "+")
        m = sum(1 for c in range(size) if grid[r][c] == "-")
        if p != row_plus[r] or m != row_minus[r]: return False
    for c in range(size):
        p = sum(1 for r in range(size) if grid[r][c] == "+")
        m = sum(1 for r in range(size) if grid[r][c] == "-")
        if p != col_plus[c] or m != col_minus[c]: return False
    return True


def generate() -> dict:
    """Generates a simplified 4×4 magnets puzzle."""
    size = 4
    # Predefined valid solution (created manually)
    # This is a valid configuration with no adjacent identical poles
    solutions = [
        [
            ["+", "-", "+", "-"],
            ["-", "+", "-", "+"],
            ["+", "-", "+", "-"],
            ["-", "+", "-", "+"],
        ],
        [
            ["+", "-", "0", "+"],
            ["-", "+", "0", "-"],
            ["0", "0", "+", "0"],
            ["+", "-", "-", "+"],
        ],
        [
            ["-", "+", "-", "+"],
            ["+", "-", "+", "-"],
            ["-", "+", "0", "0"],
            ["+", "-", "+", "-"],
        ],
    ]
    solution = random.choice(solutions)

    # Calculate clues — the number of + and - in each row/column
    row_plus = [sum(1 for c in range(size) if solution[r][c] == "+") for r in range(size)]
    row_minus = [sum(1 for c in range(size) if solution[r][c] == "-") for r in range(size)]
    col_plus = [sum(1 for r in range(size) if solution[r][c] == "+") for c in range(size)]
    col_minus = [sum(1 for r in range(size) if solution[r][c] == "-") for c in range(size)]

    # Reveal 2-3 random clues on the board
    cells = [(r, c) for r in range(size) for c in range(size)]
    random.shuffle(cells)
    n_clues = random.randint(2, 3)
    visible = set(cells[:n_clues])

    puzzle_grid = []
    for r in range(size):
        row = []
        for c in range(size):
            if (r, c) in visible:
                row.append(solution[r][c])
            else:
                row.append("?")
        puzzle_grid.append(row)

    return {
        "puzzle_data": {
            "size": size,
            "grid": puzzle_grid,  # "?" = empty, "+"/"-"/"0" = clue
            "row_plus": row_plus,
            "row_minus": row_minus,
            "col_plus": col_plus,
            "col_minus": col_minus,
        },
        "solution": {
            "grid": solution,
        },
    }


def validate(puzzle_data: dict, user_solution) -> bool:
    """
    user_solution = {"grid": [[..]]} — complete grid with "+", "-", "0".
    """
    if not isinstance(user_solution, dict): return False
    user_grid = user_solution.get("grid", [])
    size = puzzle_data["size"]
    if len(user_grid) != size: return False
    for row in user_grid:
        if len(row) != size: return False
        for v in row:
            if v not in ("+", "-", "0"): return False

    # Clues must match
    original = puzzle_data["grid"]
    for r in range(size):
        for c in range(size):
            if original[r][c] != "?" and original[r][c] != user_grid[r][c]:
                return False

    # Neighbors cannot have identical poles
    if not _check_neighbours(user_grid, size): return False

    # Counts must match
    if not _check_counts(user_grid, size,
                          puzzle_data["row_plus"], puzzle_data["row_minus"],
                          puzzle_data["col_plus"], puzzle_data["col_minus"]):
        return False

    return True