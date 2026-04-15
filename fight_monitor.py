#!/usr/bin/env python3
"""
Gorgon Coliseum Fight Monitor

Watches player.log for fight announcements, fetches odds from Firebase,
shows a small always-on-top overlay with fight info and a live countdown,
and tracks placed bets in a local SQLite DB with a stats web UI.

Requirements:
    pip install requests
    (tkinter ships with standard Python on Windows)
"""

import re
import sys
import glob
import time
import ctypes
import os
import json
import sqlite3
import datetime
import threading
import tkinter as tk
import webbrowser
import requests
from urllib.parse import quote
from http.server import HTTPServer, BaseHTTPRequestHandler

# ── Paths ────────────────────────────────────────────────────────────────────
# When frozen by PyInstaller (--onefile), sys.executable is the exe path.
# Otherwise it's the Python interpreter, so fall back to the script dir.

def _app_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

APP_DIR       = _app_dir()
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
DB_PATH       = os.path.join(APP_DIR, "bets.db")


# ── Settings ─────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS = {
    "log_file":            r"%LOCALAPPDATA%\..\LocalLow\Elder Game\Project Gorgon\player.log",
    "chat_log_dir":        r"%LOCALAPPDATA%\..\LocalLow\Elder Game\Project Gorgon\ChatLogs",
    "server":              "Dreva",
    "project_id":          "gorgon-crafting-tools",
    "stats_port":          8731,
    "fight_beep_enabled":  False,
}

def _load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, 'r', encoding='utf-8') as fh:
                settings.update(json.load(fh))
        except Exception as exc:
            print(f"[SETTINGS] Failed to read {SETTINGS_PATH}: {exc} — using defaults")
    else:
        try:
            with open(SETTINGS_PATH, 'w', encoding='utf-8') as fh:
                json.dump(DEFAULT_SETTINGS, fh, indent=2)
            print(f"[SETTINGS] Wrote default settings to {SETTINGS_PATH}")
        except Exception as exc:
            print(f"[SETTINGS] Could not write defaults: {exc}")
    # Expand %VAR% style env refs in paths
    settings['log_file']     = os.path.normpath(os.path.expandvars(settings['log_file']))
    settings['chat_log_dir'] = os.path.normpath(os.path.expandvars(settings['chat_log_dir']))
    return settings

SETTINGS = _load_settings()

LOG_FILE            = SETTINGS['log_file']
CHAT_LOG_DIR        = SETTINGS['chat_log_dir']
SERVER              = SETTINGS['server']
PROJECT_ID          = SETTINGS['project_id']
STATS_PORT          = SETTINGS['stats_port']
FIGHT_BEEP_ENABLED  = bool(SETTINGS['fight_beep_enabled'])

FIGHTERS = ['Corrak', 'Dura', 'Gloz', 'Leo', 'Otis', 'Ushug', 'Vizlark']

NAME_CORRECTIONS = {
    'corrrak': 'Corrak',
}


# ── Name Helpers ──────────────────────────────────────────────────────────────

def canonical_name(raw: str) -> str:
    corrected = NAME_CORRECTIONS.get(raw.lower(), raw)
    for f in FIGHTERS:
        if f.lower() == corrected.lower():
            return f
    return corrected


# ── Coliseum Date Logic (mirrors JS getColiseumDate) ─────────────────────────

def get_coliseum_date() -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    if now.hour < 4:
        now -= datetime.timedelta(days=1)
    return now.strftime('%Y-%m-%d')

def get_document_key() -> str:
    return f"{SERVER}:{get_coliseum_date()}"


# ── Firebase REST Fetch ───────────────────────────────────────────────────────

def fetch_tips() -> dict:
    key         = get_document_key()
    encoded_key = quote(key, safe='')
    url = (
        f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}"
        f"/databases/(default)/documents/coliseum/{encoded_key}"
    )
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 404:
            return _empty_tips()
        resp.raise_for_status()
        fields = resp.json().get('fields', {})

        def parse_map(fv):
            if not fv:
                return {}
            return {k: v.get('booleanValue', False)
                    for k, v in fv.get('mapValue', {}).get('fields', {}).items()}

        return {
            'globalAdvantages':    parse_map(fields.get('globalAdvantages')),
            'globalDisadvantages': parse_map(fields.get('globalDisadvantages')),
            'fightAdvantages':     parse_map(fields.get('fightAdvantages')),
        }
    except Exception as exc:
        print(f"[ERROR] fetch_tips: {exc}")
        return _empty_tips()

def _empty_tips() -> dict:
    return {'globalAdvantages': {}, 'globalDisadvantages': {}, 'fightAdvantages': {}}


# ── Odds Calculation (mirrors JS calcOdds / calcAdvantage) ───────────────────

def calc_odds(fighter: str, opponent: str, tips: dict) -> int:
    ga, gd, fa = tips['globalAdvantages'], tips['globalDisadvantages'], tips['fightAdvantages']
    offset = (
          ( 5 if ga.get(fighter)                 else 0)
        + (-5 if gd.get(fighter)                 else 0)
        + (10 if fa.get(f"{fighter}:{opponent}") else 0)
        + (-5 if ga.get(opponent)                else 0)
        + ( 5 if gd.get(opponent)                else 0)
        + (-10 if fa.get(f"{opponent}:{fighter}") else 0)
    )
    return 50 + offset

def calc_advantage(fighter: str, opponent: str, tips: dict) -> int:
    return calc_odds(fighter, opponent, tips) - 50


# ── Shared Fight State ────────────────────────────────────────────────────────

class FightState:
    def __init__(self):
        self._lock      = threading.Lock()
        self.fighter1   = None
        self.fighter2   = None
        self.winner     = None
        self.advantage  = 0
        self.win_pct    = 50
        self.deadline    = None
        self.fight_live  = False
        self.fight_over  = False
        self.ready_to_bet = False
        self.bet_placed  = False
        self.bet_amount  = 0
        self.tips_count  = 0
        self.won = None

    def set_fighters(self, f1: str, f2: str, tips: dict):
        adv     = calc_advantage(f1, f2, tips)
        winner  = f1 if adv > 0 else (f2 if adv < 0 else None)
        win_pct = 50 + abs(adv)
        tips_count = sum(
            sum(1 for v in m.values() if v)
            for m in (tips['globalAdvantages'], tips['globalDisadvantages'], tips['fightAdvantages'])
        )
        with self._lock:
            self.fighter1     = f1
            self.fighter2     = f2
            self.advantage    = abs(adv)
            self.winner       = winner
            self.win_pct      = win_pct
            self.tips_count   = tips_count
            self.fight_live   = False
            self.fight_over   = False
            self.ready_to_bet = False
            self.bet_placed   = False
            self.bet_amount   = 0

    def set_timer(self, seconds: int):
        with self._lock:
            self.deadline   = time.monotonic() + seconds
            self.fight_live = False

    def seconds_remaining(self) -> int | None:
        with self._lock:
            if self.deadline is None:
                return None
            remaining = self.deadline - time.monotonic()
            return max(0, int(remaining))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                'f1':           self.fighter1,
                'f2':           self.fighter2,
                'winner':       self.winner,
                'advantage':    self.advantage,
                'win_pct':      self.win_pct,
                'deadline':     self.deadline,
                'fight_live':   self.fight_live,
                'fight_over':   self.fight_over,
                'ready_to_bet': self.ready_to_bet,
                'bet_placed':   self.bet_placed,
                'bet_amount':   self.bet_amount,
                'tips_count':   self.tips_count,
                'won':          self.won,
            }

    def mark_live(self):
        with self._lock:
            self.fight_live   = True
            self.fight_over   = False
            self.ready_to_bet = False

    def mark_fight_over(self, won):
        with self._lock:
            self.fight_over = True
            self.won = won

    def mark_ready_to_bet(self):
        with self._lock:
            self.ready_to_bet = True

    def mark_bet_placed(self, amount: int):
        with self._lock:
            self.bet_placed = True
            self.bet_amount = amount

STATE = FightState()


# ── Bet Database ──────────────────────────────────────────────────────────────

class BetDB:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS pending_bet (
                        id        INTEGER PRIMARY KEY CHECK (id = 1),
                        fighter1  TEXT NOT NULL,
                        fighter2  TEXT NOT NULL,
                        advantage INTEGER NOT NULL,
                        amount    INTEGER NOT NULL,
                        placed_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS bets (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        placed_at   TEXT NOT NULL,
                        resolved_at TEXT NOT NULL,
                        fighter1    TEXT NOT NULL,
                        fighter2    TEXT NOT NULL,
                        matchup     TEXT NOT NULL,
                        advantage   INTEGER NOT NULL,
                        amount      INTEGER NOT NULL,
                        outcome     TEXT NOT NULL,
                        net         INTEGER NOT NULL
                    );
                """)
                conn.commit()
            finally:
                conn.close()

    def save_pending(self, fighter1: str, fighter2: str, advantage: int, amount: int):
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO pending_bet (id, fighter1, fighter2, advantage, amount, placed_at) "
                    "VALUES (1, ?, ?, ?, ?, ?)",
                    (fighter1, fighter2, advantage, amount, now)
                )
                conn.commit()
            finally:
                conn.close()

    def load_pending(self) -> dict | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM pending_bet WHERE id = 1").fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def clear_pending(self):
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("DELETE FROM pending_bet WHERE id = 1")
                conn.commit()
            finally:
                conn.close()

    def record_bet(self, pending: dict, outcome: str):
        now     = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        net     = round(pending['amount'] * 0.9) if outcome == 'win' else -pending['amount']
        matchup = " vs ".join(sorted([pending['fighter1'], pending['fighter2']]))
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO bets (placed_at, resolved_at, fighter1, fighter2, matchup, advantage, amount, outcome, net) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pending['placed_at'], now, pending['fighter1'], pending['fighter2'],
                     matchup, pending['advantage'], pending['amount'], outcome, net)
                )
                conn.execute("DELETE FROM pending_bet WHERE id = 1")
                conn.commit()
            finally:
                conn.close()

    def get_stats_overall(self) -> dict:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("""
                    SELECT COUNT(*) bets,
                           SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
                           SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) losses,
                           SUM(amount) total_wagered,
                           SUM(net) net
                    FROM bets
                """).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

    def get_stats_by_matchup(self) -> list:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("""
                    SELECT matchup,
                           COUNT(*) bets,
                           SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
                           SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) losses,
                           SUM(amount) total_wagered,
                           SUM(net) net,
                           ROUND(100.0 * SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) / COUNT(*), 1) win_rate
                    FROM bets
                    GROUP BY matchup
                    ORDER BY bets DESC
                """).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_stats_by_advantage(self) -> list:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("""
                    SELECT advantage,
                           COUNT(*) bets,
                           SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) wins,
                           SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) losses,
                           SUM(amount) total_wagered,
                           SUM(net) net,
                           ROUND(100.0 * SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) / COUNT(*), 1) win_rate
                    FROM bets
                    GROUP BY advantage
                    ORDER BY advantage DESC
                """).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    def get_recent_bets(self, limit: int = 20) -> list:
        with self._lock:
            conn = self._conn()
            try:
                rows = conn.execute("""
                    SELECT resolved_at, matchup, advantage, amount, outcome, net
                    FROM bets
                    ORDER BY id DESC
                    LIMIT ?
                """, (limit,)).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()


DB: BetDB | None = None


# ── Log Parsing ───────────────────────────────────────────────────────────────

_FIGHTERS_RE = re.compile(
    r'next fight will be between (\w+) and (\w+)',
    re.IGNORECASE
)

_TIMER_RE = re.compile(
    r'fight begins in <em>(?:(\d+)\s+minutes?(?:\s+and)?\s+)?(\d+)\s+seconds?</em>',
    re.IGNORECASE
)

_BET_RE = re.compile(
    r"placed your bet of <em>([\d,]+)</em> Councils on (\w+) to defeat (\w+)",
    re.IGNORECASE
)

_BET_PENDING_RE = re.compile(
    r"You are betting <em>([\d,]+)</em> Councils that (\w+) defeats (\w+)",
    re.IGNORECASE
)

_BET_WIN_RE = re.compile(
    r"Your previous bet was correct.*?You receive <em>([\d,]+)</em> Councils",
    re.IGNORECASE
)

_BET_LOSS_RE = re.compile(
    r"Your previous bet was incorrect\. Your fighter lost\.",
    re.IGNORECASE
)


def _handle_bet_outcome(outcome: str):
    if DB is None:
        return
    pending = DB.load_pending()
    if pending is None:
        print(f"[BET] Got {outcome} but no pending bet in DB — ignoring")
        return
    DB.record_bet(pending, outcome)
    net = round(pending['amount'] * 0.9) if outcome == 'win' else -pending['amount']
    print(f"[BET] {outcome.upper()}  net={net:+,}  ({pending['fighter1']} vs {pending['fighter2']})")


def _restore_pending_from_db():
    if DB is None:
        return
    pending = DB.load_pending()
    if pending is None:
        return
    with STATE._lock:
        STATE.fighter1   = pending['fighter1']
        STATE.fighter2   = pending['fighter2']
        STATE.advantage  = pending['advantage']
        STATE.bet_placed = True
        STATE.bet_amount = pending['amount']
    print(f"[BET] Restored pending bet: {pending['amount']:,} on {pending['fighter1']} vs {pending['fighter2']}")


def process_line(line: str):
    if 'ProcessTalkScreen' not in line:
        return

    fighters_match    = _FIGHTERS_RE.search(line)
    timer_match       = _TIMER_RE.search(line)
    bet_match         = _BET_RE.search(line)
    bet_pending_match = _BET_PENDING_RE.search(line)

    if bet_match:
        print(f"[LOG] Bet confirmed: {bet_match.group(0)}")
    if bet_pending_match:
        print(f"[LOG] Bet pending: {bet_pending_match.group(0)}")

    if fighters_match or bet_match or bet_pending_match:
        if fighters_match:
            f1 = canonical_name(fighters_match.group(1))
            f2 = canonical_name(fighters_match.group(2))
        elif bet_match:
            f1 = canonical_name(bet_match.group(2))
            f2 = canonical_name(bet_match.group(3))
        else:
            f1 = canonical_name(bet_pending_match.group(2))
            f2 = canonical_name(bet_pending_match.group(3))
        tips = fetch_tips()
        STATE.set_fighters(f1, f2, tips)
        snap = STATE.snapshot()
        adv, winner = snap['advantage'], snap['winner']
        if winner:
            print(f"  Fight {f1} vs {f2}  |  Winner: {winner}  (+{adv}%)")
        else:
            print(f"  Fight {f1} vs {f2}  |  Even odds (50/50)")

    if timer_match:
        minutes = int(timer_match.group(1)) if timer_match.group(1) else 0
        seconds = minutes * 60 + int(timer_match.group(2))
        STATE.set_timer(seconds)
        mins, secs = divmod(seconds, 60)
        timer_str = f"{mins}m {secs}s" if mins else f"{secs}s"
        print(f"  Fight begins in: {timer_str}")
        if seconds > 0:
            _schedule_fight_alert(seconds)

    if bet_pending_match:
        amount = int(bet_pending_match.group(1).replace(',', ''))
        STATE.mark_bet_placed(amount)
        print(f"  Bet pending: {amount:,}")
        if DB is not None:
            DB.save_pending(STATE.fighter1, STATE.fighter2, STATE.advantage, amount)
    elif bet_match and STATE.seconds_remaining() is not None and STATE.seconds_remaining() > 0:
        amount = int(bet_match.group(1).replace(',', ''))
        STATE.mark_bet_placed(amount)
        print(f"  Bet placed: {amount:,}")
        if DB is not None:
            DB.save_pending(STATE.fighter1, STATE.fighter2, STATE.advantage, amount)

    win_match  = _BET_WIN_RE.search(line)
    loss_match = _BET_LOSS_RE.search(line)
    if win_match:
        _handle_bet_outcome('win')
    elif loss_match:
        _handle_bet_outcome('loss')


# ── Sound ─────────────────────────────────────────────────────────────────────

def play_fight_sound():
    try:
        import winsound
        winsound.Beep(600,  150)
        winsound.Beep(900,  150)
        winsound.Beep(1200, 300)
        winsound.Beep(1200, 300)
    except Exception as exc:
        print(f"[SOUND] (unavailable: {exc})")

_sound_timer: threading.Timer | None = None
_sound_lock  = threading.Lock()

def _schedule_fight_alert(seconds: int):
    global _sound_timer
    with _sound_lock:
        if _sound_timer:
            _sound_timer.cancel()
        def _fire():
            STATE.mark_live()
            snap = STATE.snapshot()
            f1   = snap['f1'] or '?'
            f2   = snap['f2'] or '?'
            print(f"\n>>> FIGHT STARTING: {f1} vs {f2}! <<<\n")
            if FIGHT_BEEP_ENABLED:
                play_fight_sound()
        _sound_timer = threading.Timer(seconds, _fire)
        _sound_timer.daemon = True
        _sound_timer.start()


# ── Log Tail (background thread) ─────────────────────────────────────────────

def tail_log(path: str):
    print(f"[LOG] Watching: {path}")
    print(f"[LOG] Server: {SERVER}  Key: {get_document_key()}\n")
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        fh.seek(0, 2)
        while True:
            line = fh.readline()
            if line:
                process_line(line)
            else:
                time.sleep(0.1)


# ── Chat Log Tail (background thread) ────────────────────────────────────────

_CHAT_READY_RE = re.compile(
    r'\[NPC Chatter\] Kuzavek: I am now ready to accept bets on the next arena battle!',
    re.IGNORECASE
)
_CHAT_START_RE = re.compile(
    r'\[NPC Chatter\] Kuzavek: Attention one and all\. Let us begin the next battle!',
    re.IGNORECASE
)
_CHAT_OVER_RE = re.compile(
    r'\[NPC Chatter\] Kuzavek: (?:(\w+) wins!|.+the fight is over!|(\w+) has done it again!|The battles goes to (\w+)!|.*(\w+) has (?:won|lost).*|(\w+) has done it!|.+That\'s the end of the fight!)',
    re.IGNORECASE
)

def _latest_chat_log(directory: str) -> str:
    files = glob.glob(os.path.join(directory, "*.log"))
    if not files:
        files = glob.glob(os.path.join(directory, "*"))
    if not files:
        raise FileNotFoundError(f"No chat log files found in {directory}")
    return max(files, key=os.path.getmtime)

def process_chat_line(line: str):
    if _CHAT_READY_RE.search(line):
        STATE.mark_ready_to_bet()
        print("[CHAT] Ready for new bet")
    elif _CHAT_START_RE.search(line):
        STATE.mark_live()
        print("[CHAT] Fight started")
    else:
        m = _CHAT_OVER_RE.search(line)
        if m:
            STATE.mark_fight_over(m.group(0))
            print(f"[CHAT] Fight over – {m.group(0)}")

def tail_chat_log(directory: str):
    path = _latest_chat_log(directory)
    print(f"[CHAT] Watching: {path}")
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        fh.seek(0, 2)
        while True:
            line = fh.readline()
            if line:
                process_chat_line(line)
            else:
                time.sleep(0.1)


# ── Overlay (main thread – tkinter) ──────────────────────────────────────────

OVERLAY_BG      = "#0f0f1a"
OVERLAY_ACCENT  = "#c9a84c"
OVERLAY_DIM     = "#666688"
OVERLAY_WHITE   = "#e8e8f0"
OVERLAY_GREEN   = "#5dbb6a"
OVERLAY_RED     = "#e05555"
OVERLAY_FLASH   = "#ff4444"

class FightOverlay:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Coliseum Monitor")
        root.configure(bg=OVERLAY_BG)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.92)
        root.overrideredirect(True)

        root.geometry("270x160+25+50")

        self._drag_x = 0
        self._drag_y = 0
        root.bind("<ButtonPress-1>", self._drag_start)
        root.bind("<B1-Motion>",     self._drag_move)

        close_btn = tk.Label(root, text="✕", bg=OVERLAY_BG, fg=OVERLAY_DIM,
                             font=("Segoe UI", 9), cursor="hand2")
        close_btn.place(relx=1.0, x=-6, y=4, anchor="ne")
        close_btn.bind("<Button-1>", lambda _: root.destroy())

        stats_link = tk.Label(root, text="stats ↗", bg=OVERLAY_BG, fg=OVERLAY_ACCENT,
                              font=("Segoe UI", 8, "underline"), cursor="hand2")
        stats_link.place(x=6, y=4, anchor="nw")
        stats_link.bind(
            "<Button-1>",
            lambda _: webbrowser.open(f"http://127.0.0.1:{STATS_PORT}/"),
        )

        self.lbl_header = tk.Label(
            root, text="CROOKED COLISEUM",
            bg=OVERLAY_BG, fg=OVERLAY_ACCENT,
            font=("Segoe UI", 8, "bold"), pady=0
        )
        self.lbl_header.pack(pady=(8, 0))

        self.lbl_fight = tk.Label(
            root, text="Waiting for fight...",
            bg=OVERLAY_BG, fg=OVERLAY_DIM,
            font=("Segoe UI", 13, "bold")
        )
        self.lbl_fight.pack(pady=(2, 0))

        self.lbl_pred = tk.Label(
            root, text="",
            bg=OVERLAY_BG, fg=OVERLAY_DIM,
            font=("Segoe UI", 9)
        )
        self.lbl_pred.pack()

        self.lbl_status = tk.Label(
            root, text="",
            bg=OVERLAY_BG, fg=OVERLAY_WHITE,
            font=("Segoe UI", 11, "bold")
        )
        self.lbl_status.pack(pady=(2, 0))

        self.lbl_timer = tk.Label(
            root, text="",
            bg=OVERLAY_BG, fg=OVERLAY_WHITE,
            font=("Segoe UI", 10)
        )
        self.lbl_timer.pack(pady=(0, 0))

        self.lbl_won = tk.Label(
            root, text="",
            bg=OVERLAY_BG, fg=OVERLAY_DIM,
            font=("Segoe UI", 8),
            wraplength=250, justify="center"
        )
        self.lbl_won.pack(pady=(0, 0))

        border = tk.Frame(root, bg=OVERLAY_ACCENT, height=2)
        border.pack(fill="x", side="bottom")

        self._tick()

    def _drag_start(self, e):
        self._drag_x = e.x
        self._drag_y = e.y

    def _drag_move(self, e):
        x = self.root.winfo_x() + (e.x - self._drag_x)
        y = self.root.winfo_y() + (e.y - self._drag_y)
        self.root.geometry(f"+{x}+{y}")

    @staticmethod
    def _is_game_focused() -> bool:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value == "Project Gorgon"

    def _tick(self):
        if self._is_game_focused():
            self.root.deiconify()
        else:
            self.root.withdraw()

        snap = STATE.snapshot()
        remaining = STATE.seconds_remaining()

        f1           = snap['f1']
        f2           = snap['f2']
        winner       = snap['winner']
        adv          = snap['advantage']
        is_live      = snap['fight_live']
        fight_over   = snap['fight_over']
        ready_to_bet = snap['ready_to_bet']
        bet_placed   = snap['bet_placed']
        bet_amount   = snap['bet_amount']
        tips_count   = snap['tips_count']

        if f1 and f2:
            self.lbl_fight.config(text=f"{f1}  vs  {f2}", fg=OVERLAY_WHITE)
        else:
            self.lbl_fight.config(text="Waiting for fight...", fg=OVERLAY_DIM)

        if f1 and f2:
            tips_str = f"  ·  {tips_count} tips" if tips_count else ""
            if winner:
                self.lbl_pred.config(
                    text=f"Winner: {winner}  (+{adv}%){tips_str}",
                    fg=OVERLAY_GREEN
                )
            else:
                self.lbl_pred.config(text=f"Even odds  (50/50){tips_str}", fg=OVERLAY_DIM)
        else:
            self.lbl_pred.config(text="", fg=OVERLAY_DIM)

        won_text = snap.get('won') or ""
        if ready_to_bet:
            self.lbl_status.config(text="✓  READY FOR NEW BET  ✓", fg=OVERLAY_GREEN)
            self.lbl_timer.config(text="", fg=OVERLAY_WHITE)
            self.lbl_won.config(text="")
        elif fight_over:
            self.lbl_status.config(text="FIGHT OVER", fg=OVERLAY_DIM)
            self.lbl_timer.config(text="", fg=OVERLAY_WHITE)
            self.lbl_won.config(text=won_text, fg=OVERLAY_DIM)
        elif is_live:
            self.lbl_status.config(text="⚔  FIGHT IN PROGRESS  ⚔", fg=OVERLAY_FLASH)
            self.lbl_timer.config(text="", fg=OVERLAY_WHITE)
            self.lbl_won.config(text="")
        elif bet_placed:
            self.lbl_status.config(text=f"BET {bet_amount:,} on {f1}", fg=OVERLAY_GREEN)
            if remaining is not None and remaining > 0:
                mins, secs = divmod(remaining, 60)
                timer_str  = f"{mins}:{secs:02d}" if mins else f"0:{secs:02d}"
                self.lbl_timer.config(text=f"Fight in  {timer_str}", fg=OVERLAY_GREEN)
            else:
                self.lbl_timer.config(text="", fg=OVERLAY_WHITE)
            self.lbl_won.config(text="")
        elif remaining is None:
            self.lbl_status.config(text="", fg=OVERLAY_WHITE)
            self.lbl_timer.config(text="", fg=OVERLAY_WHITE)
            self.lbl_won.config(text="")
        elif remaining == 0:
            self.lbl_status.config(text="⚔  FIGHT STARTING  ⚔", fg=OVERLAY_FLASH)
            self.lbl_timer.config(text="", fg=OVERLAY_WHITE)
            self.lbl_won.config(text="")
        else:
            self.lbl_status.config(text="", fg=OVERLAY_WHITE)
            mins, secs = divmod(remaining, 60)
            timer_str  = f"{mins}:{secs:02d}" if mins else f"0:{secs:02d}"
            self.lbl_timer.config(text=f"Fight in {timer_str}", fg=OVERLAY_WHITE)
            self.lbl_won.config(text="")

        self.root.after(250, self._tick)


# ── Stats Web Server ─────────────────────────────────────────────────────────

STATS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Coliseum Bet Stats</title>
<style>
  :root {
    --bg: #0f0f1a; --card: #16162a; --border: #2a2a4a;
    --gold: #c9a84c; --green: #5dbb6a; --red: #e05555;
    --dim: #888aaa; --text: #e8e8f0;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; padding: 20px; }
  h1 { color: var(--gold); font-size: 1.2rem; letter-spacing: .1em; margin-bottom: 16px; }
  #pending-banner { display:none; background: #1a1a30; border: 1px solid var(--gold);
                    border-radius: 6px; padding: 10px 16px; margin-bottom: 16px; color: var(--gold); }
  .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
          padding: 14px 20px; }
  .card .label { color: var(--dim); font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; }
  .card .value { font-size: 1.5rem; font-weight: bold; margin-top: 4px; }
  .pos { color: var(--green); } .neg { color: var(--red); } .neu { color: var(--text); }
  h2 { color: var(--gold); font-size: .9rem; letter-spacing: .08em; margin: 20px 0 8px; }
  table { width: 100%; border-collapse: collapse; font-size: .85rem; }
  th { color: var(--dim); font-weight: normal; text-align: left; padding: 6px 10px;
       border-bottom: 1px solid var(--border); }
  td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
  tr:hover td { background: #1a1a30; }
  .win { color: var(--green); } .loss { color: var(--red); }
  #refresh-note { color: var(--dim); font-size: .72rem; margin-top: 16px; }
  .container { max-width: 800px; margin: 0 auto; }
</style>
</head>
<body>
<div class="container">
<h1>CROOKED COLISEUM — BET STATS</h1>
<div id="pending-banner"></div>
<div class="cards" id="cards"></div>
<h2>BY MATCHUP</h2>
<table id="tbl-matchup">
  <thead><tr><th>Matchup</th><th>Bets</th><th>W</th><th>L</th><th>Win%</th><th>Wagered</th><th>Net</th></tr></thead>
  <tbody></tbody>
</table>
<h2>BY ADVANTAGE</h2>
<table id="tbl-adv">
  <thead><tr><th>Adv%</th><th>Bets</th><th>W</th><th>L</th><th>Win%</th><th>Wagered</th><th>Net</th></tr></thead>
  <tbody></tbody>
</table>
<h2>RECENT BETS</h2>
<table id="tbl-recent">
  <thead><tr><th>Time</th><th>Matchup</th><th>Adv%</th><th>Amount</th><th>Outcome</th><th>Net</th></tr></thead>
  <tbody></tbody>
</table>
<div id="refresh-note">Auto-refreshes every 10s</div>
</div>
<script>
function fmt(n) { return n == null ? '\u2014' : Number(n).toLocaleString(); }
function netClass(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : 'neu'; }
function netStr(n) { return n == null ? '\u2014' : (n >= 0 ? '+' : '') + fmt(n); }

function renderCards(o) {
  const el = document.getElementById('cards');
  const wr = o.bets ? (100 * o.wins / o.bets).toFixed(1) : '\u2014';
  el.innerHTML = [
    ['Bets', o.bets ?? 0, 'neu'],
    ['Wins', o.wins ?? 0, 'pos'],
    ['Losses', o.losses ?? 0, 'neg'],
    ['Win Rate', (wr === '\u2014' ? '\u2014' : wr + '%'), 'neu'],
    ['Wagered', fmt(o.total_wagered), 'neu'],
    ['Net', netStr(o.net), netClass(o.net)],
  ].map(([label, value, cls]) =>
    `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`
  ).join('');
}

function renderTable(id, rows, cols) {
  const tbody = document.querySelector('#' + id + ' tbody');
  tbody.innerHTML = rows.length ? rows.map(r =>
    '<tr>' + cols.map(([key, fn]) => '<td>' + fn(r[key], r) + '</td>').join('') + '</tr>'
  ).join('') : '<tr><td colspan="' + cols.length + '" style="color:var(--dim);text-align:center">No data</td></tr>';
}

function refresh() {
  fetch('/api/stats').then(r => r.json()).then(d => {
    const o = d.overall || {};
    renderCards(o);

    const banner = document.getElementById('pending-banner');
    if (d.pending) {
      banner.style.display = 'block';
      banner.textContent = `Pending bet: ${fmt(d.pending.amount)} on ${d.pending.fighter1} vs ${d.pending.fighter2} (placed ${d.pending.placed_at})`;
    } else {
      banner.style.display = 'none';
    }

    renderTable('tbl-matchup', d.by_matchup || [], [
      ['matchup', v => v],
      ['bets',    v => v],
      ['wins',    v => `<span class="win">${v}</span>`],
      ['losses',  v => `<span class="loss">${v}</span>`],
      ['win_rate',v => (v ?? '\u2014') + (v != null ? '%' : '')],
      ['total_wagered', v => fmt(v)],
      ['net',     (v,r) => `<span class="${netClass(v)}">${netStr(v)}</span>`],
    ]);

    renderTable('tbl-adv', d.by_advantage || [], [
      ['advantage', v => '+' + v + '%'],
      ['bets',      v => v],
      ['wins',      v => `<span class="win">${v}</span>`],
      ['losses',    v => `<span class="loss">${v}</span>`],
      ['win_rate',  v => (v ?? '\u2014') + (v != null ? '%' : '')],
      ['total_wagered', v => fmt(v)],
      ['net',       (v,r) => `<span class="${netClass(v)}">${netStr(v)}</span>`],
    ]);

    renderTable('tbl-recent', d.recent || [], [
      ['resolved_at', v => v.replace('T',' ').replace('Z','')],
      ['matchup',  v => v],
      ['advantage',v => '+' + v + '%'],
      ['amount',   v => fmt(v)],
      ['outcome',  v => `<span class="${v}">${v.toUpperCase()}</span>`],
      ['net',      (v,r) => `<span class="${netClass(v)}">${netStr(v)}</span>`],
    ]);
  }).catch(() => {});
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


def _serve_stats_json() -> bytes:
    if DB is None:
        return json.dumps({}).encode()
    data = {
        'overall':      DB.get_stats_overall(),
        'by_matchup':   DB.get_stats_by_matchup(),
        'by_advantage': DB.get_stats_by_advantage(),
        'recent':       DB.get_recent_bets(20),
        'pending':      DB.load_pending(),
    }
    return json.dumps(data).encode()


def _start_stats_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path == '/':
                body = STATS_HTML.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == '/api/stats':
                body = _serve_stats_json()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)
    try:
        server = HTTPServer(('127.0.0.1', STATS_PORT), Handler)
        print(f"[STATS] http://127.0.0.1:{STATS_PORT}/")
        server.serve_forever()
    except OSError as exc:
        print(f"[STATS] Could not start stats server on port {STATS_PORT}: {exc}")


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    DB = BetDB(DB_PATH)
    _restore_pending_from_db()

    stats_thread = threading.Thread(target=_start_stats_server, daemon=True)
    stats_thread.start()

    log_thread = threading.Thread(target=tail_log, args=(LOG_FILE,), daemon=True)
    log_thread.start()

    chat_thread = threading.Thread(target=tail_chat_log, args=(CHAT_LOG_DIR,), daemon=True)
    chat_thread.start()

    root = tk.Tk()
    FightOverlay(root)
    root.mainloop()
