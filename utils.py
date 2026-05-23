import math
import ctypes
import sys

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_as_admin():
    params = " ".join([f'"{arg}"' for arg in sys.argv])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit()

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