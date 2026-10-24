"""
CRT / RF Signal Overlay for Windows 10/11
=========================================
Captures the primary monitor (Desktop Duplication via dxcam), runs it through a
GPU shader that emulates analogue TV signal paths, and shows the result in a
click-through, always-on-top, full-screen overlay.

 * Gamma-aware: source is decoded with a CRT gamma (default 2.4), all filtering
   happens in linear light, output is encoded for an sRGB monitor.
 * Manual resampling by convolution: Gaussian or Lanczos-2 kernels, separate
   luma / chroma bandwidth (YIQ), optional PAL line-averaging of chroma.
 * Phosphor decay (temporal persistence), scanlines, aperture-grille mask,
   RF snow + streak noise, ghosting, line jitter, dot crawl, hum bar, curvature.
 * Noise can be driven by CPU load, CPU temperature, NVIDIA GPU temperature or RAM.
   CPU temperature sources (tried in order): HWiNFO shared memory, LibreHardwareMonitorLib.dll
   next to this script (run as admin), LibreHardwareMonitor / OpenHardwareMonitor WMI, ACPI.
 * Optional BFI strobe (black frame insertion / Variable MPRT / rolling scan) for motion
   clarity, ported from the "BFI Presenter" ReShade shader (see licence note below).
 * Frame rate: VSync (match display), uncapped, or your own limit (Performance tab).
 * HOME key shows/hides the settings menu. The menu has Deactivate and Quit.

No admin rights needed. Install:
    pip install numpy psutil pygame moderngl dxcam mss
(also install the fallback capture:  pip install mss)
Run:
    python crt_overlay.py

BFI needs the overlay to run at exactly the display refresh rate (VSync, fixed refresh,
G-SYNC/VRR off) and works best with dxcam capture (mss is too slow). It flickers on purpose:
photosensitivity warning.

Licence: the BFI code is a port of "BFI Presenter shader v1.0.10", Copyright (C) 2026 artzox,
SPDX-License-Identifier: GPL-3.0-or-later. This file therefore is GPL-3.0-or-later too.
HDR output, peak and the ABL guard of the original are omitted (this overlay outputs SDR);
the hitch guard and LCD phase swap run on the CPU here instead of in state textures.

Notes: exclusive-fullscreen games can't be overlaid (use borderless/windowed
fullscreen). Primary monitor only.
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
WDA_EXCLUDEFROMCAPTURE = 0x11  # Win10 2004+ : window is invisible to screen capture
VK_HOME = 0x24


def keep_topmost(hwnd):
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def exclude_from_capture(hwnd):
    return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE))


def make_click_through_overlay(hwnd):
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex)
    user32.SetLayeredWindowAttributes(hwnd, 0, 255, 2)  # LWA_ALPHA, fully opaque
    if not exclude_from_capture(hwnd):  # so we never capture ourselves (no feedback loop)
        print("WARNING: could not exclude overlay from capture (needs Windows 10 2004+).")
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)


# --------------------------------------------------------------------------
# Presets. luma/chroma = blur sigma in screen px at 1920 wide.
# These are stylised approximations of each signal path, not decoded signals.
# --------------------------------------------------------------------------
PRESETS = {
    "HDMI":
        dict(lines=240, luma=0.0, chroma=0.0, pal=0, scan=0.15, mask=0.10, noise=0.0,
             ghost=0.0, dot=0.0, jitter=0.0, sat=1.0, hue=0.0),
    "HDMI Mod":
        dict(lines=240, luma=0.35, chroma=0.35, pal=0, scan=0.45, mask=0.25, noise=0.0,
             ghost=0.0, dot=0.0, jitter=0.0, sat=1.0, hue=0.0),
    "Composite":
        dict(lines=240, luma=0.9, chroma=3.5, pal=0, scan=0.5, mask=0.20, noise=0.04,
             ghost=0.0, dot=0.35, jitter=0.15, sat=1.0, hue=0.0),
    "RF Cables":
        dict(lines=240, luma=1.5, chroma=4.5, pal=0, scan=0.5, mask=0.20, noise=0.14,
             ghost=0.12, dot=0.45, jitter=0.6, sat=0.95, hue=0.0),
    "PAL-B (Composite)":
        dict(lines=288, luma=1.0, chroma=3.8, pal=1, scan=0.5, mask=0.20, noise=0.04,
             ghost=0.0, dot=0.08, jitter=0.15, sat=1.0, hue=0.0),
    "PAL-B (RF Cables)":
        dict(lines=288, luma=1.6, chroma=4.8, pal=1, scan=0.5, mask=0.20, noise=0.14,
             ghost=0.12, dot=0.10, jitter=0.6, sat=0.95, hue=0.0),
    "PAL-M (Brazilian Famiclone) (Composite)":
        dict(lines=240, luma=0.95, chroma=3.6, pal=1, scan=0.5, mask=0.20, noise=0.05,
             ghost=0.0, dot=0.10, jitter=0.2, sat=1.05, hue=0.05),
    "PAL-N (Argentinian Famiclone) (Composite)":
        dict(lines=288, luma=1.1, chroma=4.2, pal=1, scan=0.5, mask=0.20, noise=0.05,
             ghost=0.0, dot=0.10, jitter=0.2, sat=0.95, hue=-0.05),
    "PAL-D (Dendy Famiclone) (Composite)":
        dict(lines=288, luma=1.0, chroma=3.6, pal=1, scan=0.5, mask=0.20, noise=0.05,
             ghost=0.0, dot=0.12, jitter=0.25, sat=1.1, hue=0.12),
}
PRESET_ORDER = ["PAL-N (Argentinian Famiclone) (Composite)", "PAL-M (Brazilian Famiclone) (Composite)",
                "PAL-D (Dendy Famiclone) (Composite)", "PAL-B (Composite)", "PAL-B (RF Cables)",
                "HDMI Mod", "HDMI", "Composite", "RF Cables"]
KERNELS = ["Gaussian", "Lanczos-2"]
BFI_METHODS = ["BFI (black frames + highlight roll-off)", "Variable MPRT (brightness budget)"]
CAPTURES = ["Auto", "dxcam", "mss"]
FPS_MODES = ["VSync (match display refresh)", "Uncapped", "Custom limit"]
DRIVERS = ["CPU load", "CPU temperature", "GPU temperature (NVIDIA)", "RAM usage", "Off"]


def is_temp_driver(d):
    return d.startswith("CPU temp") or d.startswith("GPU temp")


class State:
    def __init__(self):
        self.active = True
        self.crt_on = True      # CRT / RF effect (independent of BFI)
        self.quit = False
        self.preset = "Composite"
        # user-tweakable (not touched by presets)
        self.decay, self.blur, self.curv, self.gam, self.react = 0.30, 1.0, 0.0, 2.4, 1.0
        self.kernel = 0
        self.driver = DRIVERS[0]
        # preset-driven
        self.lines = 240
        self.luma = self.chroma = 1.0
        self.pal = 0
        self.scan = self.mask = self.noise = self.ghost = self.dot = self.jitter = 0.0
        self.sat, self.hue = 1.0, 0.0
        self.apply_preset(self.preset)
        # live readouts
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
        # BFI strobe (defaults follow the original shader; off until you enable it)
        self.bfi_on = False
        self.bfi_method = 1
        self.bfi_frames = 2
        self.bfi_gain = 1.0
        self.bfi_floor = 0.0
        self.bfi_rolloff = True
        self.bfi_knee = 0.75
        self.bfi_sine = False
        self.bfi_invert = False
        self.bfi_rolling = False
        self.bfi_seam = 0.0
        self.bfi_decay = 0.2
        self.bfi_refresh = detect_refresh()
        self.bfi_hitch = True
        self.bfi_hold = 300.0
        self.bfi_tol = 0.75
        self.bfi_lcd = False
        self.bfi_lcd_sec = 5
        self.bfi_status = False
        self.bfi_paused = False
        self.bfi_reason = ""

    def apply_preset(self, name):
        self.preset = name
        for k, v in PRESETS[name].items():
            setattr(self, k, v)


# --------------------------------------------------------------------------
# BFI logic that the ReShade version keeps in state textures (hitch guard, LCD phase swap)
# --------------------------------------------------------------------------
def bfi_target(j, n, lit, fl, sine, inv):
    if sine:
        ph = (j / float(n)) * 6.28318531
        if inv:
            ph += 3.14159265
        return fl + (lit - fl) * (0.5 + 0.5 * math.cos(ph))
    is_lit = (j != 0) if inv else (j == 0)
    return lit if is_lit else fl


class BFIState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.until = 0.0     # hitch-guard pause until (ms)
        self.ft = 0.0        # smoothed frame time (ms)
        self.drift = 0.0     # refresh drift (ms)
        self.sx = 0          # LCD swap: cycle offset (frames)
        self.sy = 0.0        # LCD swap: fade (0 = full strobe, 1 = steady)
        self.sz = 0          # 0 idle, 1 fading to steady, 2 fading back
        self.sw = 0.0        # time of last swap (ms)
        self.reason = ""
        self.nprint = 0

    def update(self, S, ft, now, framecount):
        """Returns True while the hitch guard has BFI paused."""
        n = int(min(max(S.bfi_frames, 2), 8))
        ft = min(max(ft, 0.0), 1000.0)
        P = 1000.0 / max(S.bfi_refresh, 1.0)
        d = self.drift
        if abs(d) > 1000.0:
            d = 0.0
        d = (d + ft - P) * 0.99
        missed = False
        tol = S.bfi_tol * P
        if d > tol:
            missed = True
            d -= P
        if d < -tol:
            # Short frames are the CPU catching up after a late one (the driver queues frames);
            # they cannot make the display repeat an image, so they never count as a miss.
            d = -tol
        self.drift = d
        if self.ft <= 0.0 or self.ft > 1000.0:
            self.ft = P
        self.ft += (min(ft, 100.0) - self.ft) * 0.05
        reasons = []
        if now < 2000.0:
            reasons.append("starting up")
        if missed:
            reasons.append("missed refresh (frame took %.1f ms, expected %.1f ms)" % (ft, P))
        if ft > 3.0 * P:
            reasons.append("long frame (%.0f ms)" % ft)
        if abs(self.ft - P) > 0.1 * P:
            reasons.append("average frame time %.2f ms but expected %.2f ms (is the refresh-rate "
                           "slider right?)" % (self.ft, P))
        if reasons:
            if now >= self.until and now >= 2000.0 and self.nprint < 30:
                self.nprint += 1
                print("[bfi] hitch guard paused BFI: " + reasons[0])
            self.reason = reasons[0]
            self.until = now + S.bfi_hold
        if self.until > now + 10000.0:
            self.until = now
        paused = bool(S.bfi_hitch and now < self.until)

        # LCD phase swap: fade to the steady picture, shift the cycle one frame, fade back
        if self.sw > now + 10000.0 or self.sw <= 0.0:
            self.sx, self.sy, self.sz, self.sw = 0, 0.0, 0, now
        if (not S.bfi_lcd) or (n % 2) != 0:
            self.sy, self.sz, self.sw = 0.0, 0, now
        else:
            step = min(max(ft, 0.0), 100.0) / 400.0
            if self.sz == 0 and not paused and now - self.sw >= S.bfi_lcd_sec * 1000.0:
                self.sz = 1
            if self.sz == 1:
                self.sy = min(1.0, self.sy + step)
                slot = (framecount + self.sx) % n
                if self.sy >= 1.0 and slot == n - 1:
                    self.sx = (self.sx + n - 1) % n
                    self.sz = 2
            elif self.sz == 2:
                self.sy = max(0.0, self.sy - step)
                if self.sy <= 0.0:
                    self.sz = 0
                    self.sw = now
        return paused


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

# Pass 1: gamma-aware convolution resampling, chroma/luma split, PAL averaging,
# jitter, ghosting, dot crawl, scanlines, phosphor persistence. Output = linear light.
FRAG_PROCESS = """
#version 330
uniform sampler2D screen;
uniform sampler2D prev;
uniform vec2  res;
uniform float time, parity;
uniform float lines, lumaSigma, chromaSigma, jitter, ghost, dotamt, curv, gam, decay, sat, hue, scan;
uniform int   kernel, pal;
in vec2 v_uv;
out vec4 outc;

const int TAPS = 6;   // 13-tap kernels, 0.5 sigma spacing -> +/-3 sigma
const mat3 RGB2YIQ = mat3(0.299, 0.596, 0.211,
                          0.587,-0.274,-0.523,
                          0.114,-0.322, 0.312);
const mat3 YIQ2RGB = mat3(1.0, 1.0, 1.0,
                          0.956,-0.272,-1.106,
                          0.621,-0.647, 1.703);

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
    float pa = 3.14159265*a;
    return (sin(pa)/pa) * (sin(pa*0.5)/(pa*0.5));
}

vec3 lin(vec2 uv){ return pow(max(texture(screen, uv).rgb, 0.0), vec3(gam)); }   // CRT gamma decode

float lumaAt(vec2 uv){
    if(lumaSigma < 0.25) return dot(lin(uv), vec3(0.299,0.587,0.114));
    float acc = 0.0, ws = 0.0;
    for(int i=-TAPS;i<=TAPS;i++){
        float u = float(i)*0.5;
        float w = kw(u);
        acc += w*dot(lin(uv + vec2(u*lumaSigma/res.x, 0.0)), vec3(0.299,0.587,0.114));
        ws += w;
    }
    return acc/ws;
}

vec2 chromaAt(vec2 uv){
    if(chromaSigma < 0.25) return (RGB2YIQ*lin(uv)).yz;
    vec2 acc = vec2(0.0); float ws = 0.0;
    for(int i=-TAPS;i<=TAPS;i++){
        float u = float(i)*0.5;
        float w = kw(u);
        acc += w*(RGB2YIQ*lin(uv + vec2(u*chromaSigma/res.x, 0.0))).yz;
        ws += w;
    }
    return acc/ws;
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
    float Y  = lumaAt(suv);
    vec2  IQ = chromaAt(suv);
    if(pal == 1){   // PAL delay line: average chroma with neighbouring lines
        IQ = 0.5*IQ + 0.25*(chromaAt(suv - vec2(0.0,lineH)) + chromaAt(suv + vec2(0.0,lineH)));
    }

    // dot crawl: chroma crosstalk into luma, flipping each frame
    float checker = mod(floor(gl_FragCoord.x/(res.x/1920.0)*0.5) + ln + parity, 2.0)*2.0-1.0;
    Y += dotamt*0.35*length(IQ)*checker;

    // RF echo (ghost)
    if(ghost > 0.0) Y += ghost*lumaAt(suv - vec2(16.0/1920.0, 0.0));

    float ch = cos(hue), sh = sin(hue);
    IQ = sat*vec2(IQ.x*ch - IQ.y*sh, IQ.x*sh + IQ.y*ch);
    vec3 rgb = max(YIQ2RGB*vec3(Y, IQ), 0.0);

    // scanlines: beam gets fatter when brighter
    float pos  = fract(uv.y*lines);
    float l    = clamp(Y, 0.0, 1.0);
    float w    = mix(0.20, 0.45, sqrt(l));
    float beam = exp(-0.5*pow((pos-0.5)/w, 2.0));
    rgb *= mix(1.0, beam, scan)*(1.0 + 0.2*scan);

    // phosphor persistence (instant rise, exponential decay)
    vec3 pv = texture(prev, vec2(v_uv.x, 1.0 - v_uv.y)).rgb;
    rgb = max(rgb*inside, pv*decay);
    outc = vec4(rgb, 1.0);
}
"""

# Pass 2: sRGB encode, phosphor mask, hum bar, RF snow/streak noise.
FRAG_PRESENT = """
#version 330
uniform sampler2D tex;
uniform vec2  res;
uniform int   srcLinear;   // 1: tex = CRT-stage output (linear), 0: tex = raw captured screen (sRGB)
uniform float time, noiseAmt, mask, hum, lines;
// --- BFI (ported from BFI Presenter shader v1.0.10, (C) 2026 artzox, GPL-3.0-or-later) ---
uniform int   bfiOn, bfiMethod, bfiFrames, bfiSlot, bfiPaused, bfiRolloff, bfiSine, bfiInv, bfiRolling, bfiStatus;
uniform float bfiLit, bfiAvg, bfiFloor, bfiKnee, bfiSeam, bfiDecay, bfiSwap;
uniform vec3  statusCol;
in vec2 v_uv;
out vec4 outc;

float hash(vec3 p){
    p = fract(p*0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x+p.y)*p.z);
}
vec3 enc(vec3 c){
    c = clamp(c, 0.0, 1.0);
    return mix(c*12.92, 1.055*pow(c, vec3(1.0/2.4)) - 0.055, step(0.0031308, c));
}
vec3 srgb2lin(vec3 c){
    c = max(c, 0.0);
    return mix(c/12.92, pow((c+0.055)/1.055, vec3(2.4)), step(0.04045, c));
}

float Target(int j, float litGain, float fl){
    int n = bfiFrames;
    if(bfiSine == 1){
        float ph = (float(j)/float(n))*6.28318531;
        if(bfiInv == 1) ph += 3.14159265;
        return mix(fl, litGain, 0.5 + 0.5*cos(ph));
    }
    bool lit = (bfiInv == 1) ? (j != 0) : (j == 0);
    return lit ? litGain : fl;
}

vec3 Rolloff(vec3 lin, float g, float P, float k){
    float m = max(max(lin.r, lin.g), lin.b);
    vec3 r = lin*g;
    if(g > 1.0 && m > 1e-6){
        float u = m/P;
        float v = g*u;
        k = clamp(k, 0.05, 0.99);
        if(u >= 1.0) r = lin;
        else if(v > k){
            float a = (g-k)/(1.0-k);
            float t = clamp((v-k)/(g-k), 0.0, 1.0);
            float o = k + (1.0-k)*(1.0 - pow(1.0-t, a));
            r = lin*(o/u);
        }
    }
    return r;
}

float MprtScale(int k, float litGain, float fl, float S){
    int n = bfiFrames;
    int k0 = 0;
    if(bfiInv == 1) k0 = (bfiSine == 1) ? (n/2) : 1;
    int pos = (k + n - k0) % n;
    float fill[8];
    float carry = 0.0;
    for(int j=0;j<8;j++){
        float f = 0.0;
        if(j < n){
            float want = Target((k0+j)%n, litGain, fl) + carry;
            f = min(want, S);
            carry = want - f;
        }
        fill[j] = f;
    }
    float s = 0.0;
    for(int j2=0;j2<8;j2++){
        float f = fill[j2];
        if(j2 < n){
            float add = min(carry, S - f);
            carry -= add;
            if(j2 == pos) s = f + add;
        }
    }
    return s;
}

vec3 Strobe(vec3 lin, int j, float litGain){
    if(bfiMethod == 0){
        float g = Target(j, litGain, bfiFloor);
        return (bfiRolloff == 1) ? Rolloff(lin, g, 1.0, bfiKnee) : lin*g;
    }
    float m = max(max(lin.r, lin.g), lin.b);
    float S = (m > 1e-6) ? max(1.0/m, 1.0) : 1e6;
    return lin*MprtScale(j, litGain, bfiFloor, S);
}

float DecayShare(int s, float phi, float T, float n){
    float a = float(s) - phi;
    a = a - n*floor(a/n);
    float norm = 1.0 - exp(-n/T);
    float b = a + 1.0;
    if(b <= n) return (exp(-a/T) - exp(-b/T))/norm;
    return (exp(-a/T) - exp(-n/T) + 1.0 - exp(-(b-n)/T))/norm;
}

vec3 applyBFI(vec3 lin){
    int n = bfiFrames;
    float litGain = bfiLit;
    int slot = bfiSlot;
    vec3 o;
    if(bfiPaused == 1){
        o = lin*bfiAvg;                       // hitch guard: steady average picture
    } else if(bfiRolling == 1 && bfiDecay > 0.0){
        float fn = float(n);
        float phi = v_uv.y*fn;
        float T = max(bfiDecay, 0.02);
        o = vec3(0.0);
        for(int j=0;j<8;j++){
            if(j < n){
                int d = (slot + n - j) % n;
                float e = DecayShare(d, phi, T, fn);
                if(e > 1e-5) o += Strobe(lin, j, litGain)*e;
            }
        }
    } else if(bfiRolling == 1){
        float u = v_uv.y*float(n);
        int band = min(int(u), n-1);
        float f = u - float(band);
        float hw = max(bfiSeam*float(n)*0.5, 1e-4);
        o = Strobe(lin, (slot + n - band) % n, litGain);
        float wn = 0.0; int nb = band;
        if(f < hw && band > 0)                { wn = 0.5*(1.0 - f/hw);         nb = band - 1; }
        else if(1.0 - f < hw && band+1 < n)   { wn = 0.5*(1.0 - (1.0-f)/hw);   nb = band + 1; }
        if(wn > 0.0) o = mix(o, Strobe(lin, (slot + n - nb) % n, litGain), wn);
    } else {
        o = Strobe(lin, slot, litGain);
    }
    if(bfiPaused == 0 && bfiSwap > 0.0) o = mix(o, lin*bfiAvg, bfiSwap);   // LCD phase-swap fade
    return o;
}

void main(){
    vec3 e = (srcLinear == 1) ? enc(texture(tex, vec2(v_uv.x, 1.0 - v_uv.y)).rgb)
                              : clamp(texture(tex, v_uv).rgb, 0.0, 1.0);

    e *= 1.0 + hum*sin(v_uv.y*6.2831*1.5 - time*1.3);        // rolling hum bar

    if(mask > 0.0){                                          // aperture grille
        int k = int(mod(gl_FragCoord.x, 3.0));
        vec3 oh = vec3(float(k==0), float(k==1), float(k==2));
        e *= ((1.0-mask) + mask*1.5*oh)/(1.0 - 0.5*mask);
    }

    if(noiseAmt > 0.0){
        float t = mod(floor(time*60.0), 4096.0);
        float snow   = hash(vec3(gl_FragCoord.xy, t)) - 0.5;
        float sx     = max(1.0, res.x/1920.0*6.0);
        float streak = hash(vec3(floor(gl_FragCoord.x/sx), floor(gl_FragCoord.y/(res.y/lines)), t)) - 0.5;
        e += noiseAmt*(0.65*snow + 0.7*streak);
    }
    e = clamp(e, 0.0, 1.0);
    if(bfiOn == 1) e = enc(applyBFI(srgb2lin(e)));          // BFI goes last, in linear light
    if(bfiStatus == 1 && gl_FragCoord.x < 24.0 && v_uv.y*res.y < 24.0) e = statusCol;
    outc = vec4(clamp(e, 0.0, 1.0), 1.0);
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
        f = self.cam.grab()          # None if the screen hasn't changed
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
    """Captures on a background thread so that drawing stays locked to VSync (matters for BFI).
    grab() returns (BGRA data, width, height) when a NEW frame has arrived, otherwise None.
    dxcam = fast, but gives nothing while the desktop is perfectly static or on some GPU setups;
    mss = slower GDI capture that always works. 'Auto' starts with dxcam and falls back to mss."""

    def __init__(self, mode="Auto"):
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
_HELPERS = []          # child processes to kill on exit


def shutdown_helpers():
    for pr in list(_HELPERS):
        try:
            pr.terminate()
        except Exception:
            pass
    _HELPERS.clear()


atexit.register(shutdown_helpers)


def cpu_label_score(name):
    """How much a temperature sensor's label looks like 'the CPU temperature' (0 = not CPU)."""
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
    """Parse a HWiNFO shared-memory image (HWiNFO_SENSORS_SHARED_MEM2). Returns (deg C, label) or None."""
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
        if struct.unpack_from("<I", buf, o)[0] != 1:          # 1 = temperature reading
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
    """CPU temperature from HWiNFO (must be running with 'Shared Memory Support' enabled)."""
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
    """NVIDIA GPU temperature through NVML (ships with the driver, no admin needed)."""
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
    """No-admin fallback: ACPI thermal-zone perf counter (often missing or constant)."""
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


# One long-lived PowerShell helper (spawning one per reading would stutter the overlay). It uses
# LibreHardwareMonitorLib.dll if it sits next to this script, else the LibreHardwareMonitor /
# OpenHardwareMonitor WMI providers (if either app is running), else the ACPI thermal zone.
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
    # LibreHardwareMonitor.exe.config (binding redirects) is not honoured by powershell.exe, so
    # hand out whatever version of a dependency sits next to the DLL.
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
    """Tries HWiNFO, then the PowerShell helper (LHM dll / LHM+OHM WMI / ACPI WMI), then the
    no-admin perf counter. read() returns (deg C or None, source name)."""

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
            self.stop()                      # HWiNFO is enough; don't keep PowerShell around
            return hw[0], "HWiNFO: " + hw[1]
        self._ensure_ps()
        now = time.time()
        if self.ps_val is not None and now - self.ps_stamp < 8.0:
            return self.ps_val, self.ps_src
        if now >= self.tc_next:              # last resort, rate-limited with back-off
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
            print("[temp] No CPU temperature source found (admin: %s). Options: run HWiNFO with "
                  "'Shared Memory Support' on; or put LibreHardwareMonitorLib.dll + HidSharp.dll "
                  "next to this script and run as administrator; or run LibreHardwareMonitor / "
                  "OpenHardwareMonitor; or choose 'GPU temperature (NVIDIA)'."
                  % ("yes" if self.S.admin else "no"))
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
            chain.stop()                     # nothing needs a sensor: no background work at all
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
    ("Phosphor decay", "decay", 0.0, 0.95),
    ("Blur / bandwidth", "blur", 0.0, 3.0),
    ("RF noise", "noise", 0.0, 0.5),
    ("Scanlines", "scan", 0.0, 1.0),
    ("Phosphor mask", "mask", 0.0, 1.0),
    ("Ghosting (RF echo)", "ghost", 0.0, 0.5),
    ("Dot crawl", "dot", 0.0, 1.0),
    ("Line jitter", "jitter", 0.0, 2.0),
    ("Saturation", "sat", 0.0, 2.0),
    ("Screen curvature", "curv", 0.0, 0.3),
    ("CRT gamma", "gam", 1.8, 2.8),
    ("Sensor reactivity", "react", 0.0, 2.0),
]


INT_ATTRS = {"bfi_frames", "bfi_lcd_sec", "fps_cap"}


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
        tab_bfi = ttk.Frame(nb, padding=4)
        tab_perf = ttk.Frame(nb, padding=4)
        nb.add(tab_crt, text="CRT / RF")
        nb.add(tab_bfi, text="BFI (strobe)")
        nb.add(tab_perf, text="Performance")
        vars_ = {}

        def add_combo(parent, row, label, values, initial, on_pick):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **pad)
            var = tk.StringVar(root, value=initial)
            cb = ttk.Combobox(parent, textvariable=var, values=values, state="readonly", width=40)
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
                    ro.config(text=("%.0f" if hi >= 100 else "%.2f") % val)
                setattr(S, a, val)
            var.trace_add("write", on_change)
            on_change()

        def add_check(parent, row, label, attr):
            var = tk.BooleanVar(root, value=bool(getattr(S, attr)))
            ttk.Checkbutton(parent, text=label, variable=var,
                            command=lambda: setattr(S, attr, bool(var.get()))
                            ).grid(row=row, column=0, columnspan=3, sticky="w", **pad)

        # ---------------- CRT tab ----------------
        def on_preset(name):
            S.apply_preset(name)
            for _, attr, _, _ in SLIDERS:
                vars_[attr].set(getattr(S, attr))
        add_check(tab_crt, 0, "CRT / RF effect enabled (independent of BFI)", "crt_on")
        add_combo(tab_crt, 1, "Signal / shader", PRESET_ORDER, S.preset, on_preset)
        add_combo(tab_crt, 2, "Resampling kernel", KERNELS, KERNELS[S.kernel],
                  lambda v: setattr(S, "kernel", KERNELS.index(v)))
        add_combo(tab_crt, 3, "Noise driven by", DRIVERS, S.driver, lambda v: setattr(S, "driver", v))
        for i, (label, attr, lo, hi) in enumerate(SLIDERS):
            add_slider(tab_crt, 4 + i, label, attr, lo, hi)

        # ---------------- BFI tab ----------------
        ttk.Label(tab_bfi, foreground="#a00", wraplength=420, justify="left",
                  text="Works on its own: you can turn the CRT / RF effect off and use only BFI. "
                       "Warning: strobing flickers on purpose (photosensitivity risk). Needs VSync at "
                       "the display's fixed refresh rate (G-SYNC/VRR off) and dxcam capture."
                  ).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        add_check(tab_bfi, 1, "BFI enabled", "bfi_on")
        add_combo(tab_bfi, 2, "Method", BFI_METHODS, BFI_METHODS[S.bfi_method],
                  lambda v: setattr(S, "bfi_method", BFI_METHODS.index(v)))
        add_slider(tab_bfi, 3, "Frames per cycle", "bfi_frames", 2, 8)
        add_slider(tab_bfi, 4, "Gain vs blur", "bfi_gain", 0.3, 1.0)
        add_slider(tab_bfi, 5, "Dark frame floor", "bfi_floor", 0.0, 0.5)
        add_check(tab_bfi, 6, "Highlight roll-off (BFI method)", "bfi_rolloff")
        add_slider(tab_bfi, 7, "Roll-off knee", "bfi_knee", 0.5, 0.95)
        add_check(tab_bfi, 8, "Sine BFI", "bfi_sine")
        add_check(tab_bfi, 9, "Invert cycle (frames > 2)", "bfi_invert")
        add_check(tab_bfi, 10, "Rolling scan", "bfi_rolling")
        add_slider(tab_bfi, 11, "Rolling scan: seam", "bfi_seam", 0.0, 0.3)
        add_slider(tab_bfi, 12, "Rolling scan: phosphor decay", "bfi_decay", 0.0, 1.0)
        add_slider(tab_bfi, 13, "Display refresh rate (Hz)", "bfi_refresh", 60.0, 360.0)
        add_check(tab_bfi, 14, "Hitch guard", "bfi_hitch")
        add_slider(tab_bfi, 15, "Pause after a hitch (ms)", "bfi_hold", 0.0, 3000.0)
        add_slider(tab_bfi, 16, "Frame time tolerance", "bfi_tol", 0.2, 0.9)
        add_check(tab_bfi, 17, "LCD: prevent image retention", "bfi_lcd")
        add_slider(tab_bfi, 18, "Seconds of full strobe between swaps", "bfi_lcd_sec", 1, 300)
        add_check(tab_bfi, 19, "Show status square (green/amber/red)", "bfi_status")

        # ---------------- Performance tab ----------------
        add_combo(tab_perf, 0, "Capture method", CAPTURES, S.capture_mode,
                  lambda v: setattr(S, "capture_mode", v))
        add_combo(tab_perf, 1, "Frame rate", FPS_MODES, S.fps_mode, lambda v: setattr(S, "fps_mode", v))
        add_slider(tab_perf, 2, "Custom limit (fps)", "fps_cap", 15, 360)
        ttk.Label(tab_perf, foreground="#555", wraplength=420, justify="left",
                  text="VSync = one frame per display refresh (smoothest, lowest load). Uncapped renders "
                       "as fast as possible and will compete with your game for the GPU. Custom limit "
                       "caps at the value above. BFI always uses VSync, whatever is chosen here."
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
                  text="CPU temperature needs a sensor reader (Windows has none built in). Checked in "
                       "order: HWiNFO running with 'Shared Memory Support'; LibreHardwareMonitorLib.dll "
                       "+ HidSharp.dll next to this script (run as administrator); LibreHardwareMonitor "
                       "or OpenHardwareMonitor running; the PC's ACPI thermal zone (many PCs report "
                       "nothing useful). 'GPU temperature (NVIDIA)' needs none of this."
                  ).grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        # ---------------- shared buttons ----------------
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
        exclude_from_capture(menu_hwnd)   # menu stays crisp on top of the effect

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
            if S.relift:                      # overlay was just (re)shown: get back on top, once
                S.relift = False
                if visible:
                    keep_topmost(menu_hwnd)
            if visible and tick % 12 == 0:
                t = "n/a" if S.temp is None else "%.0f C" % S.temp
                if S.temp is not None and S.temp_source:
                    t += " (" + S.temp_source + ")"
                src_label.config(text=(S.temp_source or "none yet / not selected") +
                                 ("   [admin: yes]" if S.admin else "   [admin: no]"))
                txt = ("CPU %.0f%%  RAM %.0f%%  Temp %s  |  %.0f fps  |  %s" %
                       (S.cpu, S.ram, t, S.fps, S.capture_kind))
                if is_temp_driver(S.driver) and S.temp is None:
                    txt += "  |  no temp sensor yet: noise not reacting (see Performance tab)"
                if S.bfi_on:
                    txt += "  |  BFI " + (("PAUSED: " + S.bfi_reason) if S.bfi_paused else "on")
                    if S.capture_kind == "mss":
                        txt += " (mss is too slow for BFI)"
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

    sys.setswitchinterval(0.001)       # snappier thread hand-offs = fewer missed refreshes
    try:
        psutil.Process().nice(psutil.HIGH_PRIORITY_CLASS)   # allowed without admin
    except Exception:
        pass
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    MenuThread(S).start()
    threading.Thread(target=temp_worker, args=(S,), daemon=True).start()
    grab = Grabber(S.capture_mode)
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
    user32.ShowWindow(hwnd, 0)   # stay hidden until we hold a real frame (no black screen)

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
    idx = 0  # T[idx] holds the previous frame

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
    bst = BFIState()
    prev_crt = True
    bfi_t0 = time.perf_counter()
    prev_flip = bfi_t0
    ft_last = 1000.0 / max(S.bfi_refresh, 1.0)
    force_cap = False
    cur_interval = None       # forces the swap interval to be applied explicitly on the first frame
    warned_swap = False
    dwm_sync = False          # pace with DwmFlush when the window's own VSync doesn't throttle
    last_dt_t = time.perf_counter()

    while not S.quit:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                S.quit = True

        if S.capture_mode != applied_mode:      # user picked a capture method in the menu
            applied_mode = S.capture_mode
            grab.set_mode(applied_mode)

        want = S.active and (S.crt_on or S.bfi_on)
        grab.paused = not want              # no capture work while everything is off
        if grab.failed:
            S.quit = True
            continue

        if not want:
            if shown:
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
                shown = False
            clock.tick(20)
            continue

        # ---- frame-rate policy: VSync / uncapped / custom limit (BFI always needs VSync)
        mode_idx = FPS_MODES.index(S.fps_mode) if S.fps_mode in FPS_MODES else 0
        if S.bfi_on or mode_idx == 0:
            interval = 1
            # VSync paces the loop; only cap by hand if VSync turned out not to work
            cap = int(S.bfi_refresh) if force_cap else 0
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
        dt = max(tnow - last_dt_t, 1e-4)    # precise, so phosphor decay is right at any frame rate
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
            # No sensor reading = no reaction (it does NOT fall back to CPU load)
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
                screen_tex.swizzle = "BGRA"      # capture is BGRA; shader sees RGB
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
            user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            keep_topmost(hwnd)
            S.relift = True             # menu re-raises itself above the overlay (once)
            for f in F:
                f.clear(0, 0, 0, 1)
            shown = True
            bst.reset()
            bfi_t0 = time.perf_counter()
            prev_flip = bfi_t0

        scale = W / 1920.0
        noise_eff = min(max(S.noise + S.react * drive * 0.25, 0.0), 0.6)
        jitter_eff = S.jitter * (1.0 + S.react * drive * 1.5)
        ghost_eff = S.ghost * (1.0 + S.react * drive)
        el = now - t0
        frame += 1

        # ---- BFI state (hitch guard / LCD swap) and per-frame strobe parameters
        bn = int(min(max(S.bfi_frames, 2), 8))
        b_inv = bool(S.bfi_invert and bn > 2)
        b_lit = bn * S.bfi_gain / (bn - 1) if b_inv else bn * S.bfi_gain
        b_paused = False
        b_slot = 0
        b_avg = 1.0
        if S.bfi_on:
            now_ms = (time.perf_counter() - bfi_t0) * 1000.0
            b_paused = bst.update(S, ft_last, now_ms, frame)
            b_slot = (frame + max(bst.sx, 0)) % bn
            b_avg = sum(bfi_target(j, bn, b_lit, S.bfi_floor, S.bfi_sine, b_inv)
                        for j in range(bn)) / bn
            P_ms = 1000.0 / max(S.bfi_refresh, 1.0)
            if abs(bst.ft - P_ms) > 0.1 * P_ms:
                b_col = (1.0, 0.1, 0.1)
            elif b_paused:
                b_col = (1.0, 0.7, 0.0)
            else:
                b_col = (0.1, 1.0, 0.2)
        else:
            bst.reset()
            b_col = (0.1, 1.0, 0.2)
        S.bfi_paused = b_paused
        S.bfi_reason = bst.reason if b_paused else ""

        crt = bool(S.crt_on)
        if crt and not prev_crt:
            for f in F:
                f.clear(0, 0, 0, 1)         # drop stale phosphor trails
        prev_crt = crt

        w_idx = 1 - idx
        if crt:
            screen_tex.use(0)
            T[idx].use(1)
            setu(prog1, "screen", 0)
            setu(prog1, "prev", 1)
            setu(prog1, "res", (float(W), float(H)))
            setu(prog1, "time", float(el % 1000.0))
            setu(prog1, "parity", float(int(el * 30.0) % 2))   # dot crawl flips at 30 Hz at any fps
            setu(prog1, "lines", float(S.lines))
            setu(prog1, "lumaSigma", float(S.luma * S.blur * scale))
            setu(prog1, "chromaSigma", float(S.chroma * S.blur * scale))
            setu(prog1, "jitter", float(jitter_eff))
            setu(prog1, "ghost", float(ghost_eff))
            setu(prog1, "dotamt", float(S.dot))
            setu(prog1, "curv", float(S.curv))
            setu(prog1, "gam", float(S.gam))
            setu(prog1, "decay", float(S.decay ** (dt * 60.0)))
            setu(prog1, "sat", float(S.sat))
            setu(prog1, "hue", float(S.hue))
            setu(prog1, "scan", float(S.scan))
            setu(prog1, "kernel", int(S.kernel))
            setu(prog1, "pal", int(S.pal))
            F[w_idx].use()
            ctx.viewport = (0, 0, W, H)
            vao1.render(moderngl.TRIANGLE_STRIP)

        ctx.screen.use()
        ctx.viewport = (0, 0, W, H)
        if crt:
            T[w_idx].use(0)
        else:
            screen_tex.use(0)               # BFI only: strobe the raw captured screen
        setu(prog2, "tex", 0)
        setu(prog2, "srcLinear", 1 if crt else 0)
        setu(prog2, "res", (float(W), float(H)))
        setu(prog2, "time", float(el % 1000.0))
        setu(prog2, "noiseAmt", float(noise_eff if crt else 0.0))
        setu(prog2, "mask", float(S.mask if crt else 0.0))
        setu(prog2, "hum", float(noise_eff * 0.35 if crt else 0.0))
        setu(prog2, "lines", float(S.lines))
        setu(prog2, "bfiOn", int(S.bfi_on))
        setu(prog2, "bfiMethod", int(S.bfi_method))
        setu(prog2, "bfiFrames", bn)
        setu(prog2, "bfiSlot", int(b_slot))
        setu(prog2, "bfiPaused", int(b_paused))
        setu(prog2, "bfiRolloff", int(S.bfi_rolloff))
        setu(prog2, "bfiSine", int(S.bfi_sine))
        setu(prog2, "bfiInv", int(b_inv))
        setu(prog2, "bfiRolling", int(S.bfi_rolling))
        setu(prog2, "bfiStatus", int(S.bfi_status and S.bfi_on))
        setu(prog2, "bfiLit", float(b_lit))
        setu(prog2, "bfiAvg", float(b_avg))
        setu(prog2, "bfiFloor", float(S.bfi_floor))
        setu(prog2, "bfiKnee", float(S.bfi_knee))
        setu(prog2, "bfiSeam", float(S.bfi_seam))
        setu(prog2, "bfiDecay", float(S.bfi_decay))
        setu(prog2, "bfiSwap", float(bst.sy))
        setu(prog2, "statusCol", b_col)
        vao2.render(moderngl.TRIANGLE_STRIP)

        if diag < 5 and frame > 3:   # one-off self-check printed to the console
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
                elif out.mean() < 1 and inn.mean() > 3 and not S.bfi_on and S.crt_on:
                    print("WARNING: capture has content but shader output is black. "
                          "Please report this line.")
            except Exception as e:
                print("[diag] failed:", e)

        pygame.display.flip()
        if S.bfi_on:
            ctx.finish()    # wait for the frame to really complete: stops the driver queuing frames
                            # ahead, so measured frame times match what the display shows
        if interval == 1 and dwm_sync and not force_cap:
            try:
                dwmapi.DwmFlush()   # block until the next compositor refresh = true display pacing
            except Exception:
                pass
        tf = time.perf_counter()
        ft_last = (tf - prev_flip) * 1000.0     # frame time as the BFI hitch guard sees it
        prev_flip = tf
        if interval == 1 and frame % 240 == 0 and S.fps > S.bfi_refresh * 1.3:
            if not dwm_sync:
                dwm_sync = True
                print("Window VSync is not throttling (%.0f fps vs %.0f Hz). That is normal for a "
                      "click-through window; pacing with DwmFlush instead." % (S.fps, S.bfi_refresh))
            elif not force_cap:
                force_cap = True
                print("DwmFlush pacing did not hold (%.0f fps vs %.0f Hz): capping the frame rate "
                      "to the refresh rate manually. Check the refresh-rate slider (BFI tab)."
                      % (S.fps, S.bfi_refresh))
        if crt:
            idx = w_idx

    S.quit = True
    shutdown_helpers()
    pygame.quit()


if __name__ == "__main__":
    main()
