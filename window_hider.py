"""
Window Hider (Windows) — скрывает окна других приложений, оформление в стиле Windows XP.
Скрытые приложения продолжают работать в фоне: окно пропадает с экрана,
из панели задач и из Alt+Tab.

Горячие клавиши (работают из любого окна):
  Ctrl+Alt+H — скрыть активное окно
  Ctrl+Alt+S — вернуть все скрытые окна
  Ctrl+Alt+W — скрыть / показать само окно Window Hider
"""
import ctypes
import ctypes.wintypes as wt
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox

if sys.platform != "win32":
    raise SystemExit("Window Hider работает только в Windows.")

# ---------------------------------------------------------------- WinAPI ----
user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

user32.GetForegroundWindow.restype = wt.HWND
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

kernel32 = ctypes.windll.kernel32
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYSTEM_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"}

SW_HIDE, SW_SHOW = 0, 5
GA_ROOT = 2
GWL_EXSTYLE, WS_EX_TOOLWINDOW = -20, 0x00000080
DWMWA_CLOAKED = 14
MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
WM_HOTKEY = 0x0312
VK_H, VK_S, VK_W = 0x48, 0x53, 0x57
ID_HIDE, ID_SHOW, ID_SELF = 1, 2, 3

EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def is_cloaked(hwnd):
    val = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return val.value != 0


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def process_name(hwnd):
    """Имя exe-файла, которому принадлежит окно."""
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return "?"
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return "?"
    finally:
        kernel32.CloseHandle(handle)


def eligible(hwnd, show_all=False):
    """Окно, которое можно показать в списке.
    show_all=False — только обычные окна (как в Alt+Tab).
    show_all=True  — ещё и служебные/безымянные (кроме панели задач и рабочего стола)."""
    if not hwnd or not user32.IsWindowVisible(hwnd):
        return False
    if class_name(hwnd) in SYSTEM_CLASSES or is_cloaked(hwnd):
        return False
    if show_all:
        return True
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
        return False
    title = window_title(hwnd)
    return bool(title) and title != "Program Manager"


def list_windows(exclude=(), show_all=False):
    found = []

    def cb(hwnd, _):
        if hwnd not in exclude and eligible(hwnd, show_all):
            title = window_title(hwnd)
            if show_all:
                title = f"{title or '(без названия)'}  —  {process_name(hwnd)}"
            found.append((hwnd, title))
        return True

    proc = EnumProc(cb)
    user32.EnumWindows(proc, 0)
    return found


def hotkey_loop(q):
    """RegisterHotKey и цикл сообщений обязаны жить в одном потоке."""
    failed = []
    for hk_id, vk in ((ID_HIDE, VK_H), (ID_SHOW, VK_S), (ID_SELF, VK_W)):
        if not user32.RegisterHotKey(None, hk_id, MOD_CONTROL | MOD_ALT, vk):
            failed.append(hk_id)
    if failed:
        q.put(("warn", "Часть горячих клавиш занята другой программой"))
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            if msg.wParam == ID_HIDE:
                q.put(("hide_active", user32.GetForegroundWindow()))
            elif msg.wParam == ID_SHOW:
                q.put(("show_all", None))
            elif msg.wParam == ID_SELF:
                q.put(("toggle_self", None))


# ------------------------------------------------------------ XP-оформление --
BG = "#ECE9D8"        # классический бежевый фон XP
SIDE = "#6375D6"      # синяя боковая панель
HEAD = "#C6D3F7"      # заголовок группы
BODY = "#D6DFF7"      # тело группы
LINK = "#215DC6"
BLUE_A, BLUE_B = "#0054E3", "#3D95FF"   # градиент заголовка Luna
FONT = ("Tahoma", 9)
FONT_B = ("Tahoma", 9, "bold")
FONT_S = ("Tahoma", 8)


def lerp_color(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class App:
    def __init__(self, root):
        self.root = root
        self.hidden = {}          # hwnd -> заголовок
        self.windows = []         # то, что показано в списке «Открытые»
        self.hidden_view = []     # то, что показано в списке «Скрытые»
        self.q = queue.Queue()
        self.told_about_self = False
        self.show_all = tk.BooleanVar(value=False)

        root.title("Window Hider")
        root.geometry("700x500")
        root.minsize(620, 440)
        root.configure(bg=BG)

        self.build_ui()
        root.update_idletasks()
        self.own = user32.GetAncestor(root.winfo_id(), GA_ROOT)

        threading.Thread(target=hotkey_loop, args=(self.q,), daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()
        self.poll()

    # ---- интерфейс ----
    def build_ui(self):
        banner = tk.Canvas(self.root, height=52, highlightthickness=0, bd=0)
        banner.pack(fill="x")
        banner.bind("<Configure>", lambda e: self.draw_banner(banner, e.width, 52))

        self.status = tk.Label(self.root, text="", bg=BG, font=FONT_S, anchor="w",
                               relief="sunken", bd=1, padx=6)
        self.status.pack(side="bottom", fill="x")

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        side = tk.Frame(body, bg=SIDE, width=200)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        g = self.group(side, "Действия")
        self.link(g, "Обновить список", self.refresh)
        self.link(g, "Вернуть все окна", self.restore_all)
        self.link(g, "Скрыть это окно", self.toggle_self)
        tk.Checkbutton(g, text="Показывать все окна (и служебные, без названия)",
                       variable=self.show_all, command=self.refresh, bg=BODY,
                       activebackground=BODY, font=FONT_S, anchor="w", justify="left",
                       wraplength=150).pack(fill="x", pady=(6, 0))

        g = self.group(side, "Горячие клавиши")
        for text in ("Ctrl+Alt+H — скрыть активное окно",
                     "Ctrl+Alt+S — вернуть все окна",
                     "Ctrl+Alt+W — скрыть / показать это окно"):
            tk.Label(g, text=text, bg=BODY, font=FONT_S, justify="left",
                     anchor="w", wraplength=165).pack(fill="x", pady=1)

        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True, padx=12, pady=10)

        self.lb_open = self.list_box(main, "Открытые окна", "Скрыть выбранные", self.hide_selected)
        self.lb_hidden = self.list_box(main, "Скрытые окна (работают в фоне)", "Вернуть выбранные",
                                       self.restore_selected)

    def draw_banner(self, canvas, w, h):
        canvas.delete("all")
        for x in range(0, w, 2):
            canvas.create_line(x, 0, x, h, fill=lerp_color(BLUE_A, BLUE_B, x / max(w, 1)), width=2)
        canvas.create_text(17, h // 2 + 1, text="Window Hider", anchor="w", fill="#0A2A7A",
                           font=("Trebuchet MS", 17, "bold italic"))
        canvas.create_text(16, h // 2, text="Window Hider", anchor="w", fill="white",
                           font=("Trebuchet MS", 17, "bold italic"))

    def group(self, parent, title):
        box = tk.Frame(parent, bg=SIDE)
        box.pack(fill="x", padx=10, pady=(12, 0))
        tk.Label(box, text=title, bg=HEAD, fg=LINK, font=FONT_B, anchor="w",
                 padx=8, pady=4).pack(fill="x")
        inner = tk.Frame(box, bg=BODY, padx=8, pady=6)
        inner.pack(fill="x")
        return inner

    def link(self, parent, text, command):
        lbl = tk.Label(parent, text=text, bg=BODY, fg=LINK, font=FONT, cursor="hand2", anchor="w")
        lbl.pack(fill="x", pady=2)
        lbl.bind("<Button-1>", lambda e: command())
        lbl.bind("<Enter>", lambda e: lbl.config(font=("Tahoma", 9, "underline")))
        lbl.bind("<Leave>", lambda e: lbl.config(font=FONT))

    def list_box(self, parent, title, button_text, command):
        frame = tk.LabelFrame(parent, text=title, bg=BG, fg="#0046D5", font=FONT_B, padx=8, pady=6)
        frame.pack(fill="both", expand=True, pady=(0, 8))
        row = tk.Frame(frame, bg=BG)
        row.pack(fill="both", expand=True)
        sb = tk.Scrollbar(row)
        sb.pack(side="right", fill="y")
        lb = tk.Listbox(row, selectmode="extended", bg="white", font=FONT, bd=2, relief="sunken",
                        activestyle="none", selectbackground="#316AC5", selectforeground="white",
                        exportselection=False, yscrollcommand=sb.set)
        lb.pack(side="left", fill="both", expand=True)
        sb.config(command=lb.yview)
        tk.Button(frame, text=button_text, command=command, font=FONT, bg=BG,
                  activebackground="#FFF4C8", padx=10, pady=2).pack(anchor="e", pady=(6, 0))
        return lb

    # ---- логика ----
    def set_status(self, text):
        self.status.config(text=text)

    def hide_hwnd(self, hwnd, title=None):
        if hwnd in self.hidden:
            return
        self.hidden[hwnd] = title or window_title(hwnd) or f"Окно {hwnd}"
        user32.ShowWindow(hwnd, SW_HIDE)

    def restore_hwnds(self, hwnds):
        for hwnd in hwnds:
            title = self.hidden.pop(hwnd, None)
            if title is not None and user32.IsWindow(hwnd):
                user32.ShowWindow(hwnd, SW_SHOW)

    def restore_all(self):
        self.restore_hwnds(list(self.hidden))
        self.refresh()

    def refresh(self):
        for hwnd in [h for h in self.hidden if not user32.IsWindow(h)]:
            del self.hidden[hwnd]   # приложение закрылось само
        self.windows = [w for w in list_windows({self.own}, self.show_all.get()) if w[0] not in self.hidden]
        self.hidden_view = list(self.hidden.items())
        for lb, items in ((self.lb_open, self.windows), (self.lb_hidden, self.hidden_view)):
            lb.delete(0, "end")
            for _, title in items:
                lb.insert("end", title[:100])
        self.set_status(f"Открыто окон: {len(self.windows)}    Скрыто: {len(self.hidden)}")

    def hide_selected(self):
        for i in self.lb_open.curselection():
            self.hide_hwnd(*self.windows[i])
        self.refresh()

    def restore_selected(self):
        self.restore_hwnds([self.hidden_view[i][0] for i in self.lb_hidden.curselection()])
        self.refresh()

    def toggle_self(self):
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
            self.refresh()
            return
        if not self.told_about_self:
            messagebox.showinfo("Window Hider",
                                "Окно будет скрыто.\nВернуть его: Ctrl+Alt+W.")
            self.told_about_self = True
        self.root.withdraw()

    def poll(self):
        try:
            while True:
                action, arg = self.q.get_nowait()
                if action == "hide_active":
                    if arg == self.own:
                        self.toggle_self()
                    elif eligible(arg, True):
                        self.hide_hwnd(arg)
                        self.refresh()
                elif action == "show_all":
                    self.restore_all()
                elif action == "toggle_self":
                    self.toggle_self()
                elif action == "warn":
                    self.set_status(arg)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_close(self):
        self.restore_hwnds(list(self.hidden))   # чтобы окна не «потерялись»
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
