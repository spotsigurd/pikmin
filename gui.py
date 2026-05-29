import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, simpledialog, filedialog
import os
import re
import time
import json
import webbrowser
import subprocess
import sys
import threading

class SimulatorGUI:
    def __init__(self, root, core):
        self.root = root
        self.core = core
        
        # ── 變數綁定 ──
        self.rsd_host  = tk.StringVar()
        self.rsd_port  = tk.StringVar()
        self.connection_type = tk.StringVar(value="usb")
        self.start_lat = tk.StringVar(value="")
        self.start_lng = tk.StringVar(value="")
        self.end_lat   = tk.StringVar(value="")
        self.end_lng   = tk.StringVar(value="")
        self.speed_kmh = tk.StringVar(value="20")
        self.interval  = tk.StringVar(value="3")
        self.alert_seconds = tk.StringVar(value="0")
        self.auto_reconnect = tk.BooleanVar(value=True)
        self.map_follow = tk.BooleanVar(value=True)

        self._updating_from_map = False
        self._should_close_browser = False
        self._jump_request_index = None
        self._led_state = False
        self._timer_running = False
        self._sim_accumulated_time = 0
        self._sim_last_update_time = 0
        self._timer_after_id = None
        
        # 狀態控制變數
        self.tunneld_proc = None
        self._tunneld_generation = 0
        self._browser_proc = None
        self._is_auto_connecting = False
        self._auto_retry_count = 0
        self.saved_routes = []
        
        # 日誌檔案初始化
        self.log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "simulator.log")
        try:
            existing_lines = []
            if os.path.exists(self.log_file_path):
                with open(self.log_file_path, 'r', encoding='utf-8') as f:
                    existing_lines = f.readlines()
            header_prefix = "=== 模擬器啟動時間:"
            header_indices = [i for i, line in enumerate(existing_lines) if line.startswith(header_prefix)]
            if len(header_indices) >= 3:
                existing_lines = existing_lines[header_indices[-2]:]
            with open(self.log_file_path, 'w', encoding='utf-8') as f:
                f.writelines(existing_lines)
                f.write(f"{header_prefix} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception: pass

        # ── 初始化 UI ──
        self._build_ui()
        self._load_settings()

        # ── 核心防護機制：設定座標變更的 Trace ──
        self.start_lat.trace_add('write', self._on_coord_change)
        self.start_lng.trace_add('write', self._on_coord_change)
        self.end_lat.trace_add('write', self._on_coord_change)
        self.end_lng.trace_add('write', self._on_coord_change)

        self.root.after(2000, self._open_map_auto)
        self.root.after(500, self._auto_connect)
        self.root.after(800, self._update_led)

        self._idle_anim_idx = 0
        self._idle_frames = [
            "🚶‍♀️ 準備出發 .  ", "🚶‍♀️ 準備出發 .. ", "🏃‍♀️ 準備出發 ...", "🏃‍♀️ 準備出發 .. "
        ]
        self._update_idle_animation()

        self._log("=" * 60)
        self._log("🎯 iPhone GPS 模擬器 v17.0 - 模組化重構版 (完整版)")
        self._log("=" * 60)
        self._log("✅ 修復：阻絕競態條件造成的橡皮筋拉回現象")
        self._log("💡 請按 F12 打開瀏覽器控制台查看詳細日誌")

    # ==========================================
    # UI 佈局與主題
    # ==========================================
    def _apply_cute_theme(self):
        style = ttk.Style()
        if 'clam' in style.theme_names():
            style.theme_use('clam')
            
        BG = "#FFF0F5"       
        FG = "#5D4037"       
        ACCENT = "#FF6B81"   
        BTN_BG = "#FFD1DC"   
        ENTRY_BG = "#FFFFFF" 
        
        style.configure(".", background=BG, foreground=FG, font=("Microsoft JhengHei", 10))
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("TLabelframe", background=BG, bordercolor=FG)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=("Microsoft JhengHei", 11, "bold"))
        
        style.configure("TButton", background=BTN_BG, foreground=FG, bordercolor=FG, relief="flat")
        style.map("TButton", background=[("active", "#FFB6C1"), ("disabled", "#F5F5F5")], 
                             foreground=[("active", FG), ("disabled", "#A9A9A9")],
                             bordercolor=[("active", ACCENT), ("disabled", "#DDDDDD")])
        style.configure("TEntry", fieldbackground=ENTRY_BG, foreground=FG, bordercolor=FG, insertcolor=FG)
        style.configure("TCombobox", fieldbackground=ENTRY_BG, background=BTN_BG, foreground=FG, arrowcolor=FG)
        style.map("TCombobox", fieldbackground=[("readonly", ENTRY_BG)])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BTN_BG, foreground=FG, padding=[10, 5], bordercolor=FG)
        style.map("TNotebook.Tab", background=[("selected", FG)], foreground=[("selected", BG)])
        style.configure("TCheckbutton", background=BG, foreground=FG, indicatorcolor=ENTRY_BG)
        style.map("TCheckbutton", indicatorcolor=[("selected", ACCENT)])
        style.configure("Horizontal.TProgressbar", background=FG, troughcolor=ENTRY_BG, bordercolor=FG)

    def _validate_numeric(self, P):
        if P == "": return True
        return re.match(r'^-?\d*\.?\d*$', P) is not None

    def _validate_port(self, P):
        if P == "": return True
        return P.isdigit() and len(P) <= 5

    def _show_confirm_dialog(self, title, message, detail="", parent=None, confirm_text="確定刪除"):
        parent = parent or self.root
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.transient(parent)
        dialog.grab_set()
        dialog.configure(bg="#FFF0F5")
        dialog.resizable(False, False)

        result = [False]

        container = ttk.Frame(dialog, padding=18)
        container.pack(fill='both', expand=True)

        ttk.Label(container, text="🗑️", font=('Arial', 24)).pack(pady=(0, 6))
        ttk.Label(container, text=message, font=('Microsoft JhengHei', 12, 'bold'),
                  foreground="#5D4037", justify='center').pack()
        if detail:
            detail_label = ttk.Label(container, text=detail, justify='center', wraplength=360,
                                     foreground="#7A5C58")
            detail_label.pack(fill='x', pady=(10, 0))

        btn_frame = ttk.Frame(container)
        btn_frame.pack(fill='x', pady=(18, 0))

        def close(value):
            result[0] = value
            dialog.destroy()

        ttk.Button(btn_frame, text="取消", command=lambda: close(False)).pack(side='left', expand=True, fill='x', padx=(0, 6))
        ttk.Button(btn_frame, text=confirm_text, command=lambda: close(True)).pack(side='left', expand=True, fill='x', padx=(6, 0))

        dialog.bind('<Escape>', lambda _event: close(False))
        dialog.bind('<Return>', lambda _event: close(True))
        dialog.protocol("WM_DELETE_WINDOW", lambda: close(False))

        dialog.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - dialog.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dialog.focus_set()
        self.root.wait_window(dialog)
        return result[0]

    def _build_ui(self):
        self.root.configure(bg="#FFF0F5")
        self._apply_cute_theme()
        
        PAD = dict(padx=8, pady=4)
        vcmd_num = (self.root.register(self._validate_numeric), '%P')
        vcmd_port = (self.root.register(self._validate_port), '%P')

        tabs = ttk.Notebook(self.root)
        tabs.pack(fill='both', expand=True, padx=8, pady=8)

        controls_pane = ttk.Frame(tabs)
        log_pane = ttk.Frame(tabs)
        tabs.add(controls_pane, text="🏠 主畫面")
        tabs.add(log_pane, text="📝 執行日誌")

        # --- 標題與地圖按鈕 ---
        title_frame = ttk.Frame(controls_pane)
        title_frame.pack(fill='x', pady=(0, 10))
        ttk.Label(title_frame, text="🚀 iPhone GPS 模擬器", font=('Arial', 16, 'bold')).pack(side='left')
        
        map_btn_frame = ttk.Frame(title_frame)
        map_btn_frame.pack(side='right')
        
        self.url_entry = ttk.Entry(map_btn_frame, width=24)
        self.url_entry.pack(side='left', padx=5)
        self.url_entry.insert(0, getattr(self.core, 'map_url', "http://127.0.0.1:18765/"))
        self.url_entry.configure(state='readonly')

        ttk.Button(map_btn_frame, text="🗺️ 開啟地圖", command=self._open_map_manual).pack(side='left', padx=5)
        ttk.Button(map_btn_frame, text="📋 複製網址", command=self._copy_map_url).pack(side='left')

        # --- 滾動區域 ---
        controls_canvas = tk.Canvas(controls_pane, highlightthickness=0, bg="#FFF0F5")
        controls_scrollbar = ttk.Scrollbar(controls_pane, orient='vertical', command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=controls_scrollbar.set)
        controls_canvas.pack(side='left', fill='both', expand=True)
        controls_scrollbar.pack(side='right', fill='y')

        main_frame = ttk.Frame(controls_canvas)
        controls_window = controls_canvas.create_window((0, 0), window=main_frame, anchor='nw')

        def _resize_controls(event):
            controls_canvas.itemconfigure(controls_window, width=event.width)
        def _update_scrollregion(_event=None):
            controls_canvas.configure(scrollregion=controls_canvas.bbox('all'))
        def _on_mousewheel(event):
            controls_canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')

        controls_canvas.bind('<Configure>', _resize_controls)
        main_frame.bind('<Configure>', _update_scrollregion)
        controls_canvas.bind('<Enter>', lambda _event: controls_canvas.bind_all('<MouseWheel>', _on_mousewheel))
        controls_canvas.bind('<Leave>', lambda _event: controls_canvas.unbind_all('<MouseWheel>'))

        # --- 快捷鍵綁定 ---
        self.root.bind('<Control-b>', lambda _event: self._bookmark_location())
        self.root.bind('<Control-r>', lambda _event: self._bookmark_route())
        self.root.bind('<space>', self._on_spacebar)

        # --- 區塊 1：連線設定 ---
        f_auto = ttk.LabelFrame(main_frame, text="1. 連線設定", padding=6)
        f_auto.pack(fill='x', pady=(0, 3))
        
        row1 = ttk.Frame(f_auto)
        row1.pack(fill='x', pady=(0, 5))
        ttk.Button(row1, text="⚡ 一鍵自動連線 (Tunneld ➜ Mount ➜ RSD)", command=self._auto_connect).pack(side='left', padx=5)
        
        self.led_canvas = tk.Canvas(row1, width=12, height=12, bg="#FFF0F5", highlightthickness=0)
        self.led_canvas.pack(side='left', padx=(5, 2))
        self.led_item = self.led_canvas.create_oval(1, 1, 11, 11, fill="#D3D3D3", outline="#D3D3D3")
        self.lbl_led_status = ttk.Label(row1, text="未連線", foreground="#555555", font=('Microsoft JhengHei', 9))
        self.lbl_led_status.pack(side='left')

        self.lbl_mount = ttk.Label(row1, text="尚未執行", foreground="#555555", font=('Microsoft JhengHei', 9))
        self.lbl_mount.pack(side='right', padx=(0, 5))
        ttk.Label(row1, text="Mount:", font=('Microsoft JhengHei', 9)).pack(side='right')
        
        self.lbl_tunneld = ttk.Label(row1, text="尚未啟動", foreground="#555555", font=('Microsoft JhengHei', 9))
        self.lbl_tunneld.pack(side='right', padx=(0, 10))
        ttk.Label(row1, text="Tunneld:", font=('Microsoft JhengHei', 9)).pack(side='right')

        row2 = ttk.Frame(f_auto)
        row2.pack(fill='x')
        ttk.Label(row2, text="Mode:").pack(side='left', padx=(5,2))
        cmb_connection_type = ttk.Combobox(row2, textvariable=self.connection_type, values=("usb", "wifi"), width=6, state="readonly")
        cmb_connection_type.pack(side='left', padx=(0, 5))
        ttk.Label(row2, text="RSD Host:").pack(side='left', padx=(5,2))
        ent_rsd_host = ttk.Entry(row2, textvariable=self.rsd_host, width=22)
        ent_rsd_host.pack(side='left', padx=2)
        ttk.Label(row2, text="Port:").pack(side='left', padx=2)
        ent_rsd_port = ttk.Entry(row2, textvariable=self.rsd_port, width=6, validate="key", validatecommand=vcmd_port)
        ent_rsd_port.pack(side='left', padx=2)

        # --- 區塊 2：設定路徑與速度 ---
        f4 = ttk.LabelFrame(main_frame, text="2. 設定路徑與速度", padding=6)
        f4.pack(fill='x', pady=3)
        
        coord_frame = ttk.Frame(f4)
        coord_frame.pack(fill='x')
        
        start_frame = ttk.LabelFrame(coord_frame, text="起點", padding=5)
        start_frame.pack(side='left', fill='both', expand=True, padx=(5, 2))
        ttk.Label(start_frame, text="緯度:").grid(row=0, column=0, sticky='e', padx=3)
        ent_start_lat = ttk.Entry(start_frame, textvariable=self.start_lat, width=12, validate="key", validatecommand=vcmd_num)
        ent_start_lat.grid(row=0, column=1, padx=3)
        ttk.Label(start_frame, text="經度:").grid(row=1, column=0, sticky='e', padx=3)
        ent_start_lng = ttk.Entry(start_frame, textvariable=self.start_lng, width=12, validate="key", validatecommand=vcmd_num)
        ent_start_lng.grid(row=1, column=1, padx=3)
        
        copy_frame = ttk.Frame(coord_frame)
        copy_frame.pack(side='left', padx=2)
        ttk.Button(copy_frame, text="複製並鎖定 ➡️", command=self._copy_start_to_end).pack()

        end_frame = ttk.LabelFrame(coord_frame, text="終點", padding=5)
        end_frame.pack(side='left', fill='both', expand=True, padx=(2, 5))
        ttk.Label(end_frame, text="緯度:").grid(row=0, column=0, sticky='e', padx=3)
        ent_end_lat = ttk.Entry(end_frame, textvariable=self.end_lat, width=12, validate="key", validatecommand=vcmd_num)
        ent_end_lat.grid(row=0, column=1, padx=3)
        ttk.Label(end_frame, text="經度:").grid(row=1, column=0, sticky='e', padx=3)
        ent_end_lng = ttk.Entry(end_frame, textvariable=self.end_lng, width=12, validate="key", validatecommand=vcmd_num)
        ent_end_lng.grid(row=1, column=1, padx=3)
        
        for ent in (ent_start_lat, ent_start_lng):
            ent.bind('<<Paste>>', lambda e: self._handle_coord_paste(e, is_start=True))
            ent.bind('<Control-v>', lambda e: self._handle_coord_paste(e, is_start=True))
        for ent in (ent_end_lat, ent_end_lng):
            ent.bind('<<Paste>>', lambda e: self._handle_coord_paste(e, is_start=False))
            ent.bind('<Control-v>', lambda e: self._handle_coord_paste(e, is_start=False))
        
        speed_frame = ttk.Frame(f4)
        speed_frame.pack(fill='x', pady=(5,0))
        ttk.Label(speed_frame, text="速度(km/h):").pack(side='left', **PAD)
        ttk.Entry(speed_frame, textvariable=self.speed_kmh, width=8, validate="key", validatecommand=vcmd_num).pack(side='left', **PAD)
        ttk.Label(speed_frame, text="間隔(秒):").pack(side='left', **PAD)
        ttk.Entry(speed_frame, textvariable=self.interval, width=8, validate="key", validatecommand=vcmd_num).pack(side='left', **PAD)
        ttk.Label(speed_frame, text="到達提醒(秒):").pack(side='left', **PAD)
        ttk.Entry(speed_frame, textvariable=self.alert_seconds, width=6, validate="key", validatecommand=vcmd_num).pack(side='left', **PAD)

        # 書籤與路徑管理
        bookmark_frame = ttk.Frame(f4)
        bookmark_frame.pack(fill='x', pady=(3,0))
        
        loc_frame = ttk.Frame(bookmark_frame)
        loc_frame.pack(fill='x', pady=(0,2))
        ttk.Button(loc_frame, text="🔖 收藏地點(Ctrl+B)", command=self._bookmark_location).pack(side='left', padx=5)
        self.bookmark_var = tk.StringVar()
        self.bookmark_combo = ttk.Combobox(loc_frame, textvariable=self.bookmark_var, width=55, state="readonly")
        self.bookmark_combo.set("📂 我的地點")
        self.bookmark_combo.pack(side='left', fill='x', expand=True, padx=(0, 5))
        self.bookmark_combo.bind("<<ComboboxSelected>>", self._on_bookmark_selected)

        route_bm_frame = ttk.Frame(bookmark_frame)
        route_bm_frame.pack(fill='x', pady=(2,0))
        ttk.Button(route_bm_frame, text="🛣️ 收藏路徑(Ctrl+R)", command=self._bookmark_route).pack(side='left', padx=5)
        ttk.Button(route_bm_frame, text="📂 預覽與管理路徑", command=self._open_route_manager).pack(side='left', padx=5)
        ttk.Button(route_bm_frame, text="📥 匯入(KML/GPX)", command=self._import_route_file).pack(side='left', padx=5)

        # --- 區塊 3：控制與進度 ---
        f5 = ttk.LabelFrame(main_frame, text="3. 控制與進度", padding=6)
        f5.pack(fill='x', pady=3)
        
        ctrl_frame = ttk.Frame(f5)
        ctrl_frame.pack(fill='x')
        
        self.btn_start = ttk.Button(ctrl_frame, text="🚀 開始模擬", command=self._start_simulation, state="disabled")
        self.btn_start.pack(side='left', padx=5)
        self.btn_pause = ttk.Button(ctrl_frame, text="⏸ 暫停", command=self._pause_simulation, state="disabled")
        self.btn_pause.pack(side='left', padx=5)
        self.btn_stop = ttk.Button(ctrl_frame, text="⏹ 停止並回起點", command=self._stop_simulation, state="disabled")
        self.btn_stop.pack(side='left', padx=5)

        ttk.Checkbutton(ctrl_frame, text="停止後自動重連", variable=self.auto_reconnect, onvalue=True, offvalue=False).pack(side='left', padx=5)
        
        progress_frame = ttk.Frame(f5)
        progress_frame.pack(fill='x', pady=5)
        self.progress = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress.pack(fill='x', pady=2)
        
        status_frame = ttk.Frame(progress_frame)
        status_frame.pack(fill='x')
        self.lbl_progress = ttk.Label(status_frame, text="🚶‍♀️ 準備出發 .  ", font=('Arial', 9))
        self.lbl_progress.pack(side='left')
        self.lbl_timer = ttk.Label(status_frame, text="⏱️ 行走時間: 00:00:00", font=('Arial', 9), foreground="#4682B4")
        self.lbl_timer.pack(side='right')
        self.lbl_distance = ttk.Label(status_frame, text="📏 距離: 0.00 / 0.00 km", font=('Arial', 9), foreground="#D1495B")
        self.lbl_distance.pack(side='right', padx=(0, 15))
        self.lbl_eta = ttk.Label(status_frame, text="⏳ 預估剩餘: --:--:--", font=('Arial', 9), foreground="#FF8C00")
        self.lbl_eta.pack(side='right', padx=(0, 15))

        # --- 日誌區塊 ---
        log_frame = ttk.Frame(log_pane, padding=10)
        log_frame.pack(fill='both', expand=True)
        
        log_btn_frame = ttk.Frame(log_frame)
        log_btn_frame.pack(fill='x', pady=(0, 5))
        ttk.Button(log_btn_frame, text="📝 開啟日誌檔", command=self._open_log_file).pack(side='right')
        ttk.Button(log_btn_frame, text="🗑️ 清空日誌", command=self._clear_log).pack(side='right', padx=(0, 5))

        self.log = scrolledtext.ScrolledText(log_frame, state="disabled", font=("Consolas", 9), bg='#FFFFFF', fg='#5D4037', insertbackground='#5D4037')
        self.log.pack(fill='both', expand=True)
        self.log.tag_config("green", foreground="#2E8B57")
        self.log.tag_config("red", foreground="#D1495B")
        self.log.tag_config("orange", foreground="#FF8C00")
        self.log.tag_config("blue", foreground="#4682B4")

        self._load_bookmarks()
        self._load_routes()

    # ==========================================
    # 地圖視窗管理
    # ==========================================
    def _launch_map_app_mode(self):
        if sys.platform == 'win32':
            browsers = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
            ]
            for b in browsers:
                if os.path.exists(b):
                    try:
                        self._browser_proc = subprocess.Popen(
                            [b, f"--app={self.core.map_url}"],
                            creationflags=subprocess.CREATE_NO_WINDOW
                        )
                        return True
                    except: pass
        return False

    def _maximize_map_window(self):
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "GPS Map")
            if hwnd:
                user32.ShowWindow(hwnd, 3)
                self._log("🗖 地圖視窗已最大化")
            else:
                self.root.after(500, self._maximize_map_window)
        except Exception as e:
            self._log(f"⚠ 最大化地圖視窗失敗: {e}")

    def _open_map_auto(self):
        if self.core.map_url:
            try:
                if not self._launch_map_app_mode():
                    webbrowser.open(self.core.map_url)
                self._log(f"🌐 地圖已自動開啟")
                self.root.after(800, self._maximize_map_window)
                self.root.after(500, self.root.iconify)
            except Exception as e:
                self._log(f"⚠ 自動開啟失敗: {e}")

    def _open_map_manual(self):
        if self.core.map_url:
            try:
                if not self._launch_map_app_mode():
                    webbrowser.open(self.core.map_url)
                self._log(f"🌐 地圖已開啟: {self.core.map_url}")
            except Exception as e:
                self._log(f"❌ 開啟失敗: {e}")

    def _copy_map_url(self):
        if self.core.map_url:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.core.map_url)
            self.root.update()
            self._log(f"✅ 已複製: {self.core.map_url}")

    # ==========================================
    # 自動連線與底層控制
    # ==========================================
    def _clear_rsd(self):
        def _apply():
            self.rsd_host.set("")
            self.rsd_port.set("")
            self.btn_start.config(state="disabled")
        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self.root.after(0, _apply)
        self.core._conn_status = "disconnected"

    def _auto_connect(self, force_restart=True):
        self._is_auto_connecting = True
        self._clear_rsd()
        self._log("⚡ 開始自動連線流程 (等待 tunneld 就緒...)", color="blue")
        self._start_tunneld(force_restart=force_restart)
        self._wait_for_tunneld_ready()

    def _wait_for_tunneld_ready(self, wait_time=0):
        if not getattr(self, '_is_auto_connecting', False): return
        if self.rsd_host.get() and self.rsd_port.get():
            self._log("⚡ tunneld 已就緒，準備進行下一步...", color="blue")
            self._auto_mount()
        elif wait_time > 30000:
            self._log("❌ 等待 tunneld 就緒逾時，嘗試重連...", color="red")
            # 若 tunneld 仍在執行，直接進入掛載+偵測流程（wifi 模式需透過 start-tunnel 取得 RSD）
            if self.tunneld_proc and self.tunneld_proc.poll() is None:
                self._log("🔍 tunneld 仍在執行，嘗試透過 start-tunnel 偵測 RSD...", color="blue")
                self._auto_mount()
            else:
                self._handle_auto_connect_retry()
        else:
            self.root.after(500, lambda: self._wait_for_tunneld_ready(wait_time + 500))

    def _auto_mount(self):
        connection_type = self.connection_type.get().strip().lower()
        if connection_type == "wifi":
            self._log("ℹ WiFi 模式略過 DDI 掛載，直接偵測 RSD", color="blue")
            self._detect_rsd()
            return
        self._run_mount(on_complete=self._detect_rsd)
        
    def _handle_auto_connect_retry(self):
        self._auto_retry_count += 1
        if self._auto_retry_count <= 3:
            self._log(f"⏳ 自動連線失敗，5 秒後進行第 {self._auto_retry_count}/3 次重試...", color="orange")
            should_force_restart = getattr(self, '_last_tunnel_device_not_connected', False)
            self.root.after(5000, lambda: self._auto_connect(force_restart=should_force_restart))
        else:
            self._log("❌ 自動重連失敗已達 3 次上限，放棄重試。請檢查設備連線狀態。", color="red")
            self._is_auto_connecting = False
            self._auto_retry_count = 0

    def _finalize_auto_connect(self):
        if getattr(self, '_is_auto_connecting', False):
            self._is_auto_connecting = False
            self._auto_retry_count = 0
            self._log("✅ 自動連線與偵測全部完成！", color="green")

    def _kill_old_tunneld(self):
        try:
            result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            pids = set()
            for line in result.stdout.splitlines():
                if ":49151" in line:
                    parts = line.split()
                    if parts:
                        pid = parts[-1]
                        if pid.isdigit() and pid != "0": pids.add(pid)
            if pids:
                for pid in pids:
                    subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                self._log(f"🧹 已清除舊程序 (PID: {', '.join(pids)})", color="orange")
                time.sleep(1)
        except Exception as e:
            self._log(f"⚠ 清除舊程序: {e}")

    def _start_tunneld(self, force_restart=False):
        if force_restart:
            self._stop_tunneld_process()
        if self.tunneld_proc and self.tunneld_proc.poll() is None:
            self._log("⚠ tunneld 已在執行")
            return 
        self._log("🚀 啟動 tunneld...", color="blue")
        self.lbl_tunneld.config(text="啟動中...", foreground="orange")
        self._kill_old_tunneld()
        self._tunneld_generation += 1
        generation = self._tunneld_generation

        def _read_output():
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pymobiledevice3", "remote", "tunneld"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=subprocess.CREATE_NO_WINDOW
                )
                self.tunneld_proc = proc
                self.root.after(0, lambda: self.lbl_tunneld.config(text="✅ 執行中", foreground="green"))
                self._log("✅ tunneld 已啟動", color="green")
                for line in proc.stdout:
                    if generation != self._tunneld_generation:
                        break
                    line = line.strip()
                    if not line: continue
                    line_lower = line.lower()
                    if any(k in line_lower for k in ["rsd", "address", "tunnel", "error"]) or re.search(r'\bport\b', line_lower):
                        self._log(f"[tunneld] {line}")
                    # 跳過斷線通知，避免把舊的 RSD 地址誤判為有效連線
                    if 'disconnect' in line_lower:
                        continue
                    m = re.search(r'\b((?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4})\s+(\d{4,5})\b', line)
                    if not m:
                        m2h = re.search(r'address[:\s]+([\da-fA-F:]+)', line)
                        m2p = re.search(r'port[:\s]+(\d{4,5})', line)
                        if m2h and m2p:
                            self._set_rsd(m2h.group(1).strip(), m2p.group(1).strip(), generation=generation)
                            continue
                    if m: self._set_rsd(m.group(1), m.group(2), generation=generation)

                # tunneld 非預期結束（如 USB 拔除），若 generation 仍匹配則自動重啟
                if generation == self._tunneld_generation:
                    self._log("⚠ tunneld 已結束（裝置斷線？），5 秒後嘗試重新建立連線...", color="orange")
                    self._clear_rsd()
                    self.root.after(0, lambda: self.lbl_tunneld.config(text="重啟中...", foreground="orange"))
                    time.sleep(5)
                    if generation == self._tunneld_generation:
                        self.root.after(0, lambda: self._start_tunneld(force_restart=False))
            except Exception as e:
                self._log(f"❌ tunneld 錯誤: {e}", color="red")
                self.root.after(0, lambda: self.lbl_tunneld.config(text="❌ 失敗", foreground="red"))

        threading.Thread(target=_read_output, daemon=True).start()

    def _set_rsd(self, host, port, generation=None):
        if generation is not None and generation != self._tunneld_generation:
            return
        self.root.after(0, lambda: self.rsd_host.set(host))
        self.root.after(0, lambda: self.rsd_port.set(port))
        self.root.after(0, lambda: self.btn_start.config(state="normal")) 
        self._log(f"✅ RSD: {host}:{port}", color="green")
        self.core._conn_status = "connected"

    def _detect_rsd_sync(self, timeout=15):
        def _parse(output):
            cleaned = re.sub(r'\x1B\[[0-?]*[ -/]*[@-~]', '', output)
            m_host = re.search(r'HOST=([\da-fA-F:.\s]+)', cleaned, re.I)
            m_port = re.search(r'PORT=(\d{4,5})', cleaned, re.I)
            if m_host and m_port: return m_host.group(1).strip(), m_port.group(1).strip()
            m_rsd = re.search(r'--rsd\s+([\da-fA-F:]+)\s+(\d{4,5})', cleaned, re.I)
            if m_rsd: return m_rsd.group(1).strip(), m_rsd.group(2).strip()
            return None

        connection_type = self.connection_type.get().strip().lower()
        if connection_type not in ("usb", "wifi"):
            connection_type = "usb"

        # 若 tunneld 剛重啟，等待最多 20 秒讓其建立新 tunnel
        wait_deadline = time.time() + 20
        while time.time() < wait_deadline:
            if self.rsd_host.get() and self.rsd_port.get():
                h, p = self.rsd_host.get(), self.rsd_port.get()
                self._log(f"✅ RSD（tunneld 自動解析）: {h}:{p}", color="green")
                return h, int(p)
            time.sleep(0.5)

        try:
            result = subprocess.run([sys.executable, "-m", "pymobiledevice3", "remote", "start-tunnel", "--script-mode", "-t", connection_type], capture_output=True, text=True, timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
            output = (result.stdout or "") + (result.stderr or "")
            output_lower = output.lower()
            self._last_tunnel_device_not_connected = "device is not connected" in output_lower
            if output.strip():
                for out_line in output.splitlines():
                    if out_line.strip():
                        self._log(f"[start-tunnel] {out_line.strip()}")
            self._log(f"[start-tunnel] returncode={result.returncode}")
            if self._last_tunnel_device_not_connected:
                self._log("⚠ start-tunnel 回報 Device is not connected，下次重試將重啟 tunneld", color="orange")
            found = _parse(output)
            if found:
                self._last_tunnel_device_not_connected = False
                self._set_rsd(found[0], found[1])
                return found[0], int(found[1])
        except Exception as e:
            self._log(f"[start-tunnel] 例外: {e}", color="orange")
        raise RuntimeError(f"未偵測到 RSD（模式: {connection_type}）")

    def _detect_rsd(self):
        if self.rsd_host.get() and self.rsd_port.get():
            self._log(f"ℹ RSD 已填入: {self.rsd_host.get()}:{self.rsd_port.get()}", color="blue")
            self.btn_start.config(state="normal")
            if getattr(self, '_is_auto_connecting', False):
                self._finalize_auto_connect()
            return
        if not self.tunneld_proc or self.tunneld_proc.poll() is not None:
            self._log("⚠ 請先啟動 tunneld", color="orange")
            if getattr(self, '_is_auto_connecting', False): self._handle_auto_connect_retry()
            return
        self._log("🔍 偵測 RSD...", color="blue")
        def _run():
            try:
                self._detect_rsd_sync(15)
                if getattr(self, '_is_auto_connecting', False):
                    self.root.after(0, self._finalize_auto_connect)
            except Exception as e:
                self._log(f"❌ 偵測失敗: {e}", color="red")
                if getattr(self, '_is_auto_connecting', False):
                    self.root.after(0, self._handle_auto_connect_retry)
        threading.Thread(target=_run, daemon=True).start()

    def _run_mount(self, on_complete=None):
        self._log("🔧 執行 auto-mount...")
        self.lbl_mount.config(text="執行中...", foreground="orange")
        def _run():
            try:
                result = subprocess.run([sys.executable, "-m", "pymobiledevice3", "mounter", "auto-mount"], capture_output=True, text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
                output = (result.stdout + result.stderr).strip()
                output_lower = output.lower()
                # 記錄實際輸出以便除錯
                if output:
                    for out_line in output.splitlines():
                        if out_line.strip():
                            self._log(f"[mount] {out_line.strip()}")

                has_error_keyword = any(k in output_lower for k in [
                    "error",
                    "failed",
                    "exception",
                    "traceback",
                    "device is not connected",
                    "not connected"
                ])

                if result.returncode == 0 and not has_error_keyword:
                    self.root.after(0, lambda: self.lbl_mount.config(text="✅ 完成", foreground="green"))
                    self._log("✅ DDI 掛載完成", color="green")
                elif "already mounted" in output_lower or "image already" in output_lower:
                    self.root.after(0, lambda: self.lbl_mount.config(text="✅ 已掛載", foreground="green"))
                    self._log("✅ DDI 已掛載（先前已掛載）", color="green")
                else:
                    self.root.after(0, lambda: self.lbl_mount.config(text="⚠ 掛載失敗", foreground="orange"))
                    self._log(f"⚠ DDI 掛載失敗 (returncode={result.returncode})", color="orange")
            except Exception as e:
                self.root.after(0, lambda: self.lbl_mount.config(text="❌ 失敗", foreground="red"))
                self._log(f"❌ auto-mount 錯誤: {e}", color="red")
            finally:
                if on_complete: self.root.after(0, on_complete)
        threading.Thread(target=_run, daemon=True).start()

    def _unmount_ddi(self, sync=False):
        self._log("🔧 執行 auto-unmount DDI...", color="blue")
        self.root.after(0, lambda: self.lbl_mount.config(text="卸載中...", foreground="orange"))
        def _run():
            try:
                result = subprocess.run([sys.executable, "-m", "pymobiledevice3", "mounter", "unmount"], capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
                output = result.stdout + result.stderr
                if result.returncode == 0 or "unmounted" in output.lower() or "not mounted" in output.lower():
                    self.root.after(0, lambda: self.lbl_mount.config(text="✅ 已卸載", foreground="green"))
                    self._log("✅ DDI 已卸載", color="green")
                else:
                    self.root.after(0, lambda: self.lbl_mount.config(text="⚠ 卸載失敗", foreground="orange"))
            except Exception as e:
                self.root.after(0, lambda: self.lbl_mount.config(text="❌ 失敗", foreground="red"))
        if sync: _run()
        else: threading.Thread(target=_run, daemon=True).start()

    def _stop_tunneld_process(self):
        self._tunneld_generation += 1
        if self.tunneld_proc and self.tunneld_proc.poll() is None:
            self._log("🔪 終止 tunneld 進程...", color="red")
            try:
                self.tunneld_proc.terminate()
                self.tunneld_proc.wait(timeout=3)
            except Exception:
                try:
                    self.tunneld_proc.kill()
                except Exception:
                    pass
            self.tunneld_proc = None
        self._kill_old_tunneld()
        self._clear_rsd()
        self.root.after(0, lambda: self.lbl_tunneld.config(text="已停止", foreground="#555555"))
        self.core._conn_status = "disconnected"

    # ==========================================
    # 核心連動與防護邏輯
    # ==========================================
    def _on_coord_change(self, *args):
        if self._updating_from_map: return

        def _parse_pair(lat_var, lng_var):
            lat_text = lat_var.get().strip()
            lng_text = lng_var.get().strip()
            if not lat_text or not lng_text: return None
            return float(lat_text), float(lng_text)

        try:
            start = _parse_pair(self.start_lat, self.start_lng)
            end = _parse_pair(self.end_lat, self.end_lng)
        except ValueError: return

        points = []
        if start: points.append(start)
        if end: points.append(end) if start else points.append(end)

        # 🔥 核心防護機制：當模擬正在運行時，徹底阻斷 UI 輸入框的自動同步，防止橡皮筋效應
        if self.core.running:
            self._log("🛡️ [防護機制] 模擬運行中，已阻斷 UI 輸入框自動同步，防止橡皮筋效應", color="orange")
            return
            
        self.core.update_route_points(points)

        # 偵測起終點相同，自動鎖定
        if len(points) >= 2:
            s_lat, s_lng = points[0]
            e_lat, e_lng = points[-1]
            if abs(s_lat - e_lat) < 0.0000001 and abs(s_lng - e_lng) < 0.0000001:
                if getattr(self, '_coord_auto_timer', None): self.root.after_cancel(self._coord_auto_timer)
                def do_start():
                    if not self.core.running:
                        self._log("💡 偵測到起終點相同，自動啟動模擬...", color="orange")
                        self._start_simulation()
                self._coord_auto_timer = self.root.after(500, do_start)

    def _handle_coord_paste(self, event, is_start):
        try:
            text = self.root.clipboard_get().strip()
            m = re.match(r'^[\(\[\s]*(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)[\)\]\s]*$', text)
            if m:
                lat, lng = m.groups()
                if is_start:
                    self.start_lat.set(lat)
                    self.start_lng.set(lng)
                else:
                    self.end_lat.set(lat)
                    self.end_lng.set(lng)
                self._log(f"📋 自動解析並貼上座標: {lat}, {lng}", color="blue")
                return "break"  
        except Exception: pass
        return None

    def _copy_start_to_end(self):
        slat = self.start_lat.get().strip()
        slng = self.start_lng.get().strip()
        if not slat or not slng:
            messagebox.showwarning("提示", "請先輸入起點座標！", parent=self.root)
            return
        self.end_lat.set(slat)
        self.end_lng.set(slng)

    # ==========================================
    # 地圖互動事件路由
    # ==========================================
    def handle_map_event(self, action, data):
        if action == 'route':
            points = data.get('points', [])
            self.root.after(0, lambda: self._on_map_route(points))
        elif action == 'start_simulation':
            self.root.after(0, self._start_simulation)
        elif action == 'bookmark_location':
            self.root.after(0, lambda: self._bookmark_location(lat=data.get('lat'), lng=data.get('lng'), description=data.get('description')))
        elif action == 'bookmark_route':
            self.root.after(0, lambda: self._bookmark_route(description=data.get('description')))
        elif action == 'delete_bookmark_location':
            self.root.after(0, lambda: self._delete_bookmark_confirmed(data.get('line')))
        elif action == 'delete_bookmark_route':
            self.root.after(0, lambda: self._delete_route_confirmed(data.get('index')))
        elif action == 'toggle_pause':
            self.root.after(0, self._pause_simulation)
        else:
            lat = data.get('lat', 0)
            lng = data.get('lng', 0)
            idx = data.get('index', 0)
            self.root.after(0, lambda: self._on_map_click(action, lat, lng, idx))

    def _on_map_route(self, points):
        cleaned = []
        for point in points:
            try: cleaned.append((float(point['lat']), float(point['lng'])))
            except: continue

        old_count = len(self.core.get_route_points_snapshot())
        self.core.update_route_points(cleaned)
        
        self._updating_from_map = True
        try:
            if cleaned:
                self.start_lat.set(f"{cleaned[0][0]:.6f}")
                self.start_lng.set(f"{cleaned[0][1]:.6f}")
            else:
                self.start_lat.set(""); self.start_lng.set("")

            if len(cleaned) >= 2:
                self.end_lat.set(f"{cleaned[-1][0]:.6f}")
                self.end_lng.set(f"{cleaned[-1][1]:.6f}")
            else:
                self.end_lat.set(""); self.end_lng.set("")
        finally:
            self._updating_from_map = False

        waypoint_count = max(0, len(cleaned) - 2)
        self._log(f"🧭 路線點: {len(cleaned)} 個，途經點: {waypoint_count} 個")
        if self.core.running and len(cleaned) > old_count:
            self._log("➕ 已加入新路線點，模擬會接著跑新的路線")

    def _on_map_click(self, action, lat, lng, index=0):
        if action == 'start':
            self.start_lat.set(f"{lat:.6f}"); self.start_lng.set(f"{lng:.6f}")
            self._log(f"📍 起點: {lat:.6f}, {lng:.6f}")
        elif action == 'end':
            self.end_lat.set(f"{lat:.6f}"); self.end_lng.set(f"{lng:.6f}")
            self._log(f"🏁 終點: {lat:.6f}, {lng:.6f}")
        elif action == 'clear':
            self.core.update_route_points([])
            self._updating_from_map = True
            try:
                self.start_lat.set(""); self.start_lng.set("")
                self.end_lat.set(""); self.end_lng.set("")
            finally:
                self._updating_from_map = False
            self._log("🗑️ 已清除")
        elif action == 'auto_start':
            if self.core.running: self._log("🔄 偵測到運行中雙擊，自動切換位置...", color="orange")
            self.core.update_route_points([(lat, lng), (lat, lng)])
            self._updating_from_map = True
            try:
                self.start_lat.set(f"{lat:.6f}"); self.start_lng.set(f"{lng:.6f}")
                self.end_lat.set(f"{lat:.6f}"); self.end_lng.set(f"{lng:.6f}")
            finally: self._updating_from_map = False
            if not self.core.running: self._start_simulation()
        elif action == 'jump_to_node':
            self._jump_request_index = index
            if not self.core.running:
                self._log(f"🚀 準備從路線點 {index} 開始模擬...", color="blue")
                self._start_simulation()
            else:
                self._log(f"🚀 模擬中...準備跳轉至路線點 {index}", color="blue")

    def _update_led(self):
        status = getattr(self.core, '_conn_status', 'disconnected')
        if status == 'connected':
            color = "#77DD77" if self._led_state else "#B2EBF2"
            self.led_canvas.itemconfig(self.led_item, fill=color, outline=color)
            self.lbl_led_status.config(text="已連線", foreground="#2E8B57")
            self._led_state = not self._led_state
        elif status == 'reconnecting':
            color = "#FFB347" if self._led_state else "#FFE4B5"
            self.led_canvas.itemconfig(self.led_item, fill=color, outline=color)
            self.lbl_led_status.config(text="重連中...", foreground="#FF8C00")
            self._led_state = not self._led_state
        else:
            self.led_canvas.itemconfig(self.led_item, fill="#D3D3D3", outline="#D3D3D3")
            self.lbl_led_status.config(text="未連線", foreground="#A9A9A9")
            self._led_state = False
        self.root.after(800, self._update_led)

    # ==========================================
    # 書籤與路徑管理 (Bookmarks & Routes)
    # ==========================================
    def _bookmark_location(self, event=None, lat=None, lng=None, description=None):
        if lat is not None and lng is not None: source_desc = "地圖路線節點"
        else:
            lat, lng = self.core.current_lat, self.core.current_lng
            source_desc = "目前模擬位置"
        
        if lat is None or lng is None:
            try:
                if self.start_lat.get().strip() and self.start_lng.get().strip():
                    lat, lng = float(self.start_lat.get()), float(self.start_lng.get())
                    source_desc = "起點輸入框"
            except ValueError: pass
                
        if lat is None or lng is None:
            messagebox.showwarning("無法收藏", "目前沒有座標可供收藏！\n請先設定起點或開始模擬。")
            return
            
        desc = description.strip() if description else simpledialog.askstring("收藏地點", "請輸入地點名稱：", initialvalue=source_desc, parent=self.root)
        if not desc: return

        bookmark_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookmarks.txt")
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {desc} (緯度: {lat:.6f}, 經度: {lng:.6f})\n"
        try:
            with open(bookmark_file_path, "a", encoding="utf-8") as f: f.write(line)
            self._log(f"🔖 已收藏地點: {desc} ({lat:.6f}, {lng:.6f})", color="green")
            self._load_bookmarks() 
        except Exception as e:
            self._log(f"❌ 收藏失敗: {e}", color="red")

    def _load_bookmarks(self):
        bm_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookmarks.txt")
        if not os.path.exists(bm_file): return
        try:
            with open(bm_file, "r", encoding="utf-8") as f:
                values = [line.strip() for line in f.readlines() if line.strip()]
            self.bookmark_combo['values'] = list(reversed(values)) if values else ["(無收藏紀錄)"]
        except Exception: pass

    def _on_bookmark_selected(self, event=None):
        selection = self.bookmark_combo.get()
        if not selection or selection in ["(無收藏紀錄)", "📂 我的地點"]: return
        m = re.search(r'緯度:\s*([-\d.]+),\s*經度:\s*([-\d.]+)', selection)
        if m:
            lat, lng = m.groups()
            
            dialog = tk.Toplevel(self.root)
            dialog.title("地點操作")
            dialog.transient(self.root)
            dialog.grab_set()
            dialog.configure(bg="#FFF0F5")
            
            x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 160
            y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 100
            dialog.geometry(f"+{x}+{y}")
            
            msg_frame = ttk.Frame(dialog, padding=15)
            msg_frame.pack(fill='both', expand=True)
            ttk.Label(msg_frame, text=f"已選擇地點:\n{selection}\n\n請問要進行什麼操作？", justify="center").pack()
            
            result = [None]
            def on_click(action):
                result[0] = action
                dialog.destroy()
                
            btn_frame = ttk.Frame(dialog, padding=(10, 0, 10, 15))
            btn_frame.pack(fill='x')
            
            row1 = ttk.Frame(btn_frame)
            row1.pack(fill='x', pady=(0, 5))
            ttk.Button(row1, text="📍 設為起點", command=lambda: on_click('start')).pack(side='left', expand=True, padx=2)
            ttk.Button(row1, text="🏁 設為終點", command=lambda: on_click('end')).pack(side='left', expand=True, padx=2)
            ttk.Button(row1, text="🗑️ 刪除", command=lambda: on_click('delete')).pack(side='left', expand=True, padx=2)
            
            row2 = ttk.Frame(btn_frame)
            row2.pack(fill='x')
            ttk.Button(row2, text="🚀 設為起終點並開始模擬", command=lambda: on_click('start_end_run')).pack(side='left', fill='x', expand=True, padx=2)
            
            self.root.wait_window(dialog)
            
            if result[0] == 'start':
                self.start_lat.set(lat)
                self.start_lng.set(lng)
                self._log(f"📂 載入收藏至起點: {lat}, {lng}", color="blue")
            elif result[0] == 'end':
                self.end_lat.set(lat)
                self.end_lng.set(lng)
                self._log(f"📂 載入收藏至終點: {lat}, {lng}", color="blue")
            elif result[0] == 'start_end_run':
                self.start_lat.set(lat)
                self.start_lng.set(lng)
                self.end_lat.set(lat)
                self.end_lng.set(lng)
                self._log(f"📂 載入收藏至起終點並準備啟動: {lat}, {lng}", color="blue")
            elif result[0] == 'delete':
                self._delete_bookmark(selection)
        
        self.root.after(100, lambda: self.bookmark_combo.set("📂 我的地點"))

    def _delete_bookmark(self, target_selection=None):
        selection = target_selection or self.bookmark_combo.get()
        if not selection or selection in ["(無收藏紀錄)", "📂 我的地點"]:
            messagebox.showwarning("刪除失敗", "請先從下拉選單選擇要刪除的地點！")
            return

        if not self._show_confirm_dialog("刪除地點", "確定要刪除此地點嗎？", selection, parent=self.root):
            return

        self._delete_bookmark_confirmed(selection)

    def _delete_bookmark_confirmed(self, selection=None):
        if not selection:
            return

        bookmark_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookmarks.txt")
        try:
            with open(bookmark_file_path, "r", encoding="utf-8") as f: lines = f.readlines()
            new_lines = [line for line in lines if line.strip() != selection]
            with open(bookmark_file_path, "w", encoding="utf-8") as f: f.writelines(new_lines)
            self._log(f"🗑️ 已刪除地點收藏: {selection}", color="orange")
            self._load_bookmarks()
            self.bookmark_combo.set("📂 我的地點")
        except Exception as e:
            self._log(f"❌ 刪除地點失敗: {e}", color="red")

    def _delete_route_confirmed(self, route_index=None):
        try:
            route_index = int(route_index)
        except (TypeError, ValueError):
            return

        self._load_routes()
        if route_index < 0 or route_index >= len(self.saved_routes):
            return

        desc = self.saved_routes[route_index].get('description', '自訂路徑')
        del self.saved_routes[route_index]
        routes_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json")
        try:
            with open(routes_file, 'w', encoding='utf-8') as f:
                json.dump(self.saved_routes, f, ensure_ascii=False, indent=2)
            self._log(f"🗑️ 已刪除路徑收藏: {desc}", color="orange")
        except Exception as e:
            self._log(f"❌ 刪除路徑失敗: {e}", color="red")

    def _bookmark_route(self, event=None, description=None):
        points = self.core.get_route_points_snapshot()
        if len(points) < 2:
            try: points = [(float(self.start_lat.get()), float(self.start_lng.get())), (float(self.end_lat.get()), float(self.end_lng.get()))]
            except ValueError: pass

        if len(points) < 2:
            messagebox.showwarning("無法收藏", "請先設定起點與終點，或在地圖上規劃路徑！")
            return
            
        routes_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json")
        default_desc = f"{points[0][0]:.4f},{points[0][1]:.4f} -> {points[-1][0]:.4f},{points[-1][1]:.4f} (共{len(points)}點)"
        desc = description.strip() if description else simpledialog.askstring("收藏路徑", "請輸入路徑名稱：", initialvalue=default_desc, parent=self.root)
        if not desc: return
        
        routes_data = []
        if os.path.exists(routes_file):
            try:
                with open(routes_file, 'r', encoding='utf-8') as f: routes_data = json.load(f)
            except Exception: pass
                
        routes_data.append({"timestamp": time.strftime('%Y-%m-%d %H:%M:%S'), "description": desc, "points": points})
        try:
            with open(routes_file, 'w', encoding='utf-8') as f: json.dump(routes_data, f, ensure_ascii=False, indent=2)
            self._log(f"🛣️ 已收藏路徑: {desc}", color="green")
            self._load_routes()
        except Exception as e:
            self._log(f"❌ 收藏路徑失敗: {e}", color="red")

    def _load_routes(self):
        routes_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json")
        if not os.path.exists(routes_file): return
        try:
            with open(routes_file, 'r', encoding='utf-8') as f: self.saved_routes = json.load(f)
        except Exception: pass

    def _open_route_manager(self):
        if not self.saved_routes:
            messagebox.showinfo("無收藏", "目前還沒有任何收藏的路徑喔！")
            return

        manager_win = tk.Toplevel(self.root)
        manager_win.title("預覽與管理路徑")
        manager_win.geometry("750x400")
        manager_win.transient(self.root)
        manager_win.configure(bg="#FFF0F5")
        
        listbox_frame = ttk.Frame(manager_win)
        listbox_frame.pack(fill='both', expand=True, padx=10, pady=10)
        
        scrollbar = ttk.Scrollbar(listbox_frame)
        scrollbar.pack(side='right', fill='y')
        
        listbox = tk.Listbox(listbox_frame, yscrollcommand=scrollbar.set, font=('Microsoft JhengHei', 10),
                             bg="#FFFFFF", fg="#5D4037", selectbackground="#FFD1DC", selectforeground="#5D4037", relief="flat")
        listbox.pack(side='left', fill='both', expand=True)
        scrollbar.config(command=listbox.yview)

        for r in reversed(self.saved_routes):
            listbox.insert(tk.END, f"[{r.get('timestamp', '')}] {r.get('description', '自訂路徑')} ({len(r.get('points', []))}點)")

        def on_double_click(event):
            idx = listbox.curselection()
            if idx:
                actual_idx = len(self.saved_routes) - 1 - idx[0]
                pts = self.saved_routes[actual_idx].get('points', [])
                if len(pts) >= 2:
                    self.core.update_route_points([(float(p[0]), float(p[1])) for p in pts])
                    self._updating_from_map = True
                    try:
                        self.start_lat.set(f"{pts[0][0]:.6f}"); self.start_lng.set(f"{pts[0][1]:.6f}")
                        self.end_lat.set(f"{pts[-1][0]:.6f}"); self.end_lng.set(f"{pts[-1][1]:.6f}")
                    finally: self._updating_from_map = False
                    self._log(f"📂 已載入路徑: {self.saved_routes[actual_idx].get('description')}", color="blue")
                manager_win.destroy()

        def on_delete():
            idx = listbox.curselection()
            if not idx:
                messagebox.showwarning("提示", "請先選擇要刪除的路徑", parent=manager_win)
                return
            actual_idx = len(self.saved_routes) - 1 - idx[0]
            desc = self.saved_routes[actual_idx].get('description', '自訂路徑')
            if not self._show_confirm_dialog("刪除路徑", "確定要刪除此路徑嗎？", desc, parent=manager_win):
                return
            self._delete_route_confirmed(actual_idx)
            listbox.delete(idx[0])
            if not self.saved_routes:
                manager_win.destroy()

        def on_rename():
            idx = listbox.curselection()
            if not idx:
                messagebox.showwarning("提示", "請先選擇要修改名稱的路徑", parent=manager_win)
                return
            actual_idx = len(self.saved_routes) - 1 - idx[0]
            old_desc = self.saved_routes[actual_idx].get('description', '自訂路徑')
            
            new_desc = simpledialog.askstring("修改名稱", "請輸入新的路徑名稱：", initialvalue=old_desc, parent=manager_win)
            if new_desc is not None:
                new_desc = new_desc.strip()
                if new_desc and new_desc != old_desc:
                    self.saved_routes[actual_idx]['description'] = new_desc
                    routes_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes.json")
                    try:
                        with open(routes_file, 'w', encoding='utf-8') as f:
                            json.dump(self.saved_routes, f, ensure_ascii=False, indent=2)
                        self._log(f"✏️ 已修改路徑名稱: {old_desc} -> {new_desc}", color="blue")
                        listbox.delete(idx[0])
                        r = self.saved_routes[actual_idx]
                        listbox.insert(idx[0], f"[{r.get('timestamp', '')}] {r.get('description', '自訂路徑')} ({len(r.get('points', []))}點)")
                        listbox.selection_set(idx[0])
                    except Exception as e:
                        self._log(f"❌ 修改名稱失敗: {e}", color="red")

        listbox.bind('<Double-Button-1>', on_double_click)

        btn_frame = ttk.Frame(manager_win)
        btn_frame.pack(fill='x', padx=10, pady=(0,10))
        ttk.Button(btn_frame, text="✅ 載入所選 (或雙擊)", command=lambda: on_double_click(None)).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="✏️ 修改名稱", command=on_rename).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="🗑️ 刪除所選", command=on_delete).pack(side='right', padx=2)

    def _import_route_file(self):
        file_path = filedialog.askopenfilename(filetypes=[("GPS 路線檔", "*.kml *.gpx")], parent=self.root)
        if not file_path: return
        self._log(f"📥 準備匯入檔案: {file_path}", color="blue")
        # KML/GPX 解析交由外部或後續實作擴展 (因篇幅限制保留框架)

    # ==========================================
    # 模擬控制與進度更新
    # ==========================================
    def _on_spacebar(self, event):
        focused = self.root.focus_get()
        if isinstance(focused, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)): return
        if self.core.running: self._pause_simulation()

    def _start_simulation(self):
        if not self.rsd_host.get() or not self.rsd_port.get():
            messagebox.showerror("錯誤", "請先填入 RSD 位址")
            return
        points = self.core.get_route_points_snapshot()
        if len(points) < 2:
            try: points = [(float(self.start_lat.get()), float(self.start_lng.get())), (float(self.end_lat.get()), float(self.end_lng.get()))]
            except ValueError: return messagebox.showerror("錯誤", "座標格式錯誤")
        try:
            speed = float(self.speed_kmh.get())
            ivl = float(self.interval.get())
        except ValueError: return messagebox.showerror("錯誤", "速度格式錯誤")

        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.btn_stop.config(state="normal")
        self._start_timer()
        self.core.start_simulation(points, speed, ivl)

    def _pause_simulation(self):
        if not self.core.running: return
        self.core.paused = not self.core.paused
        if self.core.paused:
            self._pause_timer()
            self.btn_pause.config(text="▶ 繼續")
            self._log("⏸ 已暫停", color="orange")
        else:
            self._resume_timer()
            self.btn_pause.config(text="⏸ 暫停")
            self._log("▶ 繼續", color="blue")

    def _stop_simulation(self):
        self.core._stop_task = True
        self.core.paused = False
        self.core._perform_full_cleanup = True
        self._pause_timer()
        self._log("⏹ 停止模擬，正在回到起點並進行清理...", color="red")

    def _reset_ui_elements(self, clear_coords=True):
        if clear_coords:
            self.root.after(0, lambda: self.start_lat.set(""))
            self.root.after(0, lambda: self.start_lng.set(""))
            self.root.after(0, lambda: self.end_lat.set(""))
            self.root.after(0, lambda: self.end_lng.set(""))
        self.root.after(0, lambda: self.btn_start.config(state="disabled"))
        self.root.after(0, lambda: self.btn_pause.config(state="disabled", text="⏸ 暫停"))
        self.root.after(0, lambda: self.btn_stop.config(state="disabled"))
        self.root.after(0, lambda: self.lbl_progress.config(text="已停止"))
        self.root.after(0, lambda: self.progress.config(value=0))
        self.root.after(0, lambda: self.lbl_mount.config(text="尚未執行", foreground="#555555"))
        self.root.after(0, lambda: self.lbl_distance.config(text="📏 距離: 0.00 / 0.00 km"))
        self.root.after(0, lambda: self.lbl_eta.config(text="⏳ 預估剩餘: --:--:--"))
        self.root.after(0, self._reset_timer)
        self.core._current_segment_index = 1
        self._log("✅ 所有資訊已清除並重置", color="green")

    # ==========================================
    # 計時器與動畫
    # ==========================================
    def _update_idle_animation(self):
        if not self.core.running and not self.core.holding:
            current_text = self.lbl_progress.cget("text")
            if "出發" in current_text or "停止" in current_text:
                self.lbl_progress.config(text=self._idle_frames[self._idle_anim_idx])
                self._idle_anim_idx = (self._idle_anim_idx + 1) % len(self._idle_frames)
        self.root.after(500, self._update_idle_animation)

    def _reset_timer(self):
        self._sim_accumulated_time = 0
        self._sim_last_update_time = time.time()
        self._timer_running = False
        self.lbl_timer.config(text="⏱️ 行走時間: 00:00:00")

    def _start_timer(self):
        self._reset_timer()
        self._sim_last_update_time = time.time()
        self._timer_running = True
        self._update_timer_ui()

    def _pause_timer(self):
        if getattr(self, '_timer_running', False):
            self._sim_accumulated_time += time.time() - self._sim_last_update_time
            self._timer_running = False

    def _resume_timer(self):
        if not getattr(self, '_timer_running', False):
            self._sim_last_update_time = time.time()
            self._timer_running = True

    def _update_timer_ui(self):
        if not self.core.running: return
        total = self._sim_accumulated_time
        if getattr(self, '_timer_running', False): total += time.time() - self._sim_last_update_time
        hrs, rem = divmod(int(total), 3600)
        mins, secs = divmod(rem, 60)
        self.lbl_timer.config(text=f"⏱️ 行走時間: {hrs:02d}:{mins:02d}:{secs:02d}")
        self.root.after(500, self._update_timer_ui)

    def _play_alert_sound(self):
        def _play():
            try:
                if sys.platform == 'win32':
                    import winsound
                    for _ in range(3):
                        winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
                        time.sleep(0.3)
                else: print('\a')
            except Exception: pass
        threading.Thread(target=_play, daemon=True).start()

    def _show_alert_popup(self, alert_sec):
        alert_win = tk.Toplevel(self.root)
        alert_win.title("🔔 抵達提醒")
        alert_win.geometry("300x120")
        alert_win.attributes("-topmost", True)
        alert_win.configure(bg="#FFF0F5")
        
        # 置中邏輯
        x = self.root.winfo_x() + (self.root.winfo_width() // 2) - 150
        y = self.root.winfo_y() + (self.root.winfo_height() // 2) - 60
        alert_win.geometry(f"+{x}+{y}")
        
        msg_frame = ttk.Frame(alert_win, padding=15)
        msg_frame.pack(fill='both', expand=True)
        ttk.Label(msg_frame, text=f"🚀 準備接手！\n\n距離抵達不到 {alert_sec} 秒。", font=('Microsoft JhengHei', 11, 'bold'), foreground="#D1495B", justify="center").pack(pady=(0, 10))
        ttk.Button(msg_frame, text="我知道了", command=alert_win.destroy).pack()

    def _clear_log(self):
        self.log.config(state="normal")
        self.log.delete('1.0', tk.END)
        self.log.config(state="disabled")
        
    def _open_log_file(self):
        try:
            if os.path.exists(self.log_file_path):
                if sys.platform == 'win32': os.startfile(self.log_file_path)
                elif sys.platform == 'darwin': subprocess.run(['open', self.log_file_path])
                else: subprocess.run(['xdg-open', self.log_file_path])
                self._log("📝 已開啟日誌檔", color="blue")
            else: messagebox.showwarning("警告", "目前還沒有日誌檔案！")
        except Exception as e:
            self._log(f"❌ 開啟日誌檔失敗: {e}", color="red")

    def _log(self, message, color=None):
        now_time = time.strftime('%H:%M:%S')
        now_date = time.strftime('%Y-%m-%d')
        
        try:
            with open(getattr(self, 'log_file_path', 'simulator.log'), 'a', encoding='utf-8') as f:
                f.write(f"[{now_date} {now_time}] {message}\n")
        except Exception: pass

        def _write():
            self.log.config(state="normal")
            tag = color if color else ""
            self.log.insert(tk.END, f"[{now_time}] {message}\n", tag)
            self.log.see(tk.END)
            self.log.config(state="disabled")
        self.root.after(0, _write)

    # ==========================================
    # 設定存取與生命週期
    # ==========================================
    def _save_settings(self):
        settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        data = {
            'rsd_host': self.rsd_host.get(), 'rsd_port': self.rsd_port.get(),
            'connection_type': self.connection_type.get(),
            'speed_kmh': self.speed_kmh.get(), 'interval': self.interval.get(),
            'alert_seconds': self.alert_seconds.get(), 'auto_reconnect': self.auto_reconnect.get()
        }
        try:
            with open(settings_file, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception: pass

    def _load_settings(self):
        settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for key in data:
                        if hasattr(self, key): getattr(self, key).set(data[key])
            except Exception: pass

    def on_close(self):
        if not messagebox.askyesno("確認退出", "確定要退出模擬器嗎？\n(這將中斷連線並自動關閉地圖視窗)", parent=self.root): return
        self._save_settings()
        self.core.running = False
        self._should_close_browser = True
        
        # 強制關閉獨立的瀏覽器視窗
        if getattr(self, '_browser_proc', None):
            try:
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(self._browser_proc.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else: self._browser_proc.terminate()
                self._browser_proc = None
            except: pass
            
        # 卸載與終止
        if hasattr(self, '_unmount_ddi'): self._unmount_ddi(sync=True)
        if hasattr(self, '_stop_tunneld_process'): self._stop_tunneld_process()
            
        self.root.destroy()
