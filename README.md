# rclone Bisync Manager

A Windows desktop UI to manage `rclone bisync` jobs between any two paths — local folders, SMB shares, or any rclone remote.

---

## Features

- **Generic path pairs** — sync local↔local, local↔remote, or remote↔remote
- **UNC & mapped drive auto-conversion** — paste `\\server\share\sub` or `T:\sub` and the app resolves it to the matching rclone remote automatically
- **Schedule toggle per pair** — each pair has a ✓/✗ toggle; only enabled pairs run on the timer
- **Resync** — first-run `--resync` per pair; creates local directories automatically if missing
- **Dry run** — preview what would change without touching any files
- **Real-time log** — `--verbose` output streams line-by-line as bisync runs; no timeout
- **System tray** — closing the window hides to tray; right-click → Show / Quit
- **rclone Config button** — opens `rclone config` in a terminal directly from the UI
- **Preferences**
  - Launch at Windows startup
  - Bisync flags (check-access, force, fast-list, …)
  - Performance tuning (transfers, checkers, buffer-size, multi-thread-streams) with suggested values auto-detected from CPU cores and RAM
- **Right-click table rows** — Open or Edit Path 1 / Path 2 inline

---

## Requirements

- Python 3.14+
- [rclone](https://rclone.org/downloads/) in `PATH`
- `uv` for environment management

---

## Setup

```bash
# Install dependencies
uv sync

# Run
uv run rclone-setup
```

On first launch the app copies your existing system rclone config to `%APPDATA%\rclone-setup\rclone.conf` so it has its own isolated config. Use the **Config** button to manage remotes from inside the app.

---

## Usage

### Add a sync pair

Click **Add Pair**. Each row has:
- **Path 1 / Path 2** — any valid rclone path or local folder
- **Browse Local** — folder picker
- **Browse Remote** — two-phase browser: pick a remote, then navigate its directories

Pasting a UNC path (`\\server\share\sub`) or mapped drive (`T:\sub`) into the entry box auto-converts it to `remote:sub` if a matching SMB remote exists in the config.

### Resync (first run)

Select a pair and click **Resync Selected**. This runs `rclone bisync --resync` to establish the initial state. Must be done once before a pair can be included in scheduled runs.

### Scheduled sync

Set the **Interval (min)** field. Only pairs with **Schedule = ✓** are included. Click **Run Now** to trigger immediately.

### Dry Run

Click **Dry Run** to see what would be transferred/deleted without making changes. Output streams to the log in real-time.

### Preferences

Click **Preferences** (top-right) to configure:

| Section | Options |
|---|---|
| System | Launch at Windows startup |
| Bisync flags | `--check-access`, `--force`, `--fast-list`, `--create-empty-src-dirs`, `--no-cleanup`, `--ignore-case`, `--fix-case` |
| Performance | `--transfers`, `--checkers`, `--buffer-size`, `--multi-thread-streams` — pre-filled with values suggested for your machine |

Click **Use suggested** in the Performance section to auto-fill values based on detected CPU count and RAM. Clear any field to fall back to rclone's built-in default.

---

## Config & data locations

| File | Path |
|---|---|
| App config (pairs, flags, interval) | `%APPDATA%\rclone-setup\config.json` |
| rclone config | `%APPDATA%\rclone-setup\rclone.conf` |
| Last sync log | `%APPDATA%\rclone-setup\last_run.log` |
| bisync cache | `%APPDATA%\rclone-setup\cache\` |

---

## Development

```bash
# Add a dependency
uv add <package>

# Entry points
uv run rclone-setup        # via script entry point
uv run -m rclone_setup     # via __main__.py
```

### Project layout

```
src/rclone_setup/
  app.py          — UI (customtkinter), event loop, tray, dialogs
  sync_engine.py  — rclone subprocess wrapper (bisync, browse, config)
  config.py       — SyncPair / AppConfig dataclasses, JSON persistence
  __main__.py     — python -m entry point
```
