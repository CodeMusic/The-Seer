#!/bin/bash
# TheSeer setup — installs Python deps and primes the screenpipe cache.
# Re-running is safe.

set -euo pipefail
cd "$(dirname "$0")"

GREEN='\033[0;32m'; RED='\033[0;31m'; BLUE='\033[0;34m'; YELLOW='\033[0;33m'; NC='\033[0m'

echo -e "${BLUE}🛠  TheSeer setup${NC}"

[ "$(uname)" = "Darwin" ] || { echo -e "${RED}macOS only.${NC}"; exit 1; }
command -v python3 >/dev/null || { echo -e "${RED}python3 not found.${NC}";        exit 1; }
command -v npx     >/dev/null || { echo -e "${RED}Node.js / npx not found. Install Node first.${NC}"; exit 1; }

# --- 1. Python deps --------------------------------------------------
echo -e "${GREEN}1/2 Installing Python dependencies...${NC}"
python3 -m pip install --quiet --upgrade -r requirements.txt

# --- 2. Screenpipe cache --------------------------------------------
echo -e "${GREEN}2/2 Prefetching screenpipe@0.3.345...${NC}"
if command -v screenpipe >/dev/null 2>&1; then
    echo -e "   ${GREEN}✓${NC} screenpipe already on PATH: $(command -v screenpipe)"
else
    npx --prefer-offline -y screenpipe@0.3.345 --version >/dev/null 2>&1 \
        || npx -y screenpipe@0.3.345 --version >/dev/null 2>&1 \
        || true
    if find "$HOME/.npm/_npx" -path '*/node_modules/.bin/screenpipe' 2>/dev/null | grep -q .; then
        echo -e "   ${GREEN}✓${NC} screenpipe cached"
    else
        echo -e "   ${RED}✗${NC} screenpipe install failed — check npm output"
        exit 1
    fi
fi

# Warn if the screenpipe token hasn't been set
if grep -q '^SCREENPIPE_TOKEN = "sp-XXXX' configuration.py 2>/dev/null; then
    echo -e "${YELLOW}⚠  Set SCREENPIPE_TOKEN in configuration.py.${NC}"
    echo -e "${YELLOW}   Get one with: npx screenpipe@0.3.345 auth token${NC}"
fi

cat <<EOF

${GREEN}✅ Setup complete.${NC}

Launch:
   ./start_theSeer.sh              # full stack (uses MLX)
   ./start_theSeer.sh --test_mode  # smoke test (no MLX needed)

EOF
