import math
import ctypes
import sys
import os
import subprocess
import importlib.util

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    params = " ".join([f'"{arg}"' for arg in sys.argv])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit()

def find_pymobiledevice3_python():
    """
    在多個 Python 安裝中尋找已安裝 pymobiledevice3 的 Python 執行檔。
    若目前 sys.executable 已安裝，則直接回傳；否則掃描常見路徑。
    """
    # 1) 檢查當前 Python
    if _check_pymobiledevice3(sys.executable):
        return sys.executable

    # 2) 檢查 PATH 上的 "python" / "python3"
    for cmd in ("python", "python3"):
        try:
            result = subprocess.run(
                [cmd, "-c", "import pymobiledevice3; print(pymobiledevice3.__file__)"],
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            if result.returncode == 0 and result.stdout.strip():
                # 找到 python 的完整路徑
                which_result = subprocess.run(
                    ["where", cmd] if sys.platform == "win32" else ["which", cmd],
                    capture_output=True, text=True, timeout=3,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                )
                if which_result.returncode == 0:
                    for line in which_result.stdout.strip().splitlines():
                        line = line.strip()
                        if line and os.path.isfile(line) and _check_pymobiledevice3(line):
                            return line
        except Exception:
            continue

    # 3) 掃描常見 Python 安裝路徑 (Windows)
    if sys.platform == "win32":
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python"),
            os.path.expandvars(r"%PROGRAMFILES%\Python"),
            r"C:\Python",
        ]
        for base in candidates:
            if not os.path.isdir(base):
                continue
            try:
                for entry in sorted(os.listdir(base), reverse=True):
                    exe = os.path.join(base, entry, "python.exe")
                    if os.path.isfile(exe) and _check_pymobiledevice3(exe):
                        return exe
            except Exception:
                continue

    # 4) 最後 fallback 回 sys.executable
    return sys.executable

def _check_pymobiledevice3(python_exe):
    """檢查指定的 Python 執行檔是否安裝了 pymobiledevice3"""
    try:
        result = subprocess.run(
            [python_exe, "-c", "import pymobiledevice3"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        return result.returncode == 0
    except Exception:
        return False

EARTH_RADIUS_M = 6_371_000

def haversine(lat1, lng1, lat2, lng2):
    r = math.radians
    dlat = r(lat2 - lat1)
    dlng = r(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(dlng/2)**2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))

def interpolate(lat1, lng1, lat2, lng2, fraction):
    return lat1 + (lat2 - lat1) * fraction, lng1 + (lng2 - lng1) * fraction

def calc_next_position(cur_lat, cur_lng, end_lat, end_lng, speed_kmh, interval_sec):
    remaining = haversine(cur_lat, cur_lng, end_lat, end_lng)
    step_dist = (speed_kmh * 1000 / 3600) * interval_sec
    if step_dist >= remaining:
        return end_lat, end_lng, 0.0
    fraction = step_dist / remaining
    next_lat, next_lng = interpolate(cur_lat, cur_lng, end_lat, end_lng, fraction)
    return next_lat, next_lng, remaining - step_dist

def calculate_bearing(lat1, lon1, lat2, lon2):
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dLon = lon2_rad - lon1_rad
    y = math.sin(dLon) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dLon)
    initial_bearing = math.degrees(math.atan2(y, x))
    return (initial_bearing + 360) % 360