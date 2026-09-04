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
import collections
import threading
import queue
import ctypes
import subprocess
import traceback
from ctypes import wintypes

import psutil

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

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if self.header is None:
                    self.header = parts
                    self.app_idx = parts.index("Application") if "Application" in parts else 0
                    continue
                if len(parts) <= self.app_idx:
                    continue
                app = parts[self.app_idx]
                now = time.time()
                with self.lock:
                    dq = self.frames[app]
                    dq.append(now)
                    while dq and now - dq[0] > 1.0:
                        dq.popleft()
        except Exception:
            pass

    def get(self):
        """返回主进程最近 1 秒的 FPS；没有候选进程时返回 None。"""
        now = time.time()
        with self.lock:
            best = 0
            for app, dq in self.frames.items():
                while dq and now - dq[0] > 1.0:
                    dq.popleft()
                if app in self.BLOCKLIST:
                    continue
                n = len(dq)
                if n > best:
                    best = n
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
    print("Net down        : %s" % fmt_speed((net2.bytes_recv - net1.bytes_recv) / dt))
    print("Net up          : %s" % fmt_speed((net2.bytes_sent - net1.bytes_sent) / dt))


# ---------------------------------------------------------------------------
# win32 辅助：定位任务栏、设置窗口样式
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 图形界面（贴任务栏）
# ---------------------------------------------------------------------------
def run_gui():
    import tkinter as tk
    import pystray
    from PIL import Image, ImageDraw

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    log("startup: frozen=%s LHM_EXE=%s exists=%s PM_EXE=%s exists=%s" % (
        FROZEN, LHM_EXE, os.path.exists(LHM_EXE), PM_EXE, os.path.exists(PM_EXE)))

    gpu = GpuReader()
    fps_reader = FpsReader()
    psutil.cpu_percent(None)

    net_last = psutil.net_io_counters()
    net_last_t = time.time()

    state = {
        "cpu_temp": None,
        "net_down": 0.0,
        "net_up": 0.0,
        "manual_hidden": False,
    }

    cmd_q = queue.Queue()

    # ---- 贴片窗口 ----
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.88)
    root.configure(bg=BG)

    label = tk.Label(root, text="", font=(FONT, FONT_SIZE), fg=FG, bg=BG, anchor="center")
    label.pack(fill="both", expand=True)

    root.update_idletasks()
    hwnd = get_toplevel_hwnd(root.winfo_id())
    apply_clickthrough(hwnd)

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
            state["net_down"] = (net_now.bytes_recv - net_last.bytes_recv) / dt
            state["net_up"] = (net_now.bytes_sent - net_last.bytes_sent) / dt
        net_last = net_now
        net_last_t = now

        g = gpu.read()

        if tick % CPU_TEMP_EVERY == 1 or state["cpu_temp"] is None:
            state["cpu_temp"] = read_cpu_temp()

        # 组装文本：CPU → GPU → RAM → 网速
        cpu_txt = "--" if state["cpu_temp"] is None else "%.0f°" % state["cpu_temp"]
        parts = [
            "CPU %2.0f%% %s" % (cpu_pct, cpu_txt),
        ]
        if g:
            parts.append("GPU %2d%% %s/%s %d°" % (
                g["util"], fmt_gb(g["vram_used"]), fmt_gb(g["vram_total"]), g["temp"]))
        parts.append("RAM %2.0f%% %s/%s" % (vm.percent, fmt_gb(vm.used), fmt_gb(vm.total)))
        parts.append("↓%s ↑%s" % (fmt_rate_short(state["net_down"]), fmt_rate_short(state["net_up"])))
        text = "   ".join(parts)

        label.config(text=text)
        root.update_idletasks()

        # 定位到任务栏
        try:
            rects = get_taskbar_rects()
            if rects is None:
                user32.ShowWindow(hwnd, SW_HIDE)
            else:
                tbr, nr = rects
                if not taskbar_visible(tbr):
                    user32.ShowWindow(hwnd, SW_HIDE)
                else:
                    w = label.winfo_reqwidth() + 2 * HPAD
                    h = max(16, (tbr.bottom - tbr.top) - 2 * VPAD)
                    x = tbr.left + LEFT_MARGIN
                    y = tbr.top + VPAD
                    user32.SetWindowPos(hwnd, HWND_TOPMOST, x, y, w, h,
                                        SWP_NOACTIVATE | SWP_SHOWWINDOW)
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
    tray_icon.run_detached()
    root.after(100, refresh)
    root.after(120, poll_cmd)
    try:
        root.mainloop()
    except Exception:
        log_exc("GUI")
    finally:
        try:
            tray_icon.stop()
        except Exception:
            pass
        try:
            fps_reader.close()
        except Exception:
            pass


def main():
    if "--selftest" in sys.argv:
        try:
            selftest()
        except Exception:
            log_exc("selftest")
            raise
        return
    try:
        run_gui()
    except Exception:
        log_exc("main")
        raise


if __name__ == "__main__":
    main()
