# Gorgon Bet Tracker

A small always-on-top overlay for **Project Gorgon**'s Betting Arena in the Red Wing Casino.
It tails the game's `player.log` and `ChatLogs`, fetches crowd-sourced odds from
Firebase, predicts the likely winner, plays a sound when a fight starts, and
records every bet you place in a local SQLite database with a web stats UI.

These odds are setup in https://kaeus.github.io/GorgonCraftingTools/crookedColiseum.html
and available for each server.

## Install

Grab the latest `GorgonBetTracker.exe` from the
[Releases page](../../releases/latest) and double-click to run. No install
required — settings and the bet database live next to the exe.

## Run from source

```bash
pip install requests
python fight_monitor.py
```

Python 3.11+ recommended. `tkinter` and `sqlite3` ship with standard Python on
Windows.

## Settings

On first run the app writes a `settings.json` next to the executable. Edit it
to point at your game install if the defaults are wrong:

```json
{
  "log_file": "%LOCALAPPDATA%\\..\\LocalLow\\Elder Game\\Project Gorgon\\player.log",
  "chat_log_dir": "%LOCALAPPDATA%\\..\\LocalLow\\Elder Game\\Project Gorgon\\ChatLogs",
  "server": "Dreva",
  "project_id": "gorgon-crafting-tools",
  "stats_port": 8731
}
```

`%VAR%` environment variables are expanded. `%LOCALAPPDATA%\..\LocalLow` is the
standard `LocalLow` path on Windows.

Most notable you'll want to change the Server if you are not on Dreva

## Stats page

While running, open http://127.0.0.1:8731/ for a live table of your bet
history, win rate, and totals by matchup and advantage tier. Change the port
via `stats_port` in `settings.json`. You can also click the `stats ↗` on the
overlay to visit this.

## Overlay

The overlay only shows while Project Gorgon has focus. Drag it with the mouse,
click `stats ↗` to open the stats page in your browser, or `✕` to close.
Position is not persisted between runs.

## Building locally

```bash
pip install requests pyinstaller
pyinstaller --onefile --windowed --name GorgonBetTracker fight_monitor.py
```
