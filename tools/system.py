"""System status tool."""
import os
import shutil
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def system_report(tz_name: str = "UTC") -> str:
    """Time + server health, using only /proc and /sys (no extra dependencies)."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    now = datetime.now(tz)
    lines = [f"Time: {now:%A %Y-%m-%d %H:%M:%S %Z} (UTC {datetime.now(timezone.utc):%H:%M})"]

    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            lines.append(f"CPU temp: {int(f.read()) / 1000:.1f}°C")
    except OSError:
        pass
    try:
        l1, l5, l15 = os.getloadavg()
        lines.append(f"Load (1/5/15m): {l1:.2f} {l5:.2f} {l15:.2f} on {os.cpu_count()} cores")
    except OSError:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for row in f:
                k, v = row.split(":")
                mem[k] = int(v.split()[0]) // 1024
        lines.append(
            f"Memory: {mem['MemAvailable']} MB free of {mem['MemTotal']} MB"
            f" · swap used {mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)} MB"
        )
    except (OSError, KeyError, ValueError):
        pass
    try:
        du = shutil.disk_usage("/")
        lines.append(f"Disk /: {du.free // 2**30} GB free of {du.total // 2**30} GB")
    except OSError:
        pass
    try:
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        lines.append(f"Uptime: {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m")
    except OSError:
        pass
    return "\n".join(lines)