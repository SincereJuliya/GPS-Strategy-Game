"""
Mines (Minesweeper) puzzle — 5×5 grid.
The player must correctly mark all mines with flags.

Solution logic: numbers in revealed cells show how many mines are in the 8 adjacent cells.
Based on the numbers, the mine locations can be deduced.
"""

import random


def generate() -> dict:
    """Generates a 5×5 field with 3 mines and 15 revealed cells (an easier puzzle)."""
    size = 5
    n_mines = 3
    # Place mines — NOT in the corners of the same row/column to provide more clues
    mine_positions = set()
    attempts = 0
    while len(mine_positions) < n_mines and attempts < 50:
        r = random.randint(0, size - 1)
        c = random.randint(0, size - 1)
        # Check that mines are not too close to each other (distance >= 2)
        too_close = any(abs(r - mr) <= 1 and abs(c - mc) <= 1
                        for (mr, mc) in mine_positions)
        if not too_close:
            mine_positions.add((r, c))
        attempts += 1
    # If placing with conditions fails — add randomly
    while len(mine_positions) < n_mines:
        r = random.randint(0, size - 1)
        c = random.randint(0, size - 1)
        mine_positions.add((r, c))

    # Calculate numbers for each cell
    numbers = [[0] * size for _ in range(size)]
    for r in range(size):
        for c in range(size):
            if (r, c) in mine_positions:
                numbers[r][c] = -1
                continue
            count = 0
            for dr in [-1, 0, 1]:
                for dc in [-1, 0, 1]:
                    if dr == 0 and dc == 0: continue
                    nr, nc = r + dr, c + dc
                    if 0 <= nr < size and 0 <= nc < size:
                        if (nr, nc) in mine_positions:
                            count += 1
            numbers[r][c] = count

    # Reveal 15 safe cells — more clues → easier to solve
    safe_cells = [(r, c) for r in range(size) for c in range(size)
                  if (r, c) not in mine_positions]
    random.shuffle(safe_cells)
    # Reveal cells adjacent to mines first (they show numbers > 0)
    # — because they are more useful for deduction
    cells_near_mines = [(r, c) for (r, c) in safe_cells if numbers[r][c] > 0]
    cells_far_from_mines = [(r, c) for (r, c) in safe_cells if numbers[r][c] == 0]
    revealed_count = min(15, len(safe_cells))
    revealed = set(cells_near_mines[:revealed_count])
    if len(revealed) < revealed_count:
        for cell in cells_far_from_mines:
            if len(revealed) >= revealed_count: break
            revealed.add(cell)

    visible_grid = []
    for r in range(size):
        row = []
        for c in range(size):
            if (r, c) in revealed:
                row.append(numbers[r][c])
            else:
                row.append(None)
        visible_grid.append(row)

    return {
        "puzzle_data": {
            "size": size,
            "grid": visible_grid,
            "n_mines": n_mines,
        },
        "solution": {
            "mines": [[r, c] for (r, c) in mine_positions],
        },
    }


def validate(puzzle_data: dict, user_solution) -> bool:
    """
    user_solution = {"flagged": [[r, c], ...]}
    Check that exactly those cells containing mines are marked (according to _correct_mines from puzzle_data).
    """
    if not isinstance(user_solution, dict): return False
    flagged = user_solution.get("flagged", [])
    if not isinstance(flagged, list): return False

    correct_mines = set()
    for m in puzzle_data.get("_correct_mines", []):
        correct_mines.add((m[0], m[1]))

    flagged_set = set()
    for f in flagged:
        if not isinstance(f, list) or len(f) != 2: return False
        flagged_set.add((int(f[0]), int(f[1])))

    return flagged_set == correct_mines
