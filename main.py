import sys
import tkinter as tk
from utils import is_admin, run_as_admin
from simulator import SimulatorCore
from gui import SimulatorGUI

if __name__ == "__main__":
    if sys.platform == "win32" and not is_admin():
        run_as_admin()
        sys.exit()

    root = tk.Tk()
    root.title("iPhone GPS 模擬器（v17 模組化重構版）")
    
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    win_w = min(900, max(760, screen_w - 40))
    win_h = min(800, max(580, screen_h - 60)) 
    x = max(0, (screen_w - win_w) // 2)
    y = max(0, (screen_h - win_h) // 2 - 20) 
    root.geometry(f"{win_w}x{win_h}+{x}+{y}")
    root.minsize(600, 520)

    core = SimulatorCore()
    app = SimulatorGUI(root, core)
    core.set_gui(app)
    
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()