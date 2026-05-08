# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-file OPC-UA server simulator (`opcua_sim.py`) with an interactive text menu for configuring up to 12 device nodes (mixed or same type). Supports two network modes: all devices on one endpoint or each on its own IP from a range. Config is saved to `opcua_sim_config.json` and reloaded on start. Only external dependency is `asyncua>=1.1.0`.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Install as editable package (provides 'opcua-sim' console command)
pip install -e .

# Run simulator (interactive menu)
python opcua_sim.py

# Skip menu, use saved config or defaults
python opcua_sim.py --no-menu
python opcua_sim.py --no-menu --host 127.0.0.1 --port 4841 --interval 0.5

# Docker
docker build -t opcua-sim .
docker run -p 4840:4840 opcua-sim
```

No test suite exists — testing is done by connecting OPC-UA clients (UA Expert, Prosys, python-asyncua, Ignition, Node-RED).

## Architecture

Everything lives in `opcua_sim.py`. The server runs at `opc.tcp://HOST:PORT/opcua/sim` with a single custom namespace (NS index 2).

**Entry flow:** `main()` → `argparse` → `show_menu()` (loads saved config or prompts user) → `asyncio.run(run_from_config())` → dispatches to `run_single_server()` (same IP) or `run_ip_range_server()` (one server per IP). Each mode builds device trees from the config and enters an infinite update loop. Config is persisted to `opcua_sim_config.json`.

**Config file:** `opcua_sim_config.json` — stores device list (type + count), network mode, host/IP range, port, and update interval. Loaded automatically on start; user can re-run saved config or create a new one via the menu.

**Three device classes, identical pattern:**
- `Opto22GroovDevice` — CODESYS 3.5 SoftPLC hierarchy with GVL variables, PLC_PRG, FB_PID, dual-scaled analog I/O
- `SiemensS71200Device` — TIA Portal style with DB blocks, timers (T1/T2), counters (C1/C2), IW/QW analog words (0–27648 range)
- `UnitronicsDevice` — UniStream operand model with MI/ML/MB registers, 12-bit AI (0–4095), system bits

Each class has `async build(server, ns, root)` to create the OPC-UA node tree and `async update()` to simulate values each cycle.

**Simulation patterns:**
- Sine waves with Gaussian noise for analog inputs
- Coupled process loops (tank level ↔ pump/valve/flow)
- PID controller that reads writable setpoint nodes
- Timer countdown and counter increment state machines
- Random digital input toggling and alarm generation

**Writable nodes** allow clients to set values (setpoints, presets, outputs) that the simulator reads and acts on each update cycle via `node.read_value()`.

**Helper functions:** `add_var()`, `add_folder()`, `add_object()` — thin wrappers for OPC-UA node creation.

## Key Design Decisions

- Single-file by design for deployment simplicity — resist splitting unless there's a strong reason
- All devices share one namespace but have isolated object hierarchies under the root
- Vendor-specific naming conventions are intentional (Hungarian prefixes for CODESYS, DB notation for Siemens, operand notation for Unitronics)
- State is ephemeral — no persistence across restarts
- Anonymous/no-security policy — intended for lab/VPN-isolated networks
- Update loop catches and logs exceptions without crashing the server
