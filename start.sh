#!/usr/bin/env bash
# Launcher for the OPC-UA Device Simulator.
# Ensures Python 3 + asyncua are available (creating a project-local venv if
# the system Python is PEP 668-protected) and then launches opcua_sim.py.
#
# Usage:  ./start.sh [-- any args forwarded to opcua_sim.py]
#         ./start.sh --no-menu
#         ./start.sh --no-menu --port 4841

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_CYAN=$'\033[36m'

info()  { echo -e "${C_CYAN}[start.sh]${C_RESET} $*"; }
warn()  { echo -e "${C_YELLOW}[start.sh]${C_RESET} $*"; }
err()   { echo -e "${C_RED}[start.sh]${C_RESET} $*" >&2; }
ok()    { echo -e "${C_GREEN}[start.sh]${C_RESET} $*"; }

# 1. Ensure python3 is present
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is not installed. Install it: sudo apt install python3 python3-venv"
    exit 1
fi
SYS_PY="$(command -v python3)"

# 2. Pick a python: prefer the project venv, else system python.
if [[ -x "$VENV_DIR/bin/python" ]]; then
    PY="$VENV_DIR/bin/python"
else
    PY="$SYS_PY"
fi
ok "Using $($PY --version) at $PY"

# 3. asyncua check; if missing, ensure venv and install into it.
if ! $PY -c "import asyncua" >/dev/null 2>&1; then
    warn "asyncua not found — preparing environment"

    # If we're not already in the venv, create or move to it.
    if [[ "$PY" != "$VENV_DIR/bin/python" ]]; then
        if [[ ! -x "$VENV_DIR/bin/python" ]]; then
            info "Creating virtual environment at $VENV_DIR…"
            if ! $SYS_PY -m venv "$VENV_DIR" 2>/tmp/venv_err; then
                err "Failed to create venv:"
                cat /tmp/venv_err >&2 || true
                err "If the error mentions 'ensurepip', install: sudo apt install python3-venv python3-full"
                exit 1
            fi
        fi
        PY="$VENV_DIR/bin/python"
        ok "Using venv python: $PY"
    fi

    # Upgrade pip inside the venv (silent), then install deps.
    $PY -m pip install --quiet --upgrade pip || true
    if [[ -f "$REQ_FILE" ]]; then
        info "Installing from requirements.txt into venv…"
        $PY -m pip install -r "$REQ_FILE"
    else
        info "Installing asyncua into venv…"
        $PY -m pip install "asyncua>=1.1.0"
    fi
    ok "Dependencies installed"
fi

# 4. Launch (exec replaces this shell so signals go straight to Python).
info "Launching simulator…"
exec $PY "$SCRIPT_DIR/opcua_sim.py" "$@"
