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

- **One-command launcher** — `./start.sh` checks Python, creates a venv if `asyncua` is missing (handles PEP 668 / `externally-managed-environment` on Debian / Raspberry Pi OS), then launches the simulator
- **Interactive menu** — configure device types, counts, network mode, host, port, and update interval from a color TUI; config is persisted to `opcua_sim_config.json` and reloaded automatically
- **Built-in systemd integration** — install/remove a `opcua-sim` service from the menu (runs as a dedicated `opcua` system user out of `/opt/opcua-sim/`, with `CAP_NET_ADMIN` for virtual-IP creation); start/stop/restart/status from the same submenu
- **Built-in firewall (`ufw`) management** — open or revert the configured TCP port from the menu; offers `apt install ufw` if it isn't present
- **Connected-client viewer** — the running server writes a snapshot of every connected OPC-UA session (per endpoint) to `/tmp/opcua_sim_status.json`; the menu's **C** option pretty-prints it
- **Up to 12 devices** — mix and match Opto22, Siemens, and Unitronics nodes in any combination
- **Two network modes** — run all devices on a single endpoint (`same_ip`), or assign each device its own virtual IP from a range (`ip_range` — auto-creates/cleans up virtual IPs on a chosen interface)
- **Accurate node hierarchies** — each device's tree mirrors what you actually browse on the real hardware, not a generic flat layout
- **Live simulation** — analog values follow sine waves with Gaussian noise, digital inputs toggle randomly, process loops (tank/flow/temp, PID FB) run continuously
- **Writable nodes** — outputs, setpoints, and operands accept writes from any OPC-UA client; the simulator holds the value until its next update cycle
- **CODESYS 3.5 structure** — groov RIO uses the real `DeviceSet/Resources/Application/` hierarchy with GVL Hungarian prefixes (`rAI_Channel_0`, `xDI_0`), task diagnostics, and an exposed `FB_PID_Inst`
- **Siemens DB simulation** — `DB1_ProductionData` with a coupled pump/valve/tank/temp process loop; `DB2_Diagnostics` with CPU load and cycle time; running `TON` timers and `CTU` counters
- **Unitronics operand model** — MI / ML / MB memory operands, system bits (SB2/SB3/SB5), 12-bit raw AI counts, data tables (DT0 process, DT1 alarms)
- **Kiln controller tags** — every device includes a `KilnController/` folder with ~140 tags: setpoints, temperatures, schedule arrays (41-step), VFD/HRV motor arrays, power/energy metering, demand response, runtime counters, and unit identification
- **Graceful shutdown** — Ctrl+C warns if OPC-UA clients are still connected and offers to keep running or force stop; returns to the menu in interactive mode
- **Anonymous / no-security** by default — suitable for lab/VPN-isolated networks; security policies configurable via `asyncua`
- No external dependencies beyond `asyncua`

---

## Requirements

- Python 3.9 or newer
- `asyncua >= 1.1.0` (installed automatically by `./start.sh` if missing)
- Linux for `ip_range` mode (uses `ip addr add` to create virtual IPs); other modes work cross-platform
- `python3-venv` recommended on Debian / Raspberry Pi OS (the launcher uses a venv to satisfy PEP 668)

---

## Quick Start

```bash
git clone https://github.com/mitchsowa/opcua-device-simulator.git
cd opcua-device-simulator
./start.sh
```

`start.sh` checks for `asyncua`; if it's missing it creates `.venv/` and installs it there, then launches the simulator. On the first run with no saved config you get **8 Siemens PLCs in `ip_range` mode starting at `192.168.10.108`** — adjust via the menu, then press **R** to save and run.

Connect any OPC-UA client to:
```
opc.tcp://<host>:4840/opcua/sim          # same_ip mode
opc.tcp://192.168.10.108:4840/opcua/sim  # ip_range mode (one endpoint per IP)
...
opc.tcp://192.168.10.115:4840/opcua/sim
```

---

## Usage

```
./start.sh [-- args forwarded to opcua_sim.py]
python3 opcua_sim.py [--host HOST] [--port PORT] [--interval SECONDS] [--no-menu]

Options:
  --host      Bind address          (default: 0.0.0.0)
  --port      OPC-UA port           (default: 4840)
  --interval  Update interval in s  (default: 1.0)
  --no-menu   Skip menu, use saved config or defaults
```

### Interactive mode (default)

Running `./start.sh` opens a text menu:

```
1. Device selection      8 total — 8x Siemens S7-1200
2. Network mode          IP Range (one server per IP)
3. Host / IP range       192.168.10.108 → 192.168.10.115  port 4840
4. Port                  4840
5. Update interval       1.0s
──────────────────────────────────────────────────────
S. Service              install / remove / start / stop / restart
F. Firewall (ufw)       open / close port 4840/tcp
C. Connected clients    show who's connected to each PLC
──────────────────────────────────────────────────────
R. Run                  Q. Quit
```

- **1 – 5** edit device/network settings.
- **S** opens the systemd submenu — install (registers `opcua-sim.service`, creates the `opcua` system user, copies the script + current config to `/opt/opcua-sim/`, creates a venv there if needed, enables at boot), remove, start/stop/restart, or show full status.
- **F** opens or closes the configured port in `ufw`. Offers to `apt install ufw` first if it isn't installed.
- **C** reads `/tmp/opcua_sim_status.json` (written every 2 s by the running server) and lists each endpoint with the IP:port of every connected client.
- **R** saves config and starts the server. Ctrl+C with clients connected prompts before stopping; otherwise returns to the menu.
- **Q** quits.

### Headless mode

```bash
# Use saved config (or defaults: 8x Siemens in ip_range mode @ 192.168.10.108)
./start.sh --no-menu

# Override port/interval (host/start_ip come from saved config or defaults)
./start.sh --no-menu --port 4841 --interval 0.5
```

### Network modes

**Same IP** — all devices share a single OPC-UA endpoint. Device trees are isolated under the root Objects folder.

**IP Range** — each device gets its own virtual IP on a chosen network interface. The simulator adds virtual IPs on startup (`ip addr add`) and removes them on shutdown. Interactive runs use `sudo`; the bundled systemd service has `CAP_NET_ADMIN` ambient capability and adds them without sudo. Example: 8 devices starting at `192.168.10.108` creates endpoints on `.108 – .115`.

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

### Kiln Controller Tags (all devices)

Every device includes a `KilnController/` sub-folder with the following groups. This models a lumber dry kiln controller with realistic simulated values.

```
KilnController/
├── UnitIdentification/
│     TableVersion(1)  SerialNumber  PartNumber  UnitType  UnitId
│     ControlVersion  HmiVersion  CompileDate  ManufactureDate
│     Customer  SiteAddress  GPSCoordinates
├── Setpoints/                                    (all writable)
│     DryBulbSetpoint(160°F)  WetBulbSetpoint(140°F)
│     EmcSetpoint(12%)  RhSetpoint(65%)
│     DryBulbDeadband  WetBulbDeadband  McDeadband  RhDeadband
│     SuctionLineSetpoint  OperatingMode  SprayMode  HeatMode
│     RefrigMode  VentMode  HrvMode  ManualVentPercent
│     VentImplosionSecs  FanManualSpeed  FanMode  HrvSpeed
│     HrvDiff  VentDiff  GasEnable  LotId
├── TemperaturesSensors/                          (simulated, drift toward setpoints)
│     CtrlDryBulb  FwdDryBulb  RevDryBulb
│     CtrlWetBulb  FwdWetBulb  RevWetBulb
│     McCtrl  McFwd  McRev  RhCtrl  RhFwd  RhRev
│     Dlp  Slp  Slt  Temp1..Temp5
├── Commands/                                     (all writable)
│     Start  Stop  Pause  DrEnable  DrMode
│     ScheduleSkipForward  ScheduleSkipBackward
├── StatusDisplay/
│     CycleStatus  CurrentStep  FanStatus  HeatStatus  VentStatus
│     SprayStatus  BlowerStatus  CompStatus1  DamperPct(0-100%)
│     AlarmActive1..3  LightStack  LightStackStatus
├── Schedule/
│     ScheduleEnabled (W)  ScheduleFinished  RecipeCompletionTime
│     ScheduleStop (W)  SchedulePause (W)
│     StepElapsedTime  StepRemainingTime
├── ScheduleArrays/                               (ARRAY[0..40], writable, 5 steps populated)
│     StepTime  StepMode  StepDbTemp  StepRh  StepMc
│     StepExhaust  StepIntake  StepFan  StepWbSetpoint  StepPctSetpoint
│     RampCtrl  RampTime  SprayCtrl  HtCtrl  HtSet  HtTime
│     OverTempSet  HeaterCutOutSet  MoistureAddSet  MoistureShedSet
│     DcCtrl  DcHeatTime  DcVentTime  DcRestTime  DcRepeat  DcRestFanOff
│     EstStartTime  EstCompTime
├── VfdHrv/                                       (ARRAY[0..7], 2 drives active)
│     VfdFreq  VfdCurrent  VfdRpm  VfdFault  VfdStatus  VfdTemp
│     VfdFaultReset (W)
│     HrvFreq  HrvCurrent  HrvRpm  HrvFaultNumber  HrvFanFault
│     HrvStatus  HrvInlet  HrvExhaust  HrvVfdTemp  HrvVfdFaultReset (W)
│     KilnIntake  KilnExhaust
├── TotalsRuntime/                                (incrementing counters)
│     TotalRunTimeMinutes  FanRt  TotHeatOn  TotVentOn  TotRefrigOn
├── PowerEnergy/                                  (sinusoidal 35-50 kW, derived values)
│     ActivePower  ActiveFundPower  ActiveHarmonicPower
│     ApparentPower  ReactivePower  PowerFactor
│     RmsVoltage  RmsCurrent  VoltagePeak  CurrentPeak
│     MeanPhaseAngle  VoltagePhaseAngle
│     ForwardActiveEnergy  ForwardActiveFundEnergy  ForwardActiveHarmonicEnergy
│     ReverseActiveEnergy  ReverseActiveFundEnergy  ReverseActiveHarmonicEnergy
│     ForwardReactiveEnergy  ReverseReactiveEnergy  ApparentEnergy
├── DemandResponse/
│     DemandResponseEnabled (W)  DemandResponseMode (W)  DemandLimit (W)
│     CurrentDemand  DemandShed  DemandShedPercent
│     DemandResponsePackage  DrEventActive  DrRemainingTime
└── Misc/
      OneSecondPulse
```

**Simulation behavior:**
- Temperatures converge toward writable setpoints with Gaussian noise; forward sensors read slightly above setpoint, reverse slightly below
- Moisture content (MC) follows a slow downward drying curve modulated by a sine wave
- Power varies sinusoidally (35–50 kW); apparent power, power factor, and RMS current are derived physically (`S = P / PF`, `I = P / (V × PF)`)
- Energy accumulators (kWh, kVArh, kVAh) integrate continuously
- VFD/HRV arrays simulate motor noise on the first 2 drives; temps drift with a slow sine
- Runtime counters increment once per minute
- Demand response tracks active power; shed is calculated when DR is enabled and demand exceeds the limit

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

The menu's **S** option installs and manages a systemd unit for you. No need to hand-write one.

```
./start.sh   →   S → 1 (Install)
```

Install does the following (you'll be prompted with `sudo` once):

1. Creates the system user `opcua` (`useradd --system --no-create-home --shell /usr/sbin/nologin`)
2. Stages `opcua_sim.py` and the current `opcua_sim_config.json` in `/opt/opcua-sim/` owned by `opcua` (this avoids `/home/<user>/` traversal issues when the service runs as a different user)
3. If the system Python can't import `asyncua` as the `opcua` user, creates `/opt/opcua-sim/venv/` and installs `asyncua` into it; uses that interpreter in the unit file
4. Writes `/etc/systemd/system/opcua-sim.service` with `AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_ADMIN` (so the service can bind low ports and create virtual IPs without `sudo`)
5. `daemon-reload` + `systemctl enable`

The generated unit looks like:

```ini
[Unit]
Description=OPC-UA Device Simulator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=opcua
Group=opcua
WorkingDirectory=/opt/opcua-sim
ExecStart=/opt/opcua-sim/venv/bin/python /opt/opcua-sim/opcua_sim.py --no-menu
Restart=on-failure
RestartSec=5
AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_ADMIN

[Install]
WantedBy=multi-user.target
```

Other items in the **S** submenu:

| Option | Action |
|---|---|
| 1. Install | Idempotent — also refreshes the staged script + config when re-run |
| 2. Remove | Stops, disables, removes the unit file; optionally removes `/opt/opcua-sim/` and the `opcua` user |
| 3. Start | `systemctl start opcua-sim` |
| 4. Stop | `systemctl stop opcua-sim` |
| 5. Restart | `systemctl restart opcua-sim` |
| 6. Status | Full `systemctl status` output |

**Note:** edits made to `opcua_sim.py` or the config after install don't propagate automatically — re-run **S → 1** to push them into `/opt/opcua-sim/`.

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
├── start.sh                  # Launcher — ensures Python + asyncua (creates venv if needed)
├── opcua_sim_config.json     # Saved config (auto-generated, gitignored)
├── requirements.txt          # Python dependencies
├── setup.py                  # Install as package / console script (optional)
├── Dockerfile                # Container image
├── .venv/                    # Auto-created if system Python is PEP 668-protected (gitignored)
├── .gitignore
└── README.md
```

**Runtime files** (not in the repo):

| Path | Purpose |
|---|---|
| `opcua_sim_config.json` | Persisted menu config (next to `opcua_sim.py`) |
| `/tmp/opcua_sim_status.json` | Per-server connected-client snapshot, refreshed every 2 s |
| `/opt/opcua-sim/` | Staged script + config when running as a systemd service |
| `/etc/systemd/system/opcua-sim.service` | Generated unit file |

---

## Contributing

Pull requests welcome. To add a new simulated device:

1. Create a class following the existing pattern (`__init__`, `build`, `update`)
2. Add it to `DEVICE_TYPES` and `DEVICE_CLASSES` in `opcua_sim.py`
3. Document the node tree in this README

---

## License

MIT — see [LICENSE](LICENSE) for details.
