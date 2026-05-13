#!/usr/bin/env bash
# Launcher for the OPC-UA Device Simulator.
# Checks Python 3 and the asyncua dependency, installs if missing, then runs.
#
# Usage:  ./start.sh [-- any args forwarded to opcua_sim.py]
#         ./start.sh --no-menu
#         ./start.sh --no-menu --port 4841

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'

info()  { echo -e "${C_CYAN}[start.sh]${C_RESET} $*"; }
warn()  { echo -e "${C_YELLOW}[start.sh]${C_RESET} $*"; }
err()   { echo -e "${C_RED}[start.sh]${C_RESET} $*" >&2; }
ok()    { echo -e "${C_GREEN}[start.sh]${C_RESET} $*"; }

# 1. Ensure python3 is present
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is not installed. Install it first: sudo apt install python3 python3-pip"
    exit 1
fi
PY="$(command -v python3)"
ok "Using $($PY --version) at $PY"

# 2. Ensure pip is present (only needed if we have to install asyncua)
have_pip() { $PY -m pip --version >/dev/null 2>&1; }

# 3. Check asyncua; install if missing
need_install=0
if ! $PY -c "import asyncua" >/dev/null 2>&1; then
    need_install=1
    warn "asyncua not found — will install"
fi

if [[ $need_install -eq 1 ]]; then
    if ! have_pip; then
        err "pip3 not available. Install it first: sudo apt install python3-pip"
        exit 1
    fi
    REQ_FILE="$SCRIPT_DIR/requirements.txt"
    if [[ -f "$REQ_FILE" ]]; then
        info "Installing from requirements.txt…"
        # Try user install first; fall back to system install if that fails
        if ! $PY -m pip install --user -r "$REQ_FILE"; then
            warn "User install failed, retrying without --user"
            $PY -m pip install -r "$REQ_FILE"
        fi
    else
        info "Installing asyncua…"
        $PY -m pip install --user "asyncua>=1.1.0" || $PY -m pip install "asyncua>=1.1.0"
    fi
    ok "Dependencies installed"
fi

# 4. Launch
info "Launching simulator…"
exec $PY "$SCRIPT_DIR/opcua_sim.py" "$@"
