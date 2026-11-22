import io
import os
import sys
import re
import json
import time
import html
import shlex
import hashlib
import signal
import asyncio
import sqlite3
import logging
import contextvars
import tempfile
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import quote
from typing import Any, Dict, Optional, Callable, Awaitable, List, Tuple

# ==============================================================================
# 📦 REQUIREMENTS (missing packages start par apne aap install hote hain)
# ==============================================================================
AUTO_INSTALL_REQUIREMENTS = True
REQUIREMENTS = [
    "aiogram>=3.7,<4",
    "aiohttp>=3.9",
    "psutil>=5.9",
    "asyncssh>=2.14",
]


def _ensure_requirements() -> None:
    if not AUTO_INSTALL_REQUIREMENTS:
        return
    import subprocess
    from importlib import metadata
    try:
        from packaging.requirements import Requirement
    except Exception:
        Requirement = None
    missing = []
    for spec in REQUIREMENTS:
        name = re.split(r"[<>=!~ ]", spec, maxsplit=1)[0]
        try:
            ver = metadata.version(name)
        except metadata.PackageNotFoundError:
            missing.append(spec)
            continue
        if Requirement is not None:
            try:
                if not Requirement(spec).specifier.contains(ver, prereleases=True):
                    missing.append(spec)
            except Exception:
                pass
    if missing:
        print("📦 Installing requirements:", ", ".join(missing), flush=True)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet",
                                   "--disable-pip-version-check", *missing])
        except Exception as e:
            print(f"❌ Auto-install fail: {e}\nManually chalao: pip install " + " ".join(f'"{m}"' for m in missing), flush=True)


_ensure_requirements()

import aiohttp
import psutil
import asyncssh
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, BufferedInputFile
)
from aiogram.exceptions import TelegramBadRequest

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================
# Sab secret/settings ENVIRONMENT VARIABLES se aati hain (Railway me Variables tab me set karo).
# GitHub par token kabhi mat likho.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()                 # @BotFather se mila hosting bot ka token
ADMIN_ID = _env_int("ADMIN_ID", 0)   # Aapka Telegram numeric user ID
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@godzilla_hacker").strip().lstrip("@")
PAY_API_KEY = os.getenv("PAY_API_KEY", "").strip()                  # (optional) payment API key
CHILD_UID = _env_int("CHILD_UID", 65534)   # hosted bots ka unprivileged UID (0 = band)

if not MAIN_BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN environment variable set nahi hai. Railway Variables me BOT_TOKEN add karo.")
if not ADMIN_ID:
    print("⚠️ ADMIN_ID set nahi hai. Admin panel kisi ko nahi khulega. Variables me ADMIN_ID add karo.", flush=True)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR: Railway me volume ka path (jaise /data). Set na ho to app ka folder.
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "").strip() or APP_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "hosting.db")
HOST_DIR = os.path.join(DATA_DIR, "hosted_files")

BRAND = "MAYANK HOSTING"
VERSION = "1.0"
FREE_BOT_LIMIT = 2
PREMIUM_BOT_LIMIT = 10
FREE_LOG_LINES = 30
PREMIUM_LOG_LINES = 100
MAX_CRASH_RESTARTS = 5
VPS_CRASH_CAP = 100000
VPS_WATCHDOG_INTERVAL = 30
MAX_UPLOAD_MB = 20
BANNED_TEXT = "🚫 <b>Aap is service se banned hain.</b>"
CHANNEL_WATCH_INTERVAL = 30
CHANNEL_CACHE_TTL = 20
REMOTE_ROOT = "/opt/prime_bots"          # VPS par bots yahan rehte hain

REF_REWARD_DEFAULT = 1
COIN_COST_DEFAULT = 5
CLONE_PRICE_DEFAULT = "150"
EMOJI_BACKUP_EVERY = 6 * 3600

DEFAULT_CHANNELS = ["@godzillapannelhub_bot", "https://t.me/godzillapannelhub", "https://t.me/godzillapannelhubgroup"]
DEFAULT_PLANS = [
    {"days": 1, "price": "19"},
    {"days": 7, "price": "49"},
    {"days": 30, "price": "149"},
    {"days": 90, "price": "399"},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("prime_hosting")

bot = Bot(token=MAIN_BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
_BOT_USERNAME = ""
_BG_TASKS: set = set()


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# ==============================================================================
# 🗄️ DATABASE
# ==============================================================================
def db_query(query: str, params: tuple = (), fetchone: bool = False, fetchall: bool = False) -> Any:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        res = c.fetchone() if fetchone else (c.fetchall() if fetchall else None)
        conn.commit()
        return res
    except Exception as e:
        logger.error("DB Error: %s", e)
        return None
    finally:
        conn.close()


def db_insert(query: str, params: tuple = ()) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c.lastrowid
    except Exception as e:
        logger.error("DB Insert Error: %s", e)
        return None
    finally:
        conn.close()


def db_update_count(query: str, params: tuple = ()) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c.rowcount
    except Exception as e:
        logger.error("DB Update Error: %s", e)
        return 0
    finally:
        conn.close()


def _columns(table: str) -> set:
    rows = db_query(f"PRAGMA table_info({table})", fetchall=True) or []
    return {r[1] for r in rows}


_SETTINGS: Optional[Dict[str, str]] = None


def _load_settings() -> None:
    global _SETTINGS
    rows = db_query("SELECT key, value FROM settings", fetchall=True) or []
    _SETTINGS = {k: v for k, v in rows}


def get_setting(key: str, default: str = "") -> str:
    if _SETTINGS is None:
        _load_settings()
    v = (_SETTINGS or {}).get(key)
    return v if v not in (None, "") else default


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except ValueError:
        return default


def set_setting(key: str, value: Any) -> None:
    global _SETTINGS
    db_query("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
    _SETTINGS = None


def del_setting(key: str) -> None:
    global _SETTINGS
    db_query("DELETE FROM settings WHERE key=?", (key,))
    _SETTINGS = None


def del_settings_like(pattern: str) -> None:
    global _SETTINGS
    db_query("DELETE FROM settings WHERE key LIKE ?", (pattern,))
    _SETTINGS = None


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_premium INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0)")
    c.execute("""CREATE TABLE IF NOT EXISTS hosted_bots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    bot_name TEXT,
                    file_path TEXT,
                    status TEXT DEFAULT 'Stopped',
                    auto_restart INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    days INTEGER,
                    amount TEXT,
                    method TEXT,
                    status TEXT DEFAULT 'pending',
                    proof_id TEXT DEFAULT '',
                    created_at TEXT DEFAULT '',
                    decided_at TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS premium_emojis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emoji_id TEXT UNIQUE,
                    char TEXT DEFAULT '',
                    name TEXT DEFAULT '',
                    pack TEXT DEFAULT '',
                    added_at TEXT DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS vps_servers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    host TEXT,
                    port INTEGER DEFAULT 22,
                    username TEXT,
                    auth TEXT DEFAULT 'password',
                    secret TEXT,
                    status TEXT DEFAULT 'untested',
                    last_check TEXT DEFAULT '',
                    added_at TEXT DEFAULT '')""")
    conn.commit()
    conn.close()

    user_cols = _columns("users")
    migrations = {
        "first_name": "TEXT DEFAULT ''",
        "username": "TEXT DEFAULT ''",
        "premium_until": "TEXT",
        "joined_at": "TEXT DEFAULT ''",
        "coins": "INTEGER DEFAULT 0",
        "referred_by": "INTEGER DEFAULT 0",
        "coins_earned": "INTEGER DEFAULT 0",
        "ref_credited": "INTEGER DEFAULT 0",
        "extra_slots": "INTEGER DEFAULT 0",
        "is_vps": "INTEGER DEFAULT 0",
        "vps_until": "TEXT",
        "vps_server_id": "INTEGER DEFAULT 0",
    }
    for col, ddl in migrations.items():
        if col not in user_cols:
            db_query(f"ALTER TABLE users ADD COLUMN {col} {ddl}")

    bot_cols = _columns("hosted_bots")
    if "created_at" not in bot_cols:
        db_query("ALTER TABLE hosted_bots ADD COLUMN created_at TEXT DEFAULT ''")
    if "approval" not in bot_cols:
        db_query("ALTER TABLE hosted_bots ADD COLUMN approval TEXT DEFAULT 'approved'")
    if "file_sha" not in bot_cols:
        db_query("ALTER TABLE hosted_bots ADD COLUMN file_sha TEXT DEFAULT ''")

    pay_cols = _columns("payments")
    if "proof_unique" not in pay_cols:
        db_query("ALTER TABLE payments ADD COLUMN proof_unique TEXT DEFAULT ''")
    if "kind" not in pay_cols:
        db_query("ALTER TABLE payments ADD COLUMN kind TEXT DEFAULT 'premium'")

    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    global _SETTINGS
    _SETTINGS = None
    try:
        lib_seed_defaults()
    except Exception as e:
        logger.warning("Emoji library seed skipped: %s", e)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


USER_KEYS = ("user_id", "is_premium", "premium_until", "is_banned", "first_name", "username", "joined_at",
             "coins", "referred_by", "ref_credited", "extra_slots", "is_vps", "vps_until", "vps_server_id")
BOT_KEYS = ("id", "user_id", "bot_name", "file_path", "status", "auto_restart", "created_at", "approval")
PAY_KEYS = ("id", "user_id", "days", "amount", "method", "status", "proof_id", "created_at", "decided_at", "kind")
VPS_KEYS = ("id", "name", "host", "port", "username", "auth", "secret", "status", "last_check", "added_at")


def ensure_user(uid: int, first_name: str = "", username: str = ""):
    db_query("INSERT OR IGNORE INTO users (user_id, first_name, username, joined_at) VALUES (?,?,?,?)",
             (uid, first_name, username or "", now_str()))
    if first_name or username:
        db_query("UPDATE users SET first_name=?, username=? WHERE user_id=?", (first_name, username or "", uid))


def _future(s: Any) -> bool:
    try:
        return datetime.fromisoformat(str(s)) > datetime.now()
    except (TypeError, ValueError):
        return False


def is_premium_active(u: dict) -> bool:
    if not u or not u.get("is_premium"):
        return False
    until = u.get("premium_until")
    if not until:
        return True
    return _future(until)


def get_user(uid: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(USER_KEYS)} FROM users WHERE user_id=?", (uid,), fetchone=True)
    if not row:
        return None
    u = dict(zip(USER_KEYS, row))
    u["coins"] = int(u.get("coins") or 0)
    u["extra_slots"] = int(u.get("extra_slots") or 0)
    u["vps_server_id"] = int(u.get("vps_server_id") or 0)
    u["premium"] = is_premium_active(u)
    u["banned"] = bool(u["is_banned"])
    u["vps"] = bool(u.get("is_vps")) and _future(u.get("vps_until"))
    return u


def vps_user(uid: int) -> bool:
    u = get_user(uid)
    return bool(u and u["vps"])


def set_premium(uid: int, days: int):
    ensure_user(uid)
    if days <= 0:
        db_query("UPDATE users SET is_premium=1, premium_until=NULL WHERE user_id=?", (uid,))
        return
    base = datetime.now()
    u = get_user(uid)
    if u and u["premium"] and u["premium_until"]:
        try:
            base = max(base, datetime.fromisoformat(u["premium_until"]))
        except ValueError:
            pass
    until = (base + timedelta(days=days)).isoformat(timespec="seconds")
    db_query("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until, uid))


def remove_premium(uid: int):
    db_query("UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?", (uid,))


def set_vps(uid: int, days: int, server_id: int = 0):
    """VPS grant. days <= 0 = VPS hata do (server bhi reset)."""
    ensure_user(uid)
    if days <= 0:
        db_query("UPDATE users SET is_vps=0, vps_until=NULL, vps_server_id=0 WHERE user_id=?", (uid,))
        return
    until = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
    db_query("UPDATE users SET is_vps=1, vps_until=?, vps_server_id=? WHERE user_id=?", (until, server_id, uid))


def set_ban(uid: int, banned: bool):
    db_query("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, uid))


def bot_limit(u: dict) -> int:
    key, default = ("premium_limit", PREMIUM_BOT_LIMIT) if u["premium"] else ("free_limit", FREE_BOT_LIMIT)
    try:
        return max(1, int(get_setting(key, str(default))))
    except ValueError:
        return default


def user_limit(u: dict) -> int:
    return bot_limit(u) + int(u.get("extra_slots") or 0)


def user_bot_count(uid: int) -> int:
    row = db_query("SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,), fetchone=True)
    return row[0] if row else 0


def get_bot(bot_id: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(BOT_KEYS)} FROM hosted_bots WHERE id=?", (bot_id,), fetchone=True)
    return dict(zip(BOT_KEYS, row)) if row else None


def set_status(bot_id: int, status: str):
    db_query("UPDATE hosted_bots SET status=? WHERE id=?", (status, bot_id))


def set_approval(bot_id: int, approval: str):
    db_query("UPDATE hosted_bots SET approval=? WHERE id=?", (approval, bot_id))


def can_access(uid: int, hb: Optional[dict]) -> bool:
    return bool(hb) and (hb["user_id"] == uid or uid == ADMIN_ID)


# ---------------------------- VPS server CRUD ----------------------------
def get_server(sid: int) -> Optional[dict]:
    if not sid:
        return None
    row = db_query(f"SELECT {', '.join(VPS_KEYS)} FROM vps_servers WHERE id=?", (sid,), fetchone=True)
    return dict(zip(VPS_KEYS, row)) if row else None


def list_servers() -> List[dict]:
    rows = db_query(f"SELECT {', '.join(VPS_KEYS)} FROM vps_servers ORDER BY id ASC", fetchall=True) or []
    return [dict(zip(VPS_KEYS, r)) for r in rows]


def server_user_count(sid: int) -> int:
    row = db_query("SELECT COUNT(*) FROM users WHERE vps_server_id=? AND is_vps=1", (sid,), fetchone=True)
    return int(row[0]) if row else 0


def save_server(name: str, host: str, port: int, username: str, auth: str, secret: str) -> Optional[int]:
    return db_insert(
        "INSERT INTO vps_servers (name, host, port, username, auth, secret, status, last_check, added_at) "
        "VALUES (?,?,?,?,?,?,'untested','',?)",
        (name, host, port, username, auth, secret, now_str()),
    )


def set_server_status(sid: int, status: str):
    db_query("UPDATE vps_servers SET status=?, last_check=? WHERE id=?", (status, now_str(), sid))


def delete_server(sid: int):
    db_query("DELETE FROM vps_servers WHERE id=?", (sid,))
    db_query("UPDATE users SET vps_server_id=0 WHERE vps_server_id=?", (sid,))


# ---------------------------- Plans / channels / payment config ----------------------------
def get_plans() -> List[dict]:
    raw = get_setting("plans", "")
    try:
        data = json.loads(raw) if raw else DEFAULT_PLANS
    except ValueError:
        data = DEFAULT_PLANS
    out = []
    for p in data:
        try:
            out.append({"days": int(p["days"]), "price": str(p["price"])})
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda p: p["days"])


def save_plans(plans: List[dict]) -> None:
    set_setting("plans", json.dumps(sorted(plans, key=lambda p: p["days"])))


def get_plan(days: int) -> Optional[dict]:
    for p in get_plans():
        if p["days"] == days:
            return p
    return None


def get_channels() -> List[str]:
    raw = get_setting("force_channels", "")
    if not raw:
        return list(DEFAULT_CHANNELS)
    try:
        data = json.loads(raw)
    except ValueError:
        return list(DEFAULT_CHANNELS)
    return [c for c in data if isinstance(c, str) and c]


def save_channels(channels: List[str]) -> None:
    set_setting("force_channels", json.dumps(channels))


def fast_approve_on() -> bool:
    return get_setting("fast_approve", "OFF") == "ON"


def get_payment(pid: int) -> Optional[dict]:
    row = db_query(f"SELECT {', '.join(PAY_KEYS)} FROM payments WHERE id=?", (pid,), fetchone=True)
    return dict(zip(PAY_KEYS, row)) if row else None


def pay_title(pay: dict) -> str:
    if pay.get("kind") == "clone":
        return "Clone file"
    return f"{pay['days']} day(s)"


# ---------------------------- Referral / coins ----------------------------
def ref_reward() -> int:
    return get_int_setting("ref_reward", REF_REWARD_DEFAULT)


def coin_cost() -> int:
    return max(1, get_int_setting("coin_cost", COIN_COST_DEFAULT))


def invite_count(uid: int) -> int:
    row = db_query("SELECT COUNT(*) FROM users WHERE referred_by=? AND ref_credited=1", (uid,), fetchone=True)
    return int(row[0]) if row else 0


def capture_referral(uid: int, ref: int) -> bool:
    """Naye user ko referrer se jodta hai (sirf ek baar, khud ko refer nahi)."""
    if not ref or ref == uid:
        return False
    if not db_query("SELECT 1 FROM users WHERE user_id=?", (ref,), fetchone=True):
        return False
    return db_update_count("UPDATE users SET referred_by=? WHERE user_id=? AND referred_by=0", (ref, uid)) == 1


async def try_credit_referral(uid: int) -> None:
    """User channels join + Verify kar de to referrer ko coins turant milte hain (ek baar)."""
    row = db_query("SELECT referred_by, ref_credited, first_name FROM users WHERE user_id=?", (uid,), fetchone=True)
    if not row or not row[0] or row[1]:
        return
    if db_update_count("UPDATE users SET ref_credited=1 WHERE user_id=? AND ref_credited=0", (uid,)) != 1:
        return
    reward = ref_reward()
    db_query("UPDATE users SET coins=coins+?, coins_earned=coins_earned+? WHERE user_id=?", (reward, reward, row[0]))
    logger.info("Referral credit