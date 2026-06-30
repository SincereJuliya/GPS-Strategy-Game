"""
Puzzle module for GPS Strategy.

Each puzzle type provides:
    generate() -> dict — generates a puzzle and its solution
    validate(puzzle_data, user_solution) -> bool — validates a solution

Used during node capture: the player solves the puzzle, the server validates it.
"""

from game.puzzles import untangle, mines, sudoku, magnets

PUZZLE_TYPES = {
    "untangle": untangle,
    "mines": mines,
    "sudoku": sudoku,
    "magnets": magnets,
}


def generate(puzzle_type: str) -> dict:
    """Returns a dict with fields: puzzle_data (for display), solution (for validation)."""
    if puzzle_type not in PUZZLE_TYPES:
        raise ValueError(f"Unknown puzzle type: {puzzle_type}")
    return PUZZLE_TYPES[puzzle_type].generate()


def validate(puzzle_type: str, puzzle_data: dict, user_solution) -> bool:
    """Returns True if the user solution is correct."""
    if puzzle_type not in PUZZLE_TYPES:
        return False
    return PUZZLE_TYPES[puzzle_type].validate(puzzle_data, user_solution)