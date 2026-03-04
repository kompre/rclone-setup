from __future__ import annotations

import os
from datetime import datetime

import FreeSimpleGUI as sg

from rclone_setup.config import (
    DEFAULT_LOG_PATH,
    AppConfig,
    SyncPair,
    load_config,
    save_config,
)
from rclone_setup.sync_engine import (
    ensure_local_path,
    list_remote_dirs,
    normalize_remote_path,
    run_bisync,
    test_connection,
)


def pairs_table_data(config: AppConfig) -> list[list[str]]:
    rows = []
    for p in config.pairs:
        status = "Enabled" if p.enabled else "Disabled"
        init = "Yes" if p.initialized else "No"
        rows.append([p.remote_path, p.local_path, status, init])
    return rows


def log(window: sg.Window, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    widget = window["-LOG-"].Widget
    widget.configure(state="normal")
    widget.insert("end", line)
    widget.see("end")
    widget.configure(state="disabled")


def build_layout(config: AppConfig) -> list:
    table_data = pairs_table_data(config)
    headings = ["Remote Path", "Local Path", "Status", "Initialized"]

    table = sg.Table(
        values=table_data if table_data else [["", "", "", ""]],
        headings=headings,
        key="-TABLE-",
        auto_size_columns=False,
        col_widths=[30, 30, 10, 10],
        justification="left",
        num_rows=8,
        enable_events=True,
        select_mode=sg.TABLE_SELECT_MODE_BROWSE,
    )

    controls = [
        [
            sg.Button("Add Pair"),
            sg.Button("Delete Selected"),
            sg.Button("Init Selected"),
            sg.VSeparator(),
            sg.Text("Interval (min):"),
            sg.Input(
                str(config.sync_interval_minutes),
                key="-INTERVAL-",
                size=(5, 1),
                enable_events=True,
            ),
            sg.Button("Run Now"),
            sg.Button("Test Connection"),
        ],
    ]

    log_area = sg.Multiline(
        size=(90, 12),
        key="-LOG-",
        autoscroll=True,
        disabled=False,
        font=("Consolas", 9),
        right_click_menu=["", ["Copy"]],
    )

    log_controls = [
        [
            sg.Button("Clear Log"),
            sg.Button("Copy All"),
        ],
    ]

    return [
        [table],
        *controls,
        [sg.HorizontalSeparator()],
        [log_area],
        *log_controls,
    ]


def refresh_table(window: sg.Window, config: AppConfig) -> None:
    data = pairs_table_data(config)
    window["-TABLE-"].update(values=data if data else [["", "", "", ""]])


def browse_remote_popup() -> str | None:
    """Popup that lets the user navigate the remote directory tree."""
    current_path = ""
    ok, dirs = list_remote_dirs(current_path)
    if not ok:
        sg.popup_error("Cannot connect to remote.")
        return None

    layout = [
        [sg.Text("Current: /", key="-CUR-", size=(60, 1))],
        [sg.Listbox(dirs, size=(60, 15), key="-DIRS-", enable_events=True)],
        [
            sg.Button("Open"),
            sg.Button("Up"),
            sg.Button("Select This Folder"),
            sg.Button("Cancel"),
        ],
    ]
    win = sg.Window("Browse Remote", layout, modal=True)
    result_path = None

    while True:
        event, values = win.read()
        if event in (sg.WINDOW_CLOSED, "Cancel"):
            break

        if event in ("Open", "-DIRS-"):
            sel = values["-DIRS-"]
            if not sel:
                continue
            new_path = f"{current_path}/{sel[0]}" if current_path else sel[0]
            ok, subdirs = list_remote_dirs(new_path)
            if ok:
                current_path = new_path
                win["-CUR-"].update(f"Current: /{current_path}")
                win["-DIRS-"].update(subdirs)
            else:
                sg.popup_error("Cannot list that directory.")

        elif event == "Up":
            if "/" in current_path:
                current_path = current_path.rsplit("/", 1)[0]
            else:
                current_path = ""
            ok, dirs = list_remote_dirs(current_path)
            if ok:
                win["-CUR-"].update(f"Current: /{current_path}" if current_path else "Current: /")
                win["-DIRS-"].update(dirs)

        elif event == "Select This Folder":
            result_path = current_path
            break

    win.close()
    return result_path


def add_pair_popup() -> SyncPair | None:
    layout = [
        [sg.Text("Remote path (you can paste a Windows/UNC path):")],
        [
            sg.Input(key="-REMOTE-", size=(50, 1)),
            sg.Button("Browse Remote"),
        ],
        [sg.Text("Local path:")],
        [sg.Input(key="-LOCAL-", size=(50, 1)), sg.FolderBrowse()],
        [sg.Button("OK"), sg.Button("Cancel")],
    ]
    win = sg.Window("Add Sync Pair", layout, modal=True)
    pair = None
    while True:
        event, values = win.read()
        if event in (sg.WINDOW_CLOSED, "Cancel"):
            break
        if event == "Browse Remote":
            remote = browse_remote_popup()
            if remote is not None:
                win["-REMOTE-"].update(remote)
        elif event == "OK":
            remote = normalize_remote_path(values["-REMOTE-"])
            local = values["-LOCAL-"].strip()
            if remote and local:
                pair = SyncPair(remote_path=remote, local_path=local)
                break
            sg.popup("Both fields are required.")
    win.close()
    return pair


def run_sync_all(window: sg.Window, config: AppConfig) -> None:
    log(window, "--- Sync run started ---")

    ok, msg = test_connection()
    if not ok:
        log(window, f"Pre-flight failed: {msg}. Skipping this cycle.")
        return

    log(window, f"Connection OK. Syncing {len(config.pairs)} pair(s)...")
    log_lines: list[str] = []
    any_changed = False

    for pair in config.pairs:
        if not pair.enabled:
            log(window, f"  [{pair.remote_path}] Skipped (disabled)")
            continue
        if not pair.initialized:
            log(window, f"  [{pair.remote_path}] Skipped (not initialized)")
            continue

        log(window, f"  [{pair.remote_path}] Running bisync...")
        result = run_bisync(pair)
        if result.success:
            log(window, f"  [{pair.remote_path}] OK")
        else:
            log(window, f"  [{pair.remote_path}] Error: {result.error.splitlines()[0] if result.error else 'unknown'}")
            if "resync" in result.error.lower():
                pair.initialized = False
                any_changed = True
                log(window, f"  [{pair.remote_path}] Marked as needs resync")
        log_lines.append(f"=== {pair.remote_path} ({result.timestamp}) ===")
        if result.output:
            log_lines.append(result.output)
        if result.error:
            log_lines.append(result.error)

    if any_changed:
        save_config(config)
        refresh_table(window, config)

    try:
        with open(DEFAULT_LOG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))
    except OSError:
        pass

    log(window, "--- Sync run finished ---")


def init_selected(window: sg.Window, config: AppConfig, index: int) -> None:
    if index < 0 or index >= len(config.pairs):
        return
    pair = config.pairs[index]

    if pair.initialized:
        if sg.popup_yes_no(
            f"'{pair.remote_path}' is already initialized.\nRe-run --resync? This can cause data loss.",
            title="Confirm Resync",
        ) != "Yes":
            return

    log(window, f"Initializing [{pair.remote_path}]...")
    ensure_local_path(pair.local_path)

    ok, msg = test_connection()
    if not ok:
        log(window, f"Connection failed: {msg}")
        return

    result = run_bisync(pair, resync=True)
    if result.success:
        pair.initialized = True
        save_config(config)
        refresh_table(window, config)
        log(window, f"Init OK for [{pair.remote_path}]")
    else:
        log(window, f"Init failed: {result.error.splitlines()[0] if result.error else 'unknown'}")
    if result.output:
        log(window, result.output.rstrip())
    if result.error and not result.success:
        log(window, result.error.rstrip())


def main() -> None:
    sg.theme("SystemDefault1")
    config = load_config()

    window = sg.Window(
        "rclone Bisync Manager",
        build_layout(config),
        finalize=True,
        resizable=True,
    )

    # Make log area read-only but allow selection/copy via tkinter
    log_widget = window["-LOG-"].Widget
    log_widget.configure(state="disabled")

    interval_ms = config.sync_interval_minutes * 60 * 1000
    selected_row: int = -1
    sync_running = False

    while True:
        event, values = window.read(timeout=interval_ms)

        if event == sg.WINDOW_CLOSED:
            break

        if event == "__TIMEOUT__":
            if not sync_running:
                sync_running = True
                run_sync_all(window, config)
                refresh_table(window, config)
                sync_running = False
            continue

        if event == "-TABLE-" and values["-TABLE-"]:
            selected_row = values["-TABLE-"][0]

        elif event == "Test Connection":
            log(window, "Testing connection...")
            ok, msg = test_connection()
            log(window, f"Result: {msg}")

        elif event == "Add Pair":
            pair = add_pair_popup()
            if pair:
                config.pairs.append(pair)
                save_config(config)
                refresh_table(window, config)
                log(window, f"Added pair: {pair.remote_path} <-> {pair.local_path}")

        elif event == "Delete Selected":
            if 0 <= selected_row < len(config.pairs):
                removed = config.pairs.pop(selected_row)
                save_config(config)
                refresh_table(window, config)
                selected_row = -1
                log(window, f"Deleted pair: {removed.remote_path}")
            else:
                sg.popup("Select a pair first.")

        elif event == "Init Selected":
            if 0 <= selected_row < len(config.pairs):
                init_selected(window, config, selected_row)
            else:
                sg.popup("Select a pair first.")

        elif event == "Run Now":
            if not sync_running:
                sync_running = True
                run_sync_all(window, config)
                refresh_table(window, config)
                sync_running = False

        elif event == "Copy":
            try:
                widget = window["-LOG-"].Widget
                sel = widget.get("sel.first", "sel.last")
                window.TKroot.clipboard_clear()
                window.TKroot.clipboard_append(sel)
            except Exception:
                pass

        elif event == "Copy All":
            widget = window["-LOG-"].Widget
            widget.configure(state="normal")
            text = widget.get("1.0", "end-1c")
            widget.configure(state="disabled")
            window.TKroot.clipboard_clear()
            window.TKroot.clipboard_append(text)

        elif event == "Clear Log":
            widget = window["-LOG-"].Widget
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.configure(state="disabled")

        elif event == "-INTERVAL-":
            try:
                val = int(values["-INTERVAL-"])
                if val > 0:
                    config.sync_interval_minutes = val
                    interval_ms = val * 60 * 1000
                    save_config(config)
            except ValueError:
                pass

    window.close()
