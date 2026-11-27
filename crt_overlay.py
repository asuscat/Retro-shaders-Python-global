"""
CRT / RF Signal Overlay for Windows 10/11  (+ CRT-Dusha integration)
====================================================================
Captures the primary monitor (Desktop Duplication via dxcam), runs it through a
GPU shader that emulates analogue TV signal paths (with PAL/CRT & NTSC/CRT
composite carrier modulation / demodulation), and shows the result in a
click-through, always-on-top, full-screen overlay.

 * Gamma-aware: source is decoded with a CRT gamma (default 2.4), all filtering
   happens in linear light, output is encoded for an sRGB monitor.
 * Composite carrier modulation & demodulation: modulates bandlimited YIQ (NTSC)
   or PAL-alternating YUV onto a sampled subcarrier and demodulates back with
   optional PAL delay-line averaging, chroma lag, scanline bloom, dot crawl,
   line jitter, RF ghosting, scanlines, phosphor masks and phosphor decay.
 * Phosphor masks: Original (RGB columns), Aperture Grille, Slot Mask, Shadow Mask,
   with feathering, pixel spacing and mask size controls. Red/Green/Blue gain sliders.
 * Noise can be driven by CPU load, CPU temperature, NVIDIA GPU temperature or RAM.
 * Frame rate: VSync (match display), uncapped, or your own limit (Performance tab).
 * HOME key shows/hides the settings menu. The menu has Deactivate and Quit.

CRT-Dusha (MIT, (c) 2025 Maxim Lapounov) is fully integrated, OFF by default:
 * Contrast (beam voltage), Brightness (beam current), Midtone boost, Softclip
 * Color saturation, Hue shift, Phosphor tint
 * Multi-stage Fibonacci-weighted per-channel phosphor decay driven by a BFI-style
   frame cycle (BFI is OPTIONAL: its own checkbox), Uniform Pulse / Raster Sweep,
   slow-motion + frame-step debugging, on-screen frame counter
 * Scanlines (Horizontal / Vertical / Trinitron / Crosshatch)
 * 3dfx Voodoo2 16-bit (RGB565) quantisation with 2x2 / 4x4 / 8x8 Bayer dither
 * Split-screen comparison, LCD burn-in protection (Phase Jump / Frame Drop)
 * Presets: Manual / Vibrant / Old TV

Where a Dusha feature overlaps an existing feature of this script (scanlines,
saturation, hue, phosphor decay, colour tint / RGB gain) there is a choice per
feature on the "Dusha" tab: "Original script" or "CRT-Dusha (overrides)".

Optimisations vs. the ReShade version: the Uniform-Pulse decay curve is evaluated
once per frame on the CPU (not per pixel), the hue matrix is built on the CPU,
and every Dusha stage is skipped by a uniform branch when it is not in use.

No admin rights needed. Install:
    pip install numpy psutil pygame moderngl dxcam mss
Run:
    python crt_overlay.py

Notes: exclusive-fullscreen games can't be overlaid (use borderless/windowed
fullscreen). Primary monitor only. BFI only looks right with Frame rate = VSync
(match display refresh) or a custom limit equal to your refresh rate.
"""
import os
import sys
import math
import time
import ctypes
import atexit
import base64
import struct
import threading
import subprocess
from ctypes import wintypes

if sys.platform != "win32":
    sys.exit("This tool is Windows-only.")

# Must happen before any window is created so we get true pixel sizes.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import numpy as np
import psutil
import pygame
import moderngl
import tkinter as tk
from tkinter import ttk

# Tk variables get garbage-collected from the wrong thread at exit and print a harmless
# traceback; this silences it.
tk.Variable.__del__ = lambda self: None

# --------------------------------------------------------------------------
# Win32 helpers
# --------------------------------------------------------------------------
user32 = ctypes.windll.user32
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte, wintypes.DWORD]
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND

gdi32 = ctypes.windll.gdi32
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
gdi32.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]


def detect_refresh():
    try:
        dc = user32.GetDC(None)
        hz = gdi32.GetDeviceCaps(dc, 116)   # VREFRESH
        user32.ReleaseDC(None, dc)
        return float(hz) if hz > 30 else 60.0
    except Exception:
        return 60.0


dwmapi = ctypes.windll.dwmapi
dwmapi.DwmFlush.restype = ctypes.c_long


def set_swap_interval(n):
    """Turn VSync on (1) or off (0) on the current OpenGL context via WGL. True on success."""
    try:
        gl = ctypes.windll.opengl32
        gl.wglGetProcAddress.restype = ctypes.c_void_p
        gl.wglGetProcAddress.argtypes = [ctypes.c_char_p]
        addr = gl.wglGetProcAddress(b"wglSwapIntervalEXT")
        if addr:
            fn = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int)(addr)
            return bool(fn(int(n)))
    except Exception:
        pass
    return False


_THREAD_PRIORITY = {
    "Normal":        0,
    "Above Normal":  1,
    "High":          2,
    "Time Critical": 15,
}
_PROCESS_PRIORITY = {
    "Normal":        psutil.NORMAL_PRIORITY_CLASS,
    "Above Normal":  psutil.ABOVE_NORMAL_PRIORITY_CLASS,
    "High":          psutil.HIGH_PRIORITY_CLASS,
    "Realtime":      psutil.REALTIME_PRIORITY_CLASS,
}


def _set_thread_priority(level_name):
    try:
        k32 = ctypes.windll.kernel32
        h = k32.GetCurrentThread()
        return bool(k32.SetThreadPriority(h, _THREAD_PRIORITY.get(level_name, 0)))
    except Exception:
        return False


def _set_process_priority(level_name):
    try:
        psutil.Process().nice(_PROCESS_PRIORITY[level_name])
        return True
    except Exception:
        return False


def _set_gpu_priority(level):
    try:
        gdi = ctypes.windll.gdi32
        gdi.D3DKMTSetProcessSchedulingPriorityClass.argtypes = [wintypes.HANDLE, ctypes.c_uint]
        gdi.D3DKMTSetProcessSchedulingPriorityClass.restype = ctypes.c_long
        import win32api
        h = win32api.OpenProcess(0x1F0FFF, False, os.getpid())
        r = gdi.D3DKMTSetProcessSchedulingPriorityClass(int(h), int(level))
        win32api.CloseHandle(h)
        return r == 0
    except Exception:
        pass
    try:
        nt = ctypes.windll.ntdll
        nt.NtSetInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong,
                                               ctypes.c_void_p, ctypes.c_ulong]
        buf = ctypes.c_ulong(int(level))
        nt.NtSetInformationProcess(ctypes.windll.kernel32.GetCurrentProcess(),
                                   18,
                                   ctypes.byref(buf), ctypes.sizeof(buf))
        return True
    except Exception:
        return False


shell32 = ctypes.windll.shell32
shell32.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
shell32.ShellExecuteW.restype = ctypes.c_void_p


def is_admin():
    try:
        return bool(shell32.IsUserAnAdmin())
    except Exception:
        return False


GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x10, 0x20
WDA_EXCLUDEFROMCAPTURE = 0x11
VK_HOME = 0x24


def keep_topmost(hwnd):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def exclude_from_capture(hwnd):
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


def make_click_through_overlay(hwnd):
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, 2)
    if not exclude_from_capture(hwnd):
        print("WARNING: could not exclude overlay from capture (needs Windows 10 2004+).")
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)


# --------------------------------------------------------------------------
# Presets calibrated from PAL/CRT (pal.c / pal_core.c) and NTSC/CRT
# (crt-ntsc.c / crt_core.c) signal characteristics.
# --------------------------------------------------------------------------
PRESETS = {
    "HDMI":
        dict(lines=240, luma=0.0, chroma=0.0, pal=0, pal_delay=0, chroma_lag=0.0, bloom=0.0,
             scan=0.15, mask=0.10, noise=0.0, ghost=0.0, dot=0.0, jitter=0.0, sat=1.0, hue=0.0),
    "HDMI Mod":
        dict(lines=240, luma=0.35, chroma=0.35, pal=0, pal_delay=0, chroma_lag=0.0, bloom=0.15,
             scan=0.45, mask=0.25, noise=0.0, ghost=0.0, dot=0.0, jitter=0.0, sat=1.0, hue=0.0),
    "NTSC Composite":
        dict(lines=240, luma=0.95, chroma=3.4, pal=0, pal_delay=0, chroma_lag=0.0, bloom=0.35,
             scan=0.50, mask=0.20, noise=0.04, ghost=0.0, dot=0.40, jitter=0.15, sat=1.0, hue=0.0),
    "NTSC RF":
        dict(lines=240, luma=1.55, chroma=4.6, pal=0, pal_delay=0, chroma_lag=0.6, bloom=0.55,
             scan=0.50, mask=0.20, noise=0.15, ghost=0.14, dot=0.50, jitter=0.60, sat=0.95, hue=0.0),
    "PAL-B (Composite)":
        dict(lines=288, luma=1.05, chroma=3.7, pal=1, pal_delay=1, chroma_lag=0.2, bloom=0.35,
             scan=0.50, mask=0.20, noise=0.04, ghost=0.0, dot=0.08, jitter=0.15, sat=1.0, hue=0.0),
    "PAL-B (RF)":
        dict(lines=288, luma=1.65, chroma=4.9, pal=1, pal_delay=1, chroma_lag=0.8, bloom=0.55,
             scan=0.50, mask=0.20, noise=0.15, ghost=0.14, dot=0.10, jitter=0.60, sat=0.95, hue=0.0),
    "PAL-M (Brazil)":
        dict(lines=240, luma=1.00, chroma=3.6, pal=1, pal_delay=1, chroma_lag=0.2, bloom=0.35,
             scan=0.50, mask=0.20, noise=0.05, ghost=0.0, dot=0.10, jitter=0.20, sat=1.05, hue=0.04),
    "PAL-N (Argentina)":
        dict(lines=288, luma=1.15, chroma=4.2, pal=1, pal_delay=1, chroma_lag=0.3, bloom=0.40,
             scan=0.50, mask=0.20, noise=0.05, ghost=0.0, dot=0.10, jitter=0.20, sat=0.95, hue=-0.04),
    "PAL-D (Dendy)":
        dict(lines=288, luma=1.05, chroma=3.7, pal=1, pal_delay=0, chroma_lag=0.4, bloom=0.40,
             scan=0.50, mask=0.20, noise=0.05, ghost=0.0, dot=0.12, jitter=0.25, sat=1.10, hue=0.10),
}
PRESET_ORDER = [
    "PAL-B (Composite)", "PAL-B (RF)", "PAL-M (Brazil)", "PAL-N (Argentina)", "PAL-D (Dendy)",
    "NTSC Composite", "NTSC RF", "HDMI Mod", "HDMI"
]
KERNELS = ["Gaussian", "Lanczos-2"]
MASKS = ["Original settings (RGB columns)", "Aperture Grille", "Slot Mask", "Shadow Mask"]
CAPTURES = ["Auto", "dxcam", "mss"]
FPS_MODES = ["VSync (match display refresh)", "Uncapped", "Custom limit"]
DRIVERS = ["CPU load", "CPU temperature", "GPU temperature (NVIDIA)", "RAM usage", "Off"]

# ---- CRT-Dusha option lists ----
OVERRIDE_ITEMS = ["Original script", "CRT-Dusha (overrides)"]
DUSHA_PRESET_ORDER = ["Manual", "Vibrant", "Old TV"]
# Same values as the ReShade presets; attribute names are this script's State fields.
DUSHA_PRESETS = {
    "Manual": {},
    "Vibrant": dict(d_volt=2.0, d_cur=40.0, d_soft=0.9, d_speed=1.0),
    "Old TV": dict(d_volt=1.5, d_cur=5.0, d_soft=0.85, d_sat=1.1, d_hue=2.0,
                   d_tr=1.0, d_tg=0.95, d_tb=0.85, d_speed=8.0,
                   s_dark=0.5, s_thick=3, s_gap=2),
}
DUSHA_DECAY_MODES = ["Uniform Pulse", "Raster Sweep"]
DUSHA_SCAN_DIRS = ["Horizontal", "Vertical", "Trinitron", "Crosshatch"]
DUSHA_DITHER_SIZES = ["2x2", "4x4", "8x8"]
DUSHA_FLIP_METHODS = ["Phase Jump", "Frame Drop"]
PHI = 1.61803398875
DUSHA_MAX_LEVELS = 10
# State attributes that a Dusha preset may override
DUSHA_PRESET_ATTRS = ["d_volt", "d_cur", "d_soft", "d_sat", "d_hue", "d_tr", "d_tg", "d_tb",
                      "d_speed", "s_dark", "s_thick", "s_gap"]


def is_temp_driver(d):
    return d.startswith("CPU temp") or d.startswith("GPU temp")


class State:
    def __init__(self):
        self.active = True
        self.crt_on = True
        self.quit = False
        self.preset = "NTSC Composite"
        self.decay, self.blur, self.curv, self.gam, self.react = 0.30, 1.0, 0.0, 2.4, 1.0
        self.kernel = 0
        self.driver = DRIVERS[0]
        self.lines = 240
        self.luma = self.chroma = 1.0
        self.pal = 0
        self.pal_delay = 0
        self.chroma_lag = 0.0
        self.bloom = 0.0
        self.scan = self.mask = self.noise = self.ghost = self.dot = self.jitter = 0.0
        self.sat, self.hue = 1.0, 0.0
        # colour balance + mask options (not touched by presets)
        self.r_gain = self.g_gain = self.b_gain = 1.0
        self.mask_type = 0          # 0 = original RGB columns
        self.feather = 0.30         # soft edges on phosphor elements (types 1-3)
        self.gap = 0.10             # dead space between phosphor elements (types 1-3)
        self.mscale = 1.0           # mask size multiplier (types 1-3)
        self.apply_preset(self.preset)

        # ---------------- CRT-Dusha ----------------
        self.d_on = False                 # master enable (OFF = script behaves exactly as before)
        self.d_preset = "Manual"
        # per-feature choice: 0 = original script, 1 = CRT-Dusha overrides it
        self.ov_scan = 0
        self.ov_sat = 0
        self.ov_hue = 0
        self.ov_tint = 0                  # tint (Dusha) vs R/G/B gain (original)
        self.ov_decay = 0                 # Dusha phosphor decay vs original persistence trail
        # display
        self.d_volt, self.d_cur, self.d_mid, self.d_soft = 1.0, 1.0, 0.30, 0.0
        self.d_sat, self.d_hue = 1.0, 0.0
        self.d_tr = self.d_tg = self.d_tb = 1.0
        # timing / BFI (BFI is optional)
        self.bfi = False
        self.d_frames = 3
        self.d_decaymode = 0
        self.d_rfreq = 0.050
        self.d_slow = 1.0
        self.d_step = False
        # phosphor decay
        self.d_speed = 10.0
        self.d_levels = 5
        self.d_mr, self.d_mg, self.d_mb = 0.50, 0.60, 0.70
        # Voodoo2
        self.q16 = False
        self.d_dsize = 1
        self.d_dstr = 1.0
        # scanlines
        self.s_dir = 0
        self.s_thick = 2
        self.s_gap = 0
        self.s_dark = 0.5
        self.s_aper = 0.15
        # split screen
        self.sp_on = False
        self.sp_pos = 0.5
        self.sp_line = True
        # LCD burn-in protection
        self.lcd_safe = True
        self.lcd_method = 0
        self.lcd_jump = 0.0001
        self.lcd_flip = 1000

        self.cpu = self.ram = 0.0
        self.temp = None
        self.temp_source = ""
        self.admin = is_admin()
        self.fps = 0.0
        self.capture_kind = "?"
        self.capture_mode = "Auto"
        self.fps_mode = FPS_MODES[0]
        self.fps_cap = 60
        self.relift = False
        self.proc_priority = "High"
        self.render_thread_priority = "Time Critical"
        self.capture_thread_priority = "High"
        self.gpu_priority = 2
        self.apply_priorities_flag = True

    def apply_preset(self, name):
        self.preset = name
        for k, v in PRESETS[name].items():
            setattr(self, k, v)


def dusha_effective(S):
    """Dusha values after the Dusha preset has overridden the sliders it controls."""
    eff = {a: getattr(S, a) for a in DUSHA_PRESET_ATTRS}
    eff.update(DUSHA_PRESETS.get(S.d_preset, {}))
    return eff


def dusha_decay_curve(phase, speed, levels, mult):
    """Multi-stage Fibonacci(phi)-weighted exponential decay, per channel (CPU version)."""
    base = -phase * speed
    scale, total = 1.0, 0.0
    dec = [0.0, 0.0, 0.0]
    for _ in range(max(1, min(int(levels), DUSHA_MAX_LEVELS))):
        for c in range(3):
            dec[c] += scale * math.exp(base * scale * mult[c])
        total += scale
        scale *= PHI
    return tuple(d / total for d in dec)


# --------------------------------------------------------------------------
# Shaders
# --------------------------------------------------------------------------
VERT = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
void main(){
    v_uv = vec2(in_pos.x*0.5+0.5, 0.5-in_pos.y*0.5);   // (0,0) = top-left
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# Pass 1: Composite carrier modulation + quadrature demodulation modeled on
# PAL/CRT (pal.c / pal_core.c) and NTSC/CRT (crt-ntsc.c / crt_core.c),
# plus PAL delay-line averaging, chroma lag, bloom, jitter, ghosting,
# dot crawl, scanlines, and phosphor persistence. Output = linear light.
#
# (unchanged from the original script)
FRAG_PROCESS = """
#version 330
uniform sampler2D screen;
uniform sampler2D prev;
uniform vec2  res;
uniform float time, parity;
uniform float lines, lumaSigma, chromaSigma, chromaLag, bloomAmt;
uniform float jitter, ghost, dotamt, curv, gam, decay, sat, hue, scan;
uniform int   kernel, pal, palDelay, chromaTaps;
in vec2 v_uv;
out vec4 outc;

const int TAPS = 6;   // 13-tap kernels, 0.5 sigma spacing -> +/-3 sigma
const float PI = 3.14159265;

// NTSC YIQ matrices (crt_core.c / crt-ntsc.c)
const mat3 RGB2YIQ = mat3(0.299, 0.596, 0.211,
                          0.587,-0.274,-0.523,
                          0.114,-0.322, 0.312);
const mat3 YIQ2RGB = mat3(1.0, 1.0, 1.0,
                          0.956,-0.272,-1.106,
                          0.621,-0.647, 1.703);

// PAL YUV matrices (pal.c / pal_core.c)
const mat3 RGB2YUV = mat3(0.299,   -0.14713,  0.615,
                          0.587,   -0.28886, -0.51499,
                          0.114,    0.436,   -0.10001);
const mat3 YUV2RGB = mat3(1.0,      1.0,      1.0,
                          0.0,     -0.39465,  2.03211,
                          1.13983, -0.58060,  0.0);

float hash(vec3 p){
    p = fract(p*0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x+p.y)*p.z);
}

float kw(float u){
    if(kernel == 0) return exp(-0.5*u*u);
    float a = abs(u)/1.5;                 // Lanczos a=2
    if(a >= 2.0) return 0.0;
    if(a < 1e-4) return 1.0;
    float pa = PI*a;
    return (sin(pa)/pa) * (sin(pa*0.5)/(pa*0.5));
}

vec3 lin(vec2 uv){ return pow(max(texture(screen, uv).rgb, 0.0), vec3(gam)); }

float lumaBand(vec2 uv){
    if(lumaSigma < 0.25) return dot(lin(uv), vec3(0.299,0.587,0.114));
    float acc = 0.0, ws = 0.0;
    for(int i=-TAPS;i<=TAPS;i++){
        float u = float(i)*0.5;
        float w = kw(u);
        acc += w*dot(lin(uv + vec2(u*lumaSigma/res.x, 0.0)), vec3(0.299,0.587,0.114));
        ws += w;
    }
    return acc/max(ws, 1e-5);
}

float modulateAt(vec2 uv, float phaseShift, float vSign){
    vec3 rgbc = lin(uv);
    float y = dot(rgbc, vec3(0.299, 0.587, 0.114));
    vec2  c = (pal == 1) ? (RGB2YUV * rgbc).yz : (RGB2YIQ * rgbc).yz;
    float sCar = sin(phaseShift);
    float cCar = cos(phaseShift);
    float modA = c.x * sCar;
    float modB = (pal == 1) ? (vSign * c.y * cCar) : (c.y * cCar);
    return y + modA + modB;
}

// Quadrature demodulation with a comb-type window: taps are one carrier sample apart
// (pi/2 phase step, 1/1920 uv) and the triangle window half-width (chromaTaps) is a multiple
// of the 4-sample carrier period, so luma and the 2x-carrier terms cancel exactly.
vec2 demodulateQuadrature(vec2 uv, float phaseShift, float vSign){
    if(chromaSigma < 0.25){
        vec3 c0 = lin(uv);
        return (pal == 1) ? (RGB2YUV*c0).yz : (RGB2YIQ*c0).yz;
    }
    float accA = 0.0, accB = 0.0, ws = 0.0;
    float wc = float(chromaTaps);
    for(int i=-11; i<=11; i++){
        if(abs(i) >= chromaTaps) continue;
        float k = float(i);
        float w = (wc - abs(k)) / wc;
        vec2 sUv = uv + vec2(k / 1920.0, 0.0);
        float p = phaseShift + k * (PI * 0.5);
        float comp = modulateAt(sUv, p, vSign);
        accA += w * comp * 2.0 * sin(p);
        accB += w * comp * 2.0 * cos(p);
        ws += w;
    }
    float demA = accA / max(ws, 1e-5);
    float demB = (accB / max(ws, 1e-5)) * ((pal == 1) ? vSign : 1.0);
    return vec2(demA, demB);
}

void main(){
    vec2 uv = v_uv;

    // screen curvature
    vec2 cc = uv*2.0-1.0;
    cc += cc*(cc.yx*cc.yx)*curv;
    vec2 suv = cc*0.5+0.5;
    float inside = step(0.0,suv.x)*step(suv.x,1.0)*step(0.0,suv.y)*step(suv.y,1.0);

    // per-line horizontal jitter + slow wobble
    float ln  = floor(uv.y*lines);
    float lj  = hash(vec3(ln, floor(time*30.0), 1.0)) - 0.5;
    float wob = sin(uv.y*7.0 + time*1.7);
    suv.x += jitter*(lj*3.0 + wob*1.5)*(res.x/1920.0)/res.x;

    float lineH = 1.0/lines;
    float pxCoord = gl_FragCoord.x * (1920.0/res.x);
    float carrierPhase = pxCoord * (PI * 0.5);

    float Y = lumaBand(suv);
    vec2  dem;

    if(pal == 1){
        // PAL 25 Hz / field progression + V-axis alternation per line (pal.c / pal_core.c)
        float p25 = parity * (PI * 0.5) + ln * (PI * 0.02);
        float vSign = (mod(ln, 2.0) < 0.5) ? 1.0 : -1.0;
        vec2 cUv = suv + vec2(chromaLag*(res.x/1920.0)/res.x, 0.0);
        dem = demodulateQuadrature(cUv, carrierPhase + p25, vSign);
        if(palDelay == 1){
            float prevSign = -vSign;
            float prevP25 = parity * (PI * 0.5) + (ln - 1.0) * (PI * 0.02);
            vec2 demPrev = demodulateQuadrature(cUv - vec2(0.0, lineH), carrierPhase + prevP25, prevSign);
            dem = 0.5*(dem + demPrev);
        }
    } else {
        // NTSC 227.5 cycles/line phase inversion across lines/frames (crt-ntsc.c)
        float ntscFlip = mod(ln + parity, 2.0) * PI;
        vec2 cUv = suv + vec2(chromaLag*(res.x/1920.0)/res.x, 0.0);
        dem = demodulateQuadrature(cUv, carrierPhase + ntscFlip, 1.0);
    }

    // dot crawl: modulated chroma subcarrier crosstalk into luma
    float crawlFlip = (pal == 1) ? parity : (mod(ln + parity, 2.0));
    float checker = mod(floor(pxCoord*0.5) + crawlFlip, 2.0)*2.0 - 1.0;
    Y += dotamt*0.35*length(dem)*checker;

    // RF echo (ghost)
    if(ghost > 0.0) Y += ghost*lumaBand(suv - vec2(16.0/1920.0, 0.0));

    // Hue & saturation applied once after quadrature demodulation
    float ch_ = cos(hue), sh = sin(hue);
    vec2 rot = vec2(dem.x*ch_ - dem.y*sh, dem.x*sh + dem.y*ch_);
    vec2 ch = sat * rot;
    vec3 rgb = (pal == 1) ? (YUV2RGB * vec3(Y, ch)) : (YIQ2RGB * vec3(Y, ch));

    // Gamut-preserving clamp: pull out-of-range colours toward the luma instead of clamping
    // each channel (per-channel clamping of a negative green is what produced purple).
    float Yc = max(Y, 0.0);
    float mn = min(rgb.r, min(rgb.g, rgb.b));
    if(mn < 0.0){
        float t = Yc / max(Yc - mn, 1e-5);
        rgb = vec3(Yc) + (rgb - vec3(Yc)) * t;
    }
    rgb = max(rgb, 0.0);

    // scanlines + bloom (modeled after PAL_DO_BLOOM / CRT_DO_BLOOM)
    float pos  = fract(uv.y*lines);
    float l    = clamp(Y, 0.0, 1.0);
    float bloomWidth = bloomAmt * 0.14 * l;
    float w    = mix(0.20, 0.45, sqrt(l)) + bloomWidth;
    float beam = exp(-0.5*pow((pos-0.5)/w, 2.0));
    rgb *= mix(1.0, beam, scan)*(1.0 + 0.2*scan);

    // phosphor decay only
    vec3 cur = rgb * inside;
    vec3 pv = texture(prev, vec2(v_uv.x, 1.0 - v_uv.y)).rgb;
    float d = clamp(decay, 0.0, 0.95);
    vec3 pTail = pv * (d * d * 0.25);
    pTail = mix(vec3(0.0), pTail, step(0.015, pTail));
    rgb = max(cur, pTail);
    outc = vec4(rgb, 1.0);
}
"""

# Pass 2: sRGB encode, [CRT-Dusha], RGB gain, phosphor mask, hum bar, RF snow/streak noise,
# [Dusha split screen + frame counter].
FRAG_PRESENT = """
#version 330
uniform sampler2D tex;
uniform sampler2D screenRaw;     // untouched capture (split-screen comparison)
uniform vec2  res;
uniform int   srcLinear;
uniform float time, noiseAmt, mask, hum, lines;
uniform int   maskType;
uniform float feather, gap, mscale;
uniform vec3  gain;

// ---------------- CRT-Dusha uniforms ----------------
uniform int   dOn;                        // Dusha tone pipeline enabled
uniform float dVolt, dCur, dMid, dSoft;   // contrast, brightness, midtone, softclip
uniform int   dQuant, dDSize;             // Voodoo2 RGB565 + bayer size
uniform float dDStr;
uniform int   dBfi, dDecayMode, dLevels;  // BFI/decay cycle
uniform vec3  dDecayU, dMult;             // precomputed uniform-pulse decay, per-channel mult
uniform float dFraction, dRFreq, dSpeed;
uniform int   dScanOn, dSDir, dSThick, dSGap;
uniform float dSDark, dSAper;
uniform int   dSatOn, dHueOn, dTintOn;
uniform float dSatV;
uniform vec3  dHueR0, dHueR1, dHueR2, dTint;
uniform int   dSplit, dDivider, dDebug, dFrameNum, dFramesPer;
uniform float dSplitPos;

in vec2 v_uv;
out vec4 outc;

const float PHI = 1.61803398875;
const float INV_31 = 0.032258064516129;
const float INV_63 = 0.015873015873016;

const int bayer2[4] = int[4](0, 2, 3, 1);
const int bayer4[16] = int[16](
     0,  8,  2, 10,
    12,  4, 14,  6,
     3, 11,  1,  9,
    15,  7, 13,  5);
const int bayer8[64] = int[64](
     0, 32,  8, 40,  2, 34, 10, 42,
    48, 16, 56, 24, 50, 18, 58, 26,
    12, 44,  4, 36, 14, 46,  6, 38,
    60, 28, 52, 20, 62, 30, 54, 22,
     3, 35, 11, 43,  1, 33,  9, 41,
    51, 19, 59, 27, 49, 17, 57, 25,
    15, 47,  7, 39, 13, 45,  5, 37,
    63, 31, 55, 23, 61, 29, 53, 21);
// 4x5 bitmap digits for the frame-step overlay
const int digitRows[50] = int[50](
    0xE,0xA,0xA,0xA,0xE,  0x4,0xC,0x4,0x4,0xE,  0xE,0x2,0xE,0x8,0xE,  0xE,0x2,0x6,0x2,0xE,
    0xA,0xA,0xE,0x2,0x2,  0xE,0x8,0xE,0x2,0xE,  0xE,0x8,0xE,0xA,0xE,  0xE,0x2,0x4,0x4,0x4,
    0xE,0xA,0xE,0xA,0xE,  0xE,0xA,0xE,0x2,0xE);

float hash(vec3 p){
    p = fract(p*0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x+p.y)*p.z);
}
vec3 enc(vec3 c){
    c = clamp(c, 0.0, 1.0);
    return mix(c*12.92, 1.055*pow(c, vec3(1.0/2.4)) - 0.055, step(0.0031308, c));
}

// ---------------- CRT-Dusha ----------------
float bayerVal(ivec2 p){
    if(dDSize == 0) return float(bayer2[(p.y % 2) * 2 + (p.x % 2)]) * 0.333333333;
    if(dDSize == 1) return float(bayer4[(p.y % 4) * 4 + (p.x % 4)]) * 0.066666667;
    return float(bayer8[(p.y % 8) * 8 + (p.x % 8)]) * 0.015873016;
}

// multi-stage phi-weighted exponential decay (only evaluated per pixel for Raster Sweep)
vec3 decayCurve(float phase){
    float base = -phase * dSpeed;
    float sc = 1.0, sum = 0.0;
    vec3 dec = vec3(0.0);
    for(int i = 0; i < 10; i++){
        if(i >= dLevels) break;
        dec += sc * exp(base * sc * dMult);
        sum += sc;
        sc *= PHI;
    }
    return dec / sum;
}

float drawDigit(int d, ivec2 pix, ivec2 org, int sc){
    ivec2 l = pix - org;
    if(l.x < 0 || l.x >= 4*sc || l.y < 0 || l.y >= 5*sc) return 0.0;
    int val = digitRows[d*5 + l.y/sc];
    return float((val >> (3 - l.x/sc)) & 1);
}

vec3 dusha(vec3 c, ivec2 pc){
    // power-law (contrast / beam voltage)
    c = pow(max(c, 0.0), vec3(dVolt));
    // midtone boost (parabola peaks at 0.5, blacks untouched)
    vec3 mt = c * (1.0 - c) * 4.0;
    c += c * mt * dMid;
    // Voodoo2 RGB565 quantisation with ordered dither
    if(dQuant == 1){
        float d = (bayerVal(pc) - 0.5) * dDStr;
        c.r = floor(clamp(c.r * 31.0 + d, 0.0, 31.0)) * INV_31;
        c.g = floor(clamp(c.g * 63.0 + d, 0.0, 63.0)) * INV_63;
        c.b = floor(clamp(c.b * 31.0 + d, 0.0, 31.0)) * INV_31;
    }
    // beam current (brightness)
    c *= dCur;
    // BFI / phosphor decay cycle (optional)
    if(dBfi == 1){
        vec3 dec = (dDecayMode == 0) ? dDecayU
                 : decayCurve(fract((1.0 - v_uv.y) * dRFreq + dFraction));
        c *= dec;
    }
    // scanlines
    if(dScanOn == 1){
        float sm = 1.0;
        if(dSGap > 0){
            int pat = dSThick + dSGap;
            if(dSDir == 0){
                sm = ((pc.y % pat) < dSGap) ? (1.0 - dSDark) : 1.0;
            } else if(dSDir == 1){
                sm = ((pc.x % pat) < dSGap) ? (1.0 - dSDark) : 1.0;
            } else if(dSDir == 2){
                float mH = ((pc.y % pat) < dSGap) ? (1.0 - dSDark) : 1.0;
                int sub = int(gl_FragCoord.x * 3.0) % 3;
                float dim = 1.0 - dSAper;
                vec3 ap = vec3(sub == 0 ? 1.0 : dim, sub == 1 ? 1.0 : dim, sub == 2 ? 1.0 : dim);
                c *= ap * mH;
            } else {
                float mH = ((pc.y % pat) < dSGap) ? (1.0 - dSDark) : 1.0;
                float mV = ((pc.x % pat) < dSGap) ? (1.0 - dSDark) : 1.0;
                sm = mH * mV;
            }
        }
        c *= sm;
    }
    // softclip (Reinhard-style rolloff)
    vec3 sc = c / (1.0 + c);
    c = mix(c, sc, dSoft);
    // saturation
    if(dSatOn == 1){
        float l = dot(c, vec3(0.299, 0.587, 0.114));
        c = mix(vec3(l), c, dSatV);
    }
    // hue shift (matrix built on the CPU)
    if(dHueOn == 1) c = vec3(dot(dHueR0, c), dot(dHueR1, c), dot(dHueR2, c));
    // phosphor tint
    if(dTintOn == 1) c *= dTint;
    return c;
}

// coverage of a soft-edged element: 1 inside 'edge', 0 outside, blended over +/- s
float soft(float d, float edge, float s){
    return 1.0 - smoothstep(edge - s, edge + s, d);
}

// Phosphor mask value (per channel, 0..1) at a continuous screen position p (pixels).
// Triad is 3*mscale px wide. gap = dead space between elements, feather = edge softness.
vec3 maskAt(vec2 p){
    vec2 q = p / mscale;
    if(maskType == 3){
        // Shadow mask: round dots in a delta (triangle) arrangement, rows staggered
        float row = floor(q.y / 3.0);
        vec2 l = vec2(mod(q.x - 1.5*mod(row, 2.0), 3.0), mod(q.y, 3.0));
        float rr = 0.72 * (1.0 - gap);
        float s  = 0.72 * feather * 0.8 + 0.02;
        return vec3(soft(length(l - vec2(0.75, 0.9)), rr, s),
                    soft(length(l - vec2(2.25, 0.9)), rr, s),
                    soft(length(l - vec2(1.50, 2.1)), rr, s));
    }
    // Aperture grille / slot mask: vertical R,G,B stripes
    float ph = fract(q.x / 3.0);
    float hw = (1.0/6.0) * (1.0 - gap);
    float s  = (1.0/6.0) * feather * 0.8 + 0.002;
    float d0 = abs(ph - 1.0/6.0); d0 = min(d0, 1.0 - d0);
    float d1 = abs(ph - 3.0/6.0); d1 = min(d1, 1.0 - d1);
    float d2 = abs(ph - 5.0/6.0); d2 = min(d2, 1.0 - d2);
    vec3 m = vec3(soft(d0, hw, s), soft(d1, hw, s), soft(d2, hw, s));
    if(maskType == 2){
        // slot mask: stripes broken into slots, gaps staggered between adjacent triads
        float row = q.y / 6.0 + 0.5*mod(floor(q.x / 3.0), 2.0);
        float dy = min(fract(row), 1.0 - fract(row));
        float vg = 0.06 + 0.18*gap;
        float sy = 0.01 + 0.12*feather;
        m *= smoothstep(vg - sy, vg + sy, dy);
    }
    return m;
}

// Supersampled over the pixel so gap/feather read correctly even at 1 px per phosphor.
vec3 maskCov(vec2 fc){
    vec3 acc = vec3(0.0);
    float n = 0.0;
    int ny = (maskType == 1) ? 1 : 4;
    for(int j=0; j<4; j++){
        if(j >= ny) break;
        for(int i=0; i<4; i++){
            vec2 o = vec2((float(i)+0.5)*0.25, (float(j)+0.5)/float(ny)) - 0.5;
            acc += maskAt(fc + o);
            n += 1.0;
        }
    }
    return acc / n;
}

void main(){
    // top-left origin integer pixel (matches ReShade's SV_Position convention)
    ivec2 pc = ivec2(int(floor(gl_FragCoord.x)), int(floor(res.y - gl_FragCoord.y)));

    vec3 e = (srcLinear == 1) ? enc(texture(tex, vec2(v_uv.x, 1.0 - v_uv.y)).rgb)
                              : clamp(texture(tex, v_uv).rgb, 0.0, 1.0);

    if(dOn == 1) e = dusha(e, pc);

    e *= 1.0 + hum*sin(v_uv.y*6.2831*1.5 - time*1.3);

    // red / green / blue gain
    e *= gain;

    if(mask > 0.0){
        vec3 m;
        if(maskType == 0){
            // ORIGINAL: hard 1px RGB columns, exactly as in the first version
            int k = int(mod(gl_FragCoord.x, 3.0));
            m = vec3(float(k==0), float(k==1), float(k==2));
        } else {
            m = maskCov(gl_FragCoord.xy);
        }
        e *= ((1.0-mask) + mask*1.5*m)/(1.0 - 0.5*mask);
    }

    if(noiseAmt > 0.0){
        float t = mod(floor(time*60.0), 4096.0);
        float snow   = hash(vec3(gl_FragCoord.xy, t)) - 0.5;
        float sx     = max(1.0, res.x/1920.0*6.0);
        float streak = hash(vec3(floor(gl_FragCoord.x/sx), floor(gl_FragCoord.y/(res.y/lines)), t)) - 0.5;
        e += noiseAmt*(0.65*snow + 0.7*streak);
    }
    vec3 col = clamp(e, 0.0, 1.0);

    // split-screen comparison: effect on the left, original capture on the right
    if(dSplit == 1){
        if(v_uv.x > dSplitPos) col = texture(screenRaw, v_uv).rgb;
        if(dDivider == 1 && abs(v_uv.x - dSplitPos) < 2.0 / res.x) col = vec3(1.0);
    }

    // frame-step debug counter (top-right)
    if(dDebug == 1){
        int sc = 4, margin = 8;
        int nd = (dFramesPer >= 10) ? 2 : 1;
        int tw = nd * 5 * sc;
        ivec2 org = ivec2(int(res.x) - tw - margin, margin);
        float ink = 0.0;
        if(dFramesPer >= 10) ink += drawDigit(dFrameNum / 10, pc, org, sc);
        ink += drawDigit(dFrameNum % 10, pc, org + ivec2(((dFramesPer >= 10) ? 5 : 0) * sc, 0), sc);
        if(ink > 0.0) col = vec3(1.0, 1.0, 0.0);
    }
    outc = vec4(col, 1.0);
}
"""


# --------------------------------------------------------------------------
# Screen capture
# --------------------------------------------------------------------------
_DX_CAM = None


class _DxBackend:
    kind = "dxcam"

    def __init__(self):
        global _DX_CAM
        import dxcam
        if _DX_CAM is None:
            _DX_CAM = dxcam.create(output_idx=0, output_color="BGRA")
        self.cam = _DX_CAM

    def grab(self):
        f = self.cam.grab()
        if f is None:
            return None
        h, w = f.shape[:2]
        return np.array(f, copy=True, order="C"), w, h

    def close(self):
        pass


class _MssBackend:
    kind = "mss"

    def __init__(self):
        import mss
        self.sct = mss.mss()
        self.mon = self.sct.monitors[1]

    def grab(self):
        shot = self.sct.grab(self.mon)
        return shot.raw, shot.width, shot.height

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass


class Grabber:
    def __init__(self, mode="Auto", S=None):
        self.lock = threading.Lock()
        self.latest = None
        self.seq = 0
        self.seen = 0
        self.kind = None
        self.mode = mode
        self.failed = False
        self.paused = False
        self._stop = None
        self._thread = None
        self._want_mss = False
        self._last_err = None
        self._S = S
        self._capture_priority = "High"
        self.set_mode(mode)

    def set_mode(self, mode):
        self._halt()
        self.mode = mode
        self.kind = None
        self.failed = False
        self._want_mss = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(mode, self._stop), daemon=True)
        self._thread.start()

    def _halt(self):
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=2.0)
            self._thread = None

    def force_mss(self):
        self._want_mss = True

    def grab(self):
        with self.lock:
            if self.seq == self.seen:
                return None
            self.seen = self.seq
            return self.latest

    def _make(self, name):
        try:
            return _MssBackend() if name == "mss" else _DxBackend()
        except Exception as e:
            print("%s unavailable: %s%s" % (name, e, "  (pip install mss)" if name == "mss" else ""))
            return None

    def _run(self, mode, stop):
        try:
            _set_thread_priority(getattr(self, "_capture_priority", "High"))
        except Exception:
            pass
        backend = None
        for name in (["mss", "dxcam"] if mode == "mss" else ["dxcam", "mss"]):
            backend = self._make(name)
            if backend:
                break
        if backend is None:
            print("No capture backend available. Run: pip install dxcam mss")
            self.failed = True
            return
        self.kind = backend.kind
        t_start = time.time()
        got = False
        while not stop.is_set():
            if self.paused:
                t_start = time.time()
                time.sleep(0.05)
                continue
            if self._want_mss and backend.kind != "mss":
                self._want_mss = False
                nb = self._make("mss")
                if nb:
                    backend = nb
                    self.kind = "mss"
                    print("Switched to mss capture.")
                continue
            try:
                r = backend.grab()
            except Exception as e:
                if str(e) != self._last_err:
                    print("Capture error:", e)
                    self._last_err = str(e)
                if mode == "Auto" and backend.kind == "dxcam":
                    self._want_mss = True
                time.sleep(0.05)
                continue
            if r is None:
                if (not got and mode == "Auto" and backend.kind == "dxcam"
                        and time.time() - t_start > 2.0):
                    print("dxcam produced no frames for 2s -> falling back to mss.")
                    self._want_mss = True
                time.sleep(0.002)
                continue
            got = True
            with self.lock:
                self.latest = r
                self.seq += 1
            if backend.kind == "mss":
                time.sleep(0.001)
        backend.close()


# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------
_HELPERS = []


def shutdown_helpers():
    for pr in list(_HELPERS):
        try:
            pr.terminate()
        except Exception:
            pass
    _HELPERS.clear()


atexit.register(shutdown_helpers)


def cpu_label_score(name):
    n = name.lower()
    for bad in ("gpu", "drive", "nvme", "ssd", "vrm", "chipset", "motherboard", "system", "pch",
                "hdd", "water", "liquid", "aio"):
        if bad in n:
            return 0
    if "package" in n:
        return 100
    if "tctl" in n or "tdie" in n:
        return 90
    if "core max" in n or n.endswith(" max"):
        return 80
    if "cpu" in n:
        return 70
    if "core" in n:
        return 50
    return 0


def _parse_hwinfo(buf):
    if len(buf) < 44:
        return None
    (_sig, _ver, _rev, _poll, _off_s, _size_s, _n_s,
     off_r, size_r, n_r) = struct.unpack_from("<IIIqIIIIII", buf, 0)
    if size_r < 292 or n_r <= 0 or n_r > 5000 or off_r < 44:
        return None
    best = None
    for i in range(n_r):
        o = off_r + i * size_r
        if o + 292 > len(buf):
            break
        if struct.unpack_from("<I", buf, o)[0] != 1:
            continue
        lab = (buf[o + 140:o + 268].split(b"\0")[0] or buf[o + 12:o + 140].split(b"\0")[0])
        lab = lab.decode("latin-1", "replace")
        unit = buf[o + 268:o + 284].split(b"\0")[0].decode("latin-1", "replace").lower()
        val = struct.unpack_from("<d", buf, o + 284)[0]
        if "f" in unit and "c" not in unit:
            val = (val - 32.0) / 1.8
        sc = cpu_label_score(lab)
        if sc <= 0 or not (5.0 < val < 125.0):
            continue
        if best is None or (sc, val) > best[0]:
            best = ((sc, val), val, lab)
    return (best[1], best[2]) if best else None


def read_hwinfo():
    try:
        k32 = ctypes.windll.kernel32
        k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        k32.OpenFileMappingW.restype = wintypes.HANDLE
        k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.c_size_t]
        k32.MapViewOfFile.restype = ctypes.c_void_p
        k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenFileMappingW(0x4, False, "Global\\HWiNFO_SENS_SM2")
        if not h:
            return None
        base = k32.MapViewOfFile(h, 0x4, 0, 0, 0)
        if not base:
            k32.CloseHandle(h)
            return None
        try:
            hdr = struct.unpack("<IIIqIIIIII", ctypes.string_at(base, 44))
            total = max(hdr[4] + hdr[5] * hdr[6], hdr[7] + hdr[8] * hdr[9])
            if total <= 44 or total > 64 * 1024 * 1024:
                return None
            return _parse_hwinfo(ctypes.string_at(base, total))
        finally:
            k32.UnmapViewOfFile(base)
            k32.CloseHandle(h)
    except Exception:
        return None


_NVML = {"tried": False, "lib": None, "handle": None}


def read_gpu_temp():
    try:
        if not _NVML["tried"]:
            _NVML["tried"] = True
            lib = None
            for path in ("nvml.dll", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll",
                         r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.CDLL(path)
                    break
                except OSError:
                    continue
            if lib is not None and lib.nvmlInit_v2() == 0:
                h = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) == 0:
                    _NVML["lib"], _NVML["handle"] = lib, h
        lib = _NVML["lib"]
        if lib is not None:
            t = ctypes.c_uint()
            if lib.nvmlDeviceGetTemperature(_NVML["handle"], 0, ctypes.byref(t)) == 0:
                return float(t.value)
    except Exception:
        pass
    return None


def read_thermal_counter():
    try:
        r = subprocess.run(["typeperf", r"\Thermal Zone Information(*)\Temperature", "-sc", "1"],
                           capture_output=True, text=True, timeout=6, creationflags=0x08000000)
        vals = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith('"') and "PDH-CSV" not in line:
                for p in [x.strip('"') for x in line.split('","')][1:]:
                    try:
                        v = float(p)
                    except ValueError:
                        continue
                    c = v - 273.15 if v > 200 else v
                    if 10 < c < 125:
                        vals.append(c)
        return max(vals) if vals else None
    except Exception:
        return None


PS_TEMP_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$script:bestS = -1; $script:bestV = 0.0; $script:bestN = ''
function Score([string]$n) {
  if ($n -match 'GPU|Drive|NVMe|SSD|VRM|Chipset|Motherboard|System|PCH|HDD|Water|Liquid') { return 0 }
  if ($n -match 'Package') { return 100 }
  if ($n -match 'Tctl|Tdie') { return 90 }
  if ($n -match 'Core Max|Max') { return 80 }
  if ($n -match 'CPU') { return 70 }
  if ($n -match 'Core') { return 50 }
  return 0
}
function Consider($name, $val, $label) {
  if ($val -eq $null) { return }
  $sc = Score ([string]$name)
  if ($sc -lt 1) { return }
  if ($sc -gt $script:bestS -or ($sc -eq $script:bestS -and [double]$val -gt $script:bestV)) {
    $script:bestS = $sc; $script:bestV = [double]$val; $script:bestN = $label + ': ' + $name
  }
}
$dll = '__DLL__'
$comp = $null
$warnedNoSensors = $false
if (Test-Path $dll) {
  try {
    $libDir = Split-Path $dll
    Get-ChildItem $libDir -Filter *.dll | Unblock-File
    $resolver = [System.ResolveEventHandler] {
      param($sender, $e)
      $n = ($e.Name -split ',')[0]
      $p = Join-Path $libDir ($n + '.dll')
      if (Test-Path $p) { return [System.Reflection.Assembly]::LoadFrom($p) }
      return $null
    }
    [AppDomain]::CurrentDomain.add_AssemblyResolve($resolver)
    Add-Type -Path $dll
    $comp = New-Object LibreHardwareMonitor.Hardware.Computer
    $comp.IsCpuEnabled = $true
    $comp.Open()
  } catch {
    $ex = $_.Exception
    $msgs = @()
    while ($ex) {
      $msgs += $ex.Message
      if ($ex.PSObject.Properties['LoaderExceptions']) {
        foreach ($le in $ex.LoaderExceptions) { if ($le) { $msgs += $le.Message } }
      }
      $ex = $ex.InnerException
    }
    $txt = (($msgs | Select-Object -Unique) -join ' || ') -replace '[\r\n]+', ' '
    [Console]::WriteLine('INFO|LibreHardwareMonitorLib.dll could not be loaded: ' + $txt)
    $comp = $null
  }
}
while (Get-Process -Id __PID__ -ErrorAction SilentlyContinue) {
  $script:bestS = -1; $script:bestV = 0.0; $script:bestN = ''
  if ($comp) {
    foreach ($hw in $comp.Hardware) {
      if ($hw.HardwareType -eq 'Cpu') {
        $hw.Update()
        foreach ($s in $hw.Sensors) {
          if ($s.SensorType -eq 'Temperature') { Consider $s.Name $s.Value 'LibreHardwareMonitor' }
        }
      }
    }
  }
  if ($comp -and $script:bestS -lt 1 -and -not $warnedNoSensors) {
    $warnedNoSensors = $true
    [Console]::WriteLine('INFO|LibreHardwareMonitor loaded but returned no CPU temperature sensor. Recent versions need the PawnIO driver installed, and administrator rights.')
  }
  if ($script:bestS -lt 1) {
    foreach ($ns in 'root/LibreHardwareMonitor', 'root/OpenHardwareMonitor') {
      foreach ($s in (Get-CimInstance -Namespace $ns -ClassName Sensor)) {
        if ($s.SensorType -eq 'Temperature') { Consider $s.Name $s.Value ('WMI ' + $ns.Split('/')[1]) }
      }
      if ($script:bestS -ge 1) { break }
    }
  }
  if ($script:bestS -lt 1) {
    $z = Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature
    if ($z) {
      $m = ($z | Measure-Object -Property CurrentTemperature -Maximum).Maximum
      $script:bestV = [math]::Round($m / 10.0 - 273.15, 1); $script:bestS = 1; $script:bestN = 'ACPI thermal zone (WMI)'
    }
  }
  if ($script:bestS -ge 1) {
    [Console]::WriteLine('T|' + $script:bestV.ToString([Globalization.CultureInfo]::InvariantCulture) + '|' + $script:bestN)
  } else { [Console]::WriteLine('NONE') }
  [Console]::Out.Flush()
  Start-Sleep -Seconds 2
}
"""


class CpuTempChain:
    def __init__(self, S):
        self.S = S
        self.ps = None
        self.ps_val = None
        self.ps_src = ""
        self.ps_stamp = 0.0
        self.ps_next = 0.0
        self.tc_next = 0.0
        self.tc_fail = 0
        self.tc_val = None
        self.tc_stamp = 0.0
        self.t_start = time.time()
        self.reported = False

    def _pump(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("T|"):
                    _, v, name = line.split("|", 2)
                    self.ps_val, self.ps_src, self.ps_stamp = float(v), name, time.time()
                elif line == "NONE":
                    self.ps_val = None
                elif line.startswith("INFO|"):
                    print("[temp] " + line[5:])
        except Exception:
            pass

    def _ensure_ps(self):
        if self.ps is not None:
            code = self.ps.poll()
            if code is None:
                return
            print("[temp] PowerShell helper exited (code %s); retrying in 15 s" % code)
            self.ps = None
            self.ps_val = None
            self.ps_next = time.time() + 15.0
        if time.time() < self.ps_next:
            return
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            dll = os.path.join(here, "LibreHardwareMonitorLib.dll").replace("'", "''")
            script = PS_TEMP_SCRIPT.replace("__DLL__", dll).replace("__PID__", str(os.getpid()))
            enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
            self.ps = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-EncodedCommand", enc],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                text=True, bufsize=1, creationflags=0x08000000)
            _HELPERS.append(self.ps)
            threading.Thread(target=self._pump, args=(self.ps,), daemon=True).start()
        except Exception as e:
            print("[temp] could not start PowerShell helper:", e)
            self.ps = None

    def stop(self):
        if self.ps is not None:
            try:
                self.ps.terminate()
            except Exception:
                pass
            self.ps = None
            self.ps_val = None

    def read(self):
        hw = read_hwinfo()
        if hw:
            self.stop()
            return hw[0], "HWiNFO: " + hw[1]
        self._ensure_ps()
        now = time.time()
        if self.ps_val is not None and now - self.ps_stamp < 8.0:
            return self.ps_val, self.ps_src
        if now >= self.tc_next:
            v = read_thermal_counter()
            if v is None:
                self.tc_fail += 1
                self.tc_next = now + (8.0 if self.tc_fail < 3 else 60.0)
            else:
                self.tc_fail = 0
                self.tc_next = now + 8.0
                self.tc_val, self.tc_stamp = v, now
        if self.tc_val is not None and now - self.tc_stamp < 20.0:
            return self.tc_val, "ACPI thermal zone (perf counter)"
        if not self.reported and now - self.t_start > 8.0:
            self.reported = True
            print("[temp] No CPU temperature source found (admin: %s)." % ("yes" if self.S.admin else "no"))
        return None, ""


def temp_worker(S):
    chain = CpuTempChain(S)
    while not S.quit:
        d = S.driver
        if d.startswith("CPU temp"):
            t, src = chain.read()
        elif d.startswith("GPU temp"):
            chain.stop()
            t = read_gpu_temp()
            src = "NVIDIA NVML" if t is not None else ""
        else:
            chain.stop()
            time.sleep(0.5)
            continue
        S.temp, S.temp_source = t, src
        for _ in range(10):
            if S.quit:
                break
            time.sleep(0.1)
    chain.stop()


# --------------------------------------------------------------------------
# Settings menu (own thread; toggled by HOME)
# --------------------------------------------------------------------------
SLIDERS = [
    ("Active lines", "lines", 180.0, 576.0),
    ("Phosphor decay", "decay", 0.0, 0.95),
    ("Blur / bandwidth", "blur", 0.0, 3.0),
    ("Chroma lag (px)", "chroma_lag", -4.0, 4.0),
    ("Scanline bloom", "bloom", 0.0, 1.0),
    ("RF noise", "noise", 0.0, 0.5),
    ("Scanlines", "scan", 0.0, 1.0),
    ("Ghosting (RF echo)", "ghost", 0.0, 0.5),
    ("Dot crawl", "dot", 0.0, 1.0),
    ("Line jitter", "jitter", 0.0, 2.0),
    ("Saturation", "sat", 0.0, 2.0),
    ("Screen curvature", "curv", 0.0, 0.3),
    ("CRT gamma", "gam", 1.8, 2.8),
    ("Sensor reactivity", "react", 0.0, 2.0),
]

# "Mask / Color" tab
MASK_SLIDERS = [
    ("Mask strength", "mask", 0.0, 1.0),
    ("Mask feathering", "feather", 0.0, 1.0),
    ("Pixel space (gap)", "gap", 0.0, 0.7),
    ("Mask size (x)", "mscale", 1.0, 4.0),
    ("Red", "r_gain", 0.0, 2.0),
    ("Green", "g_gain", 0.0, 2.0),
    ("Blue", "b_gain", 0.0, 2.0),
]
ALL_SLIDERS = SLIDERS + MASK_SLIDERS    # the sliders an original-script preset can change

# CRT-Dusha sliders
DUSHA_DISPLAY_SLIDERS = [
    ("Contrast (beam voltage)", "d_volt", 0.01, 10.0),
    ("Brightness (beam current)", "d_cur", 0.1, 1000.0),
    ("Midtone boost", "d_mid", 0.0, 50.0),
    ("Softclip strength", "d_soft", 0.0, 1.0),
    ("Color saturation", "d_sat", 0.0, 2.0),
    ("Hue shift (deg)", "d_hue", -180.0, 180.0),
    ("Phosphor tint R", "d_tr", 0.0, 1.0),
    ("Phosphor tint G", "d_tg", 0.0, 1.0),
    ("Phosphor tint B", "d_tb", 0.0, 1.0),
]
DUSHA_TIMING_SLIDERS = [
    ("Frames per decay cycle", "d_frames", 1, 12),
    ("Raster frequency", "d_rfreq", 0.001, 1.0),
    ("Debug: slow motion factor", "d_slow", 1.0, 500.0),
    ("Global decay speed", "d_speed", 0.01, 50.0),
    ("Decay stages", "d_levels", 1, 10),
    ("Red decay multiplier", "d_mr", 0.01, 5.0),
    ("Green decay multiplier", "d_mg", 0.01, 5.0),
    ("Blue decay multiplier", "d_mb", 0.01, 5.0),
    ("LCD: phase jump rate", "lcd_jump", 0.0001, 0.1),
    ("LCD: frame drop interval", "lcd_flip", 1000, 100000),
]
DUSHA_SCAN_SLIDERS = [
    ("Scanline width (px)", "s_thick", 1, 8),
    ("Scanline gap width (px)", "s_gap", 0, 4),
    ("Gap darkness", "s_dark", 0.0, 1.0),
    ("Trinitron color separation", "s_aper", 0.0, 1.0),
    ("V2 dither strength", "d_dstr", 0.0, 2.0),
    ("Split position", "sp_pos", 0.0, 1.0),
]

INT_ATTRS = {"lines", "fps_cap", "d_frames", "d_levels", "s_thick", "s_gap", "lcd_flip"}
FMT = {"d_rfreq": "%.3f", "lcd_jump": "%.4f", "d_hue": "%.1f", "d_slow": "%.0f"}


class MenuThread(threading.Thread):
    def __init__(self, S):
        super().__init__(daemon=True)
        self.S = S

    def run(self):
        S = self.S
        root = tk.Tk()
        root.title("CRT / RF Overlay")
        root.geometry("+40+40")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        pad = dict(padx=8, pady=2)

        outer = ttk.Frame(root, padding=8)
        outer.grid(sticky="nsew")
        nb = ttk.Notebook(outer)
        nb.grid(row=0, column=0, sticky="nsew")
        tab_crt = ttk.Frame(nb, padding=4)
        tab_mask = ttk.Frame(nb, padding=4)
        tab_dusha = ttk.Frame(nb, padding=4)
        tab_dtime = ttk.Frame(nb, padding=4)
        tab_dscan = ttk.Frame(nb, padding=4)
        tab_perf = ttk.Frame(nb, padding=4)
        tab_pri = ttk.Frame(nb, padding=4)
        nb.add(tab_crt, text="CRT/RF")
        nb.add(tab_mask, text="Mask")
        nb.add(tab_dusha, text="Dusha")
        nb.add(tab_dtime, text="D-Timing")
        nb.add(tab_dscan, text="D-Scan")
        nb.add(tab_perf, text="Perf")
        nb.add(tab_pri, text="Prio")

        PROC_LEVELS = ["Normal", "Above Normal", "High", "Realtime"]
        THREAD_LEVELS = ["Normal", "Above Normal", "High", "Time Critical"]
        GPU_LEVELS = ["Normal (0)", "Above Normal (1)", "High (2)", "Realtime (3)"]

        def _pri_combo(parent, row, label, values, attr, transform=None):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
            cur = getattr(S, attr)
            display_cur = transform(cur) if transform else cur
            var = tk.StringVar(root, value=display_cur)
            cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=24)
            cb.grid(row=row, column=1, sticky="we", **pad)

            def on_pick(v, a=attr, tr=transform):
                if tr:
                    import re
                    m = re.search(r"\((\d+)\)", v)
                    setattr(S, a, int(m.group(1)) if m else 0)
                else:
                    setattr(S, a, v)
                S.apply_priorities_flag = True

            cb.bind("<<ComboboxSelected>>", lambda e, op=on_pick, v=var: op(v.get()))

        ttk.Label(tab_pri, foreground="#555", wraplength=420, justify="left",
                  text="Raising process priority above High or setting Realtime requires administrator "
                       "rights. Render-thread 'Time Critical' gives the GPU the most consistent frame "
                       "pacing. Changes take effect immediately."
                  ).grid(row=0, column=0, columnspan=2, sticky="w", **pad)

        _pri_combo(tab_pri, 1, "Process priority class", PROC_LEVELS, "proc_priority")
        _pri_combo(tab_pri, 2, "Render thread priority", THREAD_LEVELS, "render_thread_priority")
        _pri_combo(tab_pri, 3, "Capture thread priority", THREAD_LEVELS, "capture_thread_priority")
        _pri_combo(tab_pri, 4, "GPU scheduling priority", GPU_LEVELS, "gpu_priority",
                   transform=lambda v: GPU_LEVELS[min(int(v), 3)])

        ttk.Label(tab_pri, foreground="#a00", wraplength=420, justify="left",
                  text="Realtime process priority can make the system unresponsive. "
                       "Use High for gaming and Time Critical only for testing."
                  ).grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        pri_status = ttk.Label(tab_pri, text="", foreground="#555", wraplength=420, justify="left")
        pri_status.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        def _refresh_pri_status():
            try:
                p = psutil.Process()
                nice = p.nice()
                pname = {psutil.NORMAL_PRIORITY_CLASS: "Normal",
                         psutil.ABOVE_NORMAL_PRIORITY_CLASS: "Above Normal",
                         psutil.HIGH_PRIORITY_CLASS: "High",
                         psutil.REALTIME_PRIORITY_CLASS: "Realtime"}.get(nice, str(nice))
            except Exception:
                pname = "?"
            pri_status.config(text="Current process priority: %s" % pname)

        vars_ = {}
        checks_ = {}

        def add_combo(parent, row, label, values, initial, on_pick):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(root, value=initial)
            cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=38)
            cb.grid(row=row, column=1, columnspan=2, sticky="we", **pad)
            cb.bind("<<ComboboxSelected>>", lambda e: on_pick(var.get()))
            return var

        def add_slider(parent, row, label, attr, lo, hi):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
            var = tk.DoubleVar(root, value=getattr(S, attr))
            vars_[attr] = var
            readout = ttk.Label(parent, width=7, anchor="e")
            readout.grid(row=row, column=2, **pad)
            ttk.Scale(parent, from_=lo, to=hi, variable=var, length=240).grid(row=row, column=1, **pad)

            def on_change(*_, a=attr, v=var, ro=readout):
                val = float(v.get())
                if a in INT_ATTRS:
                    val = int(round(val))
                    ro.config(text="%d" % val)
                else:
                    ro.config(text=FMT.get(a, "%.0f" if hi >= 100 else "%.2f") % val)
                setattr(S, a, val)

            var.trace_add("write", on_change)
            on_change()

        def add_check(parent, row, label, attr):
            var = tk.BooleanVar(root, value=bool(getattr(S, attr)))
            checks_[attr] = var
            ttk.Checkbutton(parent, text=label, variable=var,
                            command=lambda: setattr(S, attr, int(bool(var.get())) if isinstance(getattr(S, attr), int) else bool(var.get()))
                            ).grid(row=row, column=0, columnspan=3, sticky="w", **pad)

        def add_idx_combo(parent, row, label, items, attr):
            return add_combo(parent, row, label, items, items[int(getattr(S, attr))],
                             lambda v, a=attr, it=items: setattr(S, a, it.index(v)))

        def on_preset(name):
            S.apply_preset(name)
            for _, attr, _, _ in ALL_SLIDERS:
                vars_[attr].set(getattr(S, attr))
            for attr, cb_var in checks_.items():
                if attr != "crt_on":
                    cb_var.set(bool(getattr(S, attr)))

        add_check(tab_crt, 0, "CRT / RF effect enabled", "crt_on")
        add_combo(tab_crt, 1, "Signal / shader", PRESET_ORDER, S.preset, on_preset)
        add_check(tab_crt, 2, "PAL mode (UV + alternating V phase; off = NTSC YIQ)", "pal")
        add_check(tab_crt, 3, "PAL delay-line chroma averaging (Hanover bar fix)", "pal_delay")
        add_combo(tab_crt, 4, "Resampling kernel", KERNELS, KERNELS[S.kernel],
                  lambda v: setattr(S, "kernel", KERNELS.index(v)))
        add_combo(tab_crt, 5, "Noise driven by", DRIVERS, S.driver, lambda v: setattr(S, "driver", v))
        for i, (label, attr, lo, hi) in enumerate(SLIDERS):
            add_slider(tab_crt, 6 + i, label, attr, lo, hi)

        # ---- Mask / Color tab ----
        add_combo(tab_mask, 0, "Mask type", MASKS, MASKS[S.mask_type],
                  lambda v: setattr(S, "mask_type", MASKS.index(v)))
        for i, (label, attr, lo, hi) in enumerate(MASK_SLIDERS):
            add_slider(tab_mask, 1 + i, label, attr, lo, hi)
        ttk.Label(tab_mask, foreground="#555", wraplength=420, justify="left",
                  text="'Original settings' is the first version's hard 1px RGB column mask and ignores "
                       "feathering, pixel space and mask size. Aperture Grille, Slot Mask and Shadow Mask "
                       "use them: feathering softens phosphor edges, pixel space widens the dark gap "
                       "between phosphors, mask size scales the whole triad. Red/Green/Blue are output "
                       "gains (1.00 = neutral)."
                  ).grid(row=1 + len(MASK_SLIDERS), column=0, columnspan=3, sticky="w", **pad)

        # ---- Dusha tab: master, preset, per-feature override choice, display sliders ----
        add_check(tab_dusha, 0, "Enable CRT-Dusha processing", "d_on")
        add_combo(tab_dusha, 1, "Dusha preset", DUSHA_PRESET_ORDER, S.d_preset,
                  lambda v: setattr(S, "d_preset", v))
        add_idx_combo(tab_dusha, 2, "Scanlines", OVERRIDE_ITEMS, "ov_scan")
        add_idx_combo(tab_dusha, 3, "Saturation", OVERRIDE_ITEMS, "ov_sat")
        add_idx_combo(tab_dusha, 4, "Hue", OVERRIDE_ITEMS, "ov_hue")
        add_idx_combo(tab_dusha, 5, "Tint (Dusha) / RGB gain (orig.)", OVERRIDE_ITEMS, "ov_tint")
        add_idx_combo(tab_dusha, 6, "Phosphor decay (trail)", OVERRIDE_ITEMS, "ov_decay")
        for i, (label, attr, lo, hi) in enumerate(DUSHA_DISPLAY_SLIDERS):
            add_slider(tab_dusha, 7 + i, label, attr, lo, hi)
        ttk.Label(tab_dusha, foreground="#555", wraplength=420, justify="left",
                  text="Dusha is off by default. Each overlapping feature can use the original script's "
                       "version or let CRT-Dusha override it (Dusha's saturation/hue/tint/scanlines are "
                       "only used when set to CRT-Dusha). Picking 'CRT-Dusha' for phosphor decay turns the "
                       "original persistence trail off. Contrast, brightness, midtone and softclip always "
                       "apply while Dusha is enabled. Non-Manual presets override the sliders they control "
                       "(Vibrant: contrast, brightness, softclip, decay speed; Old TV: also saturation, "
                       "hue, tint and scanlines)."
                  ).grid(row=7 + len(DUSHA_DISPLAY_SLIDERS), column=0, columnspan=3, sticky="w", **pad)

        # ---- Dusha timing / BFI / decay / LCD protection ----
        add_check(tab_dtime, 0, "Enable BFI / phosphor decay cycle (optional)", "bfi")
        add_idx_combo(tab_dtime, 1, "Decay mode", DUSHA_DECAY_MODES, "d_decaymode")
        add_check(tab_dtime, 2, "Debug: frame step + on-screen frame number", "d_step")
        add_check(tab_dtime, 3, "LCD safe mode (burn-in protection, even cycles)", "lcd_safe")
        add_idx_combo(tab_dtime, 4, "LCD phase flip method", DUSHA_FLIP_METHODS, "lcd_method")
        for i, (label, attr, lo, hi) in enumerate(DUSHA_TIMING_SLIDERS):
            add_slider(tab_dtime, 5 + i, label, attr, lo, hi)
        ttk.Label(tab_dtime, foreground="#555", wraplength=420, justify="left",
                  text="BFI: each cycle is one bright frame followed by progressively darker frames. "
                       "Best with Frame rate = VSync on a high refresh display. EVEN frame counts can "
                       "cause image retention on LCDs (LCD safe mode mitigates it); ODD counts and "
                       "OLED are safe. Low refresh rates will flicker."
                  ).grid(row=5 + len(DUSHA_TIMING_SLIDERS), column=0, columnspan=3, sticky="w", **pad)

        # ---- Dusha scanlines / Voodoo2 / split screen ----
        add_idx_combo(tab_dscan, 0, "Scanline direction", DUSHA_SCAN_DIRS, "s_dir")
        for i, (label, attr, lo, hi) in enumerate(DUSHA_SCAN_SLIDERS[:4]):
            add_slider(tab_dscan, 1 + i, label, attr, lo, hi)
        add_check(tab_dscan, 5, "3dfx Voodoo2: quantise to 16-bit (RGB565)", "q16")
        add_idx_combo(tab_dscan, 6, "V2 Bayer dither size", DUSHA_DITHER_SIZES, "d_dsize")
        add_slider(tab_dscan, 7, *DUSHA_SCAN_SLIDERS[4][:1], *DUSHA_SCAN_SLIDERS[4][1:])
        add_check(tab_dscan, 8, "Split screen (effect left / original right)", "sp_on")
        add_slider(tab_dscan, 9, *DUSHA_SCAN_SLIDERS[5][:1], *DUSHA_SCAN_SLIDERS[5][1:])
        add_check(tab_dscan, 10, "Show split divider line", "sp_line")
        ttk.Label(tab_dscan, foreground="#555", wraplength=420, justify="left",
                  text="Scanline settings only take effect when Scanlines is set to CRT-Dusha on the "
                       "Dusha tab (width + gap = pattern size; gap 0 = no pattern). Trinitron mode "
                       "reproduces the original shader, including its green-stripe tint. Split screen "
                       "works with or without Dusha enabled."
                  ).grid(row=11, column=0, columnspan=3, sticky="w", **pad)

        add_combo(tab_perf, 0, "Capture method", CAPTURES, S.capture_mode,
                  lambda v: setattr(S, "capture_mode", v))
        add_combo(tab_perf, 1, "Frame rate", FPS_MODES, S.fps_mode, lambda v: setattr(S, "fps_mode", v))
        add_slider(tab_perf, 2, "Custom limit (fps)", "fps_cap", 15, 360)
        ttk.Label(tab_perf, foreground="#555", wraplength=420, justify="left",
                  text="VSync = one frame per display refresh (smoothest, lowest load). Uncapped renders "
                       "as fast as possible and will compete with your game for the GPU. Custom limit "
                       "caps at the value above."
                  ).grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(tab_perf, text="Temperature source").grid(row=4, column=0, sticky="nw", **pad)
        src_label = ttk.Label(tab_perf, text="-", wraplength=300, justify="left")
        src_label.grid(row=4, column=1, columnspan=2, sticky="w", **pad)

        def restart_admin():
            try:
                script = os.path.abspath(sys.argv[0])
                params = subprocess.list2cmdline([script] + sys.argv[1:])
                r = shell32.ShellExecuteW(None, "runas", sys.executable, params,
                                          os.path.dirname(script), 1)
                if (r or 0) > 32:
                    S.quit = True
                    root.destroy()
                else:
                    S.temp_source = "(administrator restart cancelled or failed)"
            except Exception as e:
                S.temp_source = "(could not restart as administrator: %s)" % e

        admin_btn = ttk.Button(tab_perf, text="Restart as administrator", command=restart_admin)
        admin_btn.grid(row=5, column=0, columnspan=3, sticky="we", **pad)
        if S.admin:
            admin_btn.state(["disabled"])
        ttk.Label(tab_perf, foreground="#555", wraplength=420, justify="left",
                  text="CPU temperature needs a sensor reader. Checked in order: HWiNFO shared memory; "
                       "LibreHardwareMonitorLib.dll next to this script (as admin); LibreHardwareMonitor / "
                       "OpenHardwareMonitor WMI; ACPI thermal zone. 'GPU temperature (NVIDIA)' uses NVML."
                  ).grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        bottom = ttk.Frame(outer)
        bottom.grid(row=1, column=0, sticky="we", pady=(6, 0))
        toggle_btn = ttk.Button(bottom, width=30)
        toggle_btn.grid(row=0, column=0, sticky="we", **pad)

        def refresh_toggle():
            toggle_btn.config(text="Deactivate overlay" if S.active else "Activate overlay")

        def toggle_active():
            S.active = not S.active
            refresh_toggle()

        toggle_btn.config(command=toggle_active)
        refresh_toggle()

        def quit_all():
            S.quit = True
            root.destroy()

        ttk.Button(bottom, text="Close program", command=quit_all).grid(row=0, column=1, sticky="we", **pad)

        status = ttk.Label(bottom, text="", foreground="#555", wraplength=430, justify="left")
        status.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(bottom, text="Press HOME to open/close this menu.", foreground="#555").grid(
            row=2, column=0, columnspan=2, sticky="w", **pad)

        root.protocol("WM_DELETE_WINDOW", lambda: hide())

        root.update_idletasks()
        try:
            menu_hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        except Exception:
            menu_hwnd = root.winfo_id()
        exclude_from_capture(menu_hwnd)

        visible = True
        down = False
        tick = 0

        def hide():
            nonlocal visible
            visible = False
            root.withdraw()

        def show():
            nonlocal visible
            visible = True
            root.deiconify()
            exclude_from_capture(menu_hwnd)
            keep_topmost(menu_hwnd)
            refresh_toggle()

        def poll():
            nonlocal down, tick
            if S.quit:
                try:
                    root.destroy()
                except Exception:
                    pass
                return
            k = bool(user32.GetAsyncKeyState(VK_HOME) & 0x8000)
            if k and not down:
                hide() if visible else show()
            down = k
            tick += 1
            if S.relift:
                S.relift = False
                if visible:
                    keep_topmost(menu_hwnd)
            if visible and tick % 12 == 0:
                try:
                    _refresh_pri_status()
                except Exception:
                    pass
                t = "n/a" if S.temp is None else "%.0f C" % S.temp
                if S.temp is not None and S.temp_source:
                    t += " (" + S.temp_source + ")"
                src_label.config(text=(S.temp_source or "none yet / not selected") +
                                 ("   [admin: yes]" if S.admin else "   [admin: no]"))
                txt = ("CPU %.0f%%  RAM %.0f%%  Temp %s  |  %.0f fps  |  %s" %
                       (S.cpu, S.ram, t, S.fps, S.capture_kind))
                if is_temp_driver(S.driver) and S.temp is None:
                    txt += "  |  no temp sensor yet: noise not reacting (see Performance tab)"
                status.config(text=txt)
                refresh_toggle()
            root.after(40, poll)

        root.after(40, poll)
        root.mainloop()
        S.quit = True


# --------------------------------------------------------------------------
# Main render loop
# --------------------------------------------------------------------------
def main():
    W = user32.GetSystemMetrics(0)
    H = user32.GetSystemMetrics(1)
    S = State()
    S.capture_kind = "starting"

    sys.setswitchinterval(0.001)
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    MenuThread(S).start()
    threading.Thread(target=temp_worker, args=(S,), daemon=True).start()
    grab = Grabber(S.capture_mode, S=S)
    S.capture_kind = "starting"

    os.environ["SDL_VIDEO_WINDOW_POS"] = "0,0"
    pygame.init()
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
    pygame.display.gl_set_attribute(pygame.GL_CONTEXT_PROFILE_MASK, pygame.GL_CONTEXT_PROFILE_CORE)
    flags = pygame.OPENGL | pygame.DOUBLEBUF | pygame.NOFRAME
    try:
        pygame.display.set_mode((W, H), flags, vsync=1)
    except Exception:
        pygame.display.set_mode((W, H), flags)
    pygame.display.set_caption("CRT Overlay")
    hwnd = pygame.display.get_wm_info()["window"]
    make_click_through_overlay(hwnd)
    user32.ShowWindow(hwnd, 0)

    ctx = moderngl.create_context()
    print("OpenGL:", ctx.info.get("GL_RENDERER"), "|", ctx.info.get("GL_VERSION"))
    prog1 = ctx.program(vertex_shader=VERT, fragment_shader=FRAG_PROCESS)
    prog2 = ctx.program(vertex_shader=VERT, fragment_shader=FRAG_PRESENT)
    vbo = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4").tobytes())
    vao1 = ctx.vertex_array(prog1, [(vbo, "2f", "in_pos")])
    vao2 = ctx.vertex_array(prog2, [(vbo, "2f", "in_pos")])

    def setu(prog, name, val):
        if name in prog:
            prog[name].value = val

    T = [ctx.texture((W, H), 4, dtype="f2") for _ in range(2)]
    for t in T:
        t.filter = (moderngl.LINEAR, moderngl.LINEAR)
        t.repeat_x = t.repeat_y = False
    F = [ctx.framebuffer(color_attachments=[t]) for t in T]
    for f in F:
        f.clear(0, 0, 0, 1)
    idx = 0

    screen_tex = None
    last_data = None
    applied_mode = S.capture_mode
    t0 = time.time()
    clock = pygame.time.Clock()
    frame = 0
    shown = False
    diag = 0
    warned = False
    last_sensor = 0.0
    drive = 0.0
    psutil.cpu_percent(None)
    prev_crt = True
    force_cap = False
    cur_interval = None
    dwm_sync = False
    last_dt_t = time.perf_counter()
    _render_priority_applied = None

    while not S.quit:
        if S.apply_priorities_flag:
            S.apply_priorities_flag = False
            ok_proc = _set_process_priority(S.proc_priority)
            ok_rt = _set_thread_priority(S.render_thread_priority)
            ok_gpu = _set_gpu_priority(S.gpu_priority)
            key = (S.proc_priority, S.render_thread_priority, S.gpu_priority)
            if key != _render_priority_applied:
                _render_priority_applied = key
                grab._capture_priority = S.capture_thread_priority
                print("[priority] process=%s render-thread=%s gpu=%d  (proc=%s rt=%s gpu=%s)"
                      % (S.proc_priority, S.render_thread_priority, S.gpu_priority,
                         "ok" if ok_proc else "failed",
                         "ok" if ok_rt else "failed",
                         "ok" if ok_gpu else "failed"))

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                S.quit = True

        if S.capture_mode != applied_mode:
            applied_mode = S.capture_mode
            grab.set_mode(applied_mode)

        want = S.active and S.crt_on
        grab.paused = not want
        if grab.failed:
            S.quit = True
            continue

        if not want:
            if shown:
                user32.ShowWindow(hwnd, 0)
                shown = False
            clock.tick(20)
            continue

        mode_idx = FPS_MODES.index(S.fps_mode) if S.fps_mode in FPS_MODES else 0
        if mode_idx == 0:
            interval = 1
            cap = int(detect_refresh()) if force_cap else 0
        elif mode_idx == 1:
            interval, cap, force_cap = 0, 0, False
        else:
            interval, cap, force_cap = 0, max(int(S.fps_cap), 1), False
        if interval != cur_interval:
            cur_interval = interval
            ok = set_swap_interval(interval)
            print("[vsync] swap interval %d: %s" % (interval, "set" if ok else "not accepted by the driver"))
        clock.tick(cap)
        tnow = time.perf_counter()
        dt = max(tnow - last_dt_t, 1e-4)
        last_dt_t = tnow
        S.fps = clock.get_fps()
        now = time.time()

        if now - last_sensor > 0.5:
            last_sensor = now
            S.cpu = psutil.cpu_percent(None)
            S.ram = psutil.virtual_memory().percent
        if S.driver == "CPU load":
            target = S.cpu / 100.0
        elif is_temp_driver(S.driver):
            target = min(max((S.temp - 40.0) / 50.0, 0.0), 1.0) if S.temp is not None else 0.0
        elif S.driver == "RAM usage":
            target = S.ram / 100.0
        else:
            target = 0.0
        drive += (target - drive) * 0.08

        r = grab.grab()
        S.capture_kind = grab.kind or "none"
        if r is not None:
            data, fw, fh = r
            if screen_tex is None or screen_tex.size != (fw, fh):
                screen_tex = ctx.texture((fw, fh), 4, dtype="f1")
                screen_tex.swizzle = "BGRA"
                screen_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                screen_tex.repeat_x = screen_tex.repeat_y = False
            screen_tex.write(data, alignment=1)
            last_data = data
        if screen_tex is None:
            if not warned and now - t0 > 5:
                warned = True
                print("No frame captured yet. Try 'Capture method: mss' in the menu "
                      "(pip install mss), or move something on screen.")
            continue

        if not shown:
            user32.ShowWindow(hwnd, 4)
            keep_topmost(hwnd)
            S.relift = True
            for f in F:
                f.clear(0, 0, 0, 1)
            shown = True

        scale = W / 1920.0
        noise_eff = min(max(S.noise + S.react * drive * 0.25, 0.0), 0.6)
        jitter_eff = S.jitter * (1.0 + S.react * drive * 1.5)
        ghost_eff = S.ghost * (1.0 + S.react * drive)
        el = now - t0
        frame += 1

        crt = bool(S.crt_on)
        if crt and not prev_crt:
            for f in F:
                f.clear(0, 0, 0, 1)
        prev_crt = crt

        # chroma demod window half-width in carrier samples: a multiple of 4 (one carrier cycle)
        # so luma / 2x-carrier cancel exactly. ~chroma bandwidth, capped at 12 taps.
        chroma_taps = int(min(max(round(S.chroma * S.blur * 2.45 / 4.0), 1), 3)) * 4

        # ------------------------------------------------------------------
        # CRT-Dusha: resolve which implementation of each overlapping feature is used
        # ------------------------------------------------------------------
        d_on = bool(S.d_on)
        eff = dusha_effective(S)
        d_scan = d_on and S.ov_scan == 1
        d_sat = d_on and S.ov_sat == 1
        d_hue = d_on and S.ov_hue == 1
        d_tint = d_on and S.ov_tint == 1
        d_decay = d_on and S.ov_decay == 1
        d_bfi = d_on and bool(S.bfi)

        # BFI timing (computed once per frame on the CPU)
        frames_per = max(1, int(S.d_frames))
        eff_count = frame
        if S.lcd_safe and frames_per % 2 == 0:
            interval_f = int(S.lcd_flip)
            if interval_f % frames_per == 0:
                interval_f += 1                       # don't align with the decay cycle
            if S.lcd_method == 0:
                eff_count += int(frame * S.lcd_jump)  # phase jump
            else:
                eff_count -= frame // interval_f      # frame drop
        xr = eff_count / max(float(S.d_slow), 1.0) / frames_per
        raw_fraction = xr - math.floor(xr)
        step_idx = int(math.floor(raw_fraction * frames_per + 1e-9))
        fraction = (step_idx / frames_per) if S.d_step else raw_fraction
        frame_num = min(step_idx, frames_per - 1) + 1
        d_mult = (float(S.d_mr), float(S.d_mg), float(S.d_mb))
        decay_u = (1.0, 1.0, 1.0)
        if d_bfi and S.d_decaymode == 0:              # Uniform Pulse: same for every pixel
            decay_u = dusha_decay_curve(fraction, eff["d_speed"], S.d_levels, d_mult)

        # hue matrix rows (rotation in RGB, built on the CPU)
        ang = math.radians(eff["d_hue"])
        hs, hc = math.sin(ang), math.cos(ang)
        hue_r0 = (0.299 + 0.701 * hc + 0.168 * hs, 0.587 - 0.587 * hc + 0.330 * hs, 0.114 - 0.114 * hc - 0.497 * hs)
        hue_r1 = (0.299 - 0.299 * hc - 0.328 * hs, 0.587 + 0.413 * hc + 0.035 * hs, 0.114 - 0.114 * hc + 0.292 * hs)
        hue_r2 = (0.299 - 0.299 * hc + 1.250 * hs, 0.587 - 0.587 * hc - 1.050 * hs, 0.114 + 0.886 * hc - 0.203 * hs)
        tint = (float(eff["d_tr"]), float(eff["d_tg"]), float(eff["d_tb"]))

        w_idx = 1 - idx
        if crt:
            screen_tex.use(0)
            T[idx].use(1)
            setu(prog1, "screen", 0)
            setu(prog1, "prev", 1)
            setu(prog1, "res", (float(W), float(H)))
            setu(prog1, "time", float(el % 1000.0))
            setu(prog1, "parity", float(int(el * 30.0) % 2))
            setu(prog1, "lines", float(S.lines))
            setu(prog1, "lumaSigma", float(S.luma * S.blur * scale))
            setu(prog1, "chromaSigma", float(S.chroma * S.blur * scale))
            setu(prog1, "chromaTaps", int(chroma_taps))
            setu(prog1, "chromaLag", float(S.chroma_lag))
            setu(prog1, "bloomAmt", float(S.bloom))
            setu(prog1, "jitter", float(jitter_eff))
            setu(prog1, "ghost", float(ghost_eff))
            setu(prog1, "dotamt", float(S.dot))
            setu(prog1, "curv", float(S.curv))
            setu(prog1, "gam", float(S.gam))
            # original persistence trail is replaced when Dusha decay is selected
            setu(prog1, "decay", 0.0 if d_decay else float(S.decay ** (dt * 60.0)))
            setu(prog1, "sat", 1.0 if d_sat else float(S.sat))
            setu(prog1, "hue", 0.0 if d_hue else float(S.hue))
            setu(prog1, "scan", 0.0 if d_scan else float(S.scan))
            setu(prog1, "kernel", int(S.kernel))
            setu(prog1, "pal", int(S.pal))
            setu(prog1, "palDelay", int(S.pal_delay))
            F[w_idx].use()
            ctx.viewport = (0, 0, W, H)
            vao1.render(moderngl.TRIANGLE_STRIP)

        ctx.screen.use()
        ctx.viewport = (0, 0, W, H)
        T[w_idx].use(0)
        screen_tex.use(1)
        setu(prog2, "tex", 0)
        setu(prog2, "screenRaw", 1)
        setu(prog2, "srcLinear", 1)
        setu(prog2, "res", (float(W), float(H)))
        setu(prog2, "time", float(el % 1000.0))
        setu(prog2, "noiseAmt", float(noise_eff))
        setu(prog2, "mask", float(S.mask))
        setu(prog2, "maskType", int(S.mask_type))
        setu(prog2, "feather", float(S.feather))
        setu(prog2, "gap", float(S.gap))
        setu(prog2, "mscale", float(max(S.mscale, 1.0)))
        setu(prog2, "gain", (1.0, 1.0, 1.0) if d_tint else
             (float(S.r_gain), float(S.g_gain), float(S.b_gain)))
        setu(prog2, "hum", float(noise_eff * 0.35))
        setu(prog2, "lines", float(S.lines))
        # ---- CRT-Dusha uniforms ----
        setu(prog2, "dOn", int(d_on))
        setu(prog2, "dVolt", float(eff["d_volt"]))
        setu(prog2, "dCur", float(eff["d_cur"]))
        setu(prog2, "dMid", float(S.d_mid))
        setu(prog2, "dSoft", float(eff["d_soft"]))
        setu(prog2, "dQuant", int(d_on and bool(S.q16)))
        setu(prog2, "dDSize", int(S.d_dsize))
        setu(prog2, "dDStr", float(S.d_dstr))
        setu(prog2, "dBfi", int(d_bfi))
        setu(prog2, "dDecayMode", int(S.d_decaymode))
        setu(prog2, "dLevels", int(max(1, min(int(S.d_levels), DUSHA_MAX_LEVELS))))
        setu(prog2, "dDecayU", decay_u)
        setu(prog2, "dMult", d_mult)
        setu(prog2, "dFraction", float(fraction))
        setu(prog2, "dRFreq", float(S.d_rfreq))
        setu(prog2, "dSpeed", float(eff["d_speed"]))
        setu(prog2, "dScanOn", int(d_scan and eff["s_dark"] > 0.0))
        setu(prog2, "dSDir", int(S.s_dir))
        setu(prog2, "dSThick", int(max(1, int(eff["s_thick"]))))
        setu(prog2, "dSGap", int(max(0, int(eff["s_gap"]))))
        setu(prog2, "dSDark", float(eff["s_dark"]))
        setu(prog2, "dSAper", float(S.s_aper))
        setu(prog2, "dSatOn", int(d_sat and abs(eff["d_sat"] - 1.0) > 1e-6))
        setu(prog2, "dSatV", float(eff["d_sat"]))
        setu(prog2, "dHueOn", int(d_hue and abs(eff["d_hue"]) > 1e-6))
        setu(prog2, "dHueR0", hue_r0)
        setu(prog2, "dHueR1", hue_r1)
        setu(prog2, "dHueR2", hue_r2)
        setu(prog2, "dTintOn", int(d_tint and tint != (1.0, 1.0, 1.0)))
        setu(prog2, "dTint", tint)
        setu(prog2, "dSplit", int(bool(S.sp_on)))
        setu(prog2, "dSplitPos", float(S.sp_pos))
        setu(prog2, "dDivider", int(bool(S.sp_line)))
        setu(prog2, "dDebug", int(d_bfi and bool(S.d_step)))
        setu(prog2, "dFrameNum", int(frame_num))
        setu(prog2, "dFramesPer", int(frames_per))
        vao2.render(moderngl.TRIANGLE_STRIP)

        if diag < 5 and frame > 3:
            diag += 1
            try:
                out = np.frombuffer(ctx.screen.read(viewport=(W // 2 - 32, H // 2 - 32, 64, 64),
                                                    components=3), dtype=np.uint8)
                inn = np.frombuffer(last_data, dtype=np.uint8)[::97]
                print("[diag %d] capture=%s  input mean=%.1f max=%d  output mean=%.1f" %
                      (diag, grab.kind, inn.mean(), inn.max(), out.mean()))
                if inn.max() == 0 and grab.kind == "dxcam" and grab.mode == "Auto":
                    print("dxcam is returning black frames.")
                    grab.force_mss()
                elif out.mean() < 1 and inn.mean() > 3 and S.crt_on:
                    print("WARNING: capture has content but shader output is black. "
                          "Please report this line.")
            except Exception as e:
                print("[diag] failed:", e)

        pygame.display.flip()
        if interval == 1 and dwm_sync and not force_cap:
            try:
                dwmapi.DwmFlush()
            except Exception:
                pass
        tf = time.perf_counter()
        last_dt_t = tf
        if interval == 1 and frame % 240 == 0 and S.fps > detect_refresh() * 1.3:
            if not dwm_sync:
                dwm_sync = True
                print("Window VSync is not throttling (%.0f fps vs %.0f Hz). That is normal for a "
                      "click-through window; pacing with DwmFlush instead." % (S.fps, detect_refresh()))
            elif not force_cap:
                force_cap = True
                print("DwmFlush pacing did not hold (%.0f fps vs %.0f Hz): capping the frame rate "
                      "to the refresh rate manually." % (S.fps, detect_refresh()))
        if crt:
            idx = w_idx

    S.quit = True
    shutdown_helpers()
    pygame.quit()


if __name__ == "__main__":
    main()
