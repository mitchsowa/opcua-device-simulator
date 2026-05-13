#!/usr/bin/env python3
"""
OPC-UA Multi-Device Simulator
Simulates OPC-UA server implementations for:
  - Opto22 groov RIO
  - Siemens S7-1200
  - Unitronics UniStream / Vision PLC

Requires: asyncua (pip install asyncua)
Usage:    python3 opcua_sim.py [--port 4840] [--host 0.0.0.0]
"""

import asyncio
import argparse
import ipaddress
import json
import math
import os
import random
import subprocess
import time
import logging
from datetime import datetime, timezone
from pathlib import Path

from asyncua import Server, ua
from asyncua.common.node import Node

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).resolve().parent / "opcua_sim_config.json"

# Runtime status file — server writes connected-client snapshots here so the
# menu can show them without IPC. /tmp keeps it world-accessible.
STATUS_FILE = Path("/tmp/opcua_sim_status.json")

# Systemd service constants
SERVICE_NAME      = "opcua-sim.service"
SERVICE_UNIT_PATH = Path("/etc/systemd/system") / SERVICE_NAME
SERVICE_USER      = "opcua"
SERVICE_INSTALL_DIR = Path("/opt/opcua-sim")
FIREWALL_COMMENT  = "opcua-sim"

DEVICE_TYPES = {
    "1": ("opto22",     "Opto22 groov RIO (CODESYS 3.5)"),
    "2": ("siemens",    "Siemens S7-1200"),
    "3": ("unitronics", "Unitronics UniStream/Vision PLC"),
}

DEVICE_CLASSES = {
    "opto22":     lambda: Opto22GroovDevice,
    "siemens":    lambda: SiemensS71200Device,
    "unitronics": lambda: UnitronicsDevice,
}


def load_config():
    """Load config from disk, return dict or None if not found."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"Failed to load config: {exc}")
    return None


def save_config(cfg: dict):
    """Persist config to disk."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    log.info(f"Config saved to {CONFIG_PATH}")


def _get_default_interface():
    """Return the name of the default network interface (prefer one with an IPv4 address)."""
    ifaces = _list_interfaces()
    # Prefer an interface that already has an IP
    for name, addr in ifaces:
        if addr:
            return name
    # Fall back to first non-loopback interface
    if ifaces:
        return ifaces[0][0]
    return None


def _list_interfaces():
    """Return list of (iface_name, ipv4_addr_or_None) for non-loopback interfaces."""
    # Collect IPv4 addresses keyed by interface name
    ip_map = {}
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            iface = parts[1]
            addr = parts[3].split("/")[0]
            if iface != "lo":
                ip_map[iface] = addr
    except (subprocess.CalledProcessError, IndexError):
        pass

    # List all non-loopback interfaces (including those without an IPv4 address)
    ifaces = []
    try:
        result = subprocess.run(
            ["ip", "-o", "link", "show"],
            capture_output=True, text=True, check=True,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            # Format: "N: iface_name: ..."  — strip trailing colon
            iface = parts[1].rstrip(":")
            if iface != "lo":
                ifaces.append((iface, ip_map.get(iface)))
    except (subprocess.CalledProcessError, IndexError):
        pass
    return ifaces


# ──────────────────────────────────────────────────────────────────────────────
# ANSI Color helpers
# ──────────────────────────────────────────────────────────────────────────────
class _C:
    """ANSI color codes for the TUI."""
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    # Foreground
    RED     = "\033[31m"
    GREEN   = "\033[32m"
    YELLOW  = "\033[33m"
    BLUE    = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN    = "\033[36m"
    WHITE   = "\033[37m"
    # Bright
    BRED    = "\033[91m"
    BGREEN  = "\033[92m"
    BYELLOW = "\033[93m"
    BCYAN   = "\033[96m"
    BWHITE  = "\033[97m"

    @staticmethod
    def supported():
        """Check if the terminal likely supports color."""
        return os.environ.get("TERM", "") != "dumb" and hasattr(os, "isatty") and os.isatty(1)

# Disable colors if terminal doesn't support them
if not _C.supported():
    for attr in dir(_C):
        if attr.isupper() and not attr.startswith("_"):
            setattr(_C, attr, "")


def _clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def _get_interface_ip(iface_name):
    """Return the IPv4 address for a given interface name, or None."""
    ifaces = _list_interfaces()
    for name, addr in ifaces:
        if name == iface_name:
            return addr
    return None


def _pick_interface():
    """Show interface picker, return (iface_name, iface_ip_or_None)."""
    ifaces = _list_interfaces()
    default_iface = _get_default_interface()

    if ifaces:
        print(f"\n  {_C.CYAN}{_C.BOLD}Available interfaces:{_C.RESET}")
        for i, (name, addr) in enumerate(ifaces, 1):
            marker = f" {_C.BYELLOW}← default{_C.RESET}" if name == default_iface else ""
            if addr:
                print(f"    {_C.BWHITE}{i}.{_C.RESET} {_C.GREEN}{name}{_C.RESET} ({_C.BCYAN}{addr}{_C.RESET}){marker}")
            else:
                print(f"    {_C.BWHITE}{i}.{_C.RESET} {_C.GREEN}{name}{_C.RESET} {_C.DIM}(no IP){_C.RESET}{marker}")
        print()

    while True:
        prompt = f"  {_C.BWHITE}Interface"
        if default_iface:
            prompt += f" [{_C.GREEN}{default_iface}{_C.BWHITE}]"
        prompt += f": {_C.RESET}"
        iface_input = input(prompt).strip()

        if not iface_input and default_iface:
            return default_iface, _get_interface_ip(default_iface)
        elif iface_input.isdigit() and ifaces:
            idx = int(iface_input) - 1
            if 0 <= idx < len(ifaces):
                name = ifaces[idx][0]
                return name, ifaces[idx][1]
        elif iface_input:
            return iface_input, _get_interface_ip(iface_input)
        print(f"  {_C.RED}Enter an interface name or number.{_C.RESET}")


def _device_summary(devices):
    """Return a human-readable summary of device list."""
    counts = {}
    for d in devices:
        counts[d["type"]] = counts.get(d["type"], 0) + 1
    type_names = {tid: desc for _, (tid, desc) in DEVICE_TYPES.items()}
    parts = [f"{v}x {type_names.get(k, k)}" for k, v in counts.items()]
    return f"{len(devices)} total — {', '.join(parts)}"


def _print_banner():
    _clear_screen()
    print(f"\n  {_C.BCYAN}{_C.BOLD}╔══════════════════════════════════════════════════════╗{_C.RESET}")
    print(f"  {_C.BCYAN}{_C.BOLD}║{_C.RESET}     {_C.BWHITE}{_C.BOLD}OPC-UA Device Simulator — Configuration{_C.RESET}     {_C.BCYAN}{_C.BOLD}║{_C.RESET}")
    print(f"  {_C.BCYAN}{_C.BOLD}╚══════════════════════════════════════════════════════╝{_C.RESET}")


def _print_menu(cfg):
    """Print the main persistent menu with current config values."""
    devices = cfg.get("devices", [{"type": "siemens"}])
    mode = cfg.get("mode", "same_ip")
    host = cfg.get("host", "0.0.0.0")
    start_ip = cfg.get("start_ip", host)
    port = cfg.get("port", 4840)
    interval = cfg.get("interval", 1.0)
    interface = cfg.get("interface")

    dev_str = _device_summary(devices)
    mode_str = "Same IP (one endpoint)" if mode == "same_ip" else "IP Range (one server per IP)"

    if mode == "same_ip":
        host_str = f"{host}:{port}"
        if interface:
            host_str += f"  (iface: {interface})"
    else:
        end_ip = str(ipaddress.ip_address(int(ipaddress.ip_address(start_ip)) + len(devices) - 1))
        host_str = f"{start_ip} → {end_ip}  port {port}"
        if interface:
            host_str += f"  (iface: {interface})"

    print(f"\n  {_C.DIM}{'─' * 54}{_C.RESET}")
    print(f"  {_C.BYELLOW}{_C.BOLD}1.{_C.RESET} {_C.BWHITE}Device selection{_C.RESET}   {_C.GREEN}{dev_str}{_C.RESET}")
    print(f"  {_C.BYELLOW}{_C.BOLD}2.{_C.RESET} {_C.BWHITE}Network mode{_C.RESET}      {_C.GREEN}{mode_str}{_C.RESET}")
    print(f"  {_C.BYELLOW}{_C.BOLD}3.{_C.RESET} {_C.BWHITE}Host / IP range{_C.RESET}   {_C.GREEN}{host_str}{_C.RESET}")
    print(f"  {_C.BYELLOW}{_C.BOLD}4.{_C.RESET} {_C.BWHITE}Port{_C.RESET}              {_C.GREEN}{port}{_C.RESET}")
    print(f"  {_C.BYELLOW}{_C.BOLD}5.{_C.RESET} {_C.BWHITE}Update interval{_C.RESET}   {_C.GREEN}{interval}s{_C.RESET}")
    print(f"  {_C.DIM}{'─' * 54}{_C.RESET}")
    print(f"  {_C.BCYAN}{_C.BOLD}S.{_C.RESET} {_C.BWHITE}Service{_C.RESET}           {_C.DIM}install / remove / start / stop / restart{_C.RESET}")
    print(f"  {_C.BCYAN}{_C.BOLD}F.{_C.RESET} {_C.BWHITE}Firewall (ufw){_C.RESET}    {_C.DIM}open / close port {port}/tcp{_C.RESET}")
    print(f"  {_C.BCYAN}{_C.BOLD}C.{_C.RESET} {_C.BWHITE}Connected clients{_C.RESET} {_C.DIM}show who's connected to each PLC{_C.RESET}")
    print(f"  {_C.DIM}{'─' * 54}{_C.RESET}")
    print(f"  {_C.BGREEN}{_C.BOLD}R.{_C.RESET} {_C.BGREEN}Run{_C.RESET}               {_C.BRED}{_C.BOLD}Q.{_C.RESET} {_C.BRED}Quit{_C.RESET}")
    print(f"  {_C.DIM}{'─' * 54}{_C.RESET}")


def _edit_devices(cfg):
    """Edit device selection in-place."""
    print(f"\n  {_C.CYAN}{_C.BOLD}── Device Selection ──{_C.RESET}")
    print(f"    {_C.BWHITE}1.{_C.RESET} All same type   — one device type, choose how many")
    print(f"    {_C.BWHITE}2.{_C.RESET} Mixed types     — pick count for each type")
    while True:
        choice = input(f"  {_C.BWHITE}Select option (1/2) [{_C.GREEN}1{_C.BWHITE}]: {_C.RESET}").strip()
        if choice in ("1", "2", ""):
            break
        print(f"  {_C.RED}Enter 1 or 2.{_C.RESET}")

    if choice == "2":
        cfg["devices"] = _input_devices_mixed()
    else:
        cfg["devices"] = _input_devices_same()


def _input_devices_same():
    """All-same-type flow."""
    print(f"\n  {_C.CYAN}{_C.BOLD}── Device Type ──{_C.RESET}")
    for key, (_, desc) in DEVICE_TYPES.items():
        print(f"    {_C.BWHITE}{key}.{_C.RESET} {desc}")
    while True:
        type_choice = input(f"  {_C.BWHITE}Select type (1/2/3) [{_C.GREEN}2{_C.BWHITE}]: {_C.RESET}").strip()
        if not type_choice:
            type_choice = "2"
        if type_choice in DEVICE_TYPES:
            break
        print(f"  {_C.RED}Enter 1, 2, or 3.{_C.RESET}")

    type_id, desc = DEVICE_TYPES[type_choice]
    total = _input_total_plcs()
    devices = [{"type": type_id} for _ in range(total)]
    print(f"\n  {_C.GREEN}✓ Selected: {total}x {desc}{_C.RESET}")
    return devices


def _input_devices_mixed():
    """Mixed-type flow."""
    total = _input_total_plcs()

    print(f"\n  {_C.CYAN}{_C.BOLD}── Allocate by Type ──{_C.RESET}")
    for key, (_, desc) in DEVICE_TYPES.items():
        print(f"    {_C.BWHITE}{key}.{_C.RESET} {desc}")

    devices = []
    remaining = total
    type_keys = list(DEVICE_TYPES.keys())

    for i, key in enumerate(type_keys):
        type_id, desc = DEVICE_TYPES[key]
        is_last = (i == len(type_keys) - 1)

        if remaining <= 0:
            break

        if is_last:
            count = remaining
            print(f"\n  {desc}: {_C.BYELLOW}{count}{_C.RESET} (remaining)")
        else:
            while True:
                count_str = input(f"\n  {_C.BWHITE}How many {desc}? (0-{remaining}) [{_C.GREEN}0{_C.BWHITE}]: {_C.RESET}").strip()
                if not count_str:
                    count = 0
                    break
                try:
                    count = int(count_str)
                    if 0 <= count <= remaining:
                        break
                    print(f"  {_C.RED}Must be between 0 and {remaining}.{_C.RESET}")
                except ValueError:
                    print(f"  {_C.RED}Enter a number.{_C.RESET}")

        for _ in range(count):
            devices.append({"type": type_id})
        remaining -= count

    print(f"\n  {_C.GREEN}✓ Selected: {_device_summary(devices)}{_C.RESET}")
    return devices


def _input_total_plcs():
    """Prompt for total PLC count (1-12)."""
    while True:
        total_str = input(f"  {_C.BWHITE}Total number of PLCs (1-12) [{_C.GREEN}1{_C.BWHITE}]: {_C.RESET}").strip()
        if not total_str:
            return 1
        try:
            total = int(total_str)
            if 1 <= total <= 12:
                return total
            print(f"  {_C.RED}Must be between 1 and 12.{_C.RESET}")
        except ValueError:
            print(f"  {_C.RED}Enter a number.{_C.RESET}")


def _edit_network_mode(cfg):
    """Edit network mode in-place."""
    print(f"\n  {_C.CYAN}{_C.BOLD}── Network Mode ──{_C.RESET}")
    print(f"    {_C.BWHITE}1.{_C.RESET} Same IP     — all devices on one server endpoint")
    print(f"    {_C.BWHITE}2.{_C.RESET} IP Range    — each device on its own IP address")
    while True:
        choice = input(f"  {_C.BWHITE}Select mode (1/2): {_C.RESET}").strip()
        if choice in ("1", "2"):
            break
        print(f"  {_C.RED}Enter 1 or 2.{_C.RESET}")

    new_mode = "ip_range" if choice == "2" else "same_ip"
    old_mode = cfg.get("mode")
    cfg["mode"] = new_mode

    # When switching modes, reconfigure host/IP to match
    if new_mode != old_mode:
        _edit_host_ip(cfg)


def _edit_host_ip(cfg):
    """Edit host / IP range in-place. Both modes pick an interface first."""
    mode = cfg.get("mode", "same_ip")

    if mode == "ip_range":
        print(f"\n  {_C.CYAN}{_C.BOLD}── IP Range Configuration ──{_C.RESET}")
        print(f"  {_C.DIM}Each device gets its own IP. Virtual IPs are added to the interface.{_C.RESET}\n")

        iface, iface_ip = _pick_interface()
        cfg["interface"] = iface

        num_devices = len(cfg.get("devices", [{"type": "siemens"}]))
        default_ip = iface_ip or ""
        end_note = f" (need {num_devices} IPs)" if num_devices > 1 else ""

        while True:
            prompt = f"  {_C.BWHITE}Start IP address{end_note}"
            if default_ip:
                prompt += f" [{_C.GREEN}{default_ip}{_C.BWHITE}]"
            prompt += f": {_C.RESET}"
            start_ip = input(prompt).strip()
            if not start_ip and default_ip:
                start_ip = default_ip
            try:
                addr = ipaddress.ip_address(start_ip)
                if addr.is_loopback:
                    print(f"  {_C.RED}Loopback addresses not supported. Use a real subnet.{_C.RESET}")
                    continue
                end_addr = ipaddress.ip_address(int(addr) + num_devices - 1)
                if int(end_addr) > int(ipaddress.ip_address("255.255.255.254")):
                    print(f"  {_C.RED}IP range {start_ip} → {end_addr} is not valid.{_C.RESET}")
                    continue
                break
            except ValueError:
                print(f"  {_C.RED}Invalid IP address.{_C.RESET}")

        cfg["start_ip"] = start_ip
        cfg["host"] = "0.0.0.0"
        end_ip = str(ipaddress.ip_address(int(addr) + num_devices - 1))
        print(f"\n  {_C.GREEN}✓ Will bind {num_devices} IPs: {start_ip} → {end_ip} on {iface}{_C.RESET}")
    else:
        print(f"\n  {_C.CYAN}{_C.BOLD}── Bind Host ──{_C.RESET}")
        print(f"  {_C.DIM}Select which interface to bind to.{_C.RESET}")

        iface, iface_ip = _pick_interface()
        cfg["interface"] = iface

        default_host = iface_ip or "0.0.0.0"
        host = input(f"  {_C.BWHITE}Bind host [{_C.GREEN}{default_host}{_C.BWHITE}]: {_C.RESET}").strip()
        if not host:
            host = default_host
        cfg["host"] = host
        cfg["start_ip"] = host
        print(f"\n  {_C.GREEN}✓ Host set to {host} (iface: {iface}){_C.RESET}")


def _edit_port(cfg):
    """Edit port in-place."""
    print(f"\n  {_C.CYAN}{_C.BOLD}── Port ──{_C.RESET}")
    current = cfg.get("port", 4840)
    port_str = input(f"  {_C.BWHITE}OPC-UA port [{_C.GREEN}{current}{_C.BWHITE}]: {_C.RESET}").strip()
    if port_str.isdigit():
        cfg["port"] = int(port_str)
    print(f"  {_C.GREEN}✓ Port: {cfg['port']}{_C.RESET}")


def _edit_interval(cfg):
    """Edit update interval in-place."""
    print(f"\n  {_C.CYAN}{_C.BOLD}── Update Interval ──{_C.RESET}")
    current = cfg.get("interval", 1.0)
    interval_str = input(f"  {_C.BWHITE}Interval in seconds [{_C.GREEN}{current}{_C.BWHITE}]: {_C.RESET}").strip()
    try:
        if interval_str:
            cfg["interval"] = float(interval_str)
    except ValueError:
        pass
    print(f"  {_C.GREEN}✓ Interval: {cfg['interval']}s{_C.RESET}")


def show_menu():
    """Interactive TUI menu. Returns a config dict or None to quit."""
    # Start with saved config or sensible defaults
    existing = load_config()
    if existing:
        cfg = existing
    else:
        # Default: detect interface IP for host
        default_iface = _get_default_interface()
        default_ip = _get_interface_ip(default_iface) if default_iface else "0.0.0.0"
        cfg = {
            "devices": [{"type": "siemens"}],
            "mode": "same_ip",
            "host": default_ip or "0.0.0.0",
            "start_ip": default_ip or "0.0.0.0",
            "port": 4840,
            "interval": 1.0,
        }
        if default_iface:
            cfg["interface"] = default_iface

    while True:
        _print_banner()
        _print_menu(cfg)

        choice = input(f"\n  {_C.BWHITE}{_C.BOLD}Select option: {_C.RESET}").strip().upper()

        if choice == "1":
            _edit_devices(cfg)
        elif choice == "2":
            _edit_network_mode(cfg)
        elif choice == "3":
            _edit_host_ip(cfg)
        elif choice == "4":
            _edit_port(cfg)
        elif choice == "5":
            _edit_interval(cfg)
        elif choice == "S":
            _service_menu(cfg)
        elif choice == "F":
            _firewall_menu(cfg)
        elif choice == "C":
            _show_connected_clients()
        elif choice == "R":
            save_config(cfg)
            return cfg
        elif choice == "Q":
            return None
        else:
            print(f"  {_C.RED}Invalid choice. Enter 1-5, S, F, C, R, or Q.{_C.RESET}")
            input(f"  {_C.DIM}Press Enter to continue…{_C.RESET}")
            continue

        if choice in ("1", "2", "3", "4", "5", "C"):
            input(f"\n  {_C.DIM}Press Enter to return to menu…{_C.RESET}")

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("opcua_sim")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

async def add_var(parent: Node, ns: int, name: str, value, writable: bool = False) -> Node:
    """Add a variable node with display name and optional write access."""
    node = await parent.add_variable(ns, name, value)
    if writable:
        await node.set_writable()
    return node


async def add_folder(parent: Node, ns: int, name: str) -> Node:
    return await parent.add_folder(ns, name)


async def add_object(parent: Node, ns: int, name: str) -> Node:
    return await parent.add_object(ns, name)


# ──────────────────────────────────────────────────────────────────────────────
# Kiln Controller Tag Table (shared across all device types)
# ──────────────────────────────────────────────────────────────────────────────

class KilnTags:
    """
    Kiln controller tag table — adds a KilnController folder with groups for
    unit identification, setpoints, temperatures, commands, status, schedule,
    VFD/HRV arrays, totals, power/energy, demand response, and misc tags.
    """

    def __init__(self):
        self._tick = 0
        self._start_time = time.time()

    async def build(self, parent_node: Node, ns: int):
        kiln = await add_folder(parent_node, ns, "KilnController")

        # ── Unit Identification (static) ─────────────────────────────────────
        uid = await add_folder(kiln, ns, "UnitIdentification")
        await add_var(uid, ns, "TableVersion", 1)
        await add_var(uid, ns, "SerialNumber", "SN-2024-00451")
        await add_var(uid, ns, "PartNumber", "KD-4000-HT")
        await add_var(uid, ns, "UnitType", 1)
        await add_var(uid, ns, "UnitId", "KILN-001")
        await add_var(uid, ns, "ControlVersion", 3.50)
        await add_var(uid, ns, "HmiVersion", 2.10)
        await add_var(uid, ns, "CompileDate", 20240115)
        await add_var(uid, ns, "ManufactureDate", "2024-01-15")
        await add_var(uid, ns, "Customer", "Nyle Engineering")
        await add_var(uid, ns, "SiteAddress", "12 Stevens Rd, Brewer, ME 04412")
        await add_var(uid, ns, "GPSCoordinates", "44.78686002624483, -68.76185949532186")

        # ── Setpoints (writable) ─────────────────────────────────────────────
        sp = await add_folder(kiln, ns, "Setpoints")
        self.sp_dry_bulb = await add_var(sp, ns, "DryBulbSetpoint", 160.0, writable=True)
        self.sp_wet_bulb = await add_var(sp, ns, "WetBulbSetpoint", 140.0, writable=True)
        self.sp_emc = await add_var(sp, ns, "EmcSetpoint", 12.0, writable=True)
        self.sp_rh = await add_var(sp, ns, "RhSetpoint", 65.0, writable=True)
        self.sp_dry_bulb_db = await add_var(sp, ns, "DryBulbDeadband", 2.0, writable=True)
        self.sp_wet_bulb_db = await add_var(sp, ns, "WetBulbDeadband", 2.0, writable=True)
        self.sp_mc_db = await add_var(sp, ns, "McDeadband", 1.0, writable=True)
        self.sp_rh_db = await add_var(sp, ns, "RhDeadband", 3.0, writable=True)
        self.sp_suction_line = await add_var(sp, ns, "SuctionLineSetpoint", 38.0, writable=True)
        self.sp_operating_mode = await add_var(sp, ns, "OperatingMode", 0, writable=True)
        self.sp_spray_mode = await add_var(sp, ns, "SprayMode", 0, writable=True)
        self.sp_heat_mode = await add_var(sp, ns, "HeatMode", 1, writable=True)
        self.sp_refrig_mode = await add_var(sp, ns, "RefrigMode", 0, writable=True)
        self.sp_vent_mode = await add_var(sp, ns, "VentMode", 1, writable=True)
        self.sp_hrv_mode = await add_var(sp, ns, "HrvMode", 0, writable=True)
        self.sp_manual_vent_pct = await add_var(sp, ns, "ManualVentPercent", 0, writable=True)
        self.sp_vent_implosion = await add_var(sp, ns, "VentImplosionSecs", 5, writable=True)
        self.sp_fan_manual_speed = await add_var(sp, ns, "FanManualSpeed", 0, writable=True)
        self.sp_fan_mode = await add_var(sp, ns, "FanMode", 1, writable=True)
        self.sp_hrv_speed = await add_var(sp, ns, "HrvSpeed", 0, writable=True)
        self.sp_hrv_diff = await add_var(sp, ns, "HrvDiff", 5.0, writable=True)
        self.sp_vent_diff = await add_var(sp, ns, "VentDiff", 3.0, writable=True)
        self.sp_gas_enable = await add_var(sp, ns, "GasEnable", False, writable=True)
        self.sp_lot_id = await add_var(sp, ns, "LotId", "LOT-2024-0001", writable=True)

        # ── Temperatures & Sensors (read-only, simulated) ────────────────────
        ts = await add_folder(kiln, ns, "TemperaturesSensors")
        self.ctrl_dry_bulb = await add_var(ts, ns, "CtrlDryBulb", 158.5)
        self.ctrl_wet_bulb = await add_var(ts, ns, "CtrlWetBulb", 138.2)
        self.dlp = await add_var(ts, ns, "Dlp", 142.0)
        self.fwd_dry_bulb = await add_var(ts, ns, "FwdDryBulb", 162.0)
        self.fwd_wet_bulb = await add_var(ts, ns, "FwdWetBulb", 141.5)
        self.mc_ctrl = await add_var(ts, ns, "McCtrl", 28.0)
        self.mc_fwd = await add_var(ts, ns, "McFwd", 30.0)
        self.mc_rev = await add_var(ts, ns, "McRev", 26.0)
        self.rev_dry_bulb = await add_var(ts, ns, "RevDryBulb", 155.0)
        self.rev_wet_bulb = await add_var(ts, ns, "RevWetBulb", 136.8)
        self.rh_ctrl = await add_var(ts, ns, "RhCtrl", 64.0)
        self.rh_fwd = await add_var(ts, ns, "RhFwd", 66.0)
        self.rh_rev = await add_var(ts, ns, "RhRev", 62.0)
        self.slp = await add_var(ts, ns, "Slp", 36.5)
        self.slt = await add_var(ts, ns, "Slt", 37.2)
        self.temp1 = await add_var(ts, ns, "Temp1", 157.0)
        self.temp2 = await add_var(ts, ns, "Temp2", 159.0)
        self.temp3 = await add_var(ts, ns, "Temp3", 156.5)
        self.temp4 = await add_var(ts, ns, "Temp4", 160.2)
        self.temp5 = await add_var(ts, ns, "Temp5", 158.8)

        # ── Commands (writable) ──────────────────────────────────────────────
        cmd = await add_folder(kiln, ns, "Commands")
        self.cmd_dr_enable = await add_var(cmd, ns, "DrEnable", False, writable=True)
        self.cmd_dr_mode = await add_var(cmd, ns, "DrMode", 0, writable=True)
        self.cmd_pause = await add_var(cmd, ns, "Pause", False, writable=True)
        self.cmd_skip_back = await add_var(cmd, ns, "ScheduleSkipBackward", False, writable=True)
        self.cmd_skip_fwd = await add_var(cmd, ns, "ScheduleSkipForward", False, writable=True)
        self.cmd_start = await add_var(cmd, ns, "Start", False, writable=True)
        self.cmd_stop = await add_var(cmd, ns, "Stop", False, writable=True)

        # ── Status & Display (read-only, simulated) ──────────────────────────
        st = await add_folder(kiln, ns, "StatusDisplay")
        self.alarm_active1 = await add_var(st, ns, "AlarmActive1", 0)
        self.alarm_active2 = await add_var(st, ns, "AlarmActive2", 0)
        self.alarm_active3 = await add_var(st, ns, "AlarmActive3", 0)
        self.blower_status = await add_var(st, ns, "BlowerStatus", 1)
        self.comp_status1 = await add_var(st, ns, "CompStatus1", 0)
        self.current_step = await add_var(st, ns, "CurrentStep", 3)
        self.cycle_status = await add_var(st, ns, "CycleStatus", 1)
        self.damper_pct = await add_var(st, ns, "DamperPct", 45)
        self.fan_status = await add_var(st, ns, "FanStatus", 1)
        self.heat_status = await add_var(st, ns, "HeatStatus", 1)
        self.light_stack = await add_var(st, ns, "LightStack", 2)
        self.light_stack_status = await add_var(st, ns, "LightStackStatus", "Green")
        self.spray_status = await add_var(st, ns, "SprayStatus", 0)
        self.vent_status = await add_var(st, ns, "VentStatus", 1)

        # ── Schedule (mixed) ─────────────────────────────────────────────────
        sched = await add_folder(kiln, ns, "Schedule")
        self.sched_enabled = await add_var(sched, ns, "ScheduleEnabled", True, writable=True)
        self.recipe_completion = await add_var(sched, ns, "RecipeCompletionTime", "2024-12-15T18:00:00Z")
        self.sched_finished = await add_var(sched, ns, "ScheduleFinished", False)
        self.sched_stop = await add_var(sched, ns, "ScheduleStop", False, writable=True)
        self.sched_pause = await add_var(sched, ns, "SchedulePause", False, writable=True)
        self.step_elapsed_time = await add_var(sched, ns, "StepElapsedTime", 0)
        self.step_remaining_time = await add_var(sched, ns, "StepRemainingTime", 300)

        # ── Schedule Arrays (ARRAY[0..40], 41 elements each, writable) ───────
        sa = await add_folder(kiln, ns, "ScheduleArrays")

        def _pad_bool(first5): return first5 + [False] * 36
        def _pad_int(first5): return first5 + [0] * 36
        def _pad_float(first5): return first5 + [0.0] * 36
        def _pad_str(first5): return first5 + [""] * 36

        await add_var(sa, ns, "DcCtrl", _pad_bool([False, False, True, True, False]), writable=True)
        await add_var(sa, ns, "DcHeatTime", _pad_int([0, 0, 1800, 1800, 0]), writable=True)
        await add_var(sa, ns, "DcRepeat", _pad_int([0, 0, 3, 2, 0]), writable=True)
        await add_var(sa, ns, "DcRestFanOff", _pad_bool([False, False, True, False, False]), writable=True)
        await add_var(sa, ns, "DcRestTime", _pad_int([0, 0, 600, 600, 0]), writable=True)
        await add_var(sa, ns, "DcVentTime", _pad_int([0, 0, 300, 300, 0]), writable=True)
        await add_var(sa, ns, "EstCompTime", _pad_str(["2024-12-15T08:00Z", "2024-12-15T10:00Z", "2024-12-15T13:00Z", "2024-12-15T16:00Z", "2024-12-15T18:00Z"]), writable=True)
        await add_var(sa, ns, "EstStartTime", _pad_str(["2024-12-15T06:00Z", "2024-12-15T08:00Z", "2024-12-15T10:00Z", "2024-12-15T13:00Z", "2024-12-15T16:00Z"]), writable=True)
        await add_var(sa, ns, "HeaterCutOutSet", _pad_float([180.0, 185.0, 190.0, 185.0, 0.0]), writable=True)
        await add_var(sa, ns, "HtCtrl", _pad_bool([True, True, True, True, False]), writable=True)
        await add_var(sa, ns, "HtSet", _pad_float([140.0, 155.0, 165.0, 160.0, 0.0]), writable=True)
        await add_var(sa, ns, "HtTime", _pad_int([3600, 7200, 10800, 10800, 0]), writable=True)
        await add_var(sa, ns, "MoistureAddSet", _pad_float([0.0, 0.0, 2.0, 1.5, 0.0]), writable=True)
        await add_var(sa, ns, "MoistureShedSet", _pad_float([5.0, 4.0, 3.0, 2.0, 0.0]), writable=True)
        await add_var(sa, ns, "OverTempSet", _pad_float([200.0, 200.0, 210.0, 200.0, 0.0]), writable=True)
        await add_var(sa, ns, "RampCtrl", _pad_bool([True, True, False, False, False]), writable=True)
        await add_var(sa, ns, "RampTime", _pad_int([1800, 3600, 0, 0, 0]), writable=True)
        await add_var(sa, ns, "SprayCtrl", _pad_bool([False, True, True, False, False]), writable=True)
        await add_var(sa, ns, "StepDbTemp", _pad_float([140.0, 155.0, 170.0, 160.0, 0.0]), writable=True)
        await add_var(sa, ns, "StepExhaust", _pad_int([20, 30, 50, 40, 0]), writable=True)
        await add_var(sa, ns, "StepFan", _pad_int([80, 85, 90, 85, 0]), writable=True)
        await add_var(sa, ns, "StepIntake", _pad_int([15, 25, 40, 30, 0]), writable=True)
        await add_var(sa, ns, "StepMc", _pad_int([30, 25, 18, 12, 0]), writable=True)
        await add_var(sa, ns, "StepMode", _pad_int([1, 1, 2, 2, 0]), writable=True)
        await add_var(sa, ns, "StepPctSetpoint", _pad_float([100.0, 100.0, 100.0, 100.0, 0.0]), writable=True)
        await add_var(sa, ns, "StepRh", _pad_int([80, 70, 55, 45, 0]), writable=True)
        await add_var(sa, ns, "StepTime", _pad_int([7200, 7200, 10800, 7200, 0]), writable=True)
        await add_var(sa, ns, "StepWbSetpoint", _pad_float([130.0, 135.0, 140.0, 138.0, 0.0]), writable=True)

        # ── VFD & HRV (ARRAY[0..7], 8 elements each) ────────────────────────
        vfd = await add_folder(kiln, ns, "VfdHrv")

        def _pad8_float(first2): return first2 + [0.0] * 6
        def _pad8_int(first2): return first2 + [0] * 6
        def _pad8_bool(first2): return first2 + [False] * 6

        self.hrv_current = await add_var(vfd, ns, "HrvCurrent", _pad8_float([12.5, 11.8]))
        self.hrv_exhaust = await add_var(vfd, ns, "HrvExhaust", _pad8_float([145.0, 142.0]))
        await add_var(vfd, ns, "HrvFanFault", [False] * 8)
        await add_var(vfd, ns, "HrvFaultNumber", [0] * 8)
        self.hrv_freq = await add_var(vfd, ns, "HrvFreq", _pad8_float([55.0, 52.0]))
        self.hrv_inlet = await add_var(vfd, ns, "HrvInlet", _pad8_float([85.0, 82.0]))
        self.hrv_rpm = await add_var(vfd, ns, "HrvRpm", _pad8_float([1650.0, 1560.0]))
        await add_var(vfd, ns, "HrvStatus", _pad8_int([1, 1]))
        await add_var(vfd, ns, "HrvVfdFaultReset", [False] * 8, writable=True)
        self.hrv_vfd_temp = await add_var(vfd, ns, "HrvVfdTemp", _pad8_float([105.0, 102.0]))
        self.kiln_exhaust = await add_var(vfd, ns, "KilnExhaust", _pad8_float([148.0, 145.0]))
        self.kiln_intake = await add_var(vfd, ns, "KilnIntake", _pad8_float([78.0, 75.0]))
        self.vfd_current = await add_var(vfd, ns, "VfdCurrent", _pad8_float([18.5, 17.2]))
        await add_var(vfd, ns, "VfdFault", [0] * 8)
        await add_var(vfd, ns, "VfdFaultReset", [False] * 8, writable=True)
        self.vfd_freq = await add_var(vfd, ns, "VfdFreq", _pad8_float([58.0, 55.0]))
        self.vfd_rpm = await add_var(vfd, ns, "VfdRpm", _pad8_float([1740.0, 1650.0]))
        await add_var(vfd, ns, "VfdStatus", _pad8_int([1, 1]))
        self.vfd_temp = await add_var(vfd, ns, "VfdTemp", _pad8_float([115.0, 110.0]))

        # ── Totals & Runtime (read-only, simulated counters) ─────────────────
        tot = await add_folder(kiln, ns, "TotalsRuntime")
        self.fan_rt = await add_var(tot, ns, "FanRt", 14400)
        self.total_run_time = await add_var(tot, ns, "TotalRunTimeMinutes", 15000)
        self.tot_heat_on = await add_var(tot, ns, "TotHeatOn", 12000)
        self.tot_refrig_on = await add_var(tot, ns, "TotRefrigOn", 3000)
        self.tot_vent_on = await add_var(tot, ns, "TotVentOn", 8000)

        # ── Power & Energy (read-only, simulated) ────────────────────────────
        pwr = await add_folder(kiln, ns, "PowerEnergy")
        self.active_fund_power = await add_var(pwr, ns, "ActiveFundPower", 42.0)
        self.active_harmonic_power = await add_var(pwr, ns, "ActiveHarmonicPower", 0.8)
        self.active_power = await add_var(pwr, ns, "ActivePower", 42.8)
        self.apparent_energy = await add_var(pwr, ns, "ApparentEnergy", 12500.0)
        self.apparent_power = await add_var(pwr, ns, "ApparentPower", 48.5)
        self.current_peak = await add_var(pwr, ns, "CurrentPeak", 95.0)
        self.fwd_active_energy = await add_var(pwr, ns, "ForwardActiveEnergy", 48500.0)
        self.fwd_active_fund_energy = await add_var(pwr, ns, "ForwardActiveFundEnergy", 47800.0)
        self.fwd_active_harm_energy = await add_var(pwr, ns, "ForwardActiveHarmonicEnergy", 700.0)
        self.fwd_reactive_energy = await add_var(pwr, ns, "ForwardReactiveEnergy", 15200.0)
        self.mean_phase_angle = await add_var(pwr, ns, "MeanPhaseAngle", 28.0)
        self.power_factor = await add_var(pwr, ns, "PowerFactor", 0.88)
        self.reactive_power = await add_var(pwr, ns, "ReactivePower", 22.5)
        self.rev_active_energy = await add_var(pwr, ns, "ReverseActiveEnergy", 120.0)
        self.rev_active_fund_energy = await add_var(pwr, ns, "ReverseActiveFundEnergy", 118.0)
        self.rev_active_harm_energy = await add_var(pwr, ns, "ReverseActiveHarmonicEnergy", 2.0)
        self.rev_reactive_energy = await add_var(pwr, ns, "ReverseReactiveEnergy", 45.0)
        self.rms_current = await add_var(pwr, ns, "RmsCurrent", 68.5)
        self.rms_voltage = await add_var(pwr, ns, "RmsVoltage", 480.0)
        self.voltage_peak = await add_var(pwr, ns, "VoltagePeak", 679.0)
        self.voltage_phase_angle = await add_var(pwr, ns, "VoltagePhaseAngle", 0.0)

        # ── Demand Response (mixed) ──────────────────────────────────────────
        dr = await add_folder(kiln, ns, "DemandResponse")
        self.current_demand = await add_var(dr, ns, "CurrentDemand", 42.8)
        self.demand_limit = await add_var(dr, ns, "DemandLimit", 100.0, writable=True)
        self.dr_enabled = await add_var(dr, ns, "DemandResponseEnabled", False, writable=True)
        self.dr_mode = await add_var(dr, ns, "DemandResponseMode", 0, writable=True)
        self.dr_package = await add_var(dr, ns, "DemandResponsePackage", 0)
        self.demand_shed = await add_var(dr, ns, "DemandShed", 0.0)
        self.demand_shed_pct = await add_var(dr, ns, "DemandShedPercent", 0.0)
        self.dr_event_active = await add_var(dr, ns, "DrEventActive", False)
        self.dr_remaining_time = await add_var(dr, ns, "DrRemainingTime", 0)

        # ── Misc ─────────────────────────────────────────────────────────────
        misc = await add_folder(kiln, ns, "Misc")
        self.one_sec_pulse = await add_var(misc, ns, "OneSecondPulse", False)

        log.info("  [KilnController] Tag table built — setpoints, temps, schedule arrays, VFD/HRV, power/energy")

    async def update(self):
        self._tick += 1
        t = time.time() - self._start_time

        # ── 1. Temperatures: drift toward setpoints with noise ───────────────
        db_sp = await self.sp_dry_bulb.read_value()
        wb_sp = await self.sp_wet_bulb.read_value()
        rh_sp = await self.sp_rh.read_value()

        # Ctrl dry bulb drifts toward setpoint
        ctrl_db = await self.ctrl_dry_bulb.read_value()
        ctrl_db += (db_sp - ctrl_db) * 0.05 + random.gauss(0, 0.3)
        await self.ctrl_dry_bulb.write_value(round(ctrl_db, 1))

        # Fwd dry bulb slightly above setpoint
        fwd_db = db_sp + 2.0 + random.gauss(0, 0.4)
        await self.fwd_dry_bulb.write_value(round(fwd_db, 1))

        # Rev dry bulb slightly below setpoint
        rev_db = db_sp - 5.0 + random.gauss(0, 0.4)
        await self.rev_dry_bulb.write_value(round(rev_db, 1))

        # Ctrl wet bulb drifts toward setpoint
        ctrl_wb = await self.ctrl_wet_bulb.read_value()
        ctrl_wb += (wb_sp - ctrl_wb) * 0.05 + random.gauss(0, 0.3)
        await self.ctrl_wet_bulb.write_value(round(ctrl_wb, 1))

        # Fwd/Rev wet bulb
        fwd_wb = wb_sp + 1.5 + random.gauss(0, 0.3)
        await self.fwd_wet_bulb.write_value(round(fwd_wb, 1))
        rev_wb = wb_sp - 3.2 + random.gauss(0, 0.3)
        await self.rev_wet_bulb.write_value(round(rev_wb, 1))

        # Temp1-5 vary around CtrlDryBulb
        for i, node in enumerate([self.temp1, self.temp2, self.temp3, self.temp4, self.temp5]):
            await node.write_value(round(ctrl_db + random.gauss(0, 1.0), 1))

        # DLP and SLP/SLT
        await self.dlp.write_value(round(wb_sp + 2.0 + random.gauss(0, 0.5), 1))
        sl_sp = await self.sp_suction_line.read_value()
        await self.slp.write_value(round(sl_sp - 1.5 + random.gauss(0, 0.3), 1))
        await self.slt.write_value(round(sl_sp - 0.8 + random.gauss(0, 0.3), 1))

        # ── 2. Moisture & RH ────────────────────────────────────────────────
        # MC slowly decreases over time (drying), modulated by sine
        mc_base = max(6.0, 28.0 - t / 600.0)  # slow downward trend
        mc_mod = 2.0 * math.sin(t / 120.0)
        await self.mc_ctrl.write_value(round(mc_base + mc_mod + random.gauss(0, 0.3), 1))
        await self.mc_fwd.write_value(round(mc_base + 2.0 + mc_mod + random.gauss(0, 0.3), 1))
        await self.mc_rev.write_value(round(mc_base - 2.0 + mc_mod + random.gauss(0, 0.3), 1))

        # RH floats near setpoint
        await self.rh_ctrl.write_value(round(rh_sp + random.gauss(0, 1.5), 1))
        await self.rh_fwd.write_value(round(rh_sp + 1.0 + random.gauss(0, 1.5), 1))
        await self.rh_rev.write_value(round(rh_sp - 1.0 + random.gauss(0, 1.5), 1))

        # ── 3. Status & Display ──────────────────────────────────────────────
        await self.cycle_status.write_value(1)

        # Fan/Heat/Vent toggle occasionally
        if random.random() < 0.02:
            fs = await self.fan_status.read_value()
            await self.fan_status.write_value(0 if fs else 1)
        if random.random() < 0.02:
            hs = await self.heat_status.read_value()
            await self.heat_status.write_value(0 if hs else 1)
        if random.random() < 0.02:
            vs = await self.vent_status.read_value()
            await self.vent_status.write_value(0 if vs else 1)

        # Damper varies sinusoidally 30-70%
        damper = int(50 + 20 * math.sin(t / 30.0))
        await self.damper_pct.write_value(damper)

        # CurrentStep increments slowly (cycles through 0-4)
        step_duration = 300
        step = int(t / step_duration) % 5
        await self.current_step.write_value(step)
        elapsed_in_step = int(t % step_duration)
        await self.step_elapsed_time.write_value(elapsed_in_step)
        await self.step_remaining_time.write_value(step_duration - elapsed_in_step)

        # LightStack: 2=Green when running, 3=Yellow if alarm
        alarm1 = await self.alarm_active1.read_value()
        if alarm1:
            await self.light_stack.write_value(3)
            await self.light_stack_status.write_value("Yellow")
        else:
            await self.light_stack.write_value(2)
            await self.light_stack_status.write_value("Green")

        # Random alarm injection
        if random.random() < 0.005:
            await self.alarm_active1.write_value(random.randint(1, 5))
        elif random.random() < 0.05:
            await self.alarm_active1.write_value(0)

        # ── 4. VFD & HRV arrays ─────────────────────────────────────────────
        # Update first 2 elements with noise around defaults
        for arr_node, defaults in [
            (self.vfd_freq, [58.0, 55.0]),
            (self.vfd_current, [18.5, 17.2]),
            (self.vfd_rpm, [1740.0, 1650.0]),
            (self.hrv_freq, [55.0, 52.0]),
            (self.hrv_current, [12.5, 11.8]),
            (self.hrv_rpm, [1650.0, 1560.0]),
        ]:
            arr = await arr_node.read_value()
            arr[0] = round(defaults[0] + random.gauss(0, 0.5), 1)
            arr[1] = round(defaults[1] + random.gauss(0, 0.5), 1)
            await arr_node.write_value(arr)

        # VfdTemp and HrvVfdTemp slowly drift
        for temp_node, base in [(self.vfd_temp, [115.0, 110.0]), (self.hrv_vfd_temp, [105.0, 102.0])]:
            arr = await temp_node.read_value()
            arr[0] = round(base[0] + 3.0 * math.sin(t / 200.0) + random.gauss(0, 0.2), 1)
            arr[1] = round(base[1] + 3.0 * math.sin(t / 200.0 + 1.0) + random.gauss(0, 0.2), 1)
            await temp_node.write_value(arr)

        # ── 5. Power & Energy ────────────────────────────────────────────────
        # ActivePower varies sinusoidally 35-50 kW
        ap = 42.5 + 7.5 * math.sin(t / 45.0) + random.gauss(0, 0.3)
        ap = round(max(35.0, min(50.0, ap)), 1)
        await self.active_power.write_value(ap)
        await self.active_fund_power.write_value(round(ap - 0.8, 1))
        await self.active_harmonic_power.write_value(round(0.8 + random.gauss(0, 0.05), 2))

        # PowerFactor floats 0.85-0.92
        pf = 0.885 + 0.035 * math.sin(t / 60.0) + random.gauss(0, 0.005)
        pf = round(max(0.85, min(0.92, pf)), 3)
        await self.power_factor.write_value(pf)

        # ApparentPower = ActivePower / PowerFactor
        app_pwr = round(ap / pf, 1) if pf > 0 else ap
        await self.apparent_power.write_value(app_pwr)

        # ReactivePower
        rp = round(math.sqrt(max(0, app_pwr**2 - ap**2)), 1)
        await self.reactive_power.write_value(rp)

        # RmsCurrent proportional to power (P = V*I*PF, V=480)
        rms_i = round(ap * 1000 / (480.0 * pf), 1) if pf > 0 else 0.0
        await self.rms_current.write_value(rms_i)
        await self.rms_voltage.write_value(round(480.0 + random.gauss(0, 0.5), 1))
        await self.current_peak.write_value(round(rms_i * 1.414, 1))
        await self.voltage_peak.write_value(round(480.0 * 1.414 + random.gauss(0, 1.0), 1))
        await self.mean_phase_angle.write_value(round(math.degrees(math.acos(min(1.0, pf))), 1))
        await self.voltage_phase_angle.write_value(0.0)

        # Energy accumulators slowly increment
        incr = ap / 3600.0  # kWh per second tick
        fae = await self.fwd_active_energy.read_value()
        await self.fwd_active_energy.write_value(round(fae + incr, 1))
        fafe = await self.fwd_active_fund_energy.read_value()
        await self.fwd_active_fund_energy.write_value(round(fafe + incr * 0.99, 1))
        fahe = await self.fwd_active_harm_energy.read_value()
        await self.fwd_active_harm_energy.write_value(round(fahe + incr * 0.01, 2))
        fre = await self.fwd_reactive_energy.read_value()
        await self.fwd_reactive_energy.write_value(round(fre + rp / 3600.0, 1))
        ae = await self.apparent_energy.read_value()
        await self.apparent_energy.write_value(round(ae + app_pwr / 3600.0, 1))

        # ── 6. Totals ────────────────────────────────────────────────────────
        # Increment by 1 each minute (60 ticks at 1s interval)
        if self._tick % 60 == 0:
            trt = await self.total_run_time.read_value()
            await self.total_run_time.write_value(trt + 1)
            frt = await self.fan_rt.read_value()
            await self.fan_rt.write_value(frt + 1)
            tho = await self.tot_heat_on.read_value()
            await self.tot_heat_on.write_value(tho + 1)
            tvo = await self.tot_vent_on.read_value()
            await self.tot_vent_on.write_value(tvo + 1)
        if self._tick % 120 == 0:
            tro = await self.tot_refrig_on.read_value()
            await self.tot_refrig_on.write_value(tro + 1)

        # ── 7. Demand Response ───────────────────────────────────────────────
        await self.current_demand.write_value(ap)
        dr_en = await self.dr_enabled.read_value()
        if dr_en:
            limit = await self.demand_limit.read_value()
            shed = max(0.0, round(ap - limit, 1))
            shed_pct = round(shed / ap * 100, 1) if ap > 0 else 0.0
            await self.demand_shed.write_value(shed)
            await self.demand_shed_pct.write_value(shed_pct)
        else:
            await self.demand_shed.write_value(0.0)
            await self.demand_shed_pct.write_value(0.0)

        # ── 8. Misc ──────────────────────────────────────────────────────────
        await self.one_sec_pulse.write_value(bool(self._tick % 2))


# ──────────────────────────────────────────────────────────────────────────────
# Opto22 groov RIO — CODESYS 3.5 OPC-UA Server
# ──────────────────────────────────────────────────────────────────────────────

class Opto22GroovDevice:
    """
    groov RIO running a CODESYS 3.5 SoftPLC runtime.
    The OPC-UA server is provided entirely by CODESYS, NOT groov Manage.

    CODESYS 3.5 exposes a standard DeviceSet hierarchy.  Only variables
    explicitly added to the Symbol Configuration (or tagged with
    {attribute 'OPC_UA_ACCESS'}) are visible.  Variable names follow
    IEC 61131-3 Hungarian-prefix conventions used in CODESYS:
      r  = REAL       x  = BOOL       i  = INT
      s  = STRING     ui = UINT       dw = DWORD

    Namespace URI (registered separately from the sim namespace):
      "urn:CODESYS:OpcUaServer:groov-RIO-001"

    Node tree:
      DeviceSet/
        groov-RIO-001/                       ← device object
          ServerInfo/
            RuntimeVersion, TargetId, DeviceName, NodeName, TargetVendor
          Resources/
            Application/
              Info/
                ApplicationName, ApplicationState, ChangeTime, ProjectAuthor
              Tasks/
                MainTask/
                  CycleTime_us, LastExecTime_us, Jitter_us, CycleCount
                SlowTask/
                  CycleTime_us, LastExecTime_us, CycleCount
              GlobalVars/
                GVL_AnalogIO/
                  rAI_Channel_0 … rAI_Channel_7   REAL  (eng. units, 0.0–100.0)
                  rAI_mA_0 … rAI_mA_7             REAL  (raw 4–20 mA)
                  rAO_Channel_0 … rAO_Channel_3   REAL  (writable setpoint, 0–100 %)
                GVL_DigitalIO/
                  xDI_0 … xDI_15   BOOL  (digital inputs)
                  xDO_0 … xDO_7    BOOL  (writable digital outputs)
                GVL_System/
                  rUptime_s, xWatchdogOK, xPowerOK, xEthernetLink,
                  sLastError, uiErrorCount
              Programs/
                PLC_PRG/
                  rTankLevel_mm    REAL  (live process var)
                  rFlowRate_Ls     REAL
                  rTemp_C          REAL
                  xAutoMode        BOOL  (writable)
                  xAlarmActive     BOOL
                  iAlarmCode       INT
                  sAlarmText       STRING
                FB_PID_Inst/             ← FB instance exposed via sym config
                  rSetpoint        REAL  (writable)
                  rActual          REAL
                  rOutput          REAL
                  rError           REAL
                  xLimitHigh       BOOL
                  xLimitLow        BOOL
    """

    def __init__(self):
        self._start_time = time.time()
        self._tick       = 0
        self._di_states  = [False] * 16
        self._alarm_cnt  = 0

    async def build(self, server: Server, ns: int, root: Node, instance: int = 1):
        # ── DeviceSet container (CODESYS convention) ─────────────────────────
        suffix = f"-{instance:03d}"
        device_set = await add_folder(root,       ns, f"DeviceSet{suffix}")
        dev        = await add_object(device_set, ns, f"groov-RIO{suffix}")

        # ── ServerInfo ───────────────────────────────────────────────────────
        sinfo = await add_folder(dev, ns, "ServerInfo")
        await add_var(sinfo, ns, "RuntimeVersion", "V3.5.19.40")
        await add_var(sinfo, ns, "TargetId",       "0x00000048")   # Opto22 groov target GUID prefix
        await add_var(sinfo, ns, "DeviceName",     "groov-RIO-001")
        await add_var(sinfo, ns, "NodeName",       "groov-RIO-001")
        await add_var(sinfo, ns, "TargetVendor",   "Opto 22")
        await add_var(sinfo, ns, "MACAddress",     "00:A0:3D:0A:12:34")
        await add_var(sinfo, ns, "IPAddress",      "192.168.1.10")

        # ── Resources/Application ────────────────────────────────────────────
        resources   = await add_folder(dev,       ns, "Resources")
        application = await add_folder(resources, ns, "Application")

        # ── Application Info ─────────────────────────────────────────────────
        app_info = await add_folder(application, ns, "Info")
        await add_var(app_info, ns, "ApplicationName",  "groovRIO_ProcessApp")
        self.app_state  = await add_var(app_info, ns, "ApplicationState", "Running")
        await add_var(app_info, ns, "ChangeTime",       "2024-11-12T08:33:00Z")
        await add_var(app_info, ns, "ProjectAuthor",    "Nyle Engineering")

        # ── Tasks ────────────────────────────────────────────────────────────
        tasks      = await add_folder(application, ns, "Tasks")
        main_task  = await add_folder(tasks, ns, "MainTask")
        await add_var(main_task, ns, "CycleTime_us",    10000)   # 10 ms configured
        self.main_exec   = await add_var(main_task, ns, "LastExecTime_us", 0)
        self.main_jitter = await add_var(main_task, ns, "Jitter_us",       0)
        self.main_cycles = await add_var(main_task, ns, "CycleCount",      0)

        slow_task  = await add_folder(tasks, ns, "SlowTask")
        await add_var(slow_task, ns, "CycleTime_us",    100000)  # 100 ms configured
        self.slow_exec   = await add_var(slow_task, ns, "LastExecTime_us", 0)
        self.slow_cycles = await add_var(slow_task, ns, "CycleCount",      0)

        # ── GlobalVars ───────────────────────────────────────────────────────
        gvars = await add_folder(application, ns, "GlobalVars")

        # GVL_AnalogIO
        gvl_aio = await add_folder(gvars, ns, "GVL_AnalogIO")
        self.ai_eu_nodes = []   # engineering units 0–100 %
        self.ai_ma_nodes = []   # raw mA 4.0–20.0
        for i in range(8):
            eu = await add_var(gvl_aio, ns, f"rAI_Channel_{i}", float(0.0))
            ma = await add_var(gvl_aio, ns, f"rAI_mA_{i}",      float(4.0))
            self.ai_eu_nodes.append(eu)
            self.ai_ma_nodes.append(ma)
        self.ao_nodes = []
        for i in range(4):
            n = await add_var(gvl_aio, ns, f"rAO_Channel_{i}", float(0.0), writable=True)
            self.ao_nodes.append(n)

        # GVL_DigitalIO
        gvl_dio = await add_folder(gvars, ns, "GVL_DigitalIO")
        self.di_nodes = []
        for i in range(16):
            n = await add_var(gvl_dio, ns, f"xDI_{i}", False)
            self.di_nodes.append(n)
        self.do_nodes = []
        for i in range(8):
            n = await add_var(gvl_dio, ns, f"xDO_{i}", False, writable=True)
            self.do_nodes.append(n)

        # GVL_System
        gvl_sys = await add_folder(gvars, ns, "GVL_System")
        self.uptime      = await add_var(gvl_sys, ns, "rUptime_s",      float(0.0))
        self.wd_ok       = await add_var(gvl_sys, ns, "xWatchdogOK",    True)
        await add_var(gvl_sys, ns, "xPowerOK",       True)
        await add_var(gvl_sys, ns, "xEthernetLink",  True)
        self.last_error  = await add_var(gvl_sys, ns, "sLastError",     "")
        self.error_count = await add_var(gvl_sys, ns, "uiErrorCount",   0)

        # ── Programs ─────────────────────────────────────────────────────────
        programs = await add_folder(application, ns, "Programs")

        # PLC_PRG
        plc_prg = await add_folder(programs, ns, "PLC_PRG")
        self.tank_level  = await add_var(plc_prg, ns, "rTankLevel_mm",  float(1200.0))
        self.flow_rate   = await add_var(plc_prg, ns, "rFlowRate_Ls",   float(0.0))
        self.temp_c      = await add_var(plc_prg, ns, "rTemp_C",        float(21.0))
        self.auto_mode   = await add_var(plc_prg, ns, "xAutoMode",      False, writable=True)
        self.alarm_act   = await add_var(plc_prg, ns, "xAlarmActive",   False)
        self.alarm_code  = await add_var(plc_prg, ns, "iAlarmCode",     0)
        self.alarm_text  = await add_var(plc_prg, ns, "sAlarmText",     "")

        # FB_PID_Inst (exposed PID function block instance)
        fb_pid = await add_folder(programs, ns, "FB_PID_Inst")
        self.pid_sp      = await add_var(fb_pid, ns, "rSetpoint",   float(60.0), writable=True)
        self.pid_actual  = await add_var(fb_pid, ns, "rActual",     float(0.0))
        self.pid_output  = await add_var(fb_pid, ns, "rOutput",     float(0.0))
        self.pid_error   = await add_var(fb_pid, ns, "rError",      float(0.0))
        self.pid_lim_hi  = await add_var(fb_pid, ns, "xLimitHigh",  False)
        self.pid_lim_lo  = await add_var(fb_pid, ns, "xLimitLow",   False)

        # ── Kiln Controller ──────────────────────────────────────────────────
        self._kiln = KilnTags()
        await self._kiln.build(device_set, ns)

        log.info("  [groov RIO / CODESYS 3.5] Node tree built — DeviceSet hierarchy, "
                 "2 Tasks, 3 GVLs (8 AI, 4 AO, 16 DI, 8 DO), PLC_PRG + FB_PID_Inst")

    async def update(self):
        self._tick += 1
        t = time.time() - self._start_time

        # ── Task diagnostics ─────────────────────────────────────────────────
        await self.uptime.write_value(round(t, 1))
        exec_us = int(random.gauss(3200, 150))          # ~3.2 ms exec in a 10 ms task
        jitter  = abs(int(random.gauss(0, 80)))
        await self.main_exec.write_value(max(0, exec_us))
        await self.main_jitter.write_value(jitter)
        await self.main_cycles.write_value(self._tick)
        await self.slow_exec.write_value(int(random.gauss(1100, 80)))
        await self.slow_cycles.write_value(self._tick // 10)

        # ── GVL_AnalogIO ─────────────────────────────────────────────────────
        for i in range(8):
            phase = i * (math.pi / 4)
            eu = 50.0 + 40.0 * math.sin(t / (20 + i * 3) + phase) + random.gauss(0, 0.2)
            eu = max(0.0, min(100.0, round(eu, 3)))
            # Scale eng. units → mA (0 % = 4 mA, 100 % = 20 mA)
            ma = round(4.0 + (eu / 100.0) * 16.0, 4)
            await self.ai_eu_nodes[i].write_value(eu)
            await self.ai_ma_nodes[i].write_value(ma)

        # ── GVL_DigitalIO ─────────────────────────────────────────────────────
        for i in range(16):
            if random.random() < 0.04:
                self._di_states[i] = not self._di_states[i]
                await self.di_nodes[i].write_value(self._di_states[i])

        # ── PLC_PRG process sim ───────────────────────────────────────────────
        auto = await self.auto_mode.read_value()
        flow = round(random.gauss(1.8, 0.15), 3) if auto else 0.0
        await self.flow_rate.write_value(flow)

        lvl = await self.tank_level.read_value()
        lvl = max(0.0, min(3000.0, round(lvl + (flow - 2.0) * 0.4, 1)))
        await self.tank_level.write_value(lvl)

        temp = await self.temp_c.read_value()
        temp += random.gauss(0, 0.04)
        await self.temp_c.write_value(round(temp, 2))

        # ── FB_PID_Inst ───────────────────────────────────────────────────────
        sp     = await self.pid_sp.read_value()
        actual = await self.ai_eu_nodes[0].read_value()    # AI_Channel_0 as PV
        error  = round(sp - actual, 4)
        output = round(max(0.0, min(100.0, 50.0 + error * 0.8)), 3)
        await self.pid_actual.write_value(actual)
        await self.pid_error.write_value(error)
        await self.pid_output.write_value(output)
        await self.pid_lim_hi.write_value(output >= 99.5)
        await self.pid_lim_lo.write_value(output <= 0.5)

        # ── Random alarm ─────────────────────────────────────────────────────
        if random.random() < 0.003:
            self._alarm_cnt += 1
            code = random.randint(1, 5)
            msgs = {1: "AI_Channel_0 out of range", 2: "Watchdog near limit",
                    3: "Tank level low",             4: "Flow sensor fault",
                    5: "PID output saturated"}
            await self.alarm_act.write_value(True)
            await self.alarm_code.write_value(code)
            await self.alarm_text.write_value(msgs[code])
            await self.error_count.write_value(self._alarm_cnt)
            await self.last_error.write_value(msgs[code])
        else:
            await self.alarm_act.write_value(False)
            await self.alarm_code.write_value(0)
            await self.alarm_text.write_value("")

        await self._kiln.update()


# ──────────────────────────────────────────────────────────────────────────────
# Siemens S7-1200 Namespace
# ──────────────────────────────────────────────────────────────────────────────

class SiemensS71200Device:
    """
    Mirrors a typical S7-1200 OPC-UA server node structure (TIA Portal style).

    Namespace layout:
      Siemens_S7_1200/
        DeviceInfo/
          OrderNumber, HWRevision, FWVersion, ModuleName, IPAddress, Rack, Slot
        Inputs/
          I_0_0 … I_0_7   (%I0.0 … %I0.7 digital byte)
          IW_64            (%IW64 analog word, 0–27648)
        Outputs/
          Q_0_0 … Q_0_7   (%Q0.0 … %Q0.7 digital, writable)
          QW_64            (%QW64 analog word, writable)
        DataBlocks/
          DB1_ProductionData/
            TankLevel_mm, FlowRate_Ls, TotalVolume_L, TempSetpoint_C,
            TempActual_C, PumpRunning, ValveOpen, BatchCount
          DB2_Diagnostics/
            CycleTime_ms, CPULoad_pct, MemUsed_bytes, ErrorCode, ErrorText
        Timers/
          T1_PT_ms, T1_ET_ms, T1_Running
          T2_PT_ms, T2_ET_ms, T2_Running
        Counters/
          C1_Value, C1_Preset, C1_Done
          C2_Value, C2_Preset, C2_Done
        PLCStatus/
          OperatingMode, RunHours, DiagBuffer, LEDRun, LEDStop, LEDError
    """

    def __init__(self):
        self._tick      = 0
        self._start     = time.time()
        self._batch_cnt = 0
        self._c1_val    = 0
        self._c2_val    = 0

    async def build(self, server: Server, ns: int, root: Node, instance: int = 1):
        dev = await add_object(root, ns, f"Siemens_S7_1200_{instance:03d}")

        # ── Device Info ──────────────────────────────────────────────────────
        info = await add_folder(dev, ns, "DeviceInfo")
        await add_var(info, ns, "OrderNumber",  "6ES7 214-1AG40-0XB0")
        await add_var(info, ns, "HWRevision",   "3")
        await add_var(info, ns, "FWVersion",    "V4.5")
        await add_var(info, ns, "ModuleName",   "CPU 1214C DC/DC/DC")
        await add_var(info, ns, "IPAddress",    "192.168.1.20")
        await add_var(info, ns, "Rack",         0)
        await add_var(info, ns, "Slot",         1)

        # ── Inputs ───────────────────────────────────────────────────────────
        inp = await add_folder(dev, ns, "Inputs")
        self.di_in_nodes = []
        for bit in range(8):
            n = await add_var(inp, ns, f"I_0_{bit}", False)
            self.di_in_nodes.append(n)
        self.iw64 = await add_var(inp, ns, "IW_64", 0)   # analog in word

        # ── Outputs ──────────────────────────────────────────────────────────
        out = await add_folder(dev, ns, "Outputs")
        self.do_out_nodes = []
        for bit in range(8):
            n = await add_var(out, ns, f"Q_0_{bit}", False, writable=True)
            self.do_out_nodes.append(n)
        self.qw64 = await add_var(out, ns, "QW_64", 0, writable=True)

        # ── DB1 Production Data ──────────────────────────────────────────────
        db1 = await add_folder(dev, ns, "DB1_ProductionData")
        self.tank_level   = await add_var(db1, ns, "TankLevel_mm",    float(1500.0))
        self.flow_rate    = await add_var(db1, ns, "FlowRate_Ls",     float(0.0))
        self.total_volume = await add_var(db1, ns, "TotalVolume_L",   float(0.0))
        self.temp_sp      = await add_var(db1, ns, "TempSetpoint_C",  float(75.0), writable=True)
        self.temp_act     = await add_var(db1, ns, "TempActual_C",    float(20.0))
        self.pump_run     = await add_var(db1, ns, "PumpRunning",     False, writable=True)
        self.valve_open   = await add_var(db1, ns, "ValveOpen",       False, writable=True)
        self.batch_cnt    = await add_var(db1, ns, "BatchCount",      0)

        # ── DB2 Diagnostics ──────────────────────────────────────────────────
        db2 = await add_folder(dev, ns, "DB2_Diagnostics")
        self.cycle_time   = await add_var(db2, ns, "CycleTime_ms",    float(1.2))
        self.cpu_load     = await add_var(db2, ns, "CPULoad_pct",     float(8.0))
        self.mem_used     = await add_var(db2, ns, "MemUsed_bytes",   49152)
        self.error_code   = await add_var(db2, ns, "ErrorCode",       0)
        self.error_text   = await add_var(db2, ns, "ErrorText",       "No Fault")

        # ── Timers ───────────────────────────────────────────────────────────
        tmr = await add_folder(dev, ns, "Timers")
        self.t1_pt  = await add_var(tmr, ns, "T1_PT_ms",  5000, writable=True)
        self.t1_et  = await add_var(tmr, ns, "T1_ET_ms",  0)
        self.t1_run = await add_var(tmr, ns, "T1_Running", False)
        self.t2_pt  = await add_var(tmr, ns, "T2_PT_ms",  10000, writable=True)
        self.t2_et  = await add_var(tmr, ns, "T2_ET_ms",  0)
        self.t2_run = await add_var(tmr, ns, "T2_Running", False)

        # ── Counters ─────────────────────────────────────────────────────────
        ctr = await add_folder(dev, ns, "Counters")
        self.c1_val    = await add_var(ctr, ns, "C1_Value",  0)
        self.c1_preset = await add_var(ctr, ns, "C1_Preset", 100, writable=True)
        self.c1_done   = await add_var(ctr, ns, "C1_Done",   False)
        self.c2_val    = await add_var(ctr, ns, "C2_Value",  0)
        self.c2_preset = await add_var(ctr, ns, "C2_Preset", 500, writable=True)
        self.c2_done   = await add_var(ctr, ns, "C2_Done",   False)

        # ── PLC Status ───────────────────────────────────────────────────────
        plc = await add_folder(dev, ns, "PLCStatus")
        await add_var(plc, ns, "OperatingMode", "RUN")
        self.run_hours = await add_var(plc, ns, "RunHours",    float(0.0))
        await add_var(plc, ns, "DiagBuffer",   "No entries")
        await add_var(plc, ns, "LED_RUN",      True)
        await add_var(plc, ns, "LED_STOP",     False)
        await add_var(plc, ns, "LED_ERROR",    False)

        # ── Kiln Controller ──────────────────────────────────────────────────
        self._kiln = KilnTags()
        await self._kiln.build(dev, ns)

        log.info("  [S7-1200] Node tree built — 2 DBs, 8 DI/DO, timers, counters")

    async def update(self):
        self._tick += 1
        t = time.time() - self._start

        # ── Analog inputs (IW64: 0–27648 representing 0–100 %) ───────────────
        iw = int(13824 + 12000 * math.sin(t / 15.0) + random.randint(-50, 50))
        iw = max(0, min(27648, iw))
        await self.iw64.write_value(iw)

        # ── Digital inputs: walking bit pattern + noise ──────────────────────
        for i, node in enumerate(self.di_in_nodes):
            state = bool((self._tick >> i) & 1) if i < 4 else random.random() < 0.15
            await node.write_value(state)

        # ── DB1 process simulation ────────────────────────────────────────────
        pump = (await self.pump_run.read_value())
        valve = (await self.valve_open.read_value())
        sp = (await self.temp_sp.read_value())

        flow = round(random.gauss(2.5, 0.3), 3) if pump else 0.0
        await self.flow_rate.write_value(flow)

        # Tank drains when valve open, fills via pump
        raw_level = await self.tank_level.read_value()
        delta = (flow * 1.2) - (2.8 if valve else 0.0)
        new_level = max(0.0, min(3000.0, round(raw_level + delta * 0.5, 1)))
        await self.tank_level.write_value(new_level)

        # Volume integrator
        tv = await self.total_volume.read_value()
        await self.total_volume.write_value(round(tv + flow * 0.5, 1))

        # Temperature ramps toward setpoint
        ta = await self.temp_act.read_value()
        ta += (sp - ta) * 0.02 + random.gauss(0, 0.05)
        await self.temp_act.write_value(round(ta, 2))

        # ── DB2 diagnostics ──────────────────────────────────────────────────
        await self.cycle_time.write_value(round(1.2 + random.gauss(0, 0.05), 3))
        await self.cpu_load.write_value(round(8.0 + 4.0 * math.sin(t / 30) + random.gauss(0, 0.5), 1))
        await self.mem_used.write_value(49152 + random.randint(-512, 512))

        # ── Timers ────────────────────────────────────────────────────────────
        t1_pt = await self.t1_pt.read_value()
        t1_et = int((t * 1000) % t1_pt)
        await self.t1_et.write_value(t1_et)
        await self.t1_run.write_value(t1_et < t1_pt)

        t2_pt = await self.t2_pt.read_value()
        t2_et = int((t * 1000) % t2_pt)
        await self.t2_et.write_value(t2_et)
        await self.t2_run.write_value(t2_et < t2_pt)

        # ── Counters ─────────────────────────────────────────────────────────
        self._c1_val = (self._c1_val + 1) % 1000
        c1p = await self.c1_preset.read_value()
        await self.c1_val.write_value(self._c1_val % c1p)
        await self.c1_done.write_value(self._c1_val % c1p == 0)

        self._c2_val = self._tick % 10000
        c2p = await self.c2_preset.read_value()
        await self.c2_val.write_value(self._c2_val % c2p)
        await self.c2_done.write_value(self._c2_val % c2p == 0)

        # ── Run hours ─────────────────────────────────────────────────────────
        await self.run_hours.write_value(round(t / 3600, 4))

        await self._kiln.update()


# ──────────────────────────────────────────────────────────────────────────────
# Unitronics PLC Namespace (UniStream / Vision style)
# ──────────────────────────────────────────────────────────────────────────────

class UnitronicsDevice:
    """
    Mirrors Unitronics UniStream / Vision series OPC-UA structure.
    UniStream uses "Tags" organized by type; Vision uses operands (MI, ML, MB, etc.)

    Namespace layout:
      Unitronics_PLC/
        DeviceInfo/
          Model, FirmwareVersion, ProjectName, IPAddress, SerialNumber
        Operands/
          MemoryIntegers/   MI_0 … MI_9   (INT, writable)
          MemoryLongs/      ML_0 … ML_4   (DINT, writable)
          MemoryBits/       MB_0 … MB_15  (BOOL, writable)
          SystemBits/       SB2_RunMode, SB3_FirstScan, SB5_PowerUpFlag
        AnalogIO/
          AI_0 … AI_5   (0–4095 raw 12-bit ADC)
          AO_0 … AO_1   (0–4095, writable)
        DigitalIO/
          I_0 … I_15    (digital inputs)
          O_0 … O_7     (digital outputs, writable)
        DataTables/
          DT0_Process/
            Pressure_kPa, Temperature_C, Humidity_pct, CO2_ppm, FlowTotal_m3
          DT1_Alarms/
            AlarmActive, AlarmCode, AlarmText, AlarmCount
        PLC_Status/
          RunMode, ScanTime_ms, ProjectCRC, BatteryOK, SDCardOK
    """

    def __init__(self):
        self._tick   = 0
        self._start  = time.time()
        self._alarm_count = 0

    async def build(self, server: Server, ns: int, root: Node, instance: int = 1):
        dev = await add_object(root, ns, f"Unitronics_PLC_{instance:03d}")

        # ── Device Info ──────────────────────────────────────────────────────
        info = await add_folder(dev, ns, "DeviceInfo")
        await add_var(info, ns, "Model",           "USC-B10-T24")
        await add_var(info, ns, "FirmwareVersion", "V1.35.15")
        await add_var(info, ns, "ProjectName",     "ProcessControl_v3")
        await add_var(info, ns, "IPAddress",       "192.168.1.30")
        await add_var(info, ns, "SerialNumber",    "UN-2024-00451")

        # ── Memory Operands ──────────────────────────────────────────────────
        ops = await add_folder(dev, ns, "Operands")

        mi_folder = await add_folder(ops, ns, "MemoryIntegers")
        self.mi_nodes = []
        for i in range(10):
            n = await add_var(mi_folder, ns, f"MI_{i}", 0, writable=True)
            self.mi_nodes.append(n)

        ml_folder = await add_folder(ops, ns, "MemoryLongs")
        self.ml_nodes = []
        for i in range(5):
            n = await add_var(ml_folder, ns, f"ML_{i}", 0, writable=True)
            self.ml_nodes.append(n)

        mb_folder = await add_folder(ops, ns, "MemoryBits")
        self.mb_nodes = []
        for i in range(16):
            n = await add_var(mb_folder, ns, f"MB_{i}", False, writable=True)
            self.mb_nodes.append(n)

        sb_folder = await add_folder(ops, ns, "SystemBits")
        self.sb_run    = await add_var(sb_folder, ns, "SB2_RunMode",     True)
        self.sb_first  = await add_var(sb_folder, ns, "SB3_FirstScan",   False)
        self.sb_power  = await add_var(sb_folder, ns, "SB5_PowerUpFlag", False)

        # ── Analog I/O ───────────────────────────────────────────────────────
        aio = await add_folder(dev, ns, "AnalogIO")
        self.ai_nodes = []
        for i in range(6):
            n = await add_var(aio, ns, f"AI_{i}", 0)
            self.ai_nodes.append(n)
        self.ao_nodes = []
        for i in range(2):
            n = await add_var(aio, ns, f"AO_{i}", 0, writable=True)
            self.ao_nodes.append(n)

        # ── Digital I/O ──────────────────────────────────────────────────────
        dio = await add_folder(dev, ns, "DigitalIO")
        self.di_nodes = []
        for i in range(16):
            n = await add_var(dio, ns, f"I_{i}", False)
            self.di_nodes.append(n)
        self.do_nodes = []
        for i in range(8):
            n = await add_var(dio, ns, f"O_{i}", False, writable=True)
            self.do_nodes.append(n)

        # ── DT0 Process Data ─────────────────────────────────────────────────
        dt0 = await add_folder(dev, ns, "DT0_Process")
        self.pressure    = await add_var(dt0, ns, "Pressure_kPa",  float(101.3))
        self.temp_proc   = await add_var(dt0, ns, "Temperature_C", float(22.0))
        self.humidity    = await add_var(dt0, ns, "Humidity_pct",  float(45.0))
        self.co2_ppm     = await add_var(dt0, ns, "CO2_ppm",       float(412.0))
        self.flow_total  = await add_var(dt0, ns, "FlowTotal_m3",  float(0.0))

        # ── DT1 Alarms ───────────────────────────────────────────────────────
        dt1 = await add_folder(dev, ns, "DT1_Alarms")
        self.alarm_active = await add_var(dt1, ns, "AlarmActive", False)
        self.alarm_code   = await add_var(dt1, ns, "AlarmCode",   0)
        self.alarm_text   = await add_var(dt1, ns, "AlarmText",   "No Alarm")
        self.alarm_count  = await add_var(dt1, ns, "AlarmCount",  0)

        # ── PLC Status ───────────────────────────────────────────────────────
        plc = await add_folder(dev, ns, "PLC_Status")
        await add_var(plc, ns, "RunMode",      "RUN")
        self.scan_time = await add_var(plc, ns, "ScanTime_ms", float(2.4))
        await add_var(plc, ns, "ProjectCRC",   "0xA3F2")
        await add_var(plc, ns, "BatteryOK",    True)
        await add_var(plc, ns, "SDCardOK",     True)

        # ── Kiln Controller ──────────────────────────────────────────────────
        self._kiln = KilnTags()
        await self._kiln.build(dev, ns)

        log.info("  [Unitronics] Node tree built — MIs, MLs, MBs, 6 AI, 2 AO, 16 DI, 8 DO, DTs")

    async def update(self):
        self._tick += 1
        t = time.time() - self._start

        # ── Analog Inputs (12-bit 0–4095) ───────────────────────────────────
        for i, node in enumerate(self.ai_nodes):
            raw = int(2048 + 1800 * math.sin(t / (10 + i * 2) + i) + random.gauss(0, 8))
            raw = max(0, min(4095, raw))
            await node.write_value(raw)

        # ── Digital Inputs ───────────────────────────────────────────────────
        for i, node in enumerate(self.di_nodes):
            if random.random() < 0.04:
                val = await node.read_value()
                await node.write_value(not val)

        # ── Memory Integers: MI_0 = scan count, MI_1–MI_9 process vars ────────
        await self.mi_nodes[0].write_value(self._tick % 32767)
        for i in range(1, 10):
            await self.mi_nodes[i].write_value(int(1000 * math.sin(t / (5 + i) + i)) + 1000)

        # ── Memory Longs ─────────────────────────────────────────────────────
        await self.ml_nodes[0].write_value(self._tick)
        await self.ml_nodes[1].write_value(int(t * 1000))

        # ── Memory Bits: alternating pattern ─────────────────────────────────
        for i, node in enumerate(self.mb_nodes):
            await node.write_value(bool((self._tick // (i + 1)) % 2))

        # ── DT0 Process Data ─────────────────────────────────────────────────
        await self.pressure.write_value(round(101.3 + 5 * math.sin(t / 40) + random.gauss(0, 0.05), 2))
        await self.temp_proc.write_value(round(22.0 + 3 * math.sin(t / 25) + random.gauss(0, 0.1), 2))
        await self.humidity.write_value(round(max(0, min(100, 45.0 + 10 * math.sin(t / 60))), 1))
        await self.co2_ppm.write_value(round(412.0 + 20 * math.sin(t / 120) + random.gauss(0, 0.5), 1))
        ft = await self.flow_total.read_value()
        await self.flow_total.write_value(round(ft + abs(math.sin(t / 10)) * 0.01, 3))

        # ── DT1 Alarms: random alarm every ~5 minutes ─────────────────────────
        alarm_active = random.random() < 0.002
        if alarm_active:
            self._alarm_count += 1
            alarm_codes = {1: "High Pressure", 2: "Low Level", 3: "Temp Exceed", 4: "Flow Fault"}
            code = random.randint(1, 4)
            await self.alarm_active.write_value(True)
            await self.alarm_code.write_value(code)
            await self.alarm_text.write_value(alarm_codes.get(code, "Unknown Alarm"))
            await self.alarm_count.write_value(self._alarm_count)
        else:
            await self.alarm_active.write_value(False)
            await self.alarm_code.write_value(0)
            await self.alarm_text.write_value("No Alarm")

        # ── Scan Time ─────────────────────────────────────────────────────────
        await self.scan_time.write_value(round(2.4 + random.gauss(0, 0.1), 2))

        await self._kiln.update()


# ──────────────────────────────────────────────────────────────────────────────
# Server Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def _make_devices(device_configs: list):
    """Instantiate device objects from config list.  Returns list of (device, type_name, instance_num)."""
    counters = {}
    devices = []
    for entry in device_configs:
        dtype = entry["type"]
        counters[dtype] = counters.get(dtype, 0) + 1
        cls = DEVICE_CLASSES[dtype]()
        devices.append((cls(), dtype, counters[dtype]))
    return devices


def _get_session_count(server):
    """Return the number of active OPC-UA client sessions on a server."""
    try:
        isession_mgr = getattr(server, "iserver", None)
        if isession_mgr is None:
            return 0
        session_mgr = getattr(isession_mgr, "session_manager", None)
        if session_mgr is None:
            return 0
        active = getattr(session_mgr, "active_sessions", None)
        if active is None:
            return 0
        return len(active)
    except Exception:
        return 0


async def _shutdown_guard(servers):
    """Check if any server has connected clients. Returns True if safe to stop."""
    if not isinstance(servers, (list, tuple)):
        servers = [servers]
    total = sum(_get_session_count(s) for s in servers)
    if total > 0:
        print(f"\n  {_C.BYELLOW}{_C.BOLD}⚠  {total} client(s) still connected.{_C.RESET}")
        try:
            ans = input(f"  {_C.BWHITE}Force stop? (y/N): {_C.RESET}").strip().upper()
        except (EOFError, KeyboardInterrupt):
            ans = "Y"
        return ans == "Y"
    return True


async def run_single_server(host: str, port: int, update_interval: float, device_configs: list):
    """All devices on one server endpoint (same_ip mode)."""
    server = Server()
    await server.init()

    endpoint = f"opc.tcp://{host}:{port}/opcua/sim"
    server.set_endpoint(endpoint)
    server.set_server_name("OPC-UA Industrial Device Simulator")
    server.set_security_IDs(["Anonymous"])

    ns = await server.register_namespace("urn:nyle:opcua:simulator")
    root = server.nodes.objects

    devices = _make_devices(device_configs)
    log.info("Building OPC-UA node trees…")
    for dev, dtype, inst in devices:
        await dev.build(server, ns, root, instance=inst)

    dev_summary = {}
    for _, dtype, _ in devices:
        dev_summary[dtype] = dev_summary.get(dtype, 0) + 1
    parts = [f"{v}x {k}" for k, v in dev_summary.items()]

    log.info(f"\n{'='*60}")
    log.info(f"  OPC-UA Simulator running at: {endpoint}")
    log.info(f"  Namespace index : {ns}")
    log.info(f"  Update interval : {update_interval}s")
    log.info(f"  Devices ({len(devices)}): {', '.join(parts)}")
    log.info(f"{'='*60}\n")

    async with server:
        status_task = asyncio.create_task(_status_writer([
            {"server": server, "endpoint": endpoint,
             "device": ",".join(f"{v}x{k}" for k, v in dev_summary.items())}
        ]))
        try:
            stop_requested = False
            while not stop_requested:
                try:
                    for dev, _, _ in devices:
                        await dev.update()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.warning(f"Update error (will retry): {exc}")
                try:
                    await asyncio.sleep(update_interval)
                except asyncio.CancelledError:
                    if await _shutdown_guard(server):
                        break
                    # Client said no — keep running
                    log.info("Continuing… (clients still connected)")
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
            _clear_status_file()


def _has_net_admin() -> bool:
    """Return True if the current process already has CAP_NET_ADMIN.

    True for root, and for systemd services started with
    AmbientCapabilities=CAP_NET_ADMIN. When True, we skip 'sudo' for ip-addr
    commands (the service user can't sudo anyway).
    """
    if os.geteuid() == 0:
        return True
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("CapEff:"):
                    cap_eff = int(line.split()[1], 16)
                    return bool(cap_eff & (1 << 12))  # CAP_NET_ADMIN = 12
    except (OSError, ValueError):
        pass
    return False


def _ip_cmd(*args):
    """Prefix with sudo only when we lack CAP_NET_ADMIN."""
    return (["sudo"] if not _has_net_admin() else []) + list(args)


def _add_virtual_ips(interface: str, start_ip: str, count: int, prefix_len: int = 24):
    """Add virtual IP addresses to a network interface. Returns list of IPs added."""
    base = int(ipaddress.ip_address(start_ip))
    added_ips = []
    for i in range(count):
        ip = str(ipaddress.ip_address(base + i))
        cmd = _ip_cmd("ip", "addr", "add", f"{ip}/{prefix_len}", "dev", interface)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                added_ips.append(ip)
                log.info(f"  Added {ip}/{prefix_len} to {interface}")
            elif "File exists" in result.stderr or "already assigned" in result.stderr:
                # IP already assigned — still usable
                added_ips.append(ip)
                log.info(f"  {ip}/{prefix_len} already on {interface}")
            else:
                log.error(f"  Failed to add {ip}/{prefix_len}: {result.stderr.strip()}")
        except OSError as exc:
            log.error(f"  Failed to run ip command: {exc}")
    return added_ips


def _remove_virtual_ips(interface: str, ips: list, prefix_len: int = 24):
    """Remove virtual IP addresses from a network interface."""
    for ip in ips:
        cmd = _ip_cmd("ip", "addr", "del", f"{ip}/{prefix_len}", "dev", interface)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                log.info(f"  Removed {ip}/{prefix_len} from {interface}")
            else:
                log.warning(f"  Could not remove {ip}: {result.stderr.strip()}")
        except OSError:
            pass


async def run_ip_range_server(start_ip: str, port: int, update_interval: float,
                              device_configs: list, interface: str = None):
    """Each device on its own IP address (ip_range mode).
    Creates virtual IPs on the specified interface and cleans up on exit."""
    if not interface:
        raise RuntimeError("IP range mode requires an interface name. "
                           "Re-run the menu to configure one.")

    base = int(ipaddress.ip_address(start_ip))
    count = len(device_configs)

    # Add virtual IPs to the interface
    log.info(f"Setting up {count} virtual IPs on {interface}…")
    added_ips = _add_virtual_ips(interface, start_ip, count)
    if len(added_ips) != count:
        log.error(f"Only {len(added_ips)}/{count} IPs were added. "
                  "Check permissions (may need sudo) and interface name.")
        if not added_ips:
            _remove_virtual_ips(interface, added_ips)
            raise RuntimeError("No IPs could be added. Cannot start IP range mode.")

    devices_with_servers = []
    try:
        for i, entry in enumerate(device_configs):
            ip = str(ipaddress.ip_address(base + i))
            dtype = entry["type"]

            server = Server()
            await server.init()
            endpoint = f"opc.tcp://{ip}:{port}/opcua/sim"
            server.set_endpoint(endpoint)
            server.set_server_name(f"OPC-UA Simulator — {dtype} #{i+1}")
            server.set_security_IDs(["Anonymous"])

            ns = await server.register_namespace("urn:nyle:opcua:simulator")
            root = server.nodes.objects

            cls = DEVICE_CLASSES[dtype]()
            dev = cls()
            await dev.build(server, ns, root, instance=i + 1)

            devices_with_servers.append((server, dev, ip, dtype))
            log.info(f"  [{dtype} #{i+1}] → {endpoint}")

        log.info(f"\n{'='*60}")
        log.info(f"  OPC-UA Simulator — IP Range Mode")
        log.info(f"  {len(devices_with_servers)} servers on {start_ip} … "
                 f"{str(ipaddress.ip_address(base + count - 1))}:{port}")
        log.info(f"  Interface       : {interface}")
        log.info(f"  Update interval : {update_interval}s")
        log.info(f"{'='*60}\n")

        # Start all servers
        contexts = []
        for srv, _, _, _ in devices_with_servers:
            ctx = srv.__aenter__()
            await ctx
            contexts.append(srv)

        status_entries = [
            {"server": srv,
             "endpoint": f"opc.tcp://{ip}:{port}/opcua/sim",
             "device": dtype}
            for srv, _, ip, dtype in devices_with_servers
        ]
        status_task = asyncio.create_task(_status_writer(status_entries))

        try:
            stop_requested = False
            while not stop_requested:
                try:
                    for _, dev, _, _ in devices_with_servers:
                        await dev.update()
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.warning(f"Update error (will retry): {exc}")
                try:
                    await asyncio.sleep(update_interval)
                except asyncio.CancelledError:
                    all_servers = [s for s, _, _, _ in devices_with_servers]
                    if await _shutdown_guard(all_servers):
                        break
                    log.info("Continuing… (clients still connected)")
        finally:
            status_task.cancel()
            try:
                await status_task
            except asyncio.CancelledError:
                pass
            _clear_status_file()
            for srv in contexts:
                await srv.__aexit__(None, None, None)
    finally:
        # Always clean up virtual IPs
        log.info("Cleaning up virtual IPs…")
        _remove_virtual_ips(interface, added_ips)


async def run_from_config(cfg: dict):
    """Dispatch to the right server mode based on config."""
    if cfg["mode"] == "ip_range":
        await run_ip_range_server(
            cfg["start_ip"], cfg["port"], cfg["interval"],
            cfg["devices"], interface=cfg.get("interface"),
        )
    else:
        await run_single_server(cfg["host"], cfg["port"], cfg["interval"], cfg["devices"])


# ──────────────────────────────────────────────────────────────────────────────
# Runtime status: each running server writes connected-client info here so the
# menu (possibly in another process) can show who's connected.
# ──────────────────────────────────────────────────────────────────────────────

async def _status_writer(server_entries: list, interval: float = 2.0):
    """Periodically write connected-client snapshot to STATUS_FILE.

    server_entries: list of dicts: {"server": Server, "endpoint": str, "device": str}.
    """
    try:
        while True:
            snapshot = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(),
                "servers": [],
            }
            for entry in server_entries:
                clients = []
                transports = getattr(entry["server"].iserver, "asyncio_transports", []) or []
                for t in list(transports):
                    try:
                        peer = t.get_extra_info("peername")
                    except Exception:
                        peer = None
                    if peer:
                        clients.append({"address": peer[0], "port": peer[1]})
                snapshot["servers"].append({
                    "endpoint": entry["endpoint"],
                    "device": entry["device"],
                    "client_count": len(clients),
                    "clients": clients,
                })
            try:
                STATUS_FILE.write_text(json.dumps(snapshot, indent=2))
                # World-writable so the next run (possibly different user) can overwrite.
                try:
                    STATUS_FILE.chmod(0o666)
                except OSError:
                    pass
            except OSError as exc:
                log.debug(f"status write failed: {exc}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass


def _clear_status_file():
    """Remove the runtime status file on clean shutdown."""
    try:
        STATUS_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.debug(f"status unlink failed: {exc}")


def _read_status_file():
    """Read the status snapshot. Returns (data, age_sec) or (None, None)."""
    try:
        raw = STATUS_FILE.read_text()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None, None
    try:
        ts = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except (KeyError, ValueError):
        age = None
    return data, age


# ──────────────────────────────────────────────────────────────────────────────
# Systemd service management (menu actions)
# ──────────────────────────────────────────────────────────────────────────────

SERVICE_UNIT_TEMPLATE = """\
[Unit]
Description=OPC-UA Device Simulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory={workdir}
ExecStart={python} {script} --no-menu
Restart=on-failure
RestartSec=5
# CAP_NET_BIND_SERVICE: bind to ports <1024 if configured.
# CAP_NET_ADMIN:        add/remove virtual IPs in ip_range mode.
AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_ADMIN

[Install]
WantedBy=multi-user.target
"""


def _run(cmd, check=True, capture=True):
    """Run a subprocess, returning the CompletedProcess. Logs on failure."""
    try:
        result = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        print(f"  {_C.RED}Command not found: {exc}{_C.RESET}")
        return None
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if stderr:
            print(f"  {_C.RED}{' '.join(cmd)}{_C.RESET}\n  {_C.DIM}{stderr}{_C.RESET}")
    return result


def _service_exists():
    r = _run(["systemctl", "list-unit-files", SERVICE_NAME, "--no-legend"], check=False)
    return bool(r and r.stdout and SERVICE_NAME in r.stdout)


def _service_active():
    r = _run(["systemctl", "is-active", SERVICE_NAME], check=False)
    return bool(r and (r.stdout or "").strip() == "active")


def _service_enabled():
    r = _run(["systemctl", "is-enabled", SERVICE_NAME], check=False)
    return bool(r and (r.stdout or "").strip() == "enabled")


def _service_install(cfg):
    """Create the system user, copy script + config, write unit, enable service."""
    print(f"\n  {_C.CYAN}{_C.BOLD}── Install systemd service ──{_C.RESET}")
    print(f"  {_C.DIM}Plan:{_C.RESET}")
    print(f"    • Create system user '{SERVICE_USER}' (if missing)")
    print(f"    • Copy script + config to {SERVICE_INSTALL_DIR}")
    print(f"    • Write {SERVICE_UNIT_PATH}")
    print(f"    • Enable {SERVICE_NAME} at boot")
    print(f"  {_C.DIM}(uses sudo — you may be prompted){_C.RESET}\n")
    confirm = input(f"  {_C.BWHITE}Proceed? (y/N): {_C.RESET}").strip().lower()
    if confirm != "y":
        print(f"  {_C.YELLOW}Cancelled.{_C.RESET}")
        return

    # 1. Ensure config saved so the service has something to load
    save_config(cfg)

    # 2. Create system user if missing
    r = _run(["id", "-u", SERVICE_USER], check=False)
    user_exists = bool(r and r.returncode == 0)
    if not user_exists:
        useradd = _run([
            "sudo", "useradd", "--system", "--no-create-home",
            "--shell", "/usr/sbin/nologin", SERVICE_USER,
        ])
        if not useradd or useradd.returncode != 0:
            print(f"  {_C.RED}Failed to create user '{SERVICE_USER}'. Aborting.{_C.RESET}")
            return
        print(f"  {_C.GREEN}✓ Created system user {SERVICE_USER}{_C.RESET}")
    else:
        print(f"  {_C.DIM}User {SERVICE_USER} already exists.{_C.RESET}")

    # 3. Stage files into /opt/opcua-sim (system-readable; avoids /home traversal issue)
    src_script = Path(__file__).resolve()
    src_config = CONFIG_PATH

    if _run(["sudo", "mkdir", "-p", str(SERVICE_INSTALL_DIR)]).returncode != 0:
        return
    if _run(["sudo", "cp", str(src_script), str(SERVICE_INSTALL_DIR / "opcua_sim.py")]).returncode != 0:
        return
    if src_config.exists():
        _run(["sudo", "cp", str(src_config), str(SERVICE_INSTALL_DIR / "opcua_sim_config.json")])
    _run(["sudo", "chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", str(SERVICE_INSTALL_DIR)])
    _run(["sudo", "chmod", "755", str(SERVICE_INSTALL_DIR / "opcua_sim.py")])
    print(f"  {_C.GREEN}✓ Staged files in {SERVICE_INSTALL_DIR}{_C.RESET}")

    # 4. Pick the python that will run the service.
    # Prefer system python3 if it can already import asyncua as the service
    # user; otherwise create a venv inside the install dir.
    sys_py = subprocess.run(["which", "python3"], capture_output=True, text=True).stdout.strip() or "/usr/bin/python3"
    venv_dir = SERVICE_INSTALL_DIR / "venv"
    venv_py  = venv_dir / "bin" / "python"

    chk = subprocess.run(
        ["sudo", "-u", SERVICE_USER, sys_py, "-c", "import asyncua"],
        capture_output=True, text=True,
    )
    if chk.returncode == 0:
        py = sys_py
        print(f"  {_C.DIM}asyncua importable system-wide; using {py}{_C.RESET}")
    else:
        print(f"  {_C.DIM}asyncua not importable system-wide — creating venv at {venv_dir}…{_C.RESET}")
        # Create venv as the service user (it owns the install dir).
        mk = subprocess.run(
            ["sudo", "-u", SERVICE_USER, sys_py, "-m", "venv", str(venv_dir)],
            capture_output=True, text=True,
        )
        if mk.returncode != 0:
            stderr = (mk.stderr or "").strip()
            print(f"  {_C.RED}venv creation failed:{_C.RESET}\n  {_C.DIM}{stderr}{_C.RESET}")
            if "ensurepip" in stderr:
                print(f"  {_C.YELLOW}Install: sudo apt install python3-venv python3-full{_C.RESET}")
            return

        # Install asyncua inside the venv.
        print(f"  {_C.DIM}Installing asyncua into venv…{_C.RESET}")
        inst = subprocess.run(
            ["sudo", "-u", SERVICE_USER, str(venv_py), "-m", "pip", "install",
             "--quiet", "asyncua>=1.1.0"],
            capture_output=True, text=True,
        )
        if inst.returncode != 0:
            print(f"  {_C.RED}pip install failed:{_C.RESET}\n  {_C.DIM}{(inst.stderr or '').strip()}{_C.RESET}")
            return
        py = str(venv_py)
        print(f"  {_C.GREEN}✓ Venv ready: {py}{_C.RESET}")

    unit_text = SERVICE_UNIT_TEMPLATE.format(
        user=SERVICE_USER,
        workdir=str(SERVICE_INSTALL_DIR),
        python=py,
        script=str(SERVICE_INSTALL_DIR / "opcua_sim.py"),
    )

    # 5. Write unit file via sudo tee
    tee = subprocess.run(
        ["sudo", "tee", str(SERVICE_UNIT_PATH)],
        input=unit_text, text=True, capture_output=True,
    )
    if tee.returncode != 0:
        print(f"  {_C.RED}Failed to write unit file: {tee.stderr.strip()}{_C.RESET}")
        return
    _run(["sudo", "chmod", "644", str(SERVICE_UNIT_PATH)])
    print(f"  {_C.GREEN}✓ Wrote {SERVICE_UNIT_PATH}{_C.RESET}")

    # 6. Reload + enable
    _run(["sudo", "systemctl", "daemon-reload"])
    enable = _run(["sudo", "systemctl", "enable", SERVICE_NAME])
    if enable and enable.returncode == 0:
        print(f"  {_C.GREEN}✓ Enabled {SERVICE_NAME} at boot{_C.RESET}")


def _service_remove():
    """Stop, disable, and remove the systemd service. Optionally remove staged files / user."""
    if not _service_exists():
        print(f"  {_C.YELLOW}Service {SERVICE_NAME} is not installed.{_C.RESET}")
        return
    print(f"\n  {_C.CYAN}{_C.BOLD}── Remove systemd service ──{_C.RESET}")
    confirm = input(f"  {_C.BWHITE}Remove {SERVICE_NAME}? (y/N): {_C.RESET}").strip().lower()
    if confirm != "y":
        print(f"  {_C.YELLOW}Cancelled.{_C.RESET}")
        return

    _run(["sudo", "systemctl", "stop", SERVICE_NAME], check=False)
    _run(["sudo", "systemctl", "disable", SERVICE_NAME], check=False)
    _run(["sudo", "rm", "-f", str(SERVICE_UNIT_PATH)])
    _run(["sudo", "systemctl", "daemon-reload"])
    print(f"  {_C.GREEN}✓ Removed {SERVICE_NAME}{_C.RESET}")

    extra = input(f"\n  {_C.BWHITE}Also remove {SERVICE_INSTALL_DIR} and user '{SERVICE_USER}'? (y/N): {_C.RESET}").strip().lower()
    if extra == "y":
        _run(["sudo", "rm", "-rf", str(SERVICE_INSTALL_DIR)])
        _run(["sudo", "userdel", SERVICE_USER], check=False)
        print(f"  {_C.GREEN}✓ Removed staged files and user{_C.RESET}")


def _service_action(action: str):
    """action ∈ {start, stop, restart, status}."""
    if not _service_exists():
        print(f"  {_C.YELLOW}Service {SERVICE_NAME} is not installed yet.{_C.RESET}")
        return
    if action == "status":
        # Show status interactively (don't capture so the user sees colored output)
        subprocess.run(["systemctl", "status", SERVICE_NAME, "--no-pager"])
        return
    r = _run(["sudo", "systemctl", action, SERVICE_NAME])
    if r and r.returncode == 0:
        print(f"  {_C.GREEN}✓ systemctl {action} {SERVICE_NAME}{_C.RESET}")


def _service_menu(cfg):
    """Submenu for service install / remove / start / stop / restart / status."""
    while True:
        installed = _service_exists()
        active    = _service_active() if installed else False
        enabled   = _service_enabled() if installed else False

        if installed:
            state = (
                f"{_C.GREEN}active{_C.RESET}" if active else f"{_C.DIM}inactive{_C.RESET}"
            )
            boot  = (
                f"{_C.GREEN}enabled{_C.RESET}" if enabled else f"{_C.DIM}disabled{_C.RESET}"
            )
            status_line = f"installed, {state}, boot {boot}"
        else:
            status_line = f"{_C.DIM}not installed{_C.RESET}"

        print(f"\n  {_C.CYAN}{_C.BOLD}── Service ── {_C.RESET} {status_line}")
        print(f"    {_C.BWHITE}1.{_C.RESET} Install (enable at boot)")
        print(f"    {_C.BWHITE}2.{_C.RESET} Remove")
        print(f"    {_C.BWHITE}3.{_C.RESET} Start")
        print(f"    {_C.BWHITE}4.{_C.RESET} Stop")
        print(f"    {_C.BWHITE}5.{_C.RESET} Restart")
        print(f"    {_C.BWHITE}6.{_C.RESET} Status (full)")
        print(f"    {_C.BWHITE}B.{_C.RESET} Back")
        choice = input(f"  {_C.BWHITE}Select: {_C.RESET}").strip().upper()
        if   choice == "1": _service_install(cfg)
        elif choice == "2": _service_remove()
        elif choice == "3": _service_action("start")
        elif choice == "4": _service_action("stop")
        elif choice == "5": _service_action("restart")
        elif choice == "6": _service_action("status")
        elif choice == "B": return
        else:
            print(f"  {_C.RED}Invalid choice.{_C.RESET}")
            continue
        input(f"\n  {_C.DIM}Press Enter to continue…{_C.RESET}")


# ──────────────────────────────────────────────────────────────────────────────
# Firewall management (ufw)
# ──────────────────────────────────────────────────────────────────────────────

def _ufw_available():
    return subprocess.run(["which", "ufw"], capture_output=True).returncode == 0


def _ufw_port_open(port: int):
    """Return True if there's a ufw rule allowing this port (any proto)."""
    r = _run(["sudo", "ufw", "status"], check=False)
    if not r or r.returncode != 0:
        return False
    needle = f"{port}/tcp"
    return any(needle in line and "ALLOW" in line.upper() for line in (r.stdout or "").splitlines())


def _firewall_menu(cfg):
    if not _ufw_available():
        print(f"\n  {_C.CYAN}{_C.BOLD}── Firewall (ufw) ──{_C.RESET}")
        print(f"  {_C.RED}ufw is not installed.{_C.RESET}")
        ans = input(f"  {_C.BWHITE}Install it now with apt? (y/N): {_C.RESET}").strip().lower()
        if ans == "y":
            r = _run(["sudo", "apt", "install", "-y", "ufw"], capture=False)
            if r and r.returncode == 0:
                print(f"  {_C.GREEN}✓ ufw installed{_C.RESET}")
            else:
                print(f"  {_C.RED}Install failed. Try manually: sudo apt install ufw{_C.RESET}")
                input(f"\n  {_C.DIM}Press Enter to continue…{_C.RESET}")
                return
        else:
            input(f"\n  {_C.DIM}Press Enter to continue…{_C.RESET}")
            return
    port = int(cfg.get("port", 4840))
    while True:
        is_open = _ufw_port_open(port)
        state = f"{_C.GREEN}open{_C.RESET}" if is_open else f"{_C.DIM}closed{_C.RESET}"
        print(f"\n  {_C.CYAN}{_C.BOLD}── Firewall (ufw) ──{_C.RESET}  port {port}/tcp: {state}")
        print(f"    {_C.BWHITE}1.{_C.RESET} Open port {port}/tcp")
        print(f"    {_C.BWHITE}2.{_C.RESET} Close port {port}/tcp (revert)")
        print(f"    {_C.BWHITE}3.{_C.RESET} Show ufw status")
        print(f"    {_C.BWHITE}B.{_C.RESET} Back")
        choice = input(f"  {_C.BWHITE}Select: {_C.RESET}").strip().upper()
        if choice == "1":
            r = _run([
                "sudo", "ufw", "allow", f"{port}/tcp",
                "comment", FIREWALL_COMMENT,
            ])
            if r and r.returncode == 0:
                print(f"  {_C.GREEN}✓ Opened {port}/tcp{_C.RESET}")
        elif choice == "2":
            r = _run(["sudo", "ufw", "delete", "allow", f"{port}/tcp"])
            if r and r.returncode == 0:
                print(f"  {_C.GREEN}✓ Closed {port}/tcp{_C.RESET}")
        elif choice == "3":
            subprocess.run(["sudo", "ufw", "status", "verbose"])
        elif choice == "B":
            return
        else:
            print(f"  {_C.RED}Invalid choice.{_C.RESET}")
            continue
        input(f"\n  {_C.DIM}Press Enter to continue…{_C.RESET}")


# ──────────────────────────────────────────────────────────────────────────────
# Connected-client viewer
# ──────────────────────────────────────────────────────────────────────────────

def _show_connected_clients():
    print(f"\n  {_C.CYAN}{_C.BOLD}── Connected Clients ──{_C.RESET}")
    data, age = _read_status_file()
    if data is None:
        print(f"  {_C.YELLOW}No status file at {STATUS_FILE} — is the simulator running?{_C.RESET}")
        return
    if age is not None and age > 10:
        print(f"  {_C.YELLOW}⚠ Status snapshot is {int(age)}s old — server may be stopped.{_C.RESET}")
    else:
        print(f"  {_C.DIM}Snapshot age: {int(age) if age is not None else '?'}s   pid: {data.get('pid')}{_C.RESET}")

    servers = data.get("servers", [])
    if not servers:
        print(f"  {_C.DIM}No servers reporting.{_C.RESET}")
        return

    for s in servers:
        ep = s.get("endpoint", "?")
        dev = s.get("device", "?")
        clients = s.get("clients", [])
        header_color = _C.GREEN if clients else _C.DIM
        print(f"\n  {header_color}{ep}{_C.RESET}  {_C.DIM}[{dev}]{_C.RESET}  → {len(clients)} client(s)")
        for c in clients:
            print(f"    • {c.get('address')}:{c.get('port')}")


# ──────────────────────────────────────────────────────────────────────────────
# Entry Point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OPC-UA simulator — Opto22 groov, Siemens S7-1200, Unitronics PLC"
    )
    parser.add_argument("--host",     default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",     type=int, default=4840, help="OPC-UA port (default: 4840)")
    parser.add_argument("--interval", type=float, default=1.0, help="Update interval in seconds (default: 1.0)")
    parser.add_argument("--no-menu",  action="store_true", help="Skip menu, use saved config or defaults")
    args = parser.parse_args()

    if args.no_menu:
        cfg = load_config()
        if not cfg:
            cfg = {
                "devices": [{"type": "opto22"}, {"type": "siemens"}, {"type": "unitronics"}],
                "mode": "same_ip",
                "host": args.host,
                "start_ip": args.host,
                "port": args.port,
                "interval": args.interval,
            }
        try:
            asyncio.run(run_from_config(cfg))
        except KeyboardInterrupt:
            log.info("Simulator stopped.")
        return

    # Interactive menu mode — Ctrl+C during server returns to menu
    while True:
        try:
            cfg = show_menu()
        except KeyboardInterrupt:
            print("\n")
            log.info("Exiting.")
            break

        if cfg is None:
            # User chose Quit
            log.info("Exiting.")
            break

        try:
            asyncio.run(run_from_config(cfg))
        except KeyboardInterrupt:
            print("\n")
            log.info("Server stopped. Returning to menu…")
            continue


if __name__ == "__main__":
    main()
