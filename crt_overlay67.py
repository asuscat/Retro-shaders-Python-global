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
 * Noise can be driven by CPU load, CPU temperature (best effort) or RAM.
 * Optional BFI strobe (black frame insertion / Variable MPRT / rolling scan) for motion
   clarity, ported from the "BFI Presenter" ReShade shader (see licence note below).
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
DRIVERS = ["CPU load", "CPU temperature (falls back to load)", "RAM usage", "Off"]


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
        self.fps = 0.0
        self.capture_kind = "?"
        self.capture_mode = "Auto"
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
def temp_worker(S):
    """CPU temp without admin: ACPI thermal-zone perf counter (may be flat/absent on some PCs)."""
    while not S.quit:
        if not S.driver.startswith("CPU temp"):
            time.sleep(0.5)    # don't spawn a process every few seconds unless it is used
            continue
        t = None
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
            t = max(vals) if vals else None
        except Exception:
            t = None
        S.temp = t
        for _ in range(50):
            if S.quit:
                return
            time.sleep(0.1)


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


INT_ATTRS = {"bfi_frames", "bfi_lcd_sec"}


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
        nb.add(tab_crt, text="CRT / RF")
        nb.add(tab_bfi, text="BFI (strobe)")
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

        # ---------------- shared buttons ----------------
        bottom = ttk.Frame(outer)
        bottom.grid(row=1, column=0, sticky="we", pady=(6, 0))
        add_combo(bottom, 0, "Capture method", CAPTURES, S.capture_mode,
                  lambda v: setattr(S, "capture_mode", v))
        toggle_btn = ttk.Button(bottom, width=30)
        toggle_btn.grid(row=1, column=0, sticky="we", **pad)

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
        ttk.Button(bottom, text="Close program", command=quit_all).grid(row=1, column=1, sticky="we", **pad)

        status = ttk.Label(bottom, text="", foreground="#555")
        status.grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(bottom, text="Press HOME to open/close this menu.", foreground="#555").grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)

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
                txt = ("CPU %.0f%%  RAM %.0f%%  Temp %s  |  %.0f fps  |  %s" %
                       (S.cpu, S.ram, t, S.fps, S.capture_kind))
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

        if S.bfi_on:
            # VSync paces the loop; only cap by hand if VSync turned out not to work
            cap = int(S.bfi_refresh) if force_cap else 0
        else:
            cap = 60
        dt = clock.tick(cap) / 1000.0
        S.fps = clock.get_fps()
        now = time.time()

        if now - last_sensor > 0.5:
            last_sensor = now
            S.cpu = psutil.cpu_percent(None)
            S.ram = psutil.virtual_memory().percent
        if S.driver == "CPU load":
            target = S.cpu / 100.0
        elif S.driver.startswith("CPU temp"):
            target = min(max((S.temp - 40.0) / 50.0, 0.0), 1.0) if S.temp is not None else S.cpu / 100.0
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
            setu(prog1, "parity", float(frame % 2))
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
        tf = time.perf_counter()
        ft_last = (tf - prev_flip) * 1000.0     # frame time as the BFI hitch guard sees it
        prev_flip = tf
        if (S.bfi_on and not force_cap and frame % 240 == 0
                and S.fps > S.bfi_refresh * 1.3):
            force_cap = True
            print("VSync does not seem to be active (%.0f fps vs %.0f Hz): capping the frame rate "
                  "for BFI. Check the refresh-rate slider and your GPU's VSync setting."
                  % (S.fps, S.bfi_refresh))
        if crt:
            idx = w_idx

    S.quit = True
    pygame.quit()


if __name__ == "__main__":
    main()
