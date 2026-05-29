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
echo -e "${GREEN}1/3 Installing Python dependencies...${NC}"
python3 -m pip install --quiet --upgrade -r requirements.txt --break-system-packages

# --- 2. Screenpipe cache --------------------------------------------
echo -e "${GREEN}2/3 Prefetching screenpipe@0.3.345...${NC}"
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

# --- 3. Screenpipe token --------------------------------------------
echo -e "${GREEN}3/3 Checking screenpipe token...${NC}"
if grep -q '^SCREENPIPE_TOKEN = "sp-XXXX' configuration.py 2>/dev/null; then
    echo -e "   ${YELLOW}No token set — running: npx screenpipe@0.3.345 auth token${NC}"
    TOKEN_OUTPUT=$(npx -y screenpipe@0.3.345 auth token 2>&1)
    NEW_TOKEN=$(echo "$TOKEN_OUTPUT" | grep -oE 'sp-[a-f0-9]+' | head -1)
    if [ -n "$NEW_TOKEN" ]; then
        sed -i '' "s/SCREENPIPE_TOKEN = \"[^\"]*\"/SCREENPIPE_TOKEN = \"$NEW_TOKEN\"/" configuration.py
        echo -e "   ${GREEN}✓${NC} Token set: $NEW_TOKEN"
    else
        echo -e "   ${RED}✗${NC} Could not extract token from output:"
        echo "$TOKEN_OUTPUT"
        echo -e "${YELLOW}   Set it manually: npx screenpipe@0.3.345 auth token${NC}"
        echo -e "${YELLOW}   Then update SCREENPIPE_TOKEN in configuration.py${NC}"
    fi
else
    echo -e "   ${GREEN}✓${NC} Token already set"
fi

cat <<EOF

${GREEN}✅ Setup complete.${NC}

Launch:
   ./start_theSeer.sh              # full stack (uses MLX)
   ./start_theSeer.sh --test_mode  # smoke test (no MLX needed)

EOF
