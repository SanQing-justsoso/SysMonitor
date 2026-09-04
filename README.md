# SysMonitor

Windows 任务栏系统监控 —— 把 CPU / 内存 / 显存 / CPU 温度 / GPU 温度 / 网速 / FPS 直接贴在任务栏上。

类似 TrafficMonitor，但自研，支持 Tensor Core 游戏本（RTX 3080 Laptop 等），浅底深字，嵌入任务栏最左侧。

## 特性

- **任务栏嵌入** — 无边框、前置、半透明、点击穿透，贴在任务栏 `Shell_TrayWnd` 上
- **CPU 占用** — 通过 `psutil` 读取（无需管理员）
- **内存** — 已用 / 总量 + 百分比
- **GPU 温度 / 显存 / 利用率** — 通过 NVML（`pynvml`，无需管理员）
- **CPU 温度** — 通过 LibreHardwareMonitor 读取（需要管理员权限）
- **网速** — 上行 / 下行实时速率，短格式显示（`1.2K / 3.4M / 5.6G`）
- **FPS** — 通过 Intel PresentMon 抓取当前游戏帧率（可选）
- **托盘图标** — 右键菜单：显示/隐藏、退出
- **自动隐藏** — 任务栏隐藏时自动随之隐藏

## 系统要求

- Windows 10 / 11
- Python 3.9+（源码运行）或直接运行打包好的 `.exe`
- NVIDIA GPU（如需 GPU 温度/显存） + NVIDIA 驱动
- 管理员权限（如需 CPU 温度）

## 快速开始

### 源码运行

```bash
# 1. 安装 Python 依赖
pip install psutil pynvml pystray Pillow

# 2. 准备第三方二进制（见下方"第三方依赖"）
#    - 把 LibreHardwareMonitor 放到源码目录下的 LibreHardwareMonitor/
#    - 编译 lhm_dump.exe（见 lhm_dump/compile.bat）
#    - （可选）下载 PresentMon.exe 放到源码目录下

# 3. 运行
python taskbar_monitor.py
```

### 自检

```bash
python taskbar_monitor.py --selftest
```

会打印各传感器读数，确认数据采集正常。

### 打包为 exe

需要先准备好 `LibreHardwareMonitor/` 目录（含 `lhm_dump.exe`），然后：

```bash
pip install pyinstaller
pyinstaller SysMonitor.spec
```

### 开机自启

1. 右键 `install-autostart.bat` → 以管理员身份运行
2. 这会创建一个 `schtasks` 计划任务，在每次登录时以最高权限启动 SysMonitor

## 第三方依赖

| 组件 | 用途 | 来源 | 许可证 |
|------|------|------|--------|
| LibreHardwareMonitorLib | 读取 CPU 温度 | [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) | MPL 2.0 |
| PresentMon | 抓取 FPS（ETW） | [Intel PresentMon](https://github.com/GameTechDev/PresentMon) | MIT |

详细获取步骤见 [docs/第三方依赖说明.md](docs/第三方依赖说明.md)。

## 配置

### 显示参数

修改 `taskbar_monitor.py` 开头常量：

```python
REFRESH_MS = 1000      # 刷新间隔（毫秒）
CPU_TEMP_EVERY = 4     # CPU 温度每隔 N 个 tick 读一次
BG = "#e8e8f0"         # 背景色（浅色）
FG = "#16161c"         # 文字颜色（深色）
FONT = "Segoe UI"
FONT_SIZE = 9
LEFT_MARGIN = 8        # 贴片距任务栏左侧距离（px）
```

### 环境变量

源码运行时，可通过环境变量指定第三方工具路径：

- `LHM_DIR` — LibreHardwareMonitor 目录（含 `lhm_dump.exe`）
- `PRESENTMON_EXE` — PresentMon.exe 的完整路径

## 架构

```
taskbar_monitor.py      主程序（Python + tkinter）
├── psutil              CPU 占用、内存、网速
├── pynvml (NVML)       GPU 温度、显存、利用率
├── lhm_dump.exe        CPU 温度（LibreHardwareMonitorLib，C# 小程序）
└── PresentMon.exe      FPS (Intel PresentMon, ETW)
```

## 许可证

MIT License — 详见 [LICENSE](LICENSE)