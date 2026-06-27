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
 
RADIUS_MAX_M        = 50     # Maximum size to which the radius can grow
                              # for Opposition nodes via grow_radii
 
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
 
POINTS_PER_IDENTIFICATION = 5  # System points for each unique AGENT-ID in logs
                               # (only unique ones — duplicates do not count)
 
# Bonus +15 for each correct QR verification — hardcoded in system.py
# (no points are given for incorrect verification)