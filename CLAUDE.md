# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Goal

A Python desktop UI tool to manage `rclone bisync` jobs between a local folder and a Samba share (remote). The remote can be unreliable (disconnects). The tool should:

- Let users define and manage source/destination folder pairs
- Handle the init step (create local path if missing, run `rclone bisync --resync` on first run)
- Run bisync for all pairs on a schedule
- Show a log of the last sync run

## Commands

This project uses `uv` for environment and dependency management (Python 3.14).

```bash
# Install dependencies / set up environment
uv sync

# Run the app
uv run rclone-setup        # via entry point
uv run -m rclone_setup     # via __main__.py

# Add a dependency
uv add <package>
```

## Architecture

Uses `src` layout with a `rclone_setup` package:

```
src/rclone_setup/
  __init__.py       — package marker
  __main__.py       — `python -m rclone_setup` entry point
  app.py            — FreeSimpleGUI UI, event loop, timer
  sync_engine.py    — rclone subprocess wrapper (bisync, test_connection, browse)
  config.py         — SyncPair/AppConfig dataclasses, JSON persistence
```

- **`app.py`** — main window with pairs table, controls, log area; timer-based auto-sync
- **`sync_engine.py`** — wraps `rclone bisync` via subprocess, with `--resync` for init and `--resilient --recover` for regular runs
- **`config.py`** — config stored in `%APPDATA%/rclone-setup/config.json`; log in same directory
