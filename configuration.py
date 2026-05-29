# System Paths & Ports
SCREENPIPE_TOKEN = "sp-a7088672" #"sp-19a8515e"
SCREENPIPE_URL = "http://localhost:3030/search"
MLX_SERVER_URL = "http://localhost:8081/v1/chat/completions"
MODEL_ID = "mlx-community/MiniCPM-V-4.6-mxfp4"

# Logic Settings
CHECK_INTERVAL = 15  # Seconds between screen checks
CONTEXT_LIMIT = 10   # Number of recent frames to analyze
MAX_TOKENS = 60      # AI response length
REVERT_THRESHOLD = 2         # Consecutive clean ticks required before leaving auditor mode
NOTIFICATION_THRESHOLD = 0.6 # Minimum score (0.0–1.0) required to show an alert
SPAWN_EXPANDED = True       # Auto-expand new toasters that overflow; Cmd+0 replays always spawn collapsed
RECENCY_WINDOW_SEC = 20      # Only consider screenpipe OCR records newer than this many seconds.
                             # Should be ≥ CHECK_INTERVAL with a small buffer so an idle screen
                             # doesn't blank out every tick. Lower it if "phantom old window"
                             # notifications come back; raise it if you see lots of "no OCR" skips.

# One-shot persona cooldowns (seconds between firings).
# Long enough that the request text usually scrolls off-screen before the
# cooldown expires, so the same trigger doesn't keep re-firing.
ENTERTAINER_COOLDOWN_SEC = 300
TEACHER_COOLDOWN_SEC     = 300
MOTIVATOR_COOLDOWN_SEC   = 300
SASSY_COOLDOWN_SEC       = 300