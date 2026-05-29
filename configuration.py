# System Paths & Ports
SCREENPIPE_TOKEN = "sp-a7088672" #"sp-19a8515e"
SCREENPIPE_URL = "http://localhost:3030/search"
MLX_SERVER_URL = "http://localhost:8081/v1/chat/completions"
MODEL_ID = "mlx-community/MiniCPM-V-4.6-mxfp4"

# Logic Settings
CHECK_INTERVAL = 15  # Seconds between screen checks
CONTEXT_LIMIT = 10   # Number of recent frames to analyze
MAX_TOKENS = 100     # AI response length. Enough to finish 1–2 sentences cleanly;
                     # 60 was so tight the model got cut off mid-thought.

# Vision: send a screenshot to the (multimodal) model alongside OCR text.
# This is the biggest single lever on feedback quality — the model can see
# layout, focus, diagrams, and error highlights that OCR throws away.
SEND_SCREENSHOTS = True      # Set False to fall back to OCR-only (faster, lower quality)
SCREENSHOT_MAX_PX = 1400     # Cap the longest side before sending. Smaller = faster inference.

# Ambient chatter: when the screen is genuinely quiet, Casual only pipes up
# at most once per this many seconds. An ambient assistant that talks every
# tick becomes noise you learn to ignore — silence is a feature.
AMBIENT_QUIET_GAP_SEC = 1800  # 30 min. Lower for a chattier idle assistant; raise for near-silence.
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