#!/bin/bash
# TheSeer launcher — boots the notification server, screenpipe, and MLX,
# then runs theSeer.py in the foreground.
#
# First-time setup? Run ./setup.sh first.
# Optional flag: --test_mode  (bypasses the MLX server)

set -u
cd "$(dirname "$0")"

# --- Pretty colours ---
GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo -e "${BLUE}🚀 Starting TheSeer Ecosystem...${NC}"

# --- Cleanup handler (defined first so Ctrl+C always works) ---
cleanup() {
    echo -e "\n${BLUE}🛑 Shutting down background services...${NC}"
    for pid_var in NOTIFY_PID PIPE_PID MLX_PID; do
        pid="${!pid_var:-}"
        [ -n "$pid" ] && kill "$pid" 2>/dev/null
    done
    exit 0
}
trap cleanup INT TERM

# --- Helpers ---
wait_for_port() {
    local port=$1 name=$2 timeout=$3
    echo -e "${BLUE}⏳ Waiting for $name to spin up on port $port...${NC}"
    for ((i=1; i<=timeout; i++)); do
        if python3 -c "import socket; s=socket.socket(); s.settimeout(0.5); s.connect(('localhost', $port))" 2>/dev/null; then
            echo -e "${GREEN}✅ $name is responsive on port $port.${NC}"
            return 0
        fi
        sleep 1
    done
    echo -e "${RED}❌ $name failed to respond on port $port within ${timeout}s.${NC}"
    return 1
}

# --- Pre-flight: refuse to start if another TheSeer is already running ---
check_lock() {
    local lock=$1 name=$2
    [ -f "$lock" ] || return 0
    local existing
    existing=$(cat "$lock" 2>/dev/null)
    if [ -n "$existing" ] && kill -0 "$existing" 2>/dev/null; then
        echo -e "${RED}❌ $name is already running (PID $existing).${NC}"
        echo -e "${YELLOW}   Stop it first:  kill $existing${NC}"
        return 1
    fi
    return 0
}
check_lock /tmp/theseer_notify_server.pid "Notification server" || exit 1
check_lock /tmp/theseer_engine.pid        "Logic engine"        || exit 1

# --- Pre-flight: locate screenpipe (PATH first, then the npx cache) ---
# Avoids the redundant `npx ... @latest` registry check that can race
# and fail with ENOTEMPTY on parallel invocations.
if command -v screenpipe >/dev/null 2>&1; then
    SCREENPIPE_BIN="$(command -v screenpipe)"
else
    SCREENPIPE_BIN="$(find "$HOME/.npm/_npx" -path '*/node_modules/.bin/screenpipe' 2>/dev/null | head -1)"
fi
if [ -z "${SCREENPIPE_BIN:-}" ] || [ ! -x "$SCREENPIPE_BIN" ]; then
    echo -e "${RED}❌ screenpipe not found.${NC}"
    echo -e "${YELLOW}   Run ./setup.sh to install it.${NC}"
    exit 1
fi

# --- 1. Notification Server (menu-bar 👁) ---
echo -e "${GREEN}0/3 Starting TheSeer Notification Server...${NC}"
python3 ./notify_server.py &
NOTIFY_PID=$!
sleep 1   # let the rumps app initialise before we send the startup ping

# --- 2. Screenpipe ---
echo -e "${GREEN}1/3 Starting Screenpipe Engine...${NC}"
"$SCREENPIPE_BIN" record > screenpipe.log 2>&1 &
PIPE_PID=$!

# --- 3. MLX Server (the brain) — skip in test mode ---
if [ "${1:-}" = "--test_mode" ]; then
    echo -e "${YELLOW}2/3 Skipping MLX server (test mode).${NC}"
    MLX_PID=""
else
    echo -e "${GREEN}2/3 Loading MiniCPM-V 4.6 into RAM (Port 8081)...${NC}"
    python3 -m mlx_vlm.server --model mlx-community/MiniCPM-V-4.6-mxfp4 --port 8081 > mlx_server.log 2>&1 &
    MLX_PID=$!
fi

# --- 4. Liveness check on background processes ---
sleep 2
if ! ps -p $PIPE_PID > /dev/null; then
    echo -e "${RED}❌ Screenpipe crashed immediately. Check screenpipe.log${NC}"
    cleanup
fi
if [ -n "$MLX_PID" ] && ! ps -p $MLX_PID > /dev/null; then
    echo -e "${RED}❌ MLX Server crashed immediately. Check mlx_server.log${NC}"
    cleanup
fi

# --- 5. Port readiness ---
wait_for_port 3030 "Screenpipe" 15 || cleanup
if [ -n "$MLX_PID" ]; then
    wait_for_port 8081 "MLX Server" 45 || cleanup
fi

# --- 6. Logic engine ---
echo -e "${GREEN}3/3 Launching TheSeer Logic Engine...${NC}"
[ "${1:-}" = "--test_mode" ] && \
    echo -e "${BLUE}⚗️  TEST MODE — MLX server bypassed, using simple classifier${NC}"
echo -e "${BLUE}------------------------------------------------------------${NC}"

python3 ./theSeer.py "$@"

cleanup
