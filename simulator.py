import asyncio
import subprocess
import os
import re
import time
import json
import sys
import threading
import http.server
import socketserver
from utils import haversine, calc_next_position, calculate_bearing

class MapServer(http.server.BaseHTTPRequestHandler):
    app_core = None

    def log_message(self, format, *args): pass

    def do_GET(self):
        try:
            if self.path == '/current_pos':
                data = {'active': False, 'lat': 0, 'lng': 0}
                if self.app_core and (self.app_core.running or self.app_core.holding) and len(self.app_core.get_route_points_snapshot()) >= 2:
                    if self.app_core.current_lat is not None:
                        follow_state = self.app_core.gui.map_follow.get() if self.app_core.gui else False
                        data = {
                            'active': True, 'lat': self.app_core.current_lat, 'lng': self.app_core.current_lng,
                            'follow': follow_state, 'paused': self.app_core.paused,
                            'segment_index': getattr(self.app_core, '_current_segment_index', 1),
                            'dist_str': self.app_core.current_dist_str, 'eta_str': self.app_core.current_eta_str,
                            'bearing': self.app_core.current_bearing
                        }
                self._json(data)
            elif self.path == '/route_points':
                data = {'version': 0, 'points': [], 'center': None}
                if self.app_core:
                    points = self.app_core.get_route_points_snapshot()
                    center = None
                    if getattr(self.app_core, '_force_map_center_version', -1) == self.app_core.route_version and points:
                        center = {'lat': points[0][0], 'lng': points[0][1], 'zoom': 17}
                        self.app_core._force_map_center_version = -1
                    elif len(points) >= 2:
                        s_lat, s_lng = points[0]
                        e_lat, e_lng = points[-1]
                        if abs(s_lat - e_lat) < 0.0000001 and abs(s_lng - e_lng) < 0.0000001:
                            center = {'lat': s_lat, 'lng': s_lng, 'zoom': 17}
                    data = {'version': self.app_core.route_version, 'points': [{'lat': lat, 'lng': lng} for lat, lng in points], 'center': center}
                self._json(data)
            elif self.path == '/should_close':
                should_close = self.app_core.gui._should_close_browser if self.app_core.gui else False
                self._json({'close': should_close})
            elif self.path == '/bookmarks':
                self._json(self._read_bookmarks_json())
            elif self.path == '/bookmarked_routes':
                self._json(self._read_routes_json())
            elif self.path in ['/', '/map.html', '/index.html']:
                self._send_html()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception: pass

    def do_POST(self):
        try:
            if self.path == '/map_event':
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                if self.app_core and self.app_core.gui:
                    action = data.get('action')
                    self.app_core.gui.handle_map_event(action, data)
                self._json({'ok': True})
        except Exception: pass

    def _json(self, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        html_path = os.path.join(script_dir, "templates", "map.html")
        try:
            with open(html_path, 'r', encoding='utf-8') as f:
                html = f.read().replace('__PORT__', str(self.server.server_address[1])).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        except Exception as e:
            self.send_response(500)
            self.end_headers()

    @staticmethod
    def _read_bookmarks_json():
        script_dir = os.path.dirname(os.path.abspath(__file__))
        bm_file = os.path.join(script_dir, "bookmarks.txt")
        items = []
        if os.path.exists(bm_file):
            try:
                with open(bm_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line: continue
                        m = re.search(r'緯度:\s*([-\d.]+),\s*經度:\s*([-\d.]+)', line)
                        if m:
                            items.append({"line": line, "lat": float(m.group(1)), "lng": float(m.group(2))})
            except Exception: pass
        items.reverse()
        return {"items": items}

    @staticmethod
    def _read_routes_json():
        script_dir = os.path.dirname(os.path.abspath(__file__))
        routes_file = os.path.join(script_dir, "routes.json")
        routes = []
        if os.path.exists(routes_file):
            try:
                with open(routes_file, "r", encoding="utf-8") as f:
                    routes = json.load(f)
            except Exception: pass
        routes.reverse()
        return {"items": routes}

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class SimulatorCore:
    MAP_PORT = 18765

    def __init__(self):
        self.gui = None
        self.running = False
        self.holding = False
        self.paused = False
        self.route_points = []
        self.route_points_lock = threading.Lock()
        self.route_version = 0
        self.current_lat = None
        self.current_lng = None
        self.current_bearing = 0
        self.current_dist_str = ""
        self.current_eta_str = ""
        self._current_segment_index = 1
        self.map_url = ""
        self.tunneld_proc = None
        self._stop_task = False
        self._conn_status = "disconnected"
        self._perform_full_cleanup = False
        self.start_position_lat = None
        self.start_position_lng = None
        
        self._start_map_server()

    def set_gui(self, gui):
        self.gui = gui

    def log(self, msg, color=None):
        if self.gui: self.gui._log(msg, color)
        else: print(msg)

    def get_route_points_snapshot(self):
        with self.route_points_lock:
            return list(self.route_points)

    def update_route_points(self, points):
        with self.route_points_lock:
            if points != self.route_points:
                self.route_points = points
                self.route_version += 1
                self._force_map_center_version = self.route_version

    def _start_map_server(self):
        MapServer.app_core = self
        for port in range(self.MAP_PORT, self.MAP_PORT + 20):
            try:
                server = ThreadingHTTPServer(('127.0.0.1', port), MapServer)
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                self.MAP_PORT = port
                self.map_url = f"http://127.0.0.1:{port}/"
                print(f"地圖服務器啟動: {self.map_url}")
                return
            except OSError:
                continue

    def start_simulation(self, route_points, speed, ivl):
        self.start_position_lat = route_points[0][0]
        self.start_position_lng = route_points[0][1]
        self.running = True
        self.paused = False
        self._stop_task = False
        self._perform_full_cleanup = False
        threading.Thread(target=self._run_async_simulation, args=(route_points, speed, ivl), daemon=True).start()

    def _run_async_simulation(self, route_points, speed, ivl):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._simulate(route_points, speed, ivl))
        finally:
            loop.close()

    async def _simulate(self, route_points_param, speed, ivl):
        from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider

        route_points = self.get_route_points_snapshot()
        if len(route_points) < 2: route_points = route_points_param
        
        s_lat, s_lng = route_points[0]
        total_dist = sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(route_points, route_points[1:]))
        
        self.log(f"📏 總距離: {total_dist:.0f}m")
        self.log("🚀 開始模擬", color="blue")
        if self.gui:
            self.gui.root.after(0, lambda d=f"📏 距離: 0.00 / {total_dist/1000:.2f} km": self.gui.lbl_distance.config(text=d))
            
        init_speed_ms = speed * 1000 / 3600
        init_eta = int(total_dist / init_speed_ms) if init_speed_ms > 0 else 0
        ihrs, irem = divmod(init_eta, 3600)
        imins, isecs = divmod(irem, 60)
        if self.gui:
            self.gui.root.after(0, lambda e=f"⏳ 預估剩餘: {ihrs:02d}:{imins:02d}:{isecs:02d}": self.gui.lbl_eta.config(text=e))
            
        self.current_dist_str = f"0.00 / {total_dist/1000:.2f} km"
        self.current_eta_str = f"{ihrs:02d}:{imins:02d}:{isecs:02d}"

        cur_lat, cur_lng = s_lat, s_lng
        self.current_lat, self.current_lng = cur_lat, cur_lng
        step_count = 0
        segment_index = 1
        self._current_segment_index = segment_index
        dist_done = 0.0
        
        jump_idx = getattr(self.gui, '_jump_request_index', None) if self.gui else None
        if jump_idx is not None:
            idx = jump_idx
            self.gui._jump_request_index = None
            if 0 <= idx < len(route_points):
                cur_lat, cur_lng = route_points[idx]
                segment_index = min(idx + 1, len(route_points) - 1)
                dist_done = sum(haversine(route_points[i][0], route_points[i][1], route_points[i+1][0], route_points[i+1][1]) for i in range(idx))
                self.current_lat, self.current_lng = cur_lat, cur_lng
                self._current_segment_index = segment_index
                
        reconnect_attempts = 0
        finished = False

        def _remaining_total_from_current(points, target_index, lat, lng, done):
            if target_index >= len(points): return done
            remaining = haversine(lat, lng, points[target_index][0], points[target_index][1])
            remaining += sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points[target_index:], points[target_index + 1:]))
            return done + remaining

        try:
            while self.running:
                try:
                    if reconnect_attempts == 0 and self.gui and self.gui.rsd_host.get() and self.gui.rsd_port.get():
                        host = self.gui.rsd_host.get()
                        port = int(self.gui.rsd_port.get())
                    else:
                        host, port = await self._refresh_rsd_async("連線中斷或 port 可能已變更")

                    rsd = RemoteServiceDiscoveryService((host, port))
                    await rsd.connect()

                    async with DvtProvider(rsd) as dvt:
                        async with LocationSimulation(dvt) as location:
                            reconnect_attempts = 0
                            alert_triggered = False
                            
                            while self.running:
                                if not finished:
                                    await location.set(cur_lat, cur_lng)

                                    if total_dist == 0:
                                        if self.gui:
                                            self.gui.root.after(0, lambda: self.gui.progress.config(value=100))
                                            self.gui.root.after(0, lambda: self.gui.lbl_progress.config(text=f"📍 已鎖定: {cur_lat:.6f}, {cur_lng:.6f}"))
                                            self.gui.root.after(0, self.gui._pause_timer)
                                        for i in range(3):
                                            if not self.running: break
                                            await location.set(cur_lat + (i * 0.000001), cur_lng)
                                            await asyncio.sleep(0.5)
                                        finished = True

                                    while self.running and not finished:
                                        pause_timer = 0
                                        while self.paused and self.running:
                                            if self._stop_task:
                                                self.paused = False
                                                break
                                            if int(pause_timer * 10) % 100 == 0:  
                                                await location.set(cur_lat, cur_lng)  
                                            
                                            latest_points = self.get_route_points_snapshot()
                                            if latest_points != route_points and len(latest_points) >= 2:
                                                route_points = latest_points
                                                if segment_index >= len(route_points): segment_index = len(route_points) - 1
                                                self._current_segment_index = segment_index
                                                total_dist = _remaining_total_from_current(route_points, segment_index, cur_lat, cur_lng, dist_done)
                                                
                                                self.current_dist_str = f"{dist_done/1000:.2f} / {total_dist/1000:.2f} km"
                                                if self.gui:
                                                    self.gui.root.after(0, lambda d=f"📏 距離: {self.current_dist_str}": self.gui.lbl_distance.config(text=d))
                                                    try: cur_speed = float(self.gui.speed_kmh.get())
                                                    except ValueError: cur_speed = speed
                                                    speed_ms = cur_speed * 1000 / 3600
                                                    eta_sec = int(max(0, total_dist - dist_done) / speed_ms) if speed_ms > 0 else 0
                                                    e_hrs, e_rem = divmod(eta_sec, 3600)
                                                    e_mins, e_secs = divmod(e_rem, 60)
                                                    self.current_eta_str = f"{e_hrs:02d}:{e_mins:02d}:{e_secs:02d}"
                                                    self.gui.root.after(0, lambda e=f"⏳ 預估剩餘: {self.current_eta_str}": self.gui.lbl_eta.config(text=e))
                                                
                                            await asyncio.sleep(0.5)
                                            pause_timer += 0.5
                                            
                                        if not self.running: break
                                        if self._stop_task: break

                                        jump_idx = getattr(self.gui, '_jump_request_index', None) if self.gui else None
                                        if jump_idx is not None:
                                            idx = jump_idx
                                            self.gui._jump_request_index = None
                                            if 0 <= idx < len(route_points):
                                                cur_lat, cur_lng = route_points[idx]
                                                segment_index = min(idx + 1, len(route_points) - 1)
                                                self._current_segment_index = segment_index
                                                dist_done = sum(haversine(route_points[i][0], route_points[i][1], route_points[i+1][0], route_points[i+1][1]) for i in range(idx))
                                                total_dist = _remaining_total_from_current(route_points, segment_index, cur_lat, cur_lng, dist_done)
                                                await location.set(cur_lat, cur_lng)
                                                
                                                self.current_dist_str = f"{dist_done/1000:.2f} / {total_dist/1000:.2f} km"
                                                if self.gui: self.gui.root.after(0, lambda d=f"📏 距離: {self.current_dist_str}": self.gui.lbl_distance.config(text=d))
                                                continue

                                        latest_points = self.get_route_points_snapshot()
                                        if len(latest_points) < 2:
                                            self.running = False
                                            break
                                        if latest_points != route_points:
                                            if len(latest_points) == 2 and latest_points[0] == latest_points[1]:
                                                route_points = latest_points
                                                segment_index = 1
                                                self._current_segment_index = segment_index
                                                cur_lat, cur_lng = route_points[0]
                                                self.current_lat, self.current_lng = cur_lat, cur_lng
                                                dist_done = 0.0
                                                total_dist = 0
                                                finished = False 
                                                self.holding = False
                                                alert_triggered = False
                                                await location.set(cur_lat, cur_lng)
                                                continue
                                            else:
                                                route_points = latest_points
                                                if segment_index >= len(route_points): segment_index = len(route_points) - 1
                                                self._current_segment_index = segment_index
                                                total_dist = _remaining_total_from_current(route_points, segment_index, cur_lat, cur_lng, dist_done)

                                        try:
                                            cur_speed = float(self.gui.speed_kmh.get()) if self.gui else speed
                                            cur_ivl   = float(self.gui.interval.get()) if self.gui else ivl
                                            if cur_ivl <= 0 or cur_ivl > 60: cur_ivl = ivl
                                            if cur_speed <= 0 or cur_speed > 500: cur_speed = speed
                                        except ValueError:
                                            cur_speed = speed
                                            cur_ivl   = ivl

                                        target_lat, target_lng = route_points[segment_index]
                                        prev_lat, prev_lng = cur_lat, cur_lng
                                        next_lat, next_lng, remaining = calc_next_position(cur_lat, cur_lng, target_lat, target_lng, cur_speed, cur_ivl)
                                        
                                        if haversine(prev_lat, prev_lng, next_lat, next_lng) > 0.01:
                                            self.current_bearing = calculate_bearing(prev_lat, prev_lng, next_lat, next_lng)
                                            
                                        cur_lat, cur_lng = next_lat, next_lng
                                        self.current_lat, self.current_lng = cur_lat, cur_lng
                                        self._current_segment_index = segment_index
                                        step_count += 1

                                        await location.set(cur_lat, cur_lng)

                                        dist_done += haversine(prev_lat, prev_lng, cur_lat, cur_lng)
                                        pct = 100 if total_dist == 0 else min(100, int(dist_done / total_dist * 100))
                                        msg = f"[{step_count}] {cur_lat:.6f},{cur_lng:.6f} {pct}% {cur_speed}km/h ({segment_index}/{len(route_points)-1})"
                                        self.log(msg)
                                        if self.gui:
                                            self.gui.root.after(0, lambda p=pct: self.gui.progress.config(value=p))
                                            self.gui.root.after(0, lambda m=msg: self.gui.lbl_progress.config(text=m))
                                            self.current_dist_str = f"{dist_done/1000:.2f} / {total_dist/1000:.2f} km"
                                            self.gui.root.after(0, lambda d=f"📏 距離: {self.current_dist_str}": self.gui.lbl_distance.config(text=d))
                                            
                                            rem_dist = max(0, total_dist - dist_done)
                                            speed_ms = cur_speed * 1000 / 3600
                                            eta_sec = int(rem_dist / speed_ms) if speed_ms > 0 else 0
                                            e_hrs, e_rem = divmod(eta_sec, 3600)
                                            e_mins, e_secs = divmod(e_rem, 60)
                                            self.current_eta_str = f"{e_hrs:02d}:{e_mins:02d}:{e_secs:02d}"
                                            self.gui.root.after(0, lambda e=f"⏳ 預估剩餘: {self.current_eta_str}": self.gui.lbl_eta.config(text=e))

                                            try: alert_sec = int(self.gui.alert_seconds.get())
                                            except ValueError: alert_sec = 0
                                                
                                            if alert_sec > 0 and not alert_triggered and 0 < eta_sec <= alert_sec:
                                                alert_triggered = True
                                                self.gui._play_alert_sound()
                                                self.gui.root.after(0, lambda a=alert_sec: self.gui._show_alert_popup(a))

                                        if remaining == 0.0:
                                            if segment_index >= len(route_points) - 1:
                                                self.log("✅ 已抵達終點", color="green")
                                                finished = True
                                                if self.gui: self.gui.root.after(0, self.gui._pause_timer)
                                                break
                                            segment_index += 1
                                            self._current_segment_index = segment_index
                                            
                                        await asyncio.sleep(cur_ivl)

                                if self._stop_task:
                                    await location.set(self.start_position_lat, self.start_position_lng)
                                    self.current_lat = self.start_position_lat
                                    self.current_lng = self.start_position_lng
                                    self._stop_task = None
                                    self.running = False
                                    break

                                if finished and self.running:
                                    lat, lng = cur_lat, cur_lng
                                    self.holding = True
                                    hold_timer = 0
                                    
                                    while self.holding:
                                        await asyncio.sleep(0.5)
                                        hold_timer += 0.5
                                        if not self.holding: break
                                        
                                        jump_idx = getattr(self.gui, '_jump_request_index', None) if self.gui else None
                                        if jump_idx is not None:
                                            finished = False
                                            self.holding = False
                                            alert_triggered = False
                                            if self.gui: self.gui.root.after(0, self.gui._resume_timer)
                                            break
                                            
                                        latest_points = self.get_route_points_snapshot()
                                        if latest_points != route_points:
                                            if len(latest_points) >= 2:
                                                route_points = latest_points
                                                segment_index = 1
                                                self._current_segment_index = segment_index
                                                cur_lat, cur_lng = route_points[0]
                                                self.current_lat, self.current_lng = cur_lat, cur_lng
                                                dist_done = 0.0
                                                finished = False
                                                self.holding = False
                                                alert_triggered = False
                                                total_dist = sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(route_points, route_points[1:]))
                                                if self.gui: self.gui.root.after(0, self.gui._resume_timer)
                                                break
                                        
                                        if hold_timer >= 15:
                                            try: await location.set(lat, lng)
                                            except Exception: pass
                                            hold_timer = 0

                                    if not finished: continue

                except Exception as e:
                    if not self.running or self._stop_task: raise
                    reconnect_attempts += 1 
                    if self.gui and not self.gui.auto_reconnect.get(): raise Exception("auto_reconnect is disabled")
                    self._conn_status = "reconnecting"
                    await asyncio.sleep(min(15, 5 + reconnect_attempts * 3))
                    
        except Exception as e:
            self.log(f"❌ 模擬失敗: {e}")
        finally:
            self.running = False
            self.holding = False
            self.current_lat = None
            self.current_lng = None
            self.current_bearing = 0
            self.current_dist_str = ""
            self.current_eta_str = ""
            
            if getattr(self, '_perform_full_cleanup', False):
                if self.gui:
                    self.gui._unmount_ddi(sync=True)
                    self.gui._stop_tunneld_process()
                    is_auto = self.gui.auto_reconnect.get()
                    self.gui._reset_ui_elements(clear_coords=not is_auto)
                    if is_auto:
                        self.gui._auto_retry_count = 0
                        self.gui.root.after(0, lambda: self.gui.root.after(5000, self.gui._auto_connect))
                self._perform_full_cleanup = False
            else:
                if self.gui:
                    self.gui.root.after(0, lambda: self.gui.btn_start.config(state="normal" if self.gui.rsd_host.get() else "disabled"))
                    self.gui.root.after(0, lambda: self.gui.btn_pause.config(state="disabled", text="⏸ 暫停"))
                    self.gui.root.after(0, lambda: self.gui.btn_stop.config(state="disabled"))
                    self.gui.root.after(0, lambda: self.gui.lbl_progress.config(text="已停止"))
                    self.gui.root.after(0, lambda: self.gui.progress.config(value=0))

    async def _refresh_rsd_async(self, reason):
        # 由於涉及隧道邏輯，這部分若長度不足需依賴 gui 內的 _refresh_rsd_async 實作
        # 在完整版中應實作 PyMobileDevice3 的重連策略
        pass