#!/usr/bin/env python3
"""ALT-1 plant simulator.

Pretends to be the machine: a Modbus TCP server (slave) that the OpenPLC
runtime polls. The PLC reads the machine's sensors and buttons as discrete
inputs and writes its outputs as coils.

Two ways to drive it, usable at the same time:
  * an operator panel window (Tkinter), and
  * text commands typed in the terminal (type 'help').

Both send the same text commands through one queue, so they always agree.

Standard library only. Python 3.8 or newer. The panel needs Tkinter
(Fedora: sudo dnf install python3-tkinter); without it, or with --no-gui,
the simulator runs in the terminal only.

    python3 plant_sim.py
    python3 plant_sim.py --no-gui --port 5020 --travel 0.4
"""
import argparse
import asyncio
import queue
import signal
import struct
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:          # panel unavailable; terminal mode still works
    tk = None

# ---------------------------------------------------------------------------
# I/O map. Order matters: index n is Modbus address n.
# ---------------------------------------------------------------------------
DI_NAMES = [                # discrete inputs, read by the PLC
    "EStop_OK",             # 0  %IX0.0  NC: TRUE = healthy
    "Guard_Closed",         # 1  %IX0.1
    "Start_PB",             # 2  %IX0.2
    "Stop_PB",              # 3  %IX0.3  NC: TRUE = not pressed
    "Reset_PB",             # 4  %IX0.4
    "Pallet_At_Load",       # 5  %IX0.5
    "Part_Present",         # 6  %IX0.6
    "CapPress_Extended",    # 7  %IX0.7
    "CapPress_Retracted",   # 8  %IX1.0
]
COIL_NAMES = [              # coils, written by the PLC
    "Lamp_Running",         # 0  %QX0.0
    "CapPress_Extend",      # 1  %QX0.1
    "Lamp_Fault",           # 2  %QX0.2  any fault active
    "Lamp_Reset",           # 3  %qx0.3  a fault is active and can be reset
    "Part_Serial",          # 4  %qx0.4  serial number of current part
]
TABLE_SIZE = 64             # spare addresses so a larger poll doesn't error

BUTTONS = {"start": "Start_PB", "stop": "Stop_PB", "reset": "Reset_PB"}
PULSE_S = 0.3               # how long a typed button command holds the button
SLOW_TRAVEL_S = 3.0         # press travel when 'slow' is on (beyond the 2 s timeout)
STALE_POLL_S = 0.5          # header goes amber if the PLC stops polling this long

# ---------------------------------------------------------------------------
# Event log: printed to the terminal and copied to any panel listening
# ---------------------------------------------------------------------------
T0 = time.monotonic()
_listeners = []


def _emit(line):
    print(line, flush=True)
    for q in _listeners:
        q.put(line)


def log(msg):
    _emit(f"{time.monotonic() - T0:8.2f}s  {msg}")


def say(text):
    for line in text.splitlines():
        _emit(line)


def add_listener():
    q = queue.Queue()
    _listeners.append(q)
    return q


# ---------------------------------------------------------------------------
# Plant model. Changed only on the simulation thread; the panel only reads it.
# ---------------------------------------------------------------------------
class Plant:
    def __init__(self, travel_s):
        self.coils = [False] * TABLE_SIZE
        self.di = [False] * TABLE_SIZE
        self.registers = [0] * TABLE_SIZE
        # machine state
        self.estop_ok = True
        self.guard_closed = True
        self.pallet = False
        self.part = False
        self.press_pos = 0.0            # 0 = home, 1 = fully extended
        self.travel_s = travel_s
        # fault injection
        self.slow = False
        self.jam = False
        self.stuck_ext = False
        self.stuck_ret = False
        # pushbuttons: pressed while held, or until a pulse expires
        self.held = set()
        self.pulse_until = {b: 0.0 for b in BUTTONS.values()}
        # Modbus link health
        self.link = {"clients": 0, "addr": None, "last_poll": 0.0}
        self.step(0.0, time.monotonic())

    def output(self, name):
        return self.coils[COIL_NAMES.index(name)]

    def input(self, name):
        return self.di[DI_NAMES.index(name)]

    def serial(self):
        return self.registers[0] | (self.registers[1] << 16)

    def press(self, button):
        self.pulse_until[button] = time.monotonic() + PULSE_S

    def hold(self, button, down):
        if down:
            self.held.add(button)
        else:
            self.held.discard(button)
            self.pulse_until[button] = 0.0

    def _pressed(self, button, now):
        return button in self.held or now < self.pulse_until[button]

    def step(self, dt, now):
        travel = SLOW_TRAVEL_S if self.slow else self.travel_s
        if not self.jam and dt > 0:
            if self.output("CapPress_Extend"):
                self.press_pos = min(1.0, self.press_pos + dt / travel)
            else:
                self.press_pos = max(0.0, self.press_pos - dt / travel)
        values = {
            "EStop_OK": self.estop_ok,
            "Guard_Closed": self.guard_closed,
            "Start_PB": self._pressed("Start_PB", now),
            "Stop_PB": not self._pressed("Stop_PB", now),       # NC contact
            "Reset_PB": self._pressed("Reset_PB", now),
            "Pallet_At_Load": self.pallet,
            "Part_Present": self.part,
            "CapPress_Extended": self.press_pos >= 0.98 or self.stuck_ext,
            "CapPress_Retracted": self.press_pos <= 0.02 or self.stuck_ret,
        }
        for i, name in enumerate(DI_NAMES):
            self.di[i] = values[name]

    def snapshot(self):
        snap = {name: self.di[i] for i, name in enumerate(DI_NAMES)}
        snap.update({name: self.coils[i] for i, name in enumerate(COIL_NAMES)})
        snap["Part_Serial"] = self.serial()
        return snap

    def status(self):
        lines = ["Inputs (PLC reads):"]
        lines += ["  %-20s %s" % (n, self.di[i]) for i, n in enumerate(DI_NAMES)]
        lines += ["Outputs (PLC writes):"]
        lines += ["  %-20s %s" % (n, self.coils[i]) for i, n in enumerate(COIL_NAMES)]
        faults = [n for n, on in (("jam", self.jam), ("slow", self.slow),
                                  ("stuck ext", self.stuck_ext), ("stuck ret", self.stuck_ret)) if on]
        lines += [f"  {'Part_Serial':20s} {self.serial()}"]
        lines += [f"Press position {self.press_pos:.2f}   faults: {', '.join(faults) or 'none'}"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Minimal Modbus TCP server: function codes 1, 2, 3, 4, 5, 6, 15, 16
# ---------------------------------------------------------------------------
class ModbusError(Exception):
    def __init__(self, code):
        self.code = code


def check_range(start, qty, size, max_qty):
    if qty < 1 or qty > max_qty:
        raise ModbusError(3)            # illegal data value
    if start + qty > size:
        raise ModbusError(2)            # illegal data address


def pack_bits(bits):
    out = bytearray((len(bits) + 7) // 8)
    for i, b in enumerate(bits):
        if b:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def handle_pdu(plant, pdu):
    fc = pdu[0]
    try:
        if fc in (1, 2):                                    # read coils / discrete inputs
            start, qty = struct.unpack(">HH", pdu[1:5])
            table = plant.coils if fc == 1 else plant.di
            check_range(start, qty, len(table), 2000)
            data = pack_bits(table[start:start + qty])
            return bytes([fc, len(data)]) + data
        if fc in (3, 4):                                    # read holding / input registers
            start, qty = struct.unpack(">HH", pdu[1:5])
            check_range(start, qty, len(plant.registers), 125)
            regs = plant.registers[start:start + qty]
            return bytes([fc, 2 * qty]) + struct.pack(f">{qty}H", *regs)
        if fc == 5:                                         # write single coil
            addr, val = struct.unpack(">HH", pdu[1:5])
            check_range(addr, 1, len(plant.coils), 1)
            if val not in (0x0000, 0xFF00):
                raise ModbusError(3)
            plant.coils[addr] = val == 0xFF00
            return pdu[:5]
        if fc == 6:                                         # write single register
            addr, val = struct.unpack(">HH", pdu[1:5])
            check_range(addr, 1, len(plant.registers), 1)
            plant.registers[addr] = val
            return pdu[:5]
        if fc == 15:                                        # write multiple coils
            start, qty, nbytes = struct.unpack(">HHB", pdu[1:6])
            check_range(start, qty, len(plant.coils), 1968)
            raw = pdu[6:6 + nbytes]
            for i in range(qty):
                plant.coils[start + i] = bool((raw[i // 8] >> (i % 8)) & 1)
            return struct.pack(">BHH", fc, start, qty)
        if fc == 16:                                        # write multiple registers
            start, qty, nbytes = struct.unpack(">HHB", pdu[1:6])
            check_range(start, qty, len(plant.registers), 123)
            vals = struct.unpack(f">{qty}H", pdu[6:6 + 2 * qty])
            plant.registers[start:start + qty] = list(vals)
            return struct.pack(">BHH", fc, start, qty)
        raise ModbusError(1)                                # illegal function
    except ModbusError as e:
        return bytes([fc | 0x80, e.code])
    except (struct.error, IndexError):
        return bytes([fc | 0x80, 3])


async def serve_client(plant, reader, writer):
    peer = writer.get_extra_info("peername")
    addr = peer[0] if peer else "?"
    plant.link["clients"] += 1
    plant.link["addr"] = addr
    log(f"PLC connected from {addr}")
    try:
        while True:
            header = await reader.readexactly(7)
            tid, _pid, length, unit = struct.unpack(">HHHB", header)
            pdu = await reader.readexactly(length - 1)
            plant.link["last_poll"] = time.monotonic()
            resp = handle_pdu(plant, pdu)
            writer.write(struct.pack(">HHHB", tid, 0, len(resp) + 1, unit) + resp)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        plant.link["clients"] -= 1
        log("PLC disconnected")
        writer.close()


# ---------------------------------------------------------------------------
# Commands: the single path for every change, from the terminal or the panel
# ---------------------------------------------------------------------------
HELP = """Commands:
  start | stop | reset       press a pushbutton (held for 0.3 s)
  start down | start up      hold / release a pushbutton (same for stop, reset)
  estop | release            press / release the emergency stop
  open | close               open / close the guard door
  load | unload              put a part on the pallet / take it off
  nopallet | pallet          remove / return the pallet at the load station
  jam | unjam                cap press stops moving
  slow | normal              cap press travel 3 s (beyond the 2 s timeout)
  stuck ext|ret [on|off]     a press position sensor stuck on ('stuck off' clears both)
  status                     show all signals
  help | quit"""


def apply_command(plant, line):
    """Apply one text command. Returns False when the simulator should stop."""
    words = line.strip().lower().split()
    if not words:
        return True
    cmd, args = words[0], words[1:]
    arg = args[0] if args else ""
    p = plant
    simple = {
        "estop": lambda: setattr(p, "estop_ok", False),
        "release": lambda: setattr(p, "estop_ok", True),
        "open": lambda: setattr(p, "guard_closed", False),
        "close": lambda: setattr(p, "guard_closed", True),
        "load": lambda: (setattr(p, "pallet", True), setattr(p, "part", True)),
        "unload": lambda: setattr(p, "part", False),
        "nopallet": lambda: setattr(p, "pallet", False),
        "pallet": lambda: setattr(p, "pallet", True),
        "jam": lambda: setattr(p, "jam", True),
        "unjam": lambda: setattr(p, "jam", False),
        "slow": lambda: setattr(p, "slow", True),
        "normal": lambda: setattr(p, "slow", False),
    }
    if cmd in ("quit", "exit"):
        return False
    if cmd == "help":
        say(HELP)
    elif cmd == "status":
        say(plant.status())
    elif cmd in BUTTONS:
        button = BUTTONS[cmd]
        if arg == "down":
            p.hold(button, True)
        elif arg == "up":
            p.hold(button, False)
        else:
            p.press(button)
    elif cmd == "stuck":
        state = (args[1] if len(args) > 1 else "on") != "off"
        if arg == "ext":
            p.stuck_ext = state
        elif arg == "ret":
            p.stuck_ret = state
        elif arg == "off":
            p.stuck_ext = p.stuck_ret = False
        else:
            say("Usage: stuck ext|ret [on|off], or stuck off")
            return True
        log(f"fault injection: extended sensor stuck {'on' if p.stuck_ext else 'off'}, "
            f"retracted sensor stuck {'on' if p.stuck_ret else 'off'}")
    elif cmd in simple:
        simple[cmd]()
        if cmd in ("jam", "unjam", "slow", "normal"):
            log(f"fault injection: {cmd}")
    else:
        say(f"Unknown command '{cmd}'. Type 'help'.")
    return True


# ---------------------------------------------------------------------------
# Simulation runtime: Modbus server, plant model and command queue on one thread
# ---------------------------------------------------------------------------
async def plant_loop(plant, period=0.01):
    last = time.monotonic()
    prev = plant.snapshot()
    while True:
        await asyncio.sleep(period)
        now = time.monotonic()
        plant.step(now - last, now)
        last = now
        snap = plant.snapshot()
        for name, val in snap.items():
            if val != prev[name]:
                log(f"{name:20s} {'ON' if val else 'off'}")
        prev = snap


class SimRuntime:
    def __init__(self, plant, host, port):
        self.plant, self.host, self.port = plant, host, port
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.error = None
        self.commands = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="sim").start()
        self.started.wait(5)
        if self.error:
            raise self.error

    def submit(self, line):
        """Queue a command from any thread."""
        if not self.stopped.is_set():
            self.loop.call_soon_threadsafe(self.commands.put_nowait, line)

    def stop(self):
        self.submit(None)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except BaseException as e:      # e.g. port already in use
            self.error = e
            self.started.set()
        finally:
            self.stopped.set()

    async def _main(self):
        self.commands = asyncio.Queue()
        server = await asyncio.start_server(
            lambda r, w: serve_client(self.plant, r, w), self.host, self.port)
        log(f"ALT-1 plant simulator listening on {self.host}:{self.port}")
        log("Waiting for the PLC to connect. Type 'help' for commands.")
        self.started.set()
        ticker = asyncio.create_task(plant_loop(self.plant))
        async with server:
            while True:
                line = await self.commands.get()
                if line is None or not apply_command(self.plant, line):
                    break
        ticker.cancel()
        log("Simulator stopped")


def start_console(runtime):
    """Read terminal commands on a daemon thread so the panel and Ctrl+C keep working."""
    def reader():
        for line in sys.stdin:
            runtime.submit(line)
    threading.Thread(target=reader, daemon=True, name="console").start()


# ---------------------------------------------------------------------------
# Operator panel (Tkinter). Reads the plant; changes it only through commands.
# ---------------------------------------------------------------------------
BG, PANEL, OFF = "#eceff1", "#ffffff", "#3a3f44"
GREEN, AMBER, RED, BLUE = "#2ecc40", "#ffb000", "#e53935", "#1e88e5"
FONT = ("DejaVu Sans", 10)
BOLD = ("DejaVu Sans", 10, "bold")
SMALL = ("DejaVu Sans", 8)
BOLD_MONO = ("DejaVu Sans Mono", 9, "bold")
MONO = ("DejaVu Sans Mono", 9)
TINY_MONO = ("DejaVu Sans Mono", 7)


class Lamp:
    """Indicator with a friendly label and the PLC signal name underneath."""

    def __init__(self, parent, label, signal, colour, size=22):
        self.frame = tk.Frame(parent, bg=PANEL)
        self.colour, self.state = colour, None
        c = tk.Canvas(self.frame, width=size + 4, height=size + 4, bg=PANEL, highlightthickness=0)
        self.dot = c.create_oval(2, 2, size + 2, size + 2, fill=OFF, outline="#222")
        self.shine = c.create_oval(6, 5, 6 + size // 4, 5 + size // 5, fill="white", outline="", state="hidden")
        c.pack()
        self.canvas = c
        tk.Label(self.frame, text=label, bg=PANEL, font=SMALL).pack()
        tk.Label(self.frame, text=signal, bg=PANEL, fg="#90a4ae", font=TINY_MONO).pack()

    def set(self, on):
        if on != self.state:
            self.canvas.itemconfigure(self.dot, fill=self.colour if on else OFF)
            self.canvas.itemconfigure(self.shine, state="normal" if on else "hidden")
            self.state = on


class Panel:
    def __init__(self, root, plant, runtime):
        self.root, self.plant, self.rt = root, plant, runtime
        self.log_queue = add_listener()
        self.held = set()
        self.reset_enabled = None
        self.press_drawn = None
        root.title("ALT-1 Plant Simulator")
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabelframe", background=PANEL)
        style.configure("TLabelframe.Label", background=PANEL, font=BOLD)
        style.configure("TCheckbutton", background=PANEL, font=FONT)
        style.configure("TRadiobutton", background=PANEL, font=FONT)
        self._build()
        self.poll()

    def send(self, cmd):
        self.rt.submit(cmd)

    # --- layout ---------------------------------------------------------------
    def _build(self):
        hdr = tk.Frame(self.root, bg="#263238")
        hdr.pack(fill="x")
        tk.Label(hdr, text="ALT-1 Plant Simulator", bg="#263238", fg="white",
                 font=("DejaVu Sans", 13, "bold")).pack(side="left", padx=12, pady=8)
        self.link_text = tk.Label(hdr, text="", bg="#263238", fg="#cfd8dc", font=FONT)
        self.link_text.pack(side="right", padx=(4, 12))
        self.link_dot = tk.Canvas(hdr, width=16, height=16, bg="#263238", highlightthickness=0)
        self.link_oval = self.link_dot.create_oval(2, 2, 14, 14, fill=RED, outline="")
        self.link_dot.pack(side="right")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(2, weight=1)

        # operator controls
        op = ttk.LabelFrame(body, text="Operator controls")
        op.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        btns = tk.Frame(op, bg=PANEL)
        btns.pack(side="left", padx=10, pady=10)
        self._hold_button(btns, "START", "#2e7d32", "start").grid(row=0, column=0, padx=4)
        self._hold_button(btns, "STOP", "#c62828", "stop").grid(row=0, column=1, padx=4)
        self.reset_btn = self._hold_button(btns, "RESET", "#1565c0", "reset")
        self.reset_btn.grid(row=0, column=2, padx=4)
        es = tk.Frame(op, bg=PANEL)
        es.pack(side="left", padx=14)
        self.estop = tk.Canvas(es, width=78, height=78, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.estop.create_oval(2, 2, 76, 76, fill="#ffd600", outline="#9e8600", width=2)
        self.estop_head = self.estop.create_oval(14, 14, 64, 64, fill="#d50000", outline="#7f0000", width=2)
        self.estop_text = self.estop.create_text(39, 39, text="E-STOP", fill="white", font=("DejaVu Sans", 8, "bold"))
        self.estop.bind("<Button-1>", lambda e: self.send("release" if not self.plant.estop_ok else "estop"))
        self.estop.pack()

        # machine
        mc = ttk.LabelFrame(body, text="Machine controls")
        mc.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        left = tk.Frame(mc, bg=PANEL)
        left.pack(side="left", padx=(10, 0), pady=8, anchor="n")
        tk.Label(left, text="Guard door", bg=PANEL, font=BOLD).grid(row=0, column=0, sticky="w")
        self.guard_var = tk.StringVar(value="close")
        for col, (txt, val) in enumerate((("Closed", "close"), ("Open", "open")), start=1):
            ttk.Radiobutton(left, text=txt, variable=self.guard_var, value=val,
                            command=lambda: self.send(self.guard_var.get())).grid(row=0, column=col, padx=2, sticky="w")
        tk.Label(left, text="Part", bg=PANEL, font=BOLD).grid(row=1, column=0, sticky="w", pady=8)
        tk.Button(left, text="Load part", font=FONT, width=9, command=lambda: self.send("load")).grid(row=1, column=1, padx=4)
        tk.Button(left, text="Unload part", font=FONT, width=9, command=lambda: self.send("unload")).grid(row=1, column=2, padx=4)
        self.pallet_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Pallet at load station", variable=self.pallet_var,
                        command=lambda: self.send("pallet" if self.pallet_var.get() else "nopallet")
                        ).grid(row=2, column=0, columnspan=3, sticky="w")

        tk.Label(left, text="Part serial", bg=PANEL, font=BOLD).grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.serial_label = tk.Label(left, text="~", bg="#1e2327", fg=AMBER, font=BOLD_MONO, width=8, anchor="e", padx=6)
        self.serial_label.grid(row=3, column=1, sticky="w", pady=(8, 0))
        tk.Label(left, text="from PLC (%QW0)", bg=PANEL, fg="#90a4ae", font=TINY_MONO).grid(
            row=3, column=2, sticky="w", pady=(8, 0))


        lamps = tk.Frame(mc, bg=PANEL)
        lamps.pack(side="right", padx=(0, 16), pady=6)
        self.lamps = {}
        for i, (label, sig) in enumerate((("E-stop healthy", "EStop_OK"), ("Guard closed", "Guard_Closed"),
                                          ("Pallet in position", "Pallet_At_Load"), ("Part present", "Part_Present"))):
            lamp = Lamp(lamps, label, sig, GREEN)
            lamp.frame.grid(row=i // 2, column=i % 2, padx=8, pady=2)
            self.lamps[sig] = lamp

        # cap press
        cp = ttk.LabelFrame(body, text="Cap press")
        cp.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 8))
        self.press_canvas = tk.Canvas(cp, width=250, height=150, bg=PANEL, highlightthickness=0)
        self.press_canvas.pack(side="left", padx=8, pady=6)
        pc = self.press_canvas
        pc.create_rectangle(80, 8, 150, 60, fill="#90a4ae", outline="#37474f", width=2)
        pc.create_rectangle(70, 130, 160, 142, fill="#8d6e63", outline="#4e342e")
        pc.create_rectangle(190, 10, 210, 140, outline="#455a64")
        pc.create_text(228, 12, text="DOWN", font=("DejaVu Sans", 7), fill="#607d8b")
        pc.create_text(228, 138, text="HOME", font=("DejaVu Sans", 7), fill="#607d8b")
        pl = tk.Frame(cp, bg=PANEL)
        pl.pack(side="left", padx=6)
        for label, sig, colour in (("Press home", "CapPress_Retracted", GREEN),
                                   ("Press down", "CapPress_Extended", GREEN),
                                   ("Press solenoid", "CapPress_Extend", AMBER)):
            lamp = Lamp(pl, label, sig, colour)
            lamp.frame.pack(pady=1)
            self.lamps[sig] = lamp

        # PLC outputs and fault injection
        right = tk.Frame(body, bg=BG)
        right.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 8))
        po = ttk.LabelFrame(right, text="PLC status lamps")
        po.pack(fill="x")
        pof = tk.Frame(po, bg=PANEL)
        pof.pack(pady=6)
        for label, sig, colour in (("Running", "Lamp_Running", GREEN),
                                   ("Fault", "Lamp_Fault", RED),
                                   ("Reset available", "Lamp_Reset", BLUE)):
            lamp = Lamp(pof, label, sig, colour, 28)
            lamp.frame.pack(side="left", padx=14)
            self.lamps[sig] = lamp

        fi = ttk.LabelFrame(right, text="Fault injection  (test only, not part of the machine)")
        fi.pack(fill="x", pady=(8, 0))
        stripe = tk.Canvas(fi, height=6, bg=PANEL, highlightthickness=0)
        stripe.pack(fill="x")
        for x in range(0, 800, 16):
            stripe.create_polygon(x, 6, x + 8, 0, x + 16, 0, x + 8, 6, fill="#ffd600", outline="")
        fif = tk.Frame(fi, bg=PANEL)
        fif.pack(fill="x", padx=8, pady=6)
        self.fault_vars = {}
        faults = (("Press jammed", "jam", "jam", "unjam"),
                  ("Press slow (3 s travel)", "slow", "slow", "normal"),
                  ("Down sensor stuck on", "stuck_ext", "stuck ext on", "stuck ext off"),
                  ("Home sensor stuck on", "stuck_ret", "stuck ret on", "stuck ret off"))
        for i, (label, attr, on_cmd, off_cmd) in enumerate(faults):
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(fif, text=label, variable=var,
                            command=lambda v=var, a=on_cmd, b=off_cmd: self.send(a if v.get() else b)
                            ).grid(row=i // 2, column=i % 2, sticky="w", padx=6, pady=2)
            self.fault_vars[attr] = var

        # event log and command line
        lg = ttk.LabelFrame(body, text="Event log")
        lg.grid(row=2, column=0, columnspan=2, sticky="nsew")
        self.log_text = tk.Text(lg, height=8, font=MONO, bg="#1e2327", fg="#d0d7dc", bd=0, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=6, pady=(6, 4))
        cmd = tk.Frame(lg, bg=PANEL)
        cmd.pack(fill="x", padx=6, pady=(0, 6))
        tk.Label(cmd, text=">", bg=PANEL, font=MONO).pack(side="left")
        self.entry = tk.Entry(cmd, font=MONO)
        self.entry.pack(side="left", fill="x", expand=True, padx=4)
        self.entry.bind("<Return>", lambda e: self._send_entry())
        tk.Button(cmd, text="Send", font=FONT, command=self._send_entry).pack(side="left")

    def _hold_button(self, parent, text, colour, cmd):
        b = tk.Button(parent, text=text, bg=colour, fg="white", activebackground=colour,
                      activeforeground="white", disabledforeground="#eceff1",
                      font=BOLD, width=7, height=2, relief="raised", bd=3)
        b.colour = colour
        b.bind("<ButtonPress-1>", lambda e: self._press(b, cmd))
        b.bind("<ButtonRelease-1>", lambda e: self._release(cmd))
        return b

    def _press(self, button, cmd):
        if str(button["state"]) == "disabled":
            return "break"
        self.held.add(cmd)
        self.send(f"{cmd} down")

    def _release(self, cmd):
        if cmd in self.held:
            self.held.discard(cmd)
            self.send(f"{cmd} up")

    def _send_entry(self):
        text = self.entry.get().strip()
        if text:
            log(f"> {text}")
            self.send(text)
        self.entry.delete(0, "end")

    # --- refresh ----------------------------------------------------------------
    def poll(self):
        if self.rt.stopped.is_set():
            self.root.destroy()
            return
        p = self.plant
        snap = p.snapshot()
        for sig, lamp in self.lamps.items():
            lamp.set(snap[sig])

        # reset is only enabled while the PLC says a reset will work
        enabled = snap["Lamp_Reset"]
        if enabled != self.reset_enabled:
            self.reset_btn.configure(state="normal" if enabled else "disabled",
                                     bg=self.reset_btn.colour if enabled else "#b0bec5",
                                     relief="raised" if enabled else "flat")
            if not enabled:
                self._release("reset")
            self.reset_enabled = enabled

        # e-stop appearance
        pressed = not p.estop_ok
        self.estop.itemconfigure(self.estop_head, fill="#7f0000" if pressed else "#d50000")
        self.estop.itemconfigure(self.estop_text, text="PRESSED" if pressed else "E-STOP")

        # keep controls in step with commands typed in the terminal
        guard = "close" if p.guard_closed else "open"
        if self.guard_var.get() != guard:
            self.guard_var.set(guard)
        if self.pallet_var.get() != p.pallet:
            self.pallet_var.set(p.pallet)
        for attr, var in self.fault_vars.items():
            if var.get() != getattr(p, attr):
                var.set(getattr(p, attr))

        self.serial_label.configure(text=str(self.plant.serial()))

        self._draw_press(p.press_pos)
        self._update_link()
        self._drain_log()
        self.root.after(50, self.poll)

    def _draw_press(self, pos):
        pos = round(pos, 2)
        key = (pos, self.plant.part)
        if key == self.press_drawn:
            return
        self.press_drawn = key
        pc = self.press_canvas
        pc.delete("dyn")
        rod_end = 60 + 58 * pos
        pc.create_rectangle(108, 60, 122, rod_end, fill="#cfd8dc", outline="#37474f", tags="dyn")
        pc.create_rectangle(92, rod_end, 138, rod_end + 10, fill="#546e7a", outline="#263238", tags="dyn")
        if self.plant.part:
            pc.create_rectangle(98, 118, 132, 130, fill="#b0bec5", outline="#455a64", tags="dyn")
        pc.create_rectangle(191, 11 + 128 * (1 - pos), 209, 139, fill=AMBER, outline="", tags="dyn")
        pc.create_text(200, 146, text=f"{int(pos * 100)}%", font=SMALL, tags="dyn")

    def _update_link(self):
        link = self.plant.link
        age = time.monotonic() - link["last_poll"]
        if link["clients"] and age < STALE_POLL_S:
            last_poll = f"{age * 1000:.0f}".zfill(3)
            colour, text = GREEN, f"PLC connected {link['addr']}    last poll {last_poll} ms    Modbus :{self.rt.port}"
        elif link["clients"]:
            colour, text = AMBER, f"PLC connected but not polling ({age:.1f} s)    Modbus :{self.rt.port}"
        else:
            colour, text = RED, f"Waiting for the PLC    Modbus :{self.rt.port}"
        self.link_dot.itemconfigure(self.link_oval, fill=colour)
        self.link_text.configure(text=text)

    def _drain_log(self):
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            t = self.log_text
            t.configure(state="normal")
            t.insert("end", "\n".join(lines) + "\n")
            excess = int(t.index("end-1c").split(".")[0]) - 500
            if excess > 0:
                t.delete("1.0", f"{excess}.0")
            t.see("end")
            t.configure(state="disabled")

    def close(self):
        self.rt.stop()
        self.root.destroy()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="ALT-1 plant simulator (Modbus TCP server)")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default all)")
    ap.add_argument("--port", type=int, default=5020, help="TCP port (default 5020)")
    ap.add_argument("--travel", type=float, default=0.4, help="cap press travel time in s (default 0.4)")
    ap.add_argument("--no-gui", action="store_true", help="terminal only, no operator panel")
    args = ap.parse_args()

    plant = Plant(args.travel)
    runtime = SimRuntime(plant, args.host, args.port)
    try:
        runtime.start()
    except OSError as e:
        print(f"Could not listen on {args.host}:{args.port}: {e}")
        sys.exit(1)
    start_console(runtime)

    root = None
    if not args.no_gui:
        if tk is None:
            print("Tkinter is not installed - Running in the terminal only.")
        else:
            try:
                root = tk.Tk()
            except tk.TclError as e:
                print(f"No display available ({e}). Running in the terminal only.")

    if root is not None:
        Panel(root, plant, runtime)
        signal.signal(signal.SIGINT, lambda *_: root.after(0, root.event_generate, "<<Quit>>"))
        root.bind("<<Quit>>", lambda e: (runtime.stop(), root.destroy()))
        root.mainloop()
        runtime.stop()
    else:
        try:
            while not runtime.stopped.wait(0.2):
                pass
        except KeyboardInterrupt:
            runtime.stop()
    runtime.stopped.wait(2)


if __name__ == "__main__":
    main()
