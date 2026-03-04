from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
import winreg
from tkinter import filedialog, messagebox, ttk
from datetime import datetime

import customtkinter as ctk
import pystray
from PIL import Image, ImageDraw

from rclone_setup.config import (
    DEFAULT_LOG_PATH,
    AppConfig,
    SyncPair,
    load_config,
    save_config,
)
from rclone_setup.sync_engine import (
    _is_rclone_remote_path,
    ensure_local_path,
    ensure_rclone_config,
    list_remote_dirs,
    list_remotes,
    normalize_path,
    open_rclone_config,
    run_bisync,
    test_connection,
)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# (flag, label, description)
BISYNC_FLAG_OPTIONS: list[tuple[str, str, str]] = [
    ("--check-access", "Check access", "Verify RCLONE_TEST files exist before syncing"),
    ("--force", "Force", "Bypass --max-delete safety check"),
    ("--create-empty-src-dirs", "Create empty dirs", "Mirror empty source directories to destination"),
    ("--no-cleanup", "No cleanup", "Keep working directory after successful sync"),
    ("--ignore-case", "Ignore case", "Case-insensitive filename comparison"),
    ("--fix-case", "Fix case", "Fix destination filename case to match source"),
]

_STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_APP_NAME = "rclone-bisync-manager"


def _startup_cmd() -> str:
    argv0 = os.path.abspath(sys.argv[0])
    if argv0.lower().endswith(".exe"):
        return f'"{argv0}"'
    return f'"{sys.executable}" "{argv0}"'


def is_startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY)
        winreg.QueryValueEx(key, _STARTUP_APP_NAME)
        winreg.CloseKey(key)
        return True
    except OSError:
        return False


def set_startup(enable: bool) -> None:
    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE
    )
    if enable:
        winreg.SetValueEx(key, _STARTUP_APP_NAME, 0, winreg.REG_SZ, _startup_cmd())
    else:
        try:
            winreg.DeleteValue(key, _STARTUP_APP_NAME)
        except OSError:
            pass
    winreg.CloseKey(key)


def _style_treeview() -> None:
    style = ttk.Style()
    style.theme_use("clam")
    style.configure(
        "App.Treeview",
        background="#2b2b2b",
        foreground="#dce4ee",
        rowheight=28,
        fieldbackground="#2b2b2b",
        borderwidth=0,
        font=("Segoe UI", 10),
    )
    style.configure(
        "App.Treeview.Heading",
        background="#1f538d",
        foreground="white",
        font=("Segoe UI", 10, "bold"),
        borderwidth=0,
        relief="flat",
    )
    style.map("App.Treeview", background=[("selected", "#1f538d")])


def _make_tray_image() -> Image.Image:
    """Draw a simple blue sync-arrows icon for the system tray."""
    import math
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Blue rounded background
    draw.rounded_rectangle([2, 2, 61, 61], radius=12, fill=(31, 83, 141, 255))
    cx, cy, r, lw = size // 2, size // 2, 17, 5
    # Top arc: 150° → 330° (right half, going clockwise)
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=150, end=30, fill="white", width=lw)
    # Bottom arc: 330° → 150° (left half, going clockwise)
    draw.arc([cx - r, cy - r, cx + r, cy + r], start=330, end=210, fill="white", width=lw)
    # Arrowheads as small filled triangles
    for tip_deg, tangent_offset in [(30, 90), (210, 90)]:
        a = math.radians(tip_deg)
        tx = cx + r * math.cos(a)
        ty = cy - r * math.sin(a)
        # Tangent direction (clockwise rotation)
        ta = math.radians(tip_deg - 90)
        fwd = (math.cos(ta) * 7, -math.sin(ta) * 7)
        perp = (-fwd[1] * 0.5, fwd[0] * 0.5)
        pts = [
            (tx + fwd[0], ty + fwd[1]),
            (tx - perp[0], ty - perp[1]),
            (tx + perp[0], ty + perp[1]),
        ]
        draw.polygon(pts, fill="white")
    return img


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("rclone Bisync Manager")
        self.geometry("960x660")
        self.minsize(720, 520)

        ok, msg = ensure_rclone_config()
        if not ok:
            messagebox.showerror(
                "rclone config error",
                f"Setup failed:\n{msg}\n\nThe app may not work correctly.",
            )

        self.config_data = load_config()
        self.sync_running = False
        self.selected_index = -1
        self._sync_job: str | None = None
        self._tray_icon: pystray.Icon | None = None

        _style_treeview()
        self._build_layout()
        self._refresh_table()
        self._schedule_sync()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_tray()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=2)
        self.grid_rowconfigure(3, weight=1)

        # Top controls
        ctrl = ctk.CTkFrame(self)
        ctrl.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 0))

        self._btn_add = ctk.CTkButton(ctrl, text="Add Pair", width=100, command=self._add_pair)
        self._btn_add.pack(side="left", padx=6, pady=8)

        self._btn_delete = ctk.CTkButton(
            ctrl, text="Delete Selected", width=120,
            fg_color="#555", hover_color="#444", command=self._delete_selected,
        )
        self._btn_delete.pack(side="left", padx=6, pady=8)

        self._btn_init = ctk.CTkButton(ctrl, text="Resync Selected", width=120, command=self._init_selected)
        self._btn_init.pack(side="left", padx=6, pady=8)

        ctk.CTkLabel(ctrl, text="  |  ", text_color="gray50").pack(side="left")

        ctk.CTkLabel(ctrl, text="Interval (min):").pack(side="left", padx=(6, 2))
        self.interval_var = tk.StringVar(value=str(self.config_data.sync_interval_minutes))
        entry = ctk.CTkEntry(ctrl, textvariable=self.interval_var, width=55)
        entry.pack(side="left", padx=(0, 6))
        entry.bind("<FocusOut>", self._on_interval_change)
        entry.bind("<Return>", self._on_interval_change)

        self._btn_run = ctk.CTkButton(ctrl, text="Run Now", width=90, command=self._run_now)
        self._btn_run.pack(side="left", padx=6, pady=8)

        self._btn_dry = ctk.CTkButton(
            ctrl, text="Dry Run", width=80,
            fg_color="#555", hover_color="#444", command=self._run_dry_run,
        )
        self._btn_dry.pack(side="left", padx=6, pady=8)

        self._btn_test = ctk.CTkButton(ctrl, text="Test Connection", width=130, command=self._test_connection)
        self._btn_test.pack(side="left", padx=6, pady=8)

        # Right-side buttons
        self._btn_config = ctk.CTkButton(
            ctrl, text="Config", width=80,
            fg_color="#555", hover_color="#444", command=self._open_config,
        )
        self._btn_config.pack(side="right", padx=6, pady=8)

        self._btn_prefs = ctk.CTkButton(
            ctrl, text="Preferences", width=100,
            fg_color="#555", hover_color="#444", command=self._open_preferences,
        )
        self._btn_prefs.pack(side="right", padx=6, pady=8)

        # Table
        table_frame = ctk.CTkFrame(self)
        table_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            table_frame,
            columns=("path1", "path2", "schedule", "init"),
            show="headings",
            style="App.Treeview",
            selectmode="browse",
        )
        self.tree.heading("path1", text="Path 1")
        self.tree.heading("path2", text="Path 2")
        self.tree.heading("schedule", text="Schedule")
        self.tree.heading("init", text="Resynced")
        self.tree.column("path1", width=310, stretch=True)
        self.tree.column("path2", width=310, stretch=True)
        self.tree.column("schedule", width=80, anchor="center", stretch=False)
        self.tree.column("init", width=90, anchor="center", stretch=False)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        # Log header
        log_header = ctk.CTkFrame(self, fg_color="transparent")
        log_header.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 0))
        ctk.CTkLabel(log_header, text="Log", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=4)
        ctk.CTkButton(
            log_header, text="Copy All", width=80,
            fg_color="#555", hover_color="#444", command=self._copy_all,
        ).pack(side="right", padx=4)
        ctk.CTkButton(
            log_header, text="Clear", width=60,
            fg_color="#555", hover_color="#444", command=self._clear_log,
        ).pack(side="right", padx=4)

        # Log area
        self.log_box = ctk.CTkTextbox(
            self, state="disabled",
            font=ctk.CTkFont(family="Consolas", size=10),
            wrap="none",
        )
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(4, 0))

        # Status bar (spinner)
        status_bar = ctk.CTkFrame(self, height=32, fg_color="transparent")
        status_bar.grid(row=4, column=0, sticky="ew", padx=12, pady=(4, 8))

        self._spinner = ctk.CTkProgressBar(status_bar, mode="indeterminate", width=180, height=10)
        self._spinner_label = ctk.CTkLabel(status_bar, text="", text_color="gray70", font=ctk.CTkFont(size=11))

    # ------------------------------------------------------------------
    # Spinner
    # ------------------------------------------------------------------

    def _start_spinner(self, msg: str) -> None:
        self._spinner_label.configure(text=msg)
        self._spinner.pack(side="left", padx=(0, 8))
        self._spinner_label.pack(side="left")
        self._spinner.start()
        for btn in (self._btn_run, self._btn_dry, self._btn_init, self._btn_test):
            btn.configure(state="disabled")

    def _stop_spinner(self) -> None:
        self._spinner.stop()
        self._spinner.pack_forget()
        self._spinner_label.pack_forget()
        self._spinner_label.configure(text="")
        for btn in (self._btn_run, self._btn_dry, self._btn_init, self._btn_test):
            btn.configure(state="normal")

    # ------------------------------------------------------------------
    # Table helpers
    # ------------------------------------------------------------------

    def _refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for p in self.config_data.pairs:
            self.tree.insert(
                "", "end",
                values=(
                    p.path1,
                    p.path2,
                    "✓" if p.enabled else "✗",
                    "Yes" if p.initialized else "No",
                ),
            )
        self.selected_index = -1

    def _on_select(self, _event: object = None) -> None:
        sel = self.tree.selection()
        if sel:
            self.selected_index = self.tree.index(sel[0])

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _copy_all(self) -> None:
        self.log_box.configure(state="normal")
        text = self.log_box.get("1.0", "end-1c")
        self.log_box.configure(state="disabled")
        self.clipboard_clear()
        self.clipboard_append(text)

    # ------------------------------------------------------------------
    # Sync timer
    # ------------------------------------------------------------------

    def _schedule_sync(self) -> None:
        if self._sync_job:
            self.after_cancel(self._sync_job)
        ms = self.config_data.sync_interval_minutes * 60 * 1000
        self._sync_job = self.after(ms, self._on_timer)

    def _on_timer(self) -> None:
        if not self.sync_running:
            self._run_sync_all()
        self._schedule_sync()

    # ------------------------------------------------------------------
    # Tray
    # ------------------------------------------------------------------

    def _setup_tray(self) -> None:
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._tray_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "rclone-bisync", _make_tray_image(), "rclone Bisync Manager", menu
        )
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _on_close(self) -> None:
        self.withdraw()

    def _tray_show(self, _icon: object = None, _item: object = None) -> None:
        self.after(0, self._show_window)

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_quit(self, _icon: object = None, _item: object = None) -> None:
        if self._tray_icon:
            self._tray_icon.stop()
        self.after(0, self.destroy)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_interval_change(self, _event: object = None) -> None:
        try:
            val = int(self.interval_var.get())
            if val > 0:
                self.config_data.sync_interval_minutes = val
                save_config(self.config_data)
                self._schedule_sync()
        except ValueError:
            pass

    def _open_config(self) -> None:
        open_rclone_config()
        self.log("Opened rclone config terminal.")

    def _test_connection(self) -> None:
        self.log("Testing connection...")
        self._start_spinner("Testing connection...")

        all_paths = [p for pair in self.config_data.pairs for p in [pair.path1, pair.path2]]
        result: list[tuple[bool, str] | None] = [None]

        def task() -> None:
            result[0] = test_connection(all_paths if all_paths else None)

        def done() -> None:
            if result[0] is None:
                self.after(50, done)
                return
            self._stop_spinner()
            ok, msg = result[0]
            self.log(f"Result: {msg}")

        threading.Thread(target=task, daemon=True).start()
        self.after(50, done)

    def _run_now(self) -> None:
        if not self.sync_running:
            self._run_sync_all()

    def _run_sync_all(self) -> None:
        self.sync_running = True
        self._start_spinner("Syncing...")

        log_q: queue.Queue[str | None] = queue.Queue()

        def task() -> None:
            log_q.put("--- Sync run started ---")

            enabled_pairs = [p for p in self.config_data.pairs if p.enabled and p.initialized]
            preflight_paths = [path for pair in enabled_pairs for path in [pair.path1, pair.path2]]
            ok, msg = test_connection(preflight_paths if preflight_paths else None)
            if not ok:
                log_q.put(f"Pre-flight failed: {msg}. Skipping this cycle.")
                log_q.put(None)
                return

            pairs = self.config_data.pairs
            log_q.put(f"Connection OK. Syncing {len(pairs)} pair(s)...")
            log_lines: list[str] = []
            any_changed = False

            for pair in pairs:
                if not pair.enabled:
                    log_q.put(f"  [{pair.path1}] Skipped (disabled)")
                    continue
                if not pair.initialized:
                    log_q.put(f"  [{pair.path1}] Skipped (not resynced)")
                    continue

                log_q.put(f"  [{pair.path1}] Running bisync...")
                result = run_bisync(pair, extra_flags=self.config_data.bisync_flags or None)
                if result.success:
                    log_q.put(f"  [{pair.path1}] OK")
                else:
                    err = result.error.splitlines()[0] if result.error else "unknown"
                    log_q.put(f"  [{pair.path1}] Error: {err}")
                    if "resync" in result.error.lower():
                        pair.initialized = False
                        any_changed = True
                        log_q.put(f"  [{pair.path1}] Marked as needs resync")

                log_lines.append(f"=== {pair.path1} <-> {pair.path2} ({result.timestamp}) ===")
                if result.output:
                    log_lines.append(result.output)
                if result.error:
                    log_lines.append(result.error)

            if any_changed:
                save_config(self.config_data)
                self.after(0, self._refresh_table)

            try:
                with open(DEFAULT_LOG_PATH, "w", encoding="utf-8") as f:
                    f.write("\n".join(log_lines))
            except OSError:
                pass

            log_q.put("--- Sync run finished ---")
            log_q.put(None)  # sentinel

        def drain() -> None:
            try:
                while True:
                    msg = log_q.get_nowait()
                    if msg is None:
                        self._stop_spinner()
                        self.sync_running = False
                        return
                    self.log(msg)
            except queue.Empty:
                pass
            self.after(100, drain)

        threading.Thread(target=task, daemon=True).start()
        self.after(100, drain)

    def _on_tree_click(self, event: tk.Event) -> None:
        """Toggle schedule flag when clicking the Schedule column."""
        col_id = self.tree.identify_column(event.x)
        if col_id != "#3":
            return
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        idx = self.tree.index(row_id)
        if 0 <= idx < len(self.config_data.pairs):
            pair = self.config_data.pairs[idx]
            pair.enabled = not pair.enabled
            save_config(self.config_data)
            self._refresh_table()
            # Restore selection
            children = self.tree.get_children()
            if idx < len(children):
                self.tree.selection_set(children[idx])
                self.selected_index = idx

    def _run_dry_run(self) -> None:
        if self.sync_running:
            return
        self.sync_running = True
        self._start_spinner("Dry run...")

        log_q: queue.Queue[str | None] = queue.Queue()

        def task() -> None:
            log_q.put("--- Dry run started ---")
            pairs = [p for p in self.config_data.pairs if p.enabled and p.initialized]
            if not pairs:
                log_q.put("No enabled+resynced pairs to dry-run.")
                log_q.put(None)
                return

            flags = self.config_data.bisync_flags + ["--dry-run"]
            for pair in pairs:
                log_q.put(f"  [{pair.path1}] Dry run...")
                result = run_bisync(pair, extra_flags=flags)
                if result.success:
                    log_q.put(f"  [{pair.path1}] Dry run OK")
                else:
                    err = result.error.splitlines()[0] if result.error else "unknown"
                    log_q.put(f"  [{pair.path1}] Dry run error: {err}")
                if result.output:
                    log_q.put(result.output.rstrip())
                if result.error and not result.success:
                    log_q.put(result.error.rstrip())
            log_q.put("--- Dry run finished ---")
            log_q.put(None)

        def drain() -> None:
            try:
                while True:
                    msg = log_q.get_nowait()
                    if msg is None:
                        self._stop_spinner()
                        self.sync_running = False
                        return
                    self.log(msg)
            except queue.Empty:
                pass
            self.after(100, drain)

        threading.Thread(target=task, daemon=True).start()
        self.after(100, drain)

    def _open_preferences(self) -> None:
        PreferencesDialog(self, self.config_data)
        save_config(self.config_data)

    def _on_tree_right_click(self, event: tk.Event) -> None:
        row_id = self.tree.identify_row(event.y)
        if not row_id:
            return
        col_id = self.tree.identify_column(event.x)
        self.tree.selection_set(row_id)
        self.selected_index = self.tree.index(row_id)
        pair = self.config_data.pairs[self.selected_index]

        # Determine which path column(s) to target
        if col_id == "#1":
            targets = [("Path 1", "path1", pair.path1)]
        elif col_id == "#2":
            targets = [("Path 2", "path2", pair.path2)]
        else:
            targets = [("Path 1", "path1", pair.path1), ("Path 2", "path2", pair.path2)]

        menu = tk.Menu(self, tearoff=0)
        for label, field, path in targets:
            menu.add_command(
                label=f"Open {label}",
                command=lambda p=path: self._open_path(p),
                state="normal" if not _is_rclone_remote_path(path) else "disabled",
            )
        menu.add_separator()
        for label, field, path in targets:
            menu.add_command(
                label=f"Edit {label}",
                command=lambda idx=self.selected_index, f=field: self._edit_path(idx, f),
            )
        menu.tk_popup(event.x_root, event.y_root)

    def _open_path(self, path: str) -> None:
        try:
            os.startfile(path)
        except Exception as e:
            messagebox.showerror("Open Failed", str(e))

    def _edit_path(self, pair_index: int, field: str) -> None:
        if not (0 <= pair_index < len(self.config_data.pairs)):
            return
        pair = self.config_data.pairs[pair_index]
        current = getattr(pair, field)
        new_path = EditPathDialog(self, current).result
        if new_path and new_path != current:
            setattr(pair, field, new_path)
            pair.initialized = False
            save_config(self.config_data)
            self._refresh_table()
            self.log(f"Updated {field}: {new_path} (marked as needs resync)")

    def _add_pair(self) -> None:
        pair = AddPairDialog(self).result
        if pair:
            self.config_data.pairs.append(pair)
            save_config(self.config_data)
            self._refresh_table()
            self.log(f"Added pair: {pair.path1} <-> {pair.path2}")

    def _delete_selected(self) -> None:
        if 0 <= self.selected_index < len(self.config_data.pairs):
            removed = self.config_data.pairs.pop(self.selected_index)
            save_config(self.config_data)
            self._refresh_table()
            self.log(f"Deleted pair: {removed.path1} <-> {removed.path2}")
        else:
            messagebox.showwarning("No selection", "Select a pair first.")

    def _init_selected(self) -> None:
        if not (0 <= self.selected_index < len(self.config_data.pairs)):
            messagebox.showwarning("No selection", "Select a pair first.")
            return

        pair = self.config_data.pairs[self.selected_index]
        if pair.initialized:
            if not messagebox.askyesno(
                "Confirm Resync",
                f"'{pair.path1}' is already resynced.\nRe-run --resync? This can cause data loss.",
            ):
                return

        self.log(f"Resyncing [{pair.path1}]...")
        ensure_local_path(pair.path1)
        ensure_local_path(pair.path2)
        self._start_spinner(f"Resyncing {pair.path1}...")

        log_q: queue.Queue[str | None] = queue.Queue()
        success_holder: list[bool | None] = [None]

        def task() -> None:
            ok, msg = test_connection([pair.path1, pair.path2])
            if not ok:
                log_q.put(f"Connection failed: {msg}")
                log_q.put(None)
                return

            result = run_bisync(pair, resync=True)
            success_holder[0] = result.success
            if result.success:
                log_q.put(f"Resync OK for [{pair.path1}]")
            else:
                err = result.error.splitlines()[0] if result.error else "unknown"
                log_q.put(f"Resync failed: {err}")
            if result.output:
                log_q.put(result.output.rstrip())
            if result.error and not result.success:
                log_q.put(result.error.rstrip())
            log_q.put(None)

        def drain() -> None:
            try:
                while True:
                    msg = log_q.get_nowait()
                    if msg is None:
                        self._stop_spinner()
                        if success_holder[0]:
                            pair.initialized = True
                            save_config(self.config_data)
                            self._refresh_table()
                        return
                    self.log(msg)
            except queue.Empty:
                pass
            self.after(100, drain)

        threading.Thread(target=task, daemon=True).start()
        self.after(100, drain)


# ------------------------------------------------------------------
# Dialogs
# ------------------------------------------------------------------


class AddPairDialog(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk) -> None:
        super().__init__(parent)
        self.title("Add Sync Pair")
        self.geometry("700x180")
        self.resizable(False, False)
        self.grab_set()
        self.result: SyncPair | None = None

        self.grid_columnconfigure(1, weight=1)

        # Path 1
        ctk.CTkLabel(self, text="Path 1:").grid(row=0, column=0, padx=12, pady=(16, 6), sticky="w")
        self.path1_var = tk.StringVar()
        ctk.CTkEntry(self, textvariable=self.path1_var).grid(row=0, column=1, padx=8, pady=(16, 6), sticky="ew")
        ctk.CTkButton(self, text="Browse Local", width=105, command=self._browse_local_1).grid(
            row=0, column=2, padx=(0, 4), pady=(16, 6)
        )
        ctk.CTkButton(self, text="Browse Remote", width=115, command=self._browse_remote_1).grid(
            row=0, column=3, padx=(0, 12), pady=(16, 6)
        )

        # Path 2
        ctk.CTkLabel(self, text="Path 2:").grid(row=1, column=0, padx=12, pady=6, sticky="w")
        self.path2_var = tk.StringVar()
        ctk.CTkEntry(self, textvariable=self.path2_var).grid(row=1, column=1, padx=8, pady=6, sticky="ew")
        ctk.CTkButton(self, text="Browse Local", width=105, command=self._browse_local_2).grid(
            row=1, column=2, padx=(0, 4), pady=6
        )
        ctk.CTkButton(self, text="Browse Remote", width=115, command=self._browse_remote_2).grid(
            row=1, column=3, padx=(0, 12), pady=6
        )

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=4, pady=14)
        ctk.CTkButton(btn_frame, text="OK", width=90, command=self._ok).pack(side="left", padx=8)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=90,
            fg_color="#555", hover_color="#444", command=self.destroy,
        ).pack(side="left", padx=8)

        self.wait_window()

    def _browse_local_1(self) -> None:
        path = filedialog.askdirectory(parent=self)
        if path:
            self.path1_var.set(path)

    def _browse_remote_1(self) -> None:
        path = BrowseRemoteDialog(self).result
        if path is not None:
            self.path1_var.set(path)

    def _browse_local_2(self) -> None:
        path = filedialog.askdirectory(parent=self)
        if path:
            self.path2_var.set(path)

    def _browse_remote_2(self) -> None:
        path = BrowseRemoteDialog(self).result
        if path is not None:
            self.path2_var.set(path)

    def _ok(self) -> None:
        path1 = normalize_path(self.path1_var.get().strip())
        path2 = normalize_path(self.path2_var.get().strip())
        if path1 and path2:
            self.result = SyncPair(path1=path1, path2=path2)
            self.destroy()
        else:
            messagebox.showwarning("Missing fields", "Both fields are required.", parent=self)


class BrowseRemoteDialog(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTkToplevel) -> None:
        super().__init__(parent)
        self.title("Browse Remote")
        self.geometry("500x460")
        self.grab_set()
        self.result: str | None = None
        self._phase = "remotes"  # "remotes" | "dirs"
        self._selected_remote = ""
        self._current_path = ""  # e.g. "tecnico:" or "tecnico:subdir"

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.path_label = ctk.CTkLabel(self, text="Select a remote:", anchor="w")
        self.path_label.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))

        list_frame = ctk.CTkFrame(self)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=12, pady=4)
        list_frame.grid_columnconfigure(0, weight=1)
        list_frame.grid_rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(
            list_frame,
            bg="#2b2b2b", fg="#dce4ee", selectbackground="#1f538d",
            font=("Segoe UI", 10), borderwidth=0, highlightthickness=0, activestyle="none",
        )
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.listbox.bind("<Double-Button-1>", lambda _: self._open())

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, pady=(4, 0))
        ctk.CTkButton(btn_frame, text="Open", width=80, command=self._open).pack(side="left", padx=5)
        self._btn_back = ctk.CTkButton(
            btn_frame, text="Back", width=70,
            fg_color="#555", hover_color="#444", command=self._back,
            state="disabled",
        )
        self._btn_back.pack(side="left", padx=5)
        self._btn_select = ctk.CTkButton(
            btn_frame, text="Select This Folder", command=self._select,
            state="disabled",
        )
        self._btn_select.pack(side="left", padx=5)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=70,
            fg_color="#555", hover_color="#444", command=self.destroy,
        ).pack(side="left", padx=5)

        # Spinner bar at the bottom of the dialog
        spinner_frame = ctk.CTkFrame(self, fg_color="transparent", height=28)
        spinner_frame.grid(row=3, column=0, sticky="ew", padx=12, pady=(2, 8))
        self._spinner = ctk.CTkProgressBar(spinner_frame, mode="indeterminate", height=8)
        self._spinner_label = ctk.CTkLabel(spinner_frame, text="", text_color="gray70", font=ctk.CTkFont(size=11))

        self._load_remotes()
        self.wait_window()

    def _start_spinner(self) -> None:
        self._spinner_label.configure(text="Loading...")
        self._spinner.pack(side="left", padx=(0, 8))
        self._spinner_label.pack(side="left")
        self._spinner.start()

    def _stop_spinner(self) -> None:
        self._spinner.stop()
        self._spinner.pack_forget()
        self._spinner_label.pack_forget()

    def _set_phase(self, phase: str) -> None:
        self._phase = phase
        if phase == "remotes":
            self._btn_back.configure(state="disabled")
            self._btn_select.configure(state="disabled")
        else:
            self._btn_back.configure(state="normal")
            self._btn_select.configure(state="normal")

    def _load_remotes(self) -> None:
        self._set_phase("remotes")
        self.path_label.configure(text="Select a remote:")
        self._start_spinner()
        remotes_holder: list[list[str] | None] = [None]

        def task() -> None:
            remotes_holder[0] = list_remotes()

        def done() -> None:
            if remotes_holder[0] is None:
                self.after(50, done)
                return
            self._stop_spinner()
            remotes = remotes_holder[0]
            self.listbox.delete(0, "end")
            if remotes:
                for r in remotes:
                    self.listbox.insert("end", r)
            else:
                self.listbox.insert("end", "(no remotes found)")

        threading.Thread(target=task, daemon=True).start()
        self.after(50, done)

    def _load_dirs(self) -> None:
        self._set_phase("dirs")
        self.path_label.configure(text=f"Current: {self._current_path}")
        self._start_spinner()
        result_holder: list[tuple[bool, list[str]] | None] = [None]

        def task() -> None:
            result_holder[0] = list_remote_dirs(self._current_path)

        def done() -> None:
            if result_holder[0] is None:
                self.after(50, done)
                return
            self._stop_spinner()
            ok, dirs = result_holder[0]
            if not ok:
                messagebox.showerror("Error", f"Cannot list: {self._current_path}", parent=self)
                return
            self.listbox.delete(0, "end")
            for d in dirs:
                self.listbox.insert("end", d)

        threading.Thread(target=task, daemon=True).start()
        self.after(50, done)

    def _open(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self.listbox.get(sel[0])
        if self._phase == "remotes":
            if name == "(no remotes found)":
                return
            self._selected_remote = name
            self._current_path = f"{name}:"
            self._load_dirs()
        else:
            if self._current_path.endswith(":"):
                self._current_path = f"{self._current_path}{name}"
            else:
                self._current_path = f"{self._current_path}/{name}"
            self._load_dirs()

    def _back(self) -> None:
        if self._phase != "dirs":
            return
        if self._current_path.endswith(":"):
            # At root of remote — go back to remotes list
            self._load_remotes()
        else:
            base, sub = self._current_path.split(":", 1)
            if "/" in sub:
                parent = sub.rsplit("/", 1)[0]
                self._current_path = f"{base}:{parent}"
            else:
                self._current_path = f"{base}:"
            self._load_dirs()

    def _select(self) -> None:
        self.result = self._current_path
        self.destroy()


class EditPathDialog(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk, current_path: str) -> None:
        super().__init__(parent)
        self.title("Edit Path")
        self.geometry("700x110")
        self.resizable(False, False)
        self.grab_set()
        self.result: str | None = None

        self.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(self, text="Path:").grid(row=0, column=0, padx=12, pady=(16, 6), sticky="w")
        self.path_var = tk.StringVar(value=current_path)
        ctk.CTkEntry(self, textvariable=self.path_var).grid(row=0, column=1, padx=8, pady=(16, 6), sticky="ew")
        ctk.CTkButton(self, text="Browse Local", width=105, command=self._browse_local).grid(
            row=0, column=2, padx=(0, 4), pady=(16, 6)
        )
        ctk.CTkButton(self, text="Browse Remote", width=115, command=self._browse_remote).grid(
            row=0, column=3, padx=(0, 12), pady=(16, 6)
        )

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=1, column=0, columnspan=4, pady=10)
        ctk.CTkButton(btn_frame, text="OK", width=90, command=self._ok).pack(side="left", padx=8)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=90,
            fg_color="#555", hover_color="#444", command=self.destroy,
        ).pack(side="left", padx=8)

        self.wait_window()

    def _browse_local(self) -> None:
        path = filedialog.askdirectory(parent=self)
        if path:
            self.path_var.set(path)

    def _browse_remote(self) -> None:
        path = BrowseRemoteDialog(self).result
        if path is not None:
            self.path_var.set(path)

    def _ok(self) -> None:
        path = normalize_path(self.path_var.get().strip())
        if path:
            self.result = path
            self.destroy()
        else:
            messagebox.showwarning("Missing field", "Path is required.", parent=self)


class PreferencesDialog(ctk.CTkToplevel):
    def __init__(self, parent: ctk.CTk, config: "AppConfig") -> None:
        super().__init__(parent)
        self.title("Preferences")
        self.geometry("480x420")
        self.resizable(False, False)
        self.grab_set()
        self._config = config

        self.grid_columnconfigure(0, weight=1)

        # --- Launch at startup ---
        startup_frame = ctk.CTkFrame(self)
        startup_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        ctk.CTkLabel(
            startup_frame, text="System", font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(8, 4))
        self._startup_var = tk.BooleanVar(value=is_startup_enabled())
        ctk.CTkCheckBox(
            startup_frame,
            text="Launch at Windows startup",
            variable=self._startup_var,
            onvalue=True, offvalue=False,
        ).pack(anchor="w", padx=14, pady=(0, 10))

        # --- Bisync flags ---
        flags_frame = ctk.CTkFrame(self)
        flags_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            flags_frame, text="Extra bisync flags", font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(8, 4))

        self._flag_vars: dict[str, tk.BooleanVar] = {}
        for flag, label, desc in BISYNC_FLAG_OPTIONS:
            var = tk.BooleanVar(value=flag in config.bisync_flags)
            self._flag_vars[flag] = var
            row = ctk.CTkFrame(flags_frame, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=2)
            ctk.CTkCheckBox(row, text=label, variable=var, onvalue=True, offvalue=False, width=160).pack(side="left")
            ctk.CTkLabel(row, text=desc, text_color="gray60", font=ctk.CTkFont(size=11)).pack(side="left", padx=(8, 0))

        # --- Buttons ---
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=2, column=0, pady=14)
        ctk.CTkButton(btn_frame, text="OK", width=90, command=self._ok).pack(side="left", padx=8)
        ctk.CTkButton(
            btn_frame, text="Cancel", width=90,
            fg_color="#555", hover_color="#444", command=self.destroy,
        ).pack(side="left", padx=8)

        self.wait_window()

    def _ok(self) -> None:
        # Apply startup setting
        try:
            set_startup(self._startup_var.get())
        except OSError as e:
            messagebox.showerror("Startup Error", str(e), parent=self)
            return
        # Apply flags
        self._config.bisync_flags = [f for f, v in self._flag_vars.items() if v.get()]
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()
