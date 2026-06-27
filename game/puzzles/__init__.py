"""
Puzzle module for GPS Strategy.

Each puzzle type provides:
    generate() -> dict — generates puzzle and solution
    validate(puzzle_data, user_solution) -> bool — validates solution

Used when capturing a node: player solves the puzzle, server validates.
"""

from game.puzzles import untangle, mines, sudoku, magnets

PUZZLE_TYPES = {
    "untangle": untangle,
    "mines": mines,
    "sudoku": sudoku,
    "magnets": magnets,
}


def generate(puzzle_type: str) -> dict:
    """Returns a dict containing fields: puzzle_data (for rendering), solution (for validation)."""
    if puzzle_type not in PUZZLE_TYPES:
        raise ValueError(f"Unknown puzzle type: {puzzle_type}")
    return PUZZLE_TYPES[puzzle_type].generate()


def validate(puzzle_type: str, puzzle_data: dict, user_solution) -> bool:
    """Returns True if the user's solution is correct."""
    if puzzle_type not in PUZZLE_TYPES:
        return False
    return PUZZLE_TYPES[puzzle_type].validate(puzzle_data, user_solution)