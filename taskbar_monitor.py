#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 任务栏系统监控
把 CPU 占用 / 内存 / 显存 / CPU 温度 / GPU 温度 / 网速上下行 / FPS
直接贴在任务栏上（系统托盘左侧），类似 TrafficMonitor。

数据来源：
  - psutil          : CPU 占用、内存、网速（无需提权）
  - pynvml (NVML)   : GPU 温度、显存、GPU 利用率（无需提权）
  - lhm_dump.exe    : CPU 温度（LibreHardwareMonitorLib，需要管理员权限）
  - PresentMon.exe  : FPS（Intel PresentMon，走 ETW 抓帧）

显示实现：
  - 无边框、置顶、半透明、可点击穿透（WS_EX_TRANSPARENT）的窗口
  - 通过 win32 定位到任务栏 Shell_TrayWnd 的 TrayNotifyWnd 左侧
  - 每 1 秒刷新，跟随任务栏移动/缩放/自动隐藏
"""

import os
import sys
import json
import time
import csv
import collections
import threading
import queue
import ctypes
import subprocess
import traceback
from pathlib import Path
from ctypes import wintypes
from typing import Callable, Optional, Sequence, Tuple, Dict, Any

import psutil
from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------------------------------------------------------------------------
# 路径与显示参数
# ---------------------------------------------------------------------------
FROZEN = getattr(sys, "frozen", False)

if FROZEN:
    # PyInstaller 打包后：数据解压到 _MEIPASS，日志写在 exe 旁
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    LHM_DIR = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "LibreHardwareMonitor")
    PM_EXE = os.path.join(getattr(sys, "_MEIPASS", BASE_DIR), "PresentMon.exe")
    LOG_FILE = os.path.join(BASE_DIR, "monitor.log")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    # 开发模式：优先用环境变量，否则用源码目录下的 LibreHardwareMonitor/
    LHM_DIR = os.environ.get(
        "LHM_DIR",
        os.path.join(BASE_DIR, "LibreHardwareMonitor"),
    )
    PM_EXE = os.environ.get(
        "PRESENTMON_EXE",
        os.path.join(BASE_DIR, "PresentMon.exe"),
    )
    LOG_FILE = os.path.join(BASE_DIR, "monitor.log")

LHM_EXE = os.path.join(LHM_DIR, "lhm_dump.exe")

REFRESH_MS = 1000          # 刷新间隔（毫秒）
CPU_TEMP_EVERY = 4         # CPU 温度每隔 N 个 tick 读一次

BG = "#e8e8f0"             # 贴片背景色（浅色）
FG = "#16161c"             # 文字颜色（深色）
FONT = "Segoe UI"
FONT_SIZE = 9
HPAD = 9                   # 贴片左右内边距（px）
VPAD = 3                   # 贴片距任务栏上下边距（px）
LEFT_MARGIN = 8            # 贴片左边缘距任务栏左端的距离（px）


def log(*a):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(" ".join(str(x) for x in a) + "\n")
    except Exception:
        pass


def log_exc(tag="CRASH"):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n=== %s @ %s ===\n" % (tag, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write(traceback.format_exc())
            f.write("\n")
    except Exception:
        pass


def close_quietly(callback: Optional[Callable[[], object]]) -> None:
    if callback is None:
        return
    try:
        callback()
    except Exception:
        pass


class SingleInstance:
    """Own a named Windows mutex so only one monitor can run at a time."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\SysMonitor") -> None:
        self.name = name
        self.handle = None
        self.kernel32 = ctypes.windll.kernel32
        self.kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel32.CreateMutexW.restype = wintypes.HANDLE
        self.kernel32.GetLastError.restype = wintypes.DWORD
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

    def acquire(self) -> bool:
        self.handle = self.kernel32.CreateMutexW(None, True, self.name)
        if not self.handle:
            return False
        if self.kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS:
            self.release()
            return False
        return True

    def release(self) -> None:
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def fmt_bytes(b):
    b = float(b)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024.0:
            return "%.0f%s" % (b, unit)
        b /= 1024.0
    return "%.1fPB" % b


def fmt_speed(bps):
    bps = float(bps)
    if bps < 1024.0:
        return "%.0f B/s" % bps
    if bps < 1024.0 ** 2:
        return "%.1f KB/s" % (bps / 1024.0)
    if bps < 1024.0 ** 3:
        return "%.1f MB/s" % (bps / 1024.0 ** 2)
    return "%.1f GB/s" % (bps / 1024.0 ** 3)


def fmt_rate_short(bps):
    """短格式网速，用于任务栏贴片：0 / 1.2K / 3.4M / 5.6G。"""
    bps = float(bps)
    for u in ("", "K", "M", "G", "T"):
        if bps < 1024.0 or u == "T":
            if u == "":
                return "%.0f" % bps
            return "%.1f%s" % (bps, u)
        bps /= 1024.0
    return "%.1fT" % bps


def fmt_gb(b):
    return "%.1fG" % (float(b) / (1024.0 ** 3))


def safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 数据采集器
# ---------------------------------------------------------------------------
class CpuTempReader:
    """Read CPU temperature off the Tk event thread."""

    def __init__(self) -> None:
        self._value = None
        self._lock = threading.Lock()
        self._request = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="cpu-temp", daemon=True)
        self._thread.start()
        self.request()

    def request(self) -> None:
        self._request.set()

    def get(self) -> Optional[float]:
        with self._lock:
            return self._value

    def _run(self) -> None:
        while not self._stop.is_set():
            self._request.wait()
            self._request.clear()
            if self._stop.is_set():
                break
            value = read_cpu_temp()
            with self._lock:
                self._value = value

    def close(self) -> None:
        self._stop.set()
        self._request.set()


class GpuReader:
    """通过 NVML 读 GPU 温度 / 显存 / 利用率。"""

    def __init__(self):
        self.ok = False
        self.handle = None
        try:
            import pynvml
            self.pynvml = pynvml
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            self.ok = True
        except Exception as e:
            log("GPU init failed:", e)

    def read(self):
        if not self.ok:
            return None
        try:
            p = self.pynvml
            temp = p.nvmlDeviceGetTemperature(self.handle, p.NVML_TEMPERATURE_GPU)
            mem = p.nvmlDeviceGetMemoryInfo(self.handle)
            util = p.nvmlDeviceGetUtilizationRates(self.handle)
            return {
                "temp": safe_int(temp),
                "vram_used": mem.used,
                "vram_total": mem.total,
                "util": safe_int(util.gpu),
            }
        except Exception as e:
            log("GPU read failed:", e)
            return None


def read_cpu_temp():
    """调用 lhm_dump.exe 读 CPU 温度（Tctl/Tdie）。需管理员权限，否则返回 None。"""
    if not os.path.exists(LHM_EXE):
        return None
    try:
        p = subprocess.run(
            [LHM_EXE], cwd=LHM_DIR, capture_output=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        data = json.loads(p.stdout.decode("utf-8", "replace"))
        for s in data:
            if s.get("htype") == "Cpu" and s.get("stype") == "Temperature":
                name = s.get("name", "")
                if ("Tctl" in name) or ("Tdie" in name) or ("Package" in name):
                    v = s.get("value")
                    if v and float(v) > 0:
                        return float(v)
        return None
    except Exception as e:
        log("cpu temp read failed:", e)
        return None


def network_rates(
    previous: Sequence[int], current: Sequence[int], elapsed: float
) -> Tuple[float, float]:
    """Return non-negative receive/send rates from cumulative counters."""
    if elapsed <= 0:
        return 0.0, 0.0
    recv_delta = current[0] - previous[0]
    sent_delta = current[1] - previous[1]
    return max(0.0, recv_delta / elapsed), max(0.0, sent_delta / elapsed)


class FpsReader:
    """通过 PresentMon 读取当前主进程（游戏）的 FPS。"""

    BLOCKLIST = {
        "dwm.exe", "explorer.exe", "WindowsTerminal.exe", "conhost.exe",
        "cmd.exe", "powershell.exe", "SysMonitor.exe", "python.exe", "pythonw.exe",
        "SearchHost.exe", "SearchApp.exe", "StartMenuExperienceHost.exe",
        "ShellExperienceHost.exe", "TextInputHost.exe", "ApplicationFrameHost.exe",
        "SystemSettings.exe", "msedge.exe", "chrome.exe", "firefox.exe",
        "<unknown>", "Idle",
    }

    def __init__(self):
        self.frames = collections.defaultdict(collections.deque)
        self.lock = threading.Lock()
        self.proc = None
        self.header = None
        self.app_idx = 0
        self._start()

    def _start(self):
        if not os.path.exists(PM_EXE):
            log("PresentMon not found:", PM_EXE)
            return
        try:
            self.proc = subprocess.Popen(
                [PM_EXE, "--output_stdout", "--no_console_stats",
                 "--session_name", "sysmon", "--stop_existing_session"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self._reader, daemon=True).start()
        except Exception as e:
            log("FPS init failed:", e)

    def _parse_parts(self, parts: Sequence[str]) -> Optional[str]:
        if self.header is None:
            self.header = parts
            self.app_idx = parts.index("Application") if "Application" in parts else 0
            return None
        if len(parts) <= self.app_idx:
            return None
        return parts[self.app_idx]

    def _parse_line(self, line):
        return self._parse_parts(next(csv.reader([line])))

    @staticmethod
    def _prune(dq, now):
        while dq and now - dq[0] > 1.0:
            dq.popleft()

    def _reader(self):
        try:
            for parts in csv.reader(self.proc.stdout):
                if not parts:
                    continue
                app = self._parse_parts(parts)
                if not app:
                    continue
                now = time.time()
                with self.lock:
                    dq = self.frames[app]
                    dq.append(now)
                    self._prune(dq, now)
        except Exception:
            pass

    def get(self):
        """返回主进程最近 1 秒的 FPS；没有候选进程时返回 None。"""
        now = time.time()
        with self.lock:
            best = 0
            expired = []
            for app, dq in self.frames.items():
                self._prune(dq, now)
                if not dq:
                    expired.append(app)
                    continue
                if app in self.BLOCKLIST:
                    continue
                best = max(best, len(dq))
            for app in expired:
                self.frames.pop(app, None)
            return best if best >= 10 else None

    def close(self):
        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 自检模式
# ---------------------------------------------------------------------------
def selftest():
    gpu = GpuReader()
    psutil.cpu_percent(None)
    time.sleep(1.0)

    net1 = psutil.net_io_counters()
    t1 = time.time()
    time.sleep(1.0)
    net2 = psutil.net_io_counters()
    t2 = time.time()
    dt = t2 - t1
    net_down, net_up = network_rates(
        (net1.bytes_recv, net1.bytes_sent),
        (net2.bytes_recv, net2.bytes_sent),
        dt,
    )

    cpu_pct = psutil.cpu_percent(None)
    vm = psutil.virtual_memory()
    g = gpu.read()
    cput = read_cpu_temp()

    print("=== selftest ===")
    print("CPU%%            : %.1f%%" % cpu_pct)
    print("RAM             : %.1f%%  (%s / %s)" % (
        vm.percent, fmt_gb(vm.used), fmt_gb(vm.total)))
    if g:
        print("GPU temp        : %d C" % g["temp"])
        print("VRAM            : %s / %s  (util %d%%)" % (
            fmt_gb(g["vram_used"]), fmt_gb(g["vram_total"]), g["util"]))
    else:
        print("GPU             : unavailable")
    print("CPU temp        : %s C" % ("--" if cput is None else "%.0f" % cput))
    print("Net down        : %s" % fmt_speed(net_down))
    print("Net up          : %s" % fmt_speed(net_up))


# ---------------------------------------------------------------------------
# win32 辅助：定位任务栏、设置窗口样式
# ---------------------------------------------------------------------------

def rect(left, top, right, bottom):
    """Create a Win32 rectangle for geometry tests and layout helpers."""
    value = wintypes.RECT()
    value.left, value.top = left, top
    value.right, value.bottom = right, bottom
    return value


def overlay_geometry(tbr, nr, requested_width, requested_height):
    """Return a taskbar-safe overlay rectangle in screen coordinates."""
    taskbar_width = tbr.right - tbr.left
    taskbar_height = tbr.bottom - tbr.top
    if taskbar_width >= taskbar_height:
        available_width = nr.left - tbr.left - 2 * LEFT_MARGIN
        width = requested_width  # 不限制宽度，让窗口完整显示
        height = max(16, taskbar_height - 2 * VPAD)
        x = tbr.left + LEFT_MARGIN
        y = tbr.top + VPAD
    else:
        width = max(16, taskbar_width - 2 * VPAD)
        height = min(requested_height, max(16, taskbar_height - 2 * VPAD))
        x = tbr.left + VPAD
        y = tbr.top + VPAD
    return x, y, width, height


user32 = ctypes.windll.user32

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SW_HIDE = 0

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

try:
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG_PTR]
    user32.SetWindowLongPtrW.restype = wintypes.LONG_PTR
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = wintypes.LONG_PTR
    _SetWindowLongPtr = user32.SetWindowLongPtrW
    _GetWindowLongPtr = user32.GetWindowLongPtrW
except AttributeError:
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    user32.SetWindowLongW.restype = wintypes.LONG
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    _SetWindowLongPtr = user32.SetWindowLongW
    _GetWindowLongPtr = user32.GetWindowLongW


def get_toplevel_hwnd(widget_id):
    """从 tkinter 的 winfo_id() 找到真正的顶层窗口句柄。"""
    hwnd = widget_id
    while True:
        parent = user32.GetParent(hwnd)
        if not parent:
            break
        hwnd = parent
    return hwnd


def apply_clickthrough(hwnd):
    ex = _GetWindowLongPtr(hwnd, GWL_EXSTYLE)
    ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT | WS_EX_LAYERED
    _SetWindowLongPtr(hwnd, GWL_EXSTYLE, ex)


def get_taskbar_rects():
    """返回 (任务栏矩形, 通知区矩形)。找不到返回 None。"""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    notify = user32.FindWindowExW(tray, 0, "TrayNotifyWnd", None)
    tbr = wintypes.RECT()
    if not user32.GetWindowRect(tray, ctypes.byref(tbr)):
        return None
    nr = wintypes.RECT()
    if notify and user32.GetWindowRect(notify, ctypes.byref(nr)):
        return tbr, nr
    return tbr, tbr


def taskbar_visible(tbr):
    """判断任务栏当前是否可见（处理自动隐藏）。"""
    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    ix0 = max(tbr.left, 0)
    iy0 = max(tbr.top, 0)
    ix1 = min(tbr.right, sw)
    iy1 = min(tbr.bottom, sh)
    return (ix1 - ix0 > 50) and (iy1 - iy0 > 20)


class DashboardRenderer:
    """
    双行轻量仪表盘渲染器（基于 Pillow 内存画布）。
    特性：
    - 浅色半透明圆角卡片质感
    - 双行紧凑布局（CPU、GPU、RAM、网速、可选 FPS）
    - 随负载动态变色的彩色微型进度条（低负载科技蓝/翡翠绿/紫色，高负载橙/红警示）
    - 垂直细分割线、抗锯齿字体对齐
    """

    def __init__(
        self,
        bg_color: str = BG,
        border_color: str = "#d0d2de",
        divider_color: str = "#d5d7e4",
        fg_primary: str = "#16161c",
        fg_label: str = "#505565",
        fg_muted: str = "#6b7280",
    ) -> None:
        self.bg_color = bg_color
        self.border_color = border_color
        self.divider_color = divider_color
        self.fg_primary = fg_primary
        self.fg_label = fg_label
        self.fg_muted = fg_muted

        self.bar_track_color = "#d2d5e2"
        self.cpu_normal_color = "#0284c7"  # 天蓝
        self.gpu_normal_color = "#059669"  # 翡翠绿
        self.ram_normal_color = "#7c3aed"  # 优雅紫
        self.warning_color = "#ea580c"     # 警示橙
        self.alert_color = "#dc2626"       # 告警红
        self.net_down_color = "#0284c7"    # 醒目蓝
        self.net_up_color = "#059669"      # 翠绿
        self.fps_color = "#e11d48"         # 玫瑰红

        self._fonts_cache: Dict[Tuple[str, int], Any] = {}

    def get_font(self, name: str, size: int):
        key = (name, size)
        if key in self._fonts_cache:
            return self._fonts_cache[key]

        fonts_dir = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        is_bold = "b" in name.lower()
        candidates = [
            str(fonts_dir / name),
            name,
            str(fonts_dir / ("segoeuib.ttf" if is_bold else "segoeui.ttf")),
            str(fonts_dir / ("arialbd.ttf" if is_bold else "arial.ttf")),
            "segoeuib.ttf" if is_bold else "segoeui.ttf",
            "arial.ttf",
        ]
        font = None
        for c in candidates:
            try:
                font = ImageFont.truetype(c, size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()

        self._fonts_cache[key] = font
        return font

    def get_metric_color(self, pct: float, default_color: str = "#0284c7") -> str:
        if pct >= 85.0:
            return self.alert_color
        elif pct >= 70.0:
            return self.warning_color
        return default_color

    def measure(self, draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]

    def render(
        self,
        cpu_pct: float,
        cpu_temp: Optional[float],
        gpu_data: Optional[dict],
        ram_pct: float,
        ram_used_bytes: float,
        ram_total_bytes: float,
        net_down: float,
        net_up: float,
        fps: Optional[int] = None,
        target_height: int = 34,
    ) -> Tuple[Image.Image, int, int]:
        target_height = max(26, min(target_height, 48))

        font_label = self.get_font("segoeuib.ttf", 10)
        font_val_bold = self.get_font("segoeuib.ttf", 10)
        font_val_reg = self.get_font("segoeui.ttf", 10)
        font_sub = self.get_font("segoeui.ttf", 9)
        font_arrow = self.get_font("segoeuib.ttf", 11)

        dummy_img = Image.new("RGBA", (1, 1))
        d = ImageDraw.Draw(dummy_img)

        # 格式化各字段数据
        cpu_val_str = "%2.0f%%" % cpu_pct
        cpu_temp_str = "--" if cpu_temp is None else "%.0f°" % cpu_temp

        if gpu_data:
            gpu_val_str = "%2d%%" % gpu_data["util"]
            gpu_temp_str = "%d°" % gpu_data["temp"]
            vram_str = "%s / %s" % (fmt_gb(gpu_data["vram_used"]), fmt_gb(gpu_data["vram_total"]))
            gpu_pct_val = float(gpu_data["util"])
        else:
            gpu_val_str = "--"
            gpu_temp_str = "--"
            vram_str = "-- / --"
            gpu_pct_val = 0.0

        ram_val_str = "%2.0f%%" % ram_pct
        ram_mem_str = "%s / %s" % (fmt_gb(ram_used_bytes), fmt_gb(ram_total_bytes))

        down_str = self.fmt_speed_clean(net_down)
        up_str = self.fmt_speed_clean(net_up)

        # 1. 计算 CPU 宽度（Row 1: CPU + 数值 + 温度; Row 2: 饱满进度条）
        cpu_r1_w = (
            self.measure(d, "CPU", font_label)[0] + 6
            + self.measure(d, cpu_val_str, font_val_bold)[0] + 6
            + self.measure(d, cpu_temp_str, font_sub)[0]
        )
        cpu_w = max(cpu_r1_w, 68)

        # 2. 计算 GPU 宽度（即使未检测到也完整展示占位，不隐藏）
        gpu_r1_w = (
            self.measure(d, "GPU", font_label)[0] + 6
            + self.measure(d, gpu_val_str, font_val_bold)[0] + 6
            + self.measure(d, gpu_temp_str, font_sub)[0]
        )
        gpu_r2_w = 44 + 6 + self.measure(d, vram_str, font_sub)[0]
        gpu_w = max(gpu_r1_w, gpu_r2_w, 76)

        # 3. 计算 RAM 宽度（Row 1: RAM + 百分比; Row 2: 进度条 + 内存具体使用量，舒展不挤占）
        ram_r1_w = (
            self.measure(d, "RAM", font_label)[0] + 6
            + self.measure(d, ram_val_str, font_val_bold)[0]
        )
        ram_r2_w = 44 + 6 + self.measure(d, ram_mem_str, font_sub)[0]
        ram_w = max(ram_r1_w, ram_r2_w, 88)

        # 4. 计算 网速 宽度
        net_r1_w = self.measure(d, "↓ ", font_arrow)[0] + self.measure(d, down_str, font_val_reg)[0]
        net_r2_w = self.measure(d, "↑ ", font_arrow)[0] + self.measure(d, up_str, font_val_reg)[0]
        net_w = max(net_r1_w, net_r2_w, 76)

        # 5. 计算 FPS 宽度（若有）
        fps_w = 0
        if fps is not None:
            fps_r1_w = self.measure(d, "FPS", font_label)[0]
            fps_r2_w = self.measure(d, str(fps), font_val_bold)[0]
            fps_w = max(fps_r1_w, fps_r2_w, 28)

        sections = [
            ("cpu", cpu_w),
            ("gpu", gpu_w),
            ("ram", ram_w),
            ("net", net_w),
        ]
        if fps is not None:
            sections.append(("fps", fps_w))

        pad_x = 12
        spacing = 18
        total_w = pad_x * 2 + sum(w for _, w in sections) + spacing * (len(sections) - 1)

        img = Image.new("RGBA", (total_w, target_height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # 绘制浅色半透明圆角底板与细边框
        draw.rounded_rectangle(
            [0, 0, total_w - 1, target_height - 1],
            radius=7,
            fill=self.bg_color,
            outline=self.border_color,
            width=1,
        )

        y1 = 2 if target_height <= 36 else 3
        y2 = 18 if target_height <= 36 else 20
        bar_h = 4
        bar_y = y2 + 3

        cur_x = pad_x
        for idx, (sec_type, sec_w) in enumerate(sections):
            if idx > 0:
                div_x = cur_x - spacing // 2
                draw.line([(div_x, 5), (div_x, target_height - 5)], fill=self.divider_color, width=1)

            if sec_type == "cpu":
                x = cur_x
                draw.text((x, y1), "CPU", fill=self.fg_label, font=font_label)
                x += self.measure(d, "CPU", font_label)[0] + 6
                draw.text((x, y1), cpu_val_str, fill=self.fg_primary, font=font_val_bold)
                x += self.measure(d, cpu_val_str, font_val_bold)[0] + 6
                temp_color = self.alert_color if (cpu_temp and cpu_temp >= 85) else (
                    self.warning_color if (cpu_temp and cpu_temp >= 75) else self.fg_muted
                )
                draw.text((x, y1 + 1), cpu_temp_str, fill=temp_color, font=font_sub)

                # CPU 进度条
                bx, by, bw, bh = cur_x, bar_y, sec_w, bar_h
                draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, fill=self.bar_track_color)
                fill_w = max(2, int(bw * (min(100.0, max(0.0, cpu_pct)) / 100.0))) if cpu_pct > 0 else 0
                if fill_w > 0:
                    draw.rounded_rectangle(
                        [bx, by, bx + fill_w, by + bh],
                        radius=2,
                        fill=self.get_metric_color(cpu_pct, self.cpu_normal_color),
                    )

            elif sec_type == "gpu":
                x = cur_x
                draw.text((x, y1), "GPU", fill=self.fg_label, font=font_label)
                x += self.measure(d, "GPU", font_label)[0] + 6
                gpu_color = self.fg_primary if gpu_data else "#8c92a4"
                draw.text((x, y1), gpu_val_str, fill=gpu_color, font=font_val_bold)
                x += self.measure(d, gpu_val_str, font_val_bold)[0] + 6
                gpu_temp_color = (
                    self.alert_color if (gpu_data and gpu_data["temp"] >= 85) else (
                        self.warning_color if (gpu_data and gpu_data["temp"] >= 75) else (
                            self.fg_muted if gpu_data else "#8c92a4"
                        )
                    )
                )
                draw.text((x, y1 + 1), gpu_temp_str, fill=gpu_temp_color, font=font_sub)

                # GPU 进度条 + 显存信息
                bx, by, bw, bh = cur_x, bar_y, 44, bar_h
                draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, fill=self.bar_track_color)
                fill_w = max(2, int(bw * (min(100.0, max(0.0, gpu_pct_val)) / 100.0))) if gpu_pct_val > 0 else 0
                if fill_w > 0:
                    draw.rounded_rectangle(
                        [bx, by, bx + fill_w, by + bh],
                        radius=2,
                        fill=self.get_metric_color(gpu_data["util"], self.gpu_normal_color),
                    )
                tx = bx + bw + 6
                vram_color = self.fg_muted if gpu_data else "#8c92a4"
                draw.text((tx, y2), vram_str, fill=vram_color, font=font_sub)

            elif sec_type == "ram":
                x = cur_x
                draw.text((x, y1), "RAM", fill=self.fg_label, font=font_label)
                x += self.measure(d, "RAM", font_label)[0] + 6
                draw.text((x, y1), ram_val_str, fill=self.fg_primary, font=font_val_bold)

                # RAM 进度条 + 内存使用详情（并排舒展显示）
                bx, by, bw, bh = cur_x, bar_y, 44, bar_h
                draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=2, fill=self.bar_track_color)
                fill_w = max(2, int(bw * (min(100.0, max(0.0, ram_pct)) / 100.0))) if ram_pct > 0 else 0
                if fill_w > 0:
                    draw.rounded_rectangle(
                        [bx, by, bx + fill_w, by + bh],
                        radius=2,
                        fill=self.get_metric_color(ram_pct, self.ram_normal_color),
                    )
                tx = bx + bw + 6
                draw.text((tx, y2), ram_mem_str, fill="#555a6a", font=font_sub)

            elif sec_type == "net":
                x = cur_x
                draw.text((x, y1 - 1), "↓", fill=self.net_down_color, font=font_arrow)
                x += self.measure(d, "↓ ", font_arrow)[0]
                draw.text((x, y1), down_str, fill=self.fg_primary, font=font_val_reg)

                x2 = cur_x
                draw.text((x2, y2 - 1), "↑", fill=self.net_up_color, font=font_arrow)
                x2 += self.measure(d, "↑ ", font_arrow)[0]
                draw.text((x2, y2), up_str, fill=self.fg_primary, font=font_val_reg)

            elif sec_type == "fps":
                draw.text((cur_x, y1), "FPS", fill=self.fg_label, font=font_label)
                draw.text((cur_x, y2), str(fps), fill=self.fps_color, font=font_val_bold)

            cur_x += sec_w + spacing

        return img, total_w, target_height

    @staticmethod
    def fmt_speed_clean(bps: float) -> str:
        bps = float(bps)
        if bps < 1024.0:
            return "%.0f B/s" % bps
        if bps < 1024.0 ** 2:
            return "%.1f KB/s" % (bps / 1024.0)
        if bps < 1024.0 ** 3:
            return "%.1f MB/s" % (bps / 1024.0 ** 2)
        return "%.1f GB/s" % (bps / 1024.0 ** 3)


# ---------------------------------------------------------------------------
# 图形界面（贴任务栏）
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    import pystray
    from PIL import Image, ImageDraw

    log("startup: frozen=%s LHM_EXE=%s exists=%s PM_EXE=%s exists=%s" % (
        FROZEN, LHM_EXE, os.path.exists(LHM_EXE), PM_EXE, os.path.exists(PM_EXE)))

    gpu = GpuReader()
    fps_reader = None
    cpu_temp_reader = None
    psutil.cpu_percent(None)

    net_last = psutil.net_io_counters()
    net_last_t = time.time()

    state = {
        "net_down": 0.0,
        "net_up": 0.0,
        "manual_hidden": False,
    }

    cmd_q = queue.Queue()
    renderer = DashboardRenderer(bg_color=BG, fg_primary=FG)

    # ---- 贴片窗口 ----
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.92)
    root.configure(bg=BG)

    label = tk.Label(root, bg=BG, bd=0, highlightthickness=0)
    label.pack()  # 不 expand，窗口尺寸由 SetWindowPos 控制

    root.update_idletasks()
    hwnd = get_toplevel_hwnd(root.winfo_id())
    # 浮窗模式：不设置点击穿透，保留 WS_EX_TOPMOST

    # ---- 托盘图标（退出/显示隐藏入口，因为贴片本身点击穿透）----
    def make_icon_img():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2, 2, 62, 62], radius=13, fill=(16, 20, 26, 255))
        d.rounded_rectangle([2, 2, 62, 62], radius=13, outline=(0, 220, 150, 255), width=3)
        d.line([10, 40, 20, 40, 26, 18, 34, 46, 42, 28, 54, 28],
               fill=(0, 230, 160, 255), width=3, joint="curve")
        return img

    tray_icon = None

    def request_quit():
        cmd_q.put("quit")

    def request_toggle():
        cmd_q.put("toggle")

    def on_quit(icon, item):
        request_quit()

    def on_toggle(icon, item):
        request_toggle()

    menu = pystray.Menu(
        pystray.MenuItem("显示 / 隐藏", on_toggle, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    tray_icon = pystray.Icon("sysmon", make_icon_img(), "系统监控", menu)

    # ---- 刷新逻辑 ----
    tick = 0

    def refresh():
        nonlocal tick, net_last, net_last_t
        tick += 1

        # 手动隐藏时不显示，也不做任何事
        if state["manual_hidden"]:
            user32.ShowWindow(hwnd, SW_HIDE)
            root.after(REFRESH_MS, refresh)
            return

        # 采集
        cpu_pct = psutil.cpu_percent(None)
        vm = psutil.virtual_memory()

        net_now = psutil.net_io_counters()
        now = time.time()
        dt = now - net_last_t
        if dt > 0:
            state["net_down"], state["net_up"] = network_rates(
                (net_last.bytes_recv, net_last.bytes_sent),
                (net_now.bytes_recv, net_now.bytes_sent),
                dt,
            )
        net_last = net_now
        net_last_t = now

        g = gpu.read()

        if tick % CPU_TEMP_EVERY == 1:
            cpu_temp_reader.request()
        cpu_temp = cpu_temp_reader.get()

        # 浮窗模式：固定渲染高度
        target_h = 42

        fps = fps_reader.get() if fps_reader else None

        # 渲染双行紧凑彩色仪表板
        img, w, h = renderer.render(
            cpu_pct=cpu_pct,
            cpu_temp=cpu_temp,
            gpu_data=g,
            ram_pct=vm.percent,
            ram_used_bytes=vm.used,
            ram_total_bytes=vm.total,
            net_down=state["net_down"],
            net_up=state["net_up"],
            fps=fps,
            target_height=target_h,
        )
        photo = ImageTk.PhotoImage(img)
        label.config(image=photo)
        label.image = photo
        root.update_idletasks()

        # 浮窗定位：屏幕右下角
        try:
            sw = user32.GetSystemMetrics(0)  # 屏幕宽度
            sh = user32.GetSystemMetrics(1)  # 屏幕高度
            x = sw - w - 20  # 右下角，留 20px 边距
            y = sh - h - 80  # 任务栏上方约 80px
            user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h,
                                SWP_NOACTIVATE | SWP_SHOWWINDOW)
            root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        except Exception as e:
            log("positioning failed:", e)

        root.after(REFRESH_MS, refresh)

    # ---- 处理托盘命令 ----
    def poll_cmd():
        try:
            while True:
                c = cmd_q.get_nowait()
                if c == "quit":
                    tray_icon.stop()
                    root.destroy()
                    return
                elif c == "toggle":
                    state["manual_hidden"] = not state["manual_hidden"]
                    if state["manual_hidden"]:
                        user32.ShowWindow(hwnd, SW_HIDE)
        except queue.Empty:
            pass
        root.after(120, poll_cmd)

    # 启动
    try:
        fps_reader = FpsReader()
        cpu_temp_reader = CpuTempReader()
        tray_icon.run_detached()
        root.after(100, refresh)
        root.after(120, poll_cmd)
        root.mainloop()
    except Exception:
        log_exc("GUI")
    finally:
        close_quietly(getattr(tray_icon, "stop", None))
        close_quietly(getattr(fps_reader, "close", None))
        close_quietly(getattr(cpu_temp_reader, "close", None))


def main():
    # 尽早设置 DPI 感知，避免窗口尺寸被缩放
    # 用 SetProcessDPIAware（系统 DPI aware），比 per-monitor 更兼容 Tk
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    if "--selftest" in sys.argv:
        try:
            selftest()
        except Exception:
            log_exc("selftest")
            raise
        return

    instance = SingleInstance()
    if not instance.acquire():
        log("another SysMonitor instance is already running")
        return
    try:
        run_gui()
    except Exception:
        log_exc("main")
        raise
    finally:
        instance.release()


if __name__ == "__main__":
    main()
