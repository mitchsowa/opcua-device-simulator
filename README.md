# opcua-device-simulator

A Python OPC-UA server that simulates realistic node trees for three common industrial devices:

| Device | OPC-UA Provider | Simulated Address |
|---|---|---|
| **Opto22 groov RIO** | CODESYS 3.5 SoftPLC runtime | `192.168.1.10` |
| **Siemens S7-1200** | TIA Portal V4.5 built-in OPC-UA server | `192.168.1.20` |
| **Unitronics UniStream** | USC/Vision series native OPC-UA server | `192.168.1.30` |

All three devices share a single server endpoint but have isolated object trees — exactly as they would appear in UA Expert, an Ignition historian, or a SCADA data logger. Designed for developing and testing OPC-UA clients, dashboards, and data pipelines without physical hardware.

---

## Features

- **Accurate node hierarchies** — each device's tree mirrors what you actually browse on the real hardware, not a generic flat layout
- **Live simulation** — analog values follow sine waves with Gaussian noise, digital inputs toggle randomly, process loops (tank/flow/temp, PID FB) run continuously
- **Writable nodes** — outputs, setpoints, and operands accept writes from any OPC-UA client; the simulator holds the value until its next update cycle
- **CODESYS 3.5 structure** — groov RIO uses the real `DeviceSet/Resources/Application/` hierarchy with GVL Hungarian prefixes (`rAI_Channel_0`, `xDI_0`), task diagnostics, and an exposed `FB_PID_Inst`
- **Siemens DB simulation** — `DB1_ProductionData` with a coupled pump/valve/tank/temp process loop; `DB2_Diagnostics` with CPU load and cycle time; running `TON` timers and `CTU` counters
- **Unitronics operand model** — MI / ML / MB memory operands, system bits (SB2/SB3/SB5), 12-bit raw AI counts, data tables (DT0 process, DT1 alarms)
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

Then connect any OPC-UA client to:
```
opc.tcp://localhost:4840/opcua/sim
```

---

## Usage

```
python opcua_sim.py [--host HOST] [--port PORT] [--interval SECONDS]

Options:
  --host      Bind address          (default: 0.0.0.0)
  --port      OPC-UA TCP port       (default: 4840)
  --interval  Node update interval  (default: 1.0 s)
```

Examples:
```bash
# Bind to localhost only, faster updates
python opcua_sim.py --host 127.0.0.1 --interval 0.5

# Non-default port (e.g. alongside a real OPC-UA server)
python opcua_sim.py --port 4841

# Slow scan rate to match a slow PLC
python opcua_sim.py --interval 5.0
```

Startup log:
```
2025-04-08 09:12:03 [INFO] Building OPC-UA node trees...
2025-04-08 09:12:03 [INFO]   [groov RIO / CODESYS 3.5] Node tree built
2025-04-08 09:12:03 [INFO]   [S7-1200] Node tree built
2025-04-08 09:12:03 [INFO]   [Unitronics] Node tree built
2025-04-08 09:12:03 [INFO] ============================================================
2025-04-08 09:12:03 [INFO]   OPC-UA Simulator running at: opc.tcp://0.0.0.0:4840/opcua/sim
2025-04-08 09:12:03 [INFO]   Namespace index : 2
2025-04-08 09:12:03 [INFO]   Update interval : 1.0s
2025-04-08 09:12:03 [INFO]   Devices         : Opto22 groov RIO | Siemens S7-1200 | Unitronics PLC
2025-04-08 09:12:03 [INFO] ============================================================
```

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
docker run -p 4840:4840 opcua-sim
```

Custom update interval:
```bash
docker run -p 4840:4840 opcua-sim --interval 0.5
```

---

## Running as a systemd Service

```ini
# /etc/systemd/system/opcua-sim.service
[Unit]
Description=OPC-UA Device Simulator
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/opcua-device-simulator/opcua_sim.py --port 4840
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
OT network. To add encryption or username authentication, modify `run_server()` in `opcua_sim.py`:

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
├── opcua_sim.py        # Simulator — all three device classes + server bootstrap
├── requirements.txt    # Python dependencies
├── setup.py            # Install as package / console script (optional)
├── Dockerfile          # Container image
├── .gitignore
└── README.md
```

---

## Contributing

Pull requests welcome. To add a new simulated device:

1. Create a class following the existing pattern (`__init__`, `build`, `update`)
2. Instantiate it in `run_server()` alongside the others
3. Document the node tree in this README

---

## License

MIT — see [LICENSE](LICENSE) for details.
