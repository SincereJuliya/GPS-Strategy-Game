import os

# ─────────────────────────────────────────────────────────────────────────────
# SECRETS — must be replaced
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = ""                             # ur bot token from @BotFather
ADMIN_ID  = 111111111                     # ur telegram_id (check via @userinfobot)


# SERVER_URL is updated automatically when bot.py starts via cloudflared.
# Can be left empty — bot.py will insert the current actual URL here.
SERVER_URL = ""

# ─────────────────────────────────────────────────────────────────────────────
# RADII AND DISTANCES
# ─────────────────────────────────────────────────────────────────────────────
#
# IMPORTANT: all interaction logic now goes through THE RADIUS OF THE NODE ITSELF
# (current_radius_m, set during creation via /admin_map).
#
# Want to capture a node → stand INSIDE its circle on the map
# Want to defend a node → stand INSIDE its circle on the map
# There are no separate "interaction zones" anymore.
 
MIN_NODE_RADIUS_M   = 5       # Minimum node radius during creation in /admin_map
                              # (was hardcoded to 20m, now configurable)
 
MAX_NODE_RADIUS_M   = 1000    # Maximum allowed radius during node creation
 
RADIUS_MAX_M        = 200    # Maximum size to which a captured node's radius
                              # can grow (via grow_radii ticks AND puzzle bonuses).
                              # Keep this above your largest base node radius —
                              # otherwise captured nodes would appear to shrink.
                              # For a small map, 120–150 is fine; default 200.
 
RADIUS_GROWTH_STEP_M = 10     # How many meters the radius grows per interval
 
RADIUS_GROWTH_INTERVAL_SEC = 10  # How often (in seconds) the scheduler checks for growth
                                 # (for a real game 30 is better — less server load)
 
# These parameters ARE NO LONGER USED (kept for backward compatibility)
CAPTURE_RADIUS_M    = 50      # DEPRECATED — not used
NODE_SCAN_RADIUS_M  = 100     # DEPRECATED — not used
 
 
# ─────────────────────────────────────────────────────────────────────────────
# TIMERS — short values set for testing purposes
# ─────────────────────────────────────────────────────────────────────────────
 
CAPTURE_TIME_SEC = 90         # How many seconds to hold position at a node to capture it
                              # TEST: 90 (1.5 min)
                              # REAL GAME: 180 (3 min)
 
ABANDON_TIMEOUT_SEC = 30      # Seconds before a capture resets if everyone leaves
                              # TEST: 30 sec
                              # REAL GAME: 180 sec (3 min)
 
LOCATION_FRESH_SEC = 30       # Geolocation older than this is considered outdated
                              # TEST: 30
                              # REAL GAME: 90
 
 
# ─────────────────────────────────────────────────────────────────────────────
# GAME PHASES
# ─────────────────────────────────────────────────────────────────────────────
 
PHASE_COUNT = 3                # Total number of phases in a single match
 
PHASE_DURATION_SEC = 900       # Duration of a single phase in seconds
                               # 900 = 15 minutes — good for a long game
                               # 300 = 5 minutes — for a quick test
 
 
# ─────────────────────────────────────────────────────────────────────────────
# POINTS
# ─────────────────────────────────────────────────────────────────────────────
 
POINTS_PER_NODE = 10           # Points for each controlled node
                               # (given to both teams for their respective nodes)
 
POINTS_PER_IDENTIFICATION = 5  # System points for each unique AGENT-ID
                               # identified mid-game (via /defend or a correct
                               # QR scan). Small reward — the real prize for
                               # identification is the +30 finale guess. This
                               # keeps mid-game activity worth something
                               # without dwarfing other lines of play.

# ─────────────────────────────────────────────────────────────────────────────
# FINALE SCORING
# ─────────────────────────────────────────────────────────────────────────────
# Philosophy: the finale is a multiplier, not the main reward. The two main
# prizes are SWEEP (System identified everyone) and CHAIN (Opposition closed
# the route). Individual finale guesses are smaller, and wrong guesses cost
# points so System can't just spam every AGENT-ID hoping to hit.

FINAL_CORRECT_POINTS              = 15  # System per correct finale guess
FINAL_WRONG_PENALTY               = 10  # System loses this per wrong finale guess
FINAL_AUTO_ID_POINTS              = 10  # System per auto-identified no-show
FINAL_OPPOSITION_SURVIVAL_POINTS  = 20  # Opposition per surviving anonymity

# Main win-condition bonuses — symmetric, one per team.
FINAL_SWEEP_BONUS         = 50  # System if EVERY Opposition is identified
                                # (any channel: defend, QR, finale, no-show)
OPPOSITION_CHAIN_BONUS    = 50  # Opposition when ALEX↔BEATRICE chain closes
                                # (their main win condition)


# ─────────────────────────────────────────────────────────────────────────────
# FINALE (rendezvous + identification)
# ─────────────────────────────────────────────────────────────────────────────
#
# When Opposition completes the ALEX ↔ BEATRICE chain the game does NOT end.
# Instead it enters a two-stage final scene:
#   1. RENDEZVOUS — every player walks to the finale hub (a special node the
#      admin places via /admin_map with type='finale', hidden until this stage
#      starts). Opposition players who fail to arrive in the circle are
#      automatically identified.
#   2. IDENTIFICATION — System collectively maps remaining AGENT-IDs to faces
#      on the /finale screen and submits one final guess as a team.

RENDEZVOUS_PHASE_SEC      = 300   # Time given to walk to the finale hub (5 min default)
IDENTIFICATION_PHASE_SEC  = 300   # Time given for the System team to submit the final mapping (5 min default)
RENDEZVOUS_RADIUS_M       = 50    # Minimum radius of the rendezvous circle (overrides node radius if smaller)

# Per-action point values are defined in the FINALE SCORING block above.
