# opcua-device-simulator

A Python OPC-UA server that simulates realistic node trees for three common industrial devices:

| Device | OPC-UA Provider |
|---|---|
| **Opto22 groov RIO** | CODESYS 3.5 SoftPLC runtime |
| **Siemens S7-1200** | TIA Portal V4.5 built-in OPC-UA server |
| **Unitronics UniStream** | USC/Vision series native OPC-UA server |

Configure up to 12 device nodes (mixed or same type) via an interactive text menu. Two network modes: all devices on a single endpoint, or each on its own IP from a range. Config is saved to `opcua_sim_config.json` and reloaded on start. Designed for developing and testing OPC-UA clients, dashboards, and data pipelines without physical hardware.

---

## Features

- **Interactive menu** — configure device types, counts, network mode, host, port, and update interval from a color TUI; config is persisted to `opcua_sim_config.json` and reloaded automatically
- **Up to 12 devices** — mix and match Opto22, Siemens, and Unitronics nodes in any combination
- **Two network modes** — run all devices on a single endpoint (`same_ip`), or assign each device its own virtual IP from a range (`ip_range` — auto-creates/cleans up virtual IPs on a chosen interface)
- **Accurate node hierarchies** — each device's tree mirrors what you actually browse on the real hardware, not a generic flat layout
- **Live simulation** — analog values follow sine waves with Gaussian noise, digital inputs toggle randomly, process loops (tank/flow/temp, PID FB) run continuously
- **Writable nodes** — outputs, setpoints, and operands accept writes from any OPC-UA client; the simulator holds the value until its next update cycle
- **CODESYS 3.5 structure** — groov RIO uses the real `DeviceSet/Resources/Application/` hierarchy with GVL Hungarian prefixes (`rAI_Channel_0`, `xDI_0`), task diagnostics, and an exposed `FB_PID_Inst`
- **Siemens DB simulation** — `DB1_ProductionData` with a coupled pump/valve/tank/temp process loop; `DB2_Diagnostics` with CPU load and cycle time; running `TON` timers and `CTU` counters
- **Unitronics operand model** — MI / ML / MB memory operands, system bits (SB2/SB3/SB5), 12-bit raw AI counts, data tables (DT0 process, DT1 alarms)
- **Graceful shutdown** — Ctrl+C warns if OPC-UA clients are still connected and offers to keep running or force stop; returns to the menu in interactive mode
- **Anonymous / no-security** by default — suitable for lab/VPN-isolated networks; security policies configurable via `asyncua`
- No external dependencies beyond `asyncua`

---

## Requirements

- Python 3.9 or newer
- `asyncua >= 1.1.0`

```bash
pip install -r requirements.txt
```

---

## Quick Start

```bash
git clone https://github.com/mitchsowa/opcua-device-simulator.git
cd opcua-device-simulator
pip install -r requirements.txt
python opcua_sim.py
```

The interactive menu lets you pick device types, counts, network mode, and connection settings. Config is saved to `opcua_sim_config.json` and reloaded next time you run.

Then connect any OPC-UA client to:
```
opc.tcp://<host>:4840/opcua/sim
```

---

## Usage

```
python opcua_sim.py [--host HOST] [--port PORT] [--interval SECONDS] [--no-menu]

Options:
  --host      Bind address          (default: 0.0.0.0)
  --port      OPC-UA port           (default: 4840)
  --interval  Update interval in s  (default: 1.0)
  --no-menu   Skip menu, use saved config or defaults
```

### Interactive mode (default)

Running `python opcua_sim.py` opens a text menu where you can:

1. **Configure devices** — choose a device type and count (up to 12), or build a mixed list
2. **Set network mode** — `same_ip` (all on one endpoint) or `ip_range` (one server per IP)
3. **Set host / IP range** — bind address or starting IP for range mode
4. **Set port and update interval**

Press **R** to save config and start the server, **Q** to quit. Ctrl+C during the server returns to the menu.

### Headless mode

```bash
# Use saved config (or defaults: one of each device on 0.0.0.0:4840)
python opcua_sim.py --no-menu

# Override host/port/interval
python opcua_sim.py --no-menu --host 127.0.0.1 --port 4841 --interval 0.5
```

### Network modes

**Same IP** — all devices share a single OPC-UA endpoint. Device trees are isolated under the root Objects folder.

**IP Range** — each device gets its own virtual IP on a chosen network interface. The simulator adds virtual IPs on startup (requires `sudo` for `ip addr add`) and removes them on shutdown. Example: 4 devices starting at `192.168.1.10` creates endpoints on `.10`, `.11`, `.12`, `.13`.

---

## Node Tree Reference

### Opto22 groov RIO — CODESYS 3.5

The groov RIO OPC-UA server is provided by the CODESYS 3.5 SoftPLC runtime, not groov Manage.
CODESYS exposes variables through its `DeviceSet` hierarchy. Only variables included in the
Symbol Configuration (or tagged `{attribute 'OPC_UA_ACCESS'}`) are browsable.

```
Objects/
└── DeviceSet/
    └── groov-RIO-001/
        ├── ServerInfo/
        │     RuntimeVersion "V3.5.19.40"  TargetVendor "Opto 22"
        │     DeviceName  NodeName  IPAddress  MACAddress  TargetId
        └── Resources/
            └── Application/
                ├── Info/
                │     ApplicationName  ApplicationState  ChangeTime  ProjectAuthor
                ├── Tasks/
                │   ├── MainTask/   CycleTime_us(10 ms)  LastExecTime_us  Jitter_us  CycleCount
                │   └── SlowTask/  CycleTime_us(100 ms) LastExecTime_us  CycleCount
                ├── GlobalVars/
                │   ├── GVL_AnalogIO/
                │   │     rAI_Channel_0..7   REAL  (0.0-100.0 % eng. units, live)
                │   │     rAI_mA_0..7        REAL  (4.0-20.0 mA raw, live)
                │   │     rAO_Channel_0..3   REAL  (writable setpoint 0-100 %)
                │   ├── GVL_DigitalIO/
                │   │     xDI_0..15  BOOL    xDO_0..7  BOOL (W)
                │   └── GVL_System/
                │         rUptime_s  xWatchdogOK  xPowerOK  xEthernetLink
                │         sLastError  uiErrorCount
                └── Programs/
                    ├── PLC_PRG/
                    │     rTankLevel_mm  rFlowRate_Ls  rTemp_C
                    │     xAutoMode (W)  xAlarmActive  iAlarmCode  sAlarmText
                    └── FB_PID_Inst/
                          rSetpoint (W)  rActual  rOutput  rError
                          xLimitHigh  xLimitLow
```

Variable naming follows IEC 61131-3 Hungarian prefixes as used in CODESYS:
`r` = REAL &nbsp;·&nbsp; `x` = BOOL &nbsp;·&nbsp; `i` = INT &nbsp;·&nbsp; `s` = STRING &nbsp;·&nbsp; `ui` = UINT

---

### Siemens S7-1200 — TIA Portal V4.5

```
Objects/
└── Siemens_S7_1200/
    ├── DeviceInfo/
    │     OrderNumber "6ES7 214-1AG40-0XB0"  HWRevision  FWVersion "V4.5"
    │     ModuleName "CPU 1214C DC/DC/DC"  IPAddress  Rack  Slot
    ├── Inputs/
    │     I_0_0..I_0_7  BOOL  (%I0.0-%I0.7, walking bit + noise)
    │     IW_64          INT   (%IW64 analog word, 0-27648 engineering counts)
    ├── Outputs/
    │     Q_0_0..Q_0_7  BOOL (W)   QW_64  INT (W)
    ├── DB1_ProductionData/
    │     TankLevel_mm    REAL  (coupled process loop)
    │     FlowRate_Ls     REAL
    │     TotalVolume_L   REAL
    │     TempSetpoint_C  REAL (W)
    │     TempActual_C    REAL
    │     PumpRunning     BOOL (W)
    │     ValveOpen       BOOL (W)
    │     BatchCount      INT
    ├── DB2_Diagnostics/
    │     CycleTime_ms  CPULoad_pct  MemUsed_bytes  ErrorCode  ErrorText
    ├── Timers/
    │     T1_PT_ms (W)  T1_ET_ms  T1_Running
    │     T2_PT_ms (W)  T2_ET_ms  T2_Running
    ├── Counters/
    │     C1_Value  C1_Preset (W)  C1_Done
    │     C2_Value  C2_Preset (W)  C2_Done
    └── PLCStatus/
          OperatingMode  RunHours  DiagBuffer  LED_RUN  LED_STOP  LED_ERROR
```

`IW_64` uses 0–27648 scaling matching Siemens analog normalization (`NORM_X` / `SCALE_X`).
Timers simulate `TON` PT/ET/Q behavior. Counters simulate `CTU` with configurable preset.

---

### Unitronics UniStream — USC/Vision

```
Objects/
└── Unitronics_PLC/
    ├── DeviceInfo/
    │     Model "USC-B10-T24"  FirmwareVersion "V1.35.15"
    │     ProjectName  IPAddress  SerialNumber
    ├── Operands/
    │   ├── MemoryIntegers/   MI_0..MI_9   INT  (W)
    │   ├── MemoryLongs/      ML_0..ML_4   DINT (W)
    │   ├── MemoryBits/       MB_0..MB_15  BOOL (W)
    │   └── SystemBits/       SB2_RunMode  SB3_FirstScan  SB5_PowerUpFlag
    ├── AnalogIO/
    │     AI_0..AI_5   INT  (0-4095, 12-bit raw ADC counts)
    │     AO_0..AO_1   INT  (W)
    ├── DigitalIO/
    │     I_0..I_15  BOOL    O_0..O_7  BOOL (W)
    ├── DT0_Process/
    │     Pressure_kPa  Temperature_C  Humidity_pct  CO2_ppm  FlowTotal_m3
    ├── DT1_Alarms/
    │     AlarmActive  AlarmCode  AlarmText  AlarmCount
    └── PLC_Status/
          RunMode  ScanTime_ms  ProjectCRC  BatteryOK  SDCardOK
```

AI values are raw 12-bit counts (0–4095).
Scale to engineering units in your client: `EU = (raw / 4095.0) * span + offset`

> **(W)** = writable node — accepts OPC-UA Write from a client.

---

## Docker

```bash
docker build -t opcua-sim .
docker run -p 4840:4840 opcua-sim --no-menu
```

Custom update interval:
```bash
docker run -p 4840:4840 opcua-sim --no-menu --interval 0.5
```

---

## Running as a systemd Service

```ini
# /etc/systemd/system/opcua-sim.service
[Unit]
Description=OPC-UA Device Simulator
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/opcua-device-simulator/opcua_sim.py --no-menu --port 4840
WorkingDirectory=/opt/opcua-device-simulator
Restart=on-failure
RestartSec=5
User=nobody

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now opcua-sim
sudo systemctl status opcua-sim
```

---

## Enabling Security (Optional)

The server starts with **Anonymous / No Security** — appropriate for a lab bench or VPN-isolated
OT network. To add encryption or username authentication, modify `run_single_server()` or `run_ip_range_server()` in `opcua_sim.py`:

```python
from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256

await server.set_security_policy([
    ua.SecurityPolicyType.NoSecurity,
    ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt,
])
await server.load_certificate("server_cert.der")
await server.load_private_key("server_key.pem")
```

See the [python-asyncua security docs](https://python-asyncua.readthedocs.io/en/latest/server.html#security) for certificate generation instructions.

---

## Tested OPC-UA Clients

| Client | Notes |
|---|---|
| [UA Expert](https://www.unified-automation.com/products/development-tools/uaexpert.html) | Full browse, subscribe, write — recommended for development |
| [Prosys OPC UA Browser](https://prosysopc.com/products/opc-ua-browser/) | Good for quick node inspection |
| [python-asyncua](https://github.com/FreeOpcUa/opcua-asyncio) client | Use for automated testing of your own client code |
| [Ignition](https://inductiveautomation.com/) | Add as an OPC-UA device; all tags import via browse |
| Node-RED (`node-red-contrib-opcua`) | Works with subscribe nodes on all three device trees |

---

## Project Structure

```
opcua-device-simulator/
├── opcua_sim.py              # Simulator — device classes, menu, server modes
├── opcua_sim_config.json     # Saved config (auto-generated, gitignored)
├── requirements.txt          # Python dependencies
├── setup.py                  # Install as package / console script (optional)
├── Dockerfile                # Container image
├── .gitignore
└── README.md
```

---

## Contributing

Pull requests welcome. To add a new simulated device:

1. Create a class following the existing pattern (`__init__`, `build`, `update`)
2. Add it to `DEVICE_TYPES` and `DEVICE_CLASSES` in `opcua_sim.py`
3. Document the node tree in this README

---

## License

MIT — see [LICENSE](LICENSE) for details.
