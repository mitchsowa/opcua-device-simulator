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
import math
import random
import time
import logging
from datetime import datetime, timezone

from asyncua import Server, ua
from asyncua.common.node import Node

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

    async def build(self, server: Server, ns: int, root: Node):
        # ── DeviceSet container (CODESYS convention) ─────────────────────────
        device_set = await add_folder(root,       ns, "DeviceSet")
        dev        = await add_object(device_set, ns, "groov-RIO-001")

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

    async def build(self, server: Server, ns: int, root: Node):
        dev = await add_object(root, ns, "Siemens_S7_1200")

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

    async def build(self, server: Server, ns: int, root: Node):
        dev = await add_object(root, ns, "Unitronics_PLC")

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


# ──────────────────────────────────────────────────────────────────────────────
# Server Bootstrap
# ──────────────────────────────────────────────────────────────────────────────

async def run_server(host: str, port: int, update_interval: float):
    server = Server()
    await server.init()

    endpoint = f"opc.tcp://{host}:{port}/opcua/sim"
    server.set_endpoint(endpoint)
    server.set_server_name("OPC-UA Industrial Device Simulator")

    # Security: None/Anonymous for lab use — enable policies as needed
    await server.set_security_IDs(["Anonymous"])

    # Register a single application namespace for all simulated devices
    ns = await server.register_namespace("urn:nyle:opcua:simulator")

    root = server.nodes.objects

    # Instantiate devices
    opto    = Opto22GroovDevice()
    siemens = SiemensS71200Device()
    uni     = UnitronicsDevice()

    log.info("Building OPC-UA node trees…")
    await opto.build(server, ns, root)
    await siemens.build(server, ns, root)
    await uni.build(server, ns, root)

    log.info(f"\n{'='*60}")
    log.info(f"  OPC-UA Simulator running at: {endpoint}")
    log.info(f"  Namespace index : {ns}")
    log.info(f"  Update interval : {update_interval}s")
    log.info(f"  Devices         : Opto22 groov RIO | Siemens S7-1200 | Unitronics PLC")
    log.info(f"{'='*60}\n")

    async with server:
        while True:
            try:
                await opto.update()
                await siemens.update()
                await uni.update()
            except Exception as exc:
                log.warning(f"Update error (will retry): {exc}")
            await asyncio.sleep(update_interval)


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
    args = parser.parse_args()

    try:
        asyncio.run(run_server(args.host, args.port, args.interval))
    except KeyboardInterrupt:
        log.info("Simulator stopped.")


if __name__ == "__main__":
    main()
