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
OWNER_USERNAME = os.getenv("OWNER_USERNAME", @godzilla_hacker").strip().lstrip("@")
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

BRAND = "MAYANKS HOSTING"
VERSION = "19.0"
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
CLONE_PRICE_DEFAULT = "60"
EMOJI_BACKUP_EVERY = 6 * 3600

DEFAULT_CHANNELS = ["@godzillapannelhub_bot", "https://t.me/godzillapannelhub"]
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
    logger.info("Referral credited: referrer=%s referee=%s +%s", row[0], uid, reward)
    try:
        ref_u = get_user(row[0]) or {"coins": 0}
        await bot.send_message(row[0], ui("ref_bonus", v_reward=reward, v_coins=ref_u.get("coins", 0),
                                          v_name=esc(row[2] or "User")))
    except Exception:
        pass


# ==============================================================================
# 🖥 VPS / SSH ENGINE (admin ke apne VPS par bots chalane ke liye)
# ==============================================================================
def remote_dir(bot_id: int) -> str:
    return f"{REMOTE_ROOT}/{bot_id}"


class VPSError(Exception):
    """User ko samajh aane wala VPS error."""


def friendly_ssh_error(e: BaseException, srv: dict) -> str:
    host = f"{srv.get('host')}:{srv.get('port') or 22}"
    if isinstance(e, asyncio.TimeoutError):
        return f"⏱ {host} se connect timeout ho gaya. IP, port aur firewall check karo."
    if isinstance(e, asyncssh.PermissionDenied):
        if srv.get("auth") == "key":
            return "🔐 SSH key se login fail. Username ya key galat hai, ya key server par add nahi hai."
        return "🔐 Login fail. Username ya password galat hai, ya server par password login band hai."
    if isinstance(e, asyncssh.KeyImportError):
        return "🔑 SSH private key ka format galat hai. Poori key (BEGIN/END ke saath) bhejo."
    if isinstance(e, ConnectionRefusedError):
        return f"🔌 {host} ne connection refuse kiya. Port ya SSH service check karo."
    if isinstance(e, OSError):
        return f"🌐 {host} tak network nahi pahunch raha: {e.strerror or e}"
    if isinstance(e, asyncssh.DisconnectError):
        return f"📴 Server ne connection band kar diya: {e}"
    return f"❌ {type(e).__name__}: {e}"


def _ssh_kwargs(srv: dict) -> Dict[str, Any]:
    kw: Dict[str, Any] = {
        "host": srv["host"],
        "port": int(srv["port"] or 22),
        "username": srv["username"],
        "known_hosts": None,
        "login_timeout": 20,
        # Agent aur ~/.ssh ki default keys band: inki wajah se password login aksar fail hota tha
        "agent_path": None,
    }
    if srv["auth"] == "key":
        kw["client_keys"] = [asyncssh.import_private_key(srv["secret"])]
        kw["preferred_auth"] = "publickey"
    else:
        kw["client_keys"] = []
        kw["password"] = srv["secret"]
        kw["preferred_auth"] = "password,keyboard-interactive"
    return kw


async def ssh_conn(srv: dict):
    try:
        kw = _ssh_kwargs(srv)
        return await asyncio.wait_for(asyncssh.connect(**kw), timeout=25)
    except Exception as e:
        set_server_status(srv["id"], "fail") if srv.get("id") else None
        raise VPSError(friendly_ssh_error(e, srv)) from e


async def ssh_run(srv: dict, cmd: str, timeout: int = 60) -> Tuple[int, str, str]:
    conn = await ssh_conn(srv)
    try:
        r = await conn.run(cmd, check=False, timeout=timeout)
        return (r.exit_status or 0), (r.stdout or ""), (r.stderr or "")
    finally:
        conn.close()
        try:
            await conn.wait_closed()
        except Exception:
            pass


async def ssh_test(srv: dict) -> Tuple[bool, str]:
    cmd = ("echo \"OS: $(uname -sr)\"; echo \"Python: $(python3 -V 2>&1)\"; echo \"CPU cores: $(nproc)\"; "
           "echo \"RAM: $(free -m | sed -n 2p | awk '{print $2\" MB total, \"$7\" MB free\"}')\"; "
           "echo \"Disk: $(df -h / | tail -1 | awk '{print $4\" free of \"$2}')\"; echo \"Uptime: $(uptime -p 2>/dev/null)\"")
    try:
        code, out, err = await ssh_run(srv, cmd, timeout=30)
        ok = code == 0 or bool(out.strip())
        set_server_status(srv["id"], "ok" if ok else "fail")
        return ok, (out or err).strip()[:1500]
    except Exception as e:
        set_server_status(srv["id"], "fail")
        return False, f"{type(e).__name__}: {e}"


SETUP_SCRIPT = (
    "SUDO=''; [ \"$(id -u)\" = 0 ] || SUDO='sudo -n'; "
    "if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then "
    "  if command -v apt-get >/dev/null 2>&1; then "
    "    $SUDO apt-get update -y >/tmp/pb_setup.log 2>&1; "
    "    $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-pip python3-venv >>/tmp/pb_setup.log 2>&1; "
    "  fi; "
    "fi; "
    f"$SUDO mkdir -p {REMOTE_ROOT} && $SUDO chown \"$(id -un)\" {REMOTE_ROOT}; "
    "echo \"PY: $(python3 -V 2>&1)\"; "
    "python3 -m pip --version >/dev/null 2>&1 && echo PIP_OK || echo PIP_MISSING; "
    "echo SETUP_DONE"
)


async def ssh_setup(srv: dict) -> Tuple[bool, str]:
    try:
        code, out, err = await ssh_run(srv, SETUP_SCRIPT, timeout=900)
        ok = "SETUP_DONE" in out
        if ok:
            set_server_status(srv["id"], "ok")
        return ok, (out + ("\n" + err if err.strip() else "")).strip()[-1500:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def remote_alive(sid: int, bot_id: int) -> Optional[bool]:
    srv = get_server(sid)
    if not srv:
        return None
    d = remote_dir(bot_id)
    try:
        _, out, _ = await ssh_run(
            srv,
            f"PID=$(cat {d}/pid 2>/dev/null); [ -n \"$PID\" ] && kill -0 $PID 2>/dev/null && echo ALIVE || echo DEAD",
            timeout=25,
        )
        return "ALIVE" in out
    except Exception:
        return None


async def remote_stop(sid: int, bot_id: int, wipe: bool = False) -> None:
    srv = get_server(sid)
    if not srv:
        return
    d = remote_dir(bot_id)
    tail = f"rm -rf {d}" if wipe else "true"
    try:
        await ssh_run(
            srv,
            f"PID=$(cat {d}/pid 2>/dev/null); [ -n \"$PID\" ] && kill $PID 2>/dev/null; sleep 1; "
            f"[ -n \"$PID\" ] && kill -9 $PID 2>/dev/null; {tail}; echo STOPPED",
            timeout=40,
        )
    except Exception as e:
        logger.warning("remote_stop error: %s", e)


async def remote_logs(sid: int, bot_id: int) -> str:
    srv = get_server(sid)
    if not srv:
        return "❌ VPS server nahi mila."
    try:
        _, out, _ = await ssh_run(srv, f"tail -n 120 {remote_dir(bot_id)}/run.log 2>/dev/null || echo 'Abhi koi log nahi hai.'", timeout=25)
        return out.strip() or "Abhi koi log nahi hai."
    except Exception as e:
        return f"❌ VPS se logs nahi mile: {e}"


async def remote_pip(sid: int, bot_id: int, pkgs: List[str]) -> Tuple[bool, str]:
    srv = get_server(sid)
    if not srv:
        return False, "VPS server nahi mila."
    args = " ".join(shlex.quote(p) for p in pkgs)
    try:
        code, out, err = await ssh_run(
            srv, f"cd {remote_dir(bot_id)} && python3 -m pip install --user --break-system-packages {args} 2>&1 | tail -n 25",
            timeout=400)
        return code == 0, (out or err).strip()[-1000:]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ==============================================================================
# 🎨 EMOJI ENGINE + UI TEXTS
# ==============================================================================
EMOJI_SLOTS = [
    ("brand", "💎", "5264892613630111886"), ("profile", "👤", "6294257044526469584"), ("user_id", "🆔", "5474625972751837256"),
    ("name", "📛", "5215399540814781035"), ("gem", "💎", "5264892613630111886"), ("crown", "👑", "6246706873181932296"),
    ("free", "🆓", ""), ("slots", "📦", "6104726133528335361"), ("shield", "🛡", "6318816861525906943"),
    ("settings", "⚙️", "5267334530171169409"), ("check", "✅", "5219899949281453881"), ("cross", "❌", "5040042498634810056"),
    ("warn", "⚠", "6316488138847950002"), ("stats", "📊", "5042290883949495533"), ("server", "🖥", ""),
    ("ram", "💾", ""), ("disk", "💽", ""), ("bots", "🤖", "5983534401386976295"),
    ("back", "🔙", "5877536313623711363"), ("help", "❓", "5258387666616994756"), ("chat", "💬", ""),
    ("logs", "📋", "6005986106703613755"), ("file", "📄", ""), ("download", "📥", ""),
    ("upload", "📤", ""), ("phone", "📱", ""), ("play", "▶️", "6266829404849050968"),
    ("stop", "🛑", "6267262260243076354"), ("restart", "🔁", ""), ("clean", "🧹", ""),
    ("delete", "🗑", ""), ("pip", "📦", ""), ("bolt", "⚡", "5219943216781995020"),
    ("broadcast", "📢", "5889016933004612635"), ("users", "👥", "5904630315946611415"), ("search", "🔍", ""),
    ("point_down", "👇", "6269288372410258490"), ("calendar", "📅", "5927151848841547704"), ("clock", "⏳", ""),
    ("link", "🔗", "5778351117733729026"), ("video", "▶️", ""), ("channel", "📌", ""),
    ("backup", "🗂", ""), ("maint", "🔧", ""), ("ban", "🚫", "5219805369806629055"),
    ("limit", "📏", ""), ("emoji", "🎨", ""), ("edit", "✏️", ""),
    ("money", "💰", "6267068789146260253"), ("card", "💳", "5370871279551258810"), ("star", "⭐", "5258039825805624495"),
    ("fire", "🔥", "5222148368955877900"), ("rocket", "🚀", "5267255034621484253"), ("party", "🎉", "6267117038808870117"),
    ("heart", "❤", "5454249887690415056"), ("key", "🔑", "5278573677900752088"), ("globe", "🌐", "6318671502652740087"),
    ("trophy", "🏆", "6266973397922616654"), ("gift", "🎁", "6269214795325510848"), ("bell", "🔔", "5431374840332302296"),
    ("info", "ℹ️", ""), ("tools", "🛠", ""), ("pin", "📍", ""),
    ("wave", "👋", "6275959461643293224"), ("thumb", "👍", "6273836553798160975"), ("eyes", "👀", ""),
    ("cloud", "☁️", ""), ("laptop", "💻", ""), ("python", "🐍", ""),
    ("new", "🆕", ""), ("lightning", "💥", "5219901967916084166"), ("diamond", "💎", "5264892613630111886"),
    ("sparkles", "✨", "5222108309795908493"), ("medal", "🥇", "6265004494719816749"), ("coin", "🪙", "5237761614458933049"),
    ("vps", "🛰", ""), ("clone", "🧬", ""), ("owner", "👑", "6246706873181932296"),
    ("ludo", "🎲", "5235588635885054955"),
]
DEFAULT_EMOJIS: Dict[str, str] = {s: i for s, _f, i in EMOJI_SLOTS}
EMOJI_FALLBACK: Dict[str, str] = {s: f for s, f, _i in EMOJI_SLOTS}

_EMOJI_SLOT_MAP = {
    "🎬": "brand", "✨": "brand", "👤": "profile", "🆔": "user_id", "📛": "name",
    "💎": "gem", "👑": "crown", "🆓": "free", "📦": "slots", "🛡": "shield", "🔒": "shield",
    "⚙": "settings", "✅": "check", "🟢": "check", "🚫": "cross", "❌": "cross", "🔴": "cross",
    "🛑": "cross", "✖": "cross", "⚠": "warn", "😅": "warn", "📊": "stats", "📈": "stats",
    "🖥": "server", "💾": "ram", "💽": "disk", "🤖": "bots", "📱": "phone", "🔙": "back",
    "❓": "help", "💬": "chat", "📋": "logs", "📄": "file", "📥": "download", "📤": "upload",
    "▶": "play", "⏹": "stop", "🔁": "restart", "🔄": "restart", "🧹": "clean", "🗑": "delete",
    "⚡": "bolt", "📢": "broadcast", "👥": "users", "🔍": "search", "🔎": "search",
    "👇": "point_down", "📅": "calendar", "🗓": "calendar", "⏳": "clock", "⌛": "clock",
    "🔗": "link", "📌": "channel", "🗂": "backup", "🔧": "maint", "⛔": "ban", "📏": "limit",
    "🎨": "emoji", "✏": "edit", "💰": "money", "💳": "card",
    "⭐": "star", "🔥": "fire", "🚀": "rocket", "🎉": "party", "❤": "heart", "🔑": "key",
    "🌐": "globe", "🏆": "trophy", "🎁": "gift", "🔔": "bell", "ℹ": "info", "🛠": "tools",
    "📍": "pin", "👋": "wave", "👍": "thumb", "👀": "eyes", "☁": "cloud", "💻": "laptop",
    "🐍": "python", "🆕": "new", "🌩": "lightning", "🔷": "diamond", "🌟": "sparkles", "🥇": "medal",
    "🪙": "coin", "🛰": "vps", "🧬": "clone",
}
_SPLIT_RE = re.compile(r"(<pre.*?</pre>|<code.*?</code>|<tg-emoji.*?</tg-emoji>|<[^>]+>)", re.S)
_TRAIL_EMOJI_RE = re.compile(
    "(" + "|".join(sorted((re.escape(k) for k in _EMOJI_SLOT_MAP), key=len, reverse=True)) + ")\ufe0f?\\s*$"
)
_CUR_UI = contextvars.ContextVar("cur_ui", default=None)
YT_RE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/\S+$", re.I)
REF_START_RE = re.compile(r"^/start\s+ref_(\d+)\s*$")


def get_emoji_icon(slot: str, ui_key: Optional[str] = None) -> str:
    if get_setting("premium_emoji", "ON") == "OFF":
        return ""
    if ui_key:
        ov = get_setting(f"uiemoji_{ui_key}_{slot}", "")
        if ov.isdigit():
            return ov
    stored = get_setting(f"emoji_{slot}", "")
    if stored.isdigit():
        return stored
    return DEFAULT_EMOJIS.get(slot, "")


def get_emoji(slot: str, ui_key: Optional[str] = None) -> str:
    fb = EMOJI_FALLBACK.get(slot, "✨")
    icon = get_emoji_icon(slot, ui_key)
    return f'<tg-emoji emoji-id="{icon}">{fb}</tg-emoji>' if icon else fb


def _emap_key(ch: str) -> str:
    return "emap_" + "_".join(f"{ord(c):x}" for c in ch.replace("\ufe0f", ""))


def custom_emoji_map() -> Dict[str, str]:
    if _SETTINGS is None:
        _load_settings()
    out: Dict[str, str] = {}
    for k, v in (_SETTINGS or {}).items():
        if k.startswith("emap_") and str(v).isdigit():
            try:
                out["".join(chr(int(h, 16)) for h in k[5:].split("_"))] = str(v)
            except ValueError:
                pass
    return out


def _icon_for_char(ch: str) -> str:
    base = ch.replace("\ufe0f", "")
    cmap = custom_emoji_map()
    if base in cmap:
        return cmap[base]
    if base in _EMOJI_SLOT_MAP:
        return get_emoji_icon(_EMOJI_SLOT_MAP[base])
    return ""


_PLAIN_RE_CACHE: Dict[tuple, Any] = {}


def _plain_emoji_re():
    keys = tuple(sorted(set(_EMOJI_SLOT_MAP) | set(custom_emoji_map())))
    rx = _PLAIN_RE_CACHE.get(keys)
    if rx is None:
        rx = re.compile("(" + "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True)) + ")\ufe0f?")
        if len(_PLAIN_RE_CACHE) > 20:
            _PLAIN_RE_CACHE.clear()
        _PLAIN_RE_CACHE[keys] = rx
    return rx


def premiumize_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    rx = _plain_emoji_re()
    parts = _SPLIT_RE.split(text)

    def _sub(m):
        icon = _icon_for_char(m.group(1))
        return f'<tg-emoji emoji-id="{icon}">{m.group(0)}</tg-emoji>' if icon else m.group(0)

    for i in range(0, len(parts), 2):
        parts[i] = rx.sub(_sub, parts[i])
    return "".join(parts)


def premiumize_markup(markup):
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    changed = False
    rows = []
    for row in markup.inline_keyboard:
        new_row = []
        for b in row:
            try:
                if b.text and not getattr(b, "icon_custom_emoji_id", None):
                    t = b.text.strip()
                    m = _plain_emoji_re().match(t)
                    if m:
                        rest = t[m.end():].strip()
                    else:
                        m = _TRAIL_EMOJI_RE.search(t)
                        rest = t[:m.start()].strip() if m else ""
                    if m:
                        icon = _icon_for_char(m.group(1))
                        if icon:
                            b = b.model_copy(update={"text": rest or b.text, "icon_custom_emoji_id": icon})
                            changed = True
            except Exception:
                pass
            new_row.append(b)
        rows.append(new_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if changed else markup


def ui_video_button(key: str) -> Optional[InlineKeyboardButton]:
    link = get_setting(f"ui_yt_{key}", "")
    if not link:
        return None
    return InlineKeyboardButton(text="▶️ Watch Video", url=link)


def _attach_ui_video(method) -> None:
    try:
        cur = _CUR_UI.get()
        if not cur:
            return
        key, prefix = cur
        if "reply_markup" not in getattr(type(method), "model_fields", {}):
            return
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if not isinstance(text, str) or prefix not in text:
            return
        _CUR_UI.set(None)
        btn_ = ui_video_button(key)
        if not btn_:
            return
        rm = getattr(method, "reply_markup", None)
        if rm is None:
            rows = []
        elif isinstance(rm, InlineKeyboardMarkup):
            rows = [list(r) for r in rm.inline_keyboard]
        else:
            return
        if any(getattr(b, "url", None) == btn_.url for r in rows for b in r):
            return
        rows.insert(0, [btn_])
        setattr(method, "reply_markup", InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception as e:
        logger.warning("UI video attach skipped: %s", e)


async def premium_emoji_request_middleware(make_request, bot_, method):
    orig: Dict[str, Any] = {}
    try:
        _attach_ui_video(method)
        if get_setting("premium_emoji", "ON") != "OFF":
            fields = getattr(type(method), "model_fields", {})
            pm = getattr(method, "parse_mode", None)
            if "parse_mode" in fields and (pm is None or not isinstance(pm, str) or pm.upper() == "HTML"):
                for fld in ("text", "caption"):
                    val = getattr(method, fld, None)
                    if isinstance(val, str) and val:
                        new = premiumize_text(val)
                        if new != val:
                            orig[fld] = val
                            setattr(method, fld, new)
            rm = getattr(method, "reply_markup", None)
            if rm is not None:
                new_rm = premiumize_markup(rm)
                if new_rm is not rm:
                    orig["reply_markup"] = rm
                    setattr(method, "reply_markup", new_rm)
    except Exception as e:
        logger.warning("Premium emoji middleware skipped: %s", e)
    try:
        return await make_request(bot_, method)
    except TelegramBadRequest as e:
        if orig and "not modified" not in str(e).lower():
            for k, v in orig.items():
                setattr(method, k, v)
            logger.warning("Premium emoji rejected (%s) -> normal emoji se dobara bhej raha hu", e)
            return await make_request(bot_, method)
        raise


try:
    bot.session.middleware(premium_emoji_request_middleware)
except Exception as _e:
    logger.warning("Auto premium emoji enable nahi hua: %s", _e)

DEFAULT_UI: Dict[str, str] = {
    "home": (
        "╔═══════════════════╗\n"
        "   {brand} <b>{v_brand} v{v_version}</b> {brand}\n"
        "╚═══════════════════╝\n"
        "{check} Welcome, <b>{v_name}</b>!\n\n"
        "┏━━━━━━━━━━━━━━━━━┓\n"
        "┃ {profile} <b>Profile Card</b>\n"
        "┃ {user_id} ID: <code>{v_id}</code>\n"
        "┃ {gem} Plan: {v_plan}\n"
        "┃ {slots} Slots: <b>{v_slots}</b>\n"
        "┃ {coin} Coins: <b>{v_coins}</b>\n"
        "┃ {vps} VPS: {v_vps}\n"
        "┗━━━━━━━━━━━━━━━━━┛\n"
        "{point_down} <i>Neeche se option chuno:</i>"
    ),
    "profile": (
        "{profile} <b>My Profile</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{name} Name: {v_name}\n"
        "{user_id} ID: <code>{v_id}</code>\n"
        "{gem} Plan: {v_plan}\n"
        "{calendar} Premium Till: {v_till}\n"
        "{slots} Slots: {v_slots}\n"
        "{coin} Coins: <b>{v_coins}</b>\n"
        "{users} Invites: <b>{v_invites}</b>\n"
        "{vps} VPS: {v_vps}\n"
        "{calendar} Joined: {v_joined}\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
    "status": (
        "{stats} <b>Server Status</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{server} CPU: <b>{v_cpu}%</b>\n"
        "{ram} RAM: <b>{v_ram}%</b>\n"
        "{disk} Disk: <b>{v_disk}%</b>\n"
        "{bots} Active Bots: <b>{v_active}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
    "help": (
        "{help} <b>Help Guide</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{upload} 1. Apni <b>.py</b> file upload karo\n"
        "{check} 2. Admin approval ke baad bot start hoga\n"
        "{play} 3. <b>Start</b> button dabao\n"
        "{logs} 4. Crash ho to <b>Live Logs</b> dekho\n"
        "{pip} 5. Missing module ke liye <b>Pip Install</b>\n"
        "{gift} 6. Dost ko refer karo, <b>coins</b> kamao, slot lo\n"
        "{gem} 7. Zyada bots ke liye <b>Premium</b> lo\n\n"
        "{chat} Commands: /start /help /cancel"
    ),
    "upload_prompt": (
        "{upload} <b>Apni Python (.py) file bhejo</b>\n\n"
        "{slots} Slots: {v_slots}\n"
        "{limit} Max size: {v_max} MB"
    ),
    "maintenance": (
        "{maint} <b>Maintenance Mode</b>\n\n"
        "Service abhi update ho rahi hai. Thodi der baad dobara try karo."
    ),
    "join_required": (
        "{channel} <b>Pehle hamare channels join karo</b>\n\n"
        "Bot use karne aur hosting chalane ke liye ye sare channels join karna zaroori hai. "
        "Channel leave karoge to aapke bots band ho jayenge.\n"
        "Join karne ke baad <b>✅ Verify</b> dabao.\n\n"
        "{point_down} Neeche wale channels join karo."
    ),
    "buy": (
        "{gem} <b>Premium Plans</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Free me sirf <b>{v_free}</b> bots host kar sakte ho.\n"
        "Premium me <b>{v_prem}</b> bots tak.\n\n"
        "{point_down} <i>Plan chuno, phir payment karo:</i>"
    ),
    "pending": (
        "{clock} <b>Admin Approval Pending</b>\n\n"
        "Aapki file admin check kar raha hai. Approve hote hi bot start ho sakta hai."
    ),
    "refer": (
        "{gift} <b>Refer & Earn</b>\n━━━━━━━━━━━━━━━━━━\n"
        "{coin} Coins: <b>{v_coins}</b>\n"
        "{users} Invites: <b>{v_invites}</b>\n\n"
        "{point_down} Har naye user ke channels join + Verify karne par: <b>+{v_reward} coins</b>\n"
        "{coin} <b>{v_cost} coins</b> = <b>+1 Bot Slot</b>\n\n"
        "🔗 Aapka referral link:\n<code>{v_link}</code>\n━━━━━━━━━━━━━━━━━━"
    ),
    "ref_bonus": (
        "{gift} <b>Referral Bonus!</b>\nEk user ({v_name}) ne aapke link se join karke verify kiya.\n"
        "{coin} <b>+{v_reward} coins</b> mile. Balance: <b>{v_coins}</b>"
    ),
    "clone": (
        "{clone} <b>Clone Bot</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Is hosting bot ki ready-made copy <b>₹{v_price}</b> me milegi.\n"
        "Payment ke baad admin approve karega, phir aapse <b>Bot Token</b> aur <b>Admin ID</b> maanga jayega, "
        "aur clone bot apne aap chal jayega. Koi file nahi milegi.\n"
        "━━━━━━━━━━━━━━━━━━"
    ),
}

UI_EDITABLE: Dict[str, Tuple[str, str]] = {
    "home": ("Home / Start", "{v_brand} {v_version} {v_name} {v_id} {v_plan} {v_slots} {v_coins} {v_vps} + emoji"),
    "profile": ("My Profile", "{v_name} {v_id} {v_plan} {v_till} {v_slots} {v_coins} {v_invites} {v_vps} {v_joined} + emoji"),
    "status": ("Server Status", "{v_cpu} {v_ram} {v_disk} {v_active} + emoji slots"),
    "help": ("Help Guide", "emoji slots like {upload} {play} {logs}"),
    "upload_prompt": ("Upload Prompt", "{v_slots} {v_max} + emoji slots"),
    "maintenance": ("Maintenance Message", "emoji slots like {maint}"),
    "join_required": ("Join Channels Message", "{channel} {point_down} + emoji slots"),
    "buy": ("Premium Plans Text", "{v_free} {v_prem} + emoji slots"),
    "pending": ("Pending Approval Text", "{clock} + emoji slots"),
    "refer": ("Refer & Earn", "{v_coins} {v_invites} {v_reward} {v_cost} {v_link} + emoji slots"),
    "clone": ("Clone Bot Text", "{v_price} + emoji slots"),
}


def ui(key: str, **kwargs) -> str:
    template = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    for slot in DEFAULT_EMOJIS:
        tag = "{" + slot + "}"
        if tag in template:
            template = template.replace(tag, get_emoji(slot, ui_key=key))
    for k, v in kwargs.items():
        template = template.replace("{" + k + "}", str(v))
    try:
        _CUR_UI.set((key, template[:40]))
    except Exception:
        pass
    return template


# ==============================================================================
# 💾 EMOJI BACKUP (export / import / auto-backup)
# ==============================================================================
EMOJI_BACKUP_PREFIXES = ("emoji_", "uiemoji_", "emap_", "ui_", "premium_emoji", "lib_automap")


def build_emoji_backup() -> bytes:
    if _SETTINGS is None:
        _load_settings()
    settings = {k: v for k, v in (_SETTINGS or {}).items() if k.startswith(EMOJI_BACKUP_PREFIXES)}
    lib = db_query("SELECT emoji_id, char, name, pack FROM premium_emojis", fetchall=True) or []
    data = {
        "format": "prime_emoji_backup",
        "version": 1,
        "created": now_str(),
        "settings": settings,
        "library": [{"emoji_id": a, "char": b, "name": c, "pack": d} for a, b, c, d in lib],
    }
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def import_emoji_backup(raw: bytes) -> Tuple[int, int]:
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or data.get("format") != "prime_emoji_backup":
        raise ValueError("Ye prime emoji backup file nahi hai.")
    n_set = 0
    for k, v in (data.get("settings") or {}).items():
        if isinstance(k, str) and k.startswith(EMOJI_BACKUP_PREFIXES):
            set_setting(k, v)
            n_set += 1
    n_lib = 0
    for item in data.get("library") or []:
        if lib_add(item.get("emoji_id", ""), item.get("char", ""), name=item.get("name", ""), pack=item.get("pack", "")):
            n_lib += 1
    return n_set, n_lib


async def emoji_backup_watcher() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            raw = build_emoji_backup()
            h = hashlib.sha256(raw).hexdigest()
            if h != get_setting("last_emoji_backup_hash", ""):
                await bot.send_document(
                    ADMIN_ID,
                    BufferedInputFile(raw, filename=f"emoji_backup_{datetime.now():%Y%m%d_%H%M}.json"),
                    caption="💾 <b>Auto emoji backup</b> (emoji set me change hua hai)",
                )
                set_setting("last_emoji_backup_hash", h)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("emoji_backup_watcher error")
        await asyncio.sleep(EMOJI_BACKUP_EVERY)


# ==============================================================================
# 🧬 CLONE SOURCE (token / admin / API key hata ke)
# ==============================================================================
def clone_source_bytes() -> bytes:
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as f:
        src = f.read()
    src = re.sub(r'^MAIN_BOT_TOKEN = .*$', 'MAIN_BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()', src, count=1, flags=re.M)
    src = re.sub(r'^ADMIN_ID = .*$', 'ADMIN_ID = 0', src, count=1, flags=re.M)
    src = re.sub(r'^PAY_API_KEY = .*$', 'PAY_API_KEY = os.getenv("PAY_API_KEY", "").strip()', src, count=1, flags=re.M)
    return src.encode("utf-8")


# ==============================================================================
# 🧠 STATES
# ==============================================================================
class HostingStates(StatesGroup):
    wait_file = State()
    wait_pip_package = State()


class CloneStates(StatesGroup):
    wait_token = State()
    wait_admin = State()


class PayStates(StatesGroup):
    wait_proof = State()


class AdminStates(StatesGroup):
    wait_input = State()
    wait_ui_text = State()
    wait_ui_video = State()
    wait_ui_emoji = State()
    wait_emoji_value = State()
    wait_emap = State()
    wait_lib_add = State()
    wait_lib_find = State()
    wait_lib_char = State()
    wait_lib_name = State()
    wait_emoji_import = State()
    wait_vps_input = State()


# ==============================================================================
# 🛡 SECURITY: file scanner, SHA lock, folder hardening
# ==============================================================================
PENDING_SCORE = 4   # is score se upar file admin review me jayegi (critical ho to hamesha)

# (regex, weight, label, critical)
SCAN_RULES = [
    (r"hosting\.db|hosted_files|prime_hosting|PAY_API_KEY", 6,
     "Hosting ke andar ki files/settings ka reference", True),
    (r"/proc/|/etc/(passwd|shadow|sudoers)|/root/|\.ssh/|id_rsa", 6,
     "System ya secret files ka access", True),
    (r"/bin/(ba)?sh|/bin/zsh|pty\.spawn|\bdup2\(|\bnc\s+-e\b", 6,
     "Reverse shell jaisa pattern", True),
    (r"\bptrace\b|\bctypes\b|process_vm_|/dev/mem|/dev/sd", 5,
     "Low-level memory/process/disk access", True),
    (r"(base64|zlib|marshal|codecs)[^\n]{0,80}(exec|eval)\s*\(|(exec|eval)\s*\(\s*(base64|zlib|marshal|codecs)", 6,
     "Obfuscated code run karna", True),
    (r"os\.walk\(\s*['\"]/['\"]|glob\(\s*['\"]/|os\.listdir\(\s*['\"]/['\"]|rglob\(\s*['\"]/", 5,
     "Root filesystem scan", True),
    (r"shutil\.rmtree|os\.removedirs|os\.unlink\(\s*['\"]/", 3, "Folder/file delete", False),
    (r"\bos\.system\(|\bos\.popen\(|\bsubprocess\.", 3, "Shell/command run", False),
    (r"\bexec\s*\(|\beval\s*\(|__import__\(|(?<![.\w])compile\s*\(", 3, "Dynamic code run", False),
    (r"\bos\.environ\b|\bgetenv\(", 2, "Environment variables padhna", False),
    (r"discord(app)?\.com/api/webhooks|pastebin\.com|ngrok|transfer\.sh|requests\.post\(\s*['\"]http", 4,
     "Data bahar bhejne ka pattern", False),
    (r"\bsocket\b", 2, "Raw socket use", False),
    (r"getChatMember|setWebhook", 1, "Telegram admin/webhook manipulation", False),
]


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_bot_source(text: str) -> Tuple[int, List[str], bool]:
    """Returns (score, reasons, critical). Hosting/secret access ya reverse shell = critical."""
    score = 0
    reasons: List[str] = []
    critical = False
    for rx, weight, label, crit in SCAN_RULES:
        if re.search(rx, text, flags=re.I):
            score += weight
            reasons.append(label)
            critical = critical or crit
    if any(len(line) > 1500 for line in text.splitlines()):
        score += 3
        reasons.append("Bahut lamba line (obfuscation ka shak)")
    return score, reasons, critical


def _chown_child(path: str) -> None:
    if os.getuid() == 0 and CHILD_UID:
        try:
            os.chown(path, CHILD_UID, CHILD_UID)
        except OSError:
            pass


def secure_bot_folder(folder: str, file_path: str) -> None:
    """Folder child UID ka (sirf wahi padh/likh sake); .py file root ki, read-only."""
    try:
        os.makedirs(folder, exist_ok=True)
        _chown_child(folder)
        os.chmod(folder, 0o700)
        if os.path.exists(file_path):
            if os.getuid() == 0:
                os.chown(file_path, 0, 0)
            os.chmod(file_path, 0o444)
    except OSError as e:
        logger.warning("Bot folder secure nahi hua: %s", e)


def harden_data_dirs() -> None:
    """Hosting DB/secret folder ko bots se chhupao: traverse ho sake, list/read nahi."""
    try:
        os.chmod(DATA_DIR, 0o711)
        os.makedirs(HOST_DIR, exist_ok=True)
        os.chmod(HOST_DIR, 0o711)
        if os.path.exists(DB_PATH):
            os.chmod(DB_PATH, 0o600)
    except OSError as e:
        logger.warning("Data dir harden nahi hua: %s", e)


def approve_current_sha(bot_id: int) -> None:
    hb = get_bot(bot_id)
    if hb and os.path.exists(hb["file_path"]):
        db_query("UPDATE hosted_bots SET file_sha=? WHERE id=?", (file_sha256(hb["file_path"]), bot_id))


# ==============================================================================
# 🤖 BOT MANAGER (local + VPS)
# ==============================================================================
CHILD_ENV_WHITELIST = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")


def child_env(home_dir: str) -> Dict[str, str]:
    env = {k: os.environ[k] for k in CHILD_ENV_WHITELIST if k in os.environ}
    env.update({"HOME": home_dir, "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _child_preexec():
    try:
        import resource
        mem = 512 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        fsize = 100 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    except Exception:
        pass
    if CHILD_UID and os.getuid() == 0:
        try:
            os.setgid(CHILD_UID)
            os.setuid(CHILD_UID)
        except Exception:
            pass


class BotInstance:
    def __init__(self, bot_id: int, user_id: int, file_path: str, auto_restart: bool):
        self.bot_id = bot_id
        self.user_id = user_id
        self.file_path = file_path
        self.auto_restart = auto_restart
        self.process: Optional[asyncio.subprocess.Process] = None
        self.logs: deque = deque(maxlen=300)
        self.is_running = False
        self.should_run = False
        self.crashes = 0
        self.started_at = 0.0
        self.vps = False
        self.remote_sid = 0          # >0 = bot VPS par chal raha hai
        self._lock = asyncio.Lock()

    def log(self, text: str):
        self.logs.append(text)

    @property
    def server_name(self) -> str:
        srv = get_server(self.remote_sid) if self.remote_sid else None
        return srv["name"] if srv else ""

    async def clear_webhook(self):
        try:
            with open(self.file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            match = re.search(r"[0-9]{8,10}:[a-zA-Z0-9_-]{35}", content)
            if match:
                async with aiohttp.ClientSession() as session:
                    await session.get(f"https://api.telegram.org/bot{match.group(0)}/deleteWebhook?drop_pending_updates=true")
                self.log("✅ Webhook cleared.")
        except Exception:
            pass

    async def security_gate(self) -> Optional[str]:
        """Start se pehle: approval + SHA check. Error string = start nahi hoga."""
        hb_now = get_bot(self.bot_id) or {}
        if hb_now.get("approval") != "approved":
            return "⏳ Admin approval nahi hai. Bot start nahi hoga."
        cur = file_sha256(self.file_path)
        row = db_query("SELECT file_sha FROM hosted_bots WHERE id=?", (self.bot_id,), fetchone=True)
        want = (row[0] if row else "") or ""
        if want and cur == want:
            return None
        try:
            with open(self.file_path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            content = ""
        score, reasons, critical = scan_bot_source(content)
        if not want and not (critical or score >= PENDING_SCORE):
            db_query("UPDATE hosted_bots SET file_sha=? WHERE id=?", (cur, self.bot_id))
            return None
        reason = "🚨 File badal gayi hai (tampering)." if want else "🔎 Purani file par naya security check fail."
        set_approval(self.bot_id, "pending")
        self.should_run = False
        self.log(reason + " Admin approval chahiye.")
        try:
            body = reason + "\n" + "\n".join(f"• {esc(r)}" for r in reasons)
            await notify_admin_bot(hb_now, hb_now.get("user_id") or 0, "", body,
                                   markup=approval_markup(self.bot_id), prefix="🚨 Security alert")
        except Exception as e:
            logger.warning("Security alert send fail: %s", e)
        return reason + " Admin approval pending."

    async def start(self) -> bool:
        async with self._lock:
            if self.is_running:
                return True
            u = get_user(self.user_id) or {}
            self.vps = bool(u.get("vps"))
            self.remote_sid = int(u.get("vps_server_id") or 0) if self.vps else 0
            if self.remote_sid and not get_server(self.remote_sid):
                self.remote_sid = 0
            self.should_run = True
            self.logs.clear()
            self.log("⏳ Booting process...")

            if not os.path.exists(self.file_path):
                self.log("❌ Error: File not found.")
                return False

            gate_err = await self.security_gate()
            if gate_err:
                self.log(gate_err)
                return False
            secure_bot_folder(os.path.dirname(self.file_path), self.file_path)

            if self.remote_sid:
                return await self._start_remote()

            await self.clear_webhook()
            try:
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", self.file_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=os.path.dirname(self.file_path),
                    env=child_env(os.path.dirname(self.file_path)),
                    preexec_fn=_child_preexec,
                    start_new_session=True,
                )
            except Exception as e:
                self.log(f"❌ System Error: {e}")
                self.is_running = False
                return False

            self.is_running = True
            self.started_at = time.time()
            set_status(self.bot_id, "Running")
            self.log("🚀 Bot is live.")
            asyncio.create_task(self._watch(self.process))
            return True

    async def _start_remote(self) -> bool:
        srv = get_server(self.remote_sid)
        d = remote_dir(self.bot_id)
        fname = os.path.basename(self.file_path)
        try:
            # Pehle se chal raha ho to naya process mat banao
            if await remote_alive(self.remote_sid, self.bot_id):
                self.is_running = True
                set_status(self.bot_id, "Running")
                self.log(f"🛰 VPS ({srv['name']}) par pehle se chal raha hai.")
                return True
            conn = await ssh_conn(srv)
            try:
                await conn.run(f"mkdir -p {d}", check=True)
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(self.file_path, f"{d}/{fname}")
                r = await conn.run(
                    f"cd {d} && (nohup python3 -u {shlex.quote(fname)} < /dev/null >> run.log 2>&1 & echo $! > pid); cat {d}/pid",
                    check=False, timeout=40)
                pid = (r.stdout or "").strip()
            finally:
                conn.close()
                try:
                    await conn.wait_closed()
                except Exception:
                    pass
        except Exception as e:
            self.log(f"❌ VPS error: {type(e).__name__}: {e}")
            self.is_running = False
            return False
        self.is_running = True
        self.started_at = time.time()
        set_status(self.bot_id, "Running")
        self.log(f"🛰 VPS <{srv['name']}> par bot live. PID {pid or '?'}")
        return True

    async def _watch(self, proc):
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore").rstrip()
            if text:
                self.log(text)

        code = await proc.wait()
        if proc is not self.process:
            return
        self.is_running = False
        self.log(f"⚠️ Bot stopped (Code: {code})")
        set_status(self.bot_id, "Stopped")

        if not self.should_run or not (self.auto_restart or self.vps):
            return
        if time.time() - self.started_at > 300:
            self.crashes = 0
        self.crashes += 1
        cap = VPS_CRASH_CAP if self.vps else MAX_CRASH_RESTARTS
        if self.crashes <= cap:
            self.log(f"🔄 Crash Guard: restart {self.crashes} in 5s...")
            await asyncio.sleep(5)
            if self.should_run:
                await self.start()
        else:
            self.should_run = False
            self.log("🛑 Crash Guard: bahut zyada crashes, bot band kar diya gaya.")

    async def stop(self):
        self.should_run = False
        if self.remote_sid:
            await remote_stop(self.remote_sid, self.bot_id)
        elif self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except asyncio.TimeoutError:
                self.process.kill()
        self.is_running = False
        set_status(self.bot_id, "Stopped")

    def kill_silently(self):
        """Shutdown par: DB status nahi badalta. VPS wale bots chalte rehte hain."""
        self.should_run = False
        if not self.remote_sid and self.process and self.process.returncode is None:
            self.process.terminate()


ACTIVE_BOTS: Dict[int, BotInstance] = {}


def get_instance(bot_id: int) -> Optional[BotInstance]:
    if bot_id in ACTIVE_BOTS:
        return ACTIVE_BOTS[bot_id]
    hb = get_bot(bot_id)
    if not hb:
        return None
    inst = BotInstance(bot_id, hb["user_id"], hb["file_path"], bool(hb["auto_restart"]))
    ACTIVE_BOTS[bot_id] = inst
    return inst


async def vps_watchdog() -> None:
    """VPS bots ko 24x7 zinda rakhta hai. VPS expire ho to remote bot band karta hai."""
    await asyncio.sleep(25)
    while True:
        try:
            rows = db_query("SELECT id, user_id FROM hosted_bots WHERE status='Running' AND approval='approved'", fetchall=True) or []
            for bid, owner in rows:
                inst = get_instance(bid)
                if not inst:
                    continue
                is_v = vps_user(owner) or owner == ADMIN_ID
                if inst.remote_sid:
                    if not vps_user(owner):
                        await inst.stop()
                        try:
                            await bot.send_message(owner, f"🛰 <b>VPS expire ho gaya.</b>\nBot <code>{esc(bid)}</code> band kar diya gaya hai. VPS lene ke liye admin se contact karo.")
                        except Exception:
                            pass
                        continue
                    alive = await remote_alive(inst.remote_sid, bid)
                    if alive is False:
                        logger.info("🛰 VPS watchdog: bot %s dobara start", bid)
                        inst.is_running = False
                        await inst.start()
                elif is_v and not inst.is_running and vps_user(owner):
                    await inst.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("vps_watchdog error")
        await asyncio.sleep(VPS_WATCHDOG_INTERVAL)


# ==============================================================================
# 🎨 KEYBOARDS & TEXT
# ==============================================================================
def btn(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def owner_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="👑 Owner — Contact", url=f"https://t.me/{OWNER_USERNAME}")


def main_menu(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [btn("📤 Upload Bot (.py)", "upload")],
        [btn("📱 My Hosted Bots", "my_bots")],
        [btn("👤 My Profile", "profile"), btn("📊 Server Status", "status")],
        [btn("💎 Buy Premium", "buy"), btn("🎁 Refer & Earn", "refer")],
        [btn("💰 My Coins", "my_coins"), btn("👥 My Refer", "my_refer")],
        [btn("🏆 Lead Board", "lead"), btn("🧬 Clone Bot", "clone")],
        [btn("❓ Help", "help")],
        [owner_button()],
    ]
    if uid == ADMIN_ID:
        rows.append([btn("👑 ADMIN PANEL", "admin_main")])
    return kb(*rows)


def bot_panel_kb(bot_id: int, auto_on: bool, viewer_premium: bool, approval: str = "approved") -> InlineKeyboardMarkup:
    gem = "" if viewer_premium else " 💎"
    auto_label = f"⚡ Auto-Restart: {'🟢 ON' if auto_on else '🔴 OFF'}{gem}"
    dl_label = "📥 Download Logs 💎" if not viewer_premium else "📥 Download Logs"
    return kb(
        [btn("🟢 Start", f"start:{bot_id}"), btn("🔴 Stop", f"stop:{bot_id}"), btn("🔁 Restart", f"restart:{bot_id}")],
        [btn("📋 Live Logs", f"logs:{bot_id}"), btn("🧹 Clear Logs", f"clearlog:{bot_id}")],
        [btn("📦 Pip Install", f"pip:{bot_id}"), btn(auto_label, f"toggle:{bot_id}")],
        [btn(dl_label, f"dllog:{bot_id}"), btn("📄 Download File", f"dlfile:{bot_id}")],
        [btn("🗑 Delete Bot", f"del:{bot_id}")],
        [btn("🔙 Back", "my_bots")],
    )


def admin_main_menu() -> InlineKeyboardMarkup:
    maint = get_setting("maintenance", "OFF")
    pe = get_setting("premium_emoji", "ON")
    fa = "ON" if fast_approve_on() else "OFF"
    return kb(
        [btn("━━━━ 📊 USERS & BOTS ━━━━", "noop")],
        [btn("📈 Stats", "adm_stats"), btn("👥 Users", "adm_users")],
        [btn("🤖 All Bots", "adm_bots"), btn("🔎 Open Bot by ID", "adm_in:open_bot")],
        [btn("⏳ Pending Bots", "adm_pending"), btn("🔍 User Info", "adm_in:info")],
        [btn("⛔ Ban User", "adm_in:ban"), btn("✅ Unban User", "adm_in:unban")],
        [btn("🛑 Stop User Bots", "adm_in:stop_user"), btn("⛔ Banned List", "adm_ban_list")],
        [btn("📢 Broadcast", "adm_in:broadcast"), btn("📢 Broadcast Premium", "adm_in:broadcast_prem")],
        [btn("━━━━ 💎 PAYMENTS & PLANS ━━━━", "noop")],
        [btn("💳 Payments", "adm_pay"), btn("💰 Plans & Prices", "adm_plans")],
        [btn("💎 Give Premium", "adm_in:give_prem"), btn("🚫 Remove Premium", "adm_in:rm_prem")],
        [btn("💎 Premium List", "adm_prem_list"), btn("🧬 Clone Price", "adm_in:clone_price")],
        [btn("💳 Payment Setup", "adm_paycfg"), btn("📌 Force Join Channels", "adm_chans")],
        [btn("━━━━ 🛰 VPS & REFERRAL ━━━━", "noop")],
        [btn("🖥 VPS Manager", "adm_vps"), btn("🛰 VPS Grant", "adm_in:vps_give")],
        [btn("🎁 Referral Settings", "adm_in:ref_cfg"), btn("📏 Free Limit", "adm_in:limit_free")],
        [btn("🏆 Lead: Naam Add", "adm_in:lead_add"), btn("🗑 Lead: Clear", "adm_lead_clear")],
        [btn("📏 Premium Limit", "adm_in:limit_prem")],
        [btn("━━━━ 🎨 DESIGN & EMOJI ━━━━", "noop")],
        [btn("✏️ Edit UI Texts", "adm_ui"), btn("🎨 Edit Emojis", "adm_emoji")],
        [btn("🧰 Premium Emoji Library", "adm_lib:0"), btn("🗺 Emoji Map", "adm_emap")],
        [btn("💾 Export Emoji Backup", "adm_emb_export"), btn("📥 Import Emoji Backup", "adm_emb_import")],
        [btn(f"✨ Premium Emoji: {pe}", "adm_toggle_pe")],
        [btn("━━━━ ⚙️ SYSTEM ━━━━", "noop")],
        [btn(f"⚡ Fast Approve: {fa}", "adm_fast"), btn(f"🔧 Maintenance: {maint}", "adm_maint")],
        [btn("🗂 Backup DB", "adm_backup"), btn("🛑 Stop All Bots", "adm_stopall")],
        [btn("🔙 Back to Main", "back_main")],
    )


def vps_menu() -> InlineKeyboardMarkup:
    rows = []
    for s in list_servers():
        icon = {"ok": "🟢", "fail": "🔴"}.get(s["status"], "⚪")
        rows.append([btn(f"{icon} {s['name']}  ·  {s['host']}", f"vsv:{s['id']}")])
    rows.append([btn("➕ Add VPS Server", "vps_add")])
    rows.append([btn("🔙 Admin Panel", "admin_main")])
    return kb(*rows)


ADMIN_PROMPTS = {
    "broadcast": "📢 <b>Broadcast Message</b>\nJo bhi bhejna hai (text / photo / file) wo abhi send karo.",
    "broadcast_prem": "📢 <b>Broadcast (sirf Premium users)</b>\nJo bhejna hai (text / photo / file) wo abhi send karo.",
    "give_prem": "💎 <b>Give Premium</b>\nFormat: <code>USER_ID DAYS</code>\n(DAYS = 0 matlab Lifetime ♾)",
    "rm_prem": "🚫 <b>Remove Premium</b>\nUser ID bhejo.",
    "vps_give": "🛰 <b>VPS Grant</b>\nFormat: <code>USER_ID DAYS</code>\n(DAYS = 0 matlab VPS hata do)\nAgle step me VPS server chunna hoga.",
    "lead_add": "🏆 <b>Lead Board me naam add karo</b>\nFormat: <code>NAME COUNT</code>\nExample: <code>Rahul 25</code>\n(Ye entry Lead Board par dikhegi, real referral me nahi jodi jayegi)",
    "ref_cfg": "🎁 <b>Referral Settings</b>\nFormat: <code>REWARD COST</code> (e.g. <code>3 5</code>)\nREWARD = har referral par coins, COST = 1 slot ke coins.",
    "clone_price": "🧬 <b>Clone Bot ki price</b>\nSirf number bhejo (e.g. <code>60</code>). Currency: ₹",
    "ban": "⛔ <b>Ban User</b>\nUser ID bhejo.",
    "unban": "✅ <b>Unban User</b>\nUser ID bhejo.",
    "info": "🔍 <b>User Info</b>\nUser ID bhejo.",
    "open_bot": "🔎 <b>Open Bot by ID</b>\nHosted Bot ka ID bhejo.",
    "stop_user": "🛑 <b>Stop User Bots</b>\nUser ID bhejo — uske saare running bots band ho jayenge.",
    "limit_free": "📏 <b>Free Bot Limit</b>\nFree users ke liye naya max bots number bhejo (1-1000).",
    "limit_prem": "📏 <b>Premium Bot Limit</b>\nPremium users ke liye naya max bots number bhejo (1-1000).",
    "chan_add": "📌 <b>Naya Force-Join Channel add karo</b>\nChannel ka <code>@username</code> ya link bhejo.\n⚠️ Bot ko us channel me <b>admin</b> banana zaroori hai.",
    "upi_set": "💳 <b>UPI ID set karo</b>\nFormat: <code>name@bank</code> (e.g. <code>yourname@okaxis</code>)",
    "api_url": "🔗 <b>Payment API URL set karo</b>\nPayment order banane wala endpoint bhejo (https://...).",
    "api_key": "🔑 <b>Payment API Key set karo</b>\nKey bhejo (ye message delete kar diya jayega).",
    "plan_add": "➕ <b>Naya Plan add karo</b>\nFormat: <code>DAYS PRICE</code> (e.g. <code>15 79</code>)",
}


def admin_prompt_text(action: str, extra: str = "") -> str:
    text = ADMIN_PROMPTS.get(action, "")
    if action == "chan_add":
        chans = get_channels()
        text += "\n\nCurrent: " + (", ".join(esc(c) for c in chans) if chans else "<i>none</i>")
    elif action == "upi_set":
        text += f"\n\nCurrent: <b>{esc(get_setting('upi_id', '') or 'not set')}</b>"
    elif action == "limit_free":
        text += f"\nCurrent: <b>{bot_limit({'premium': False})}</b>"
    elif action == "limit_prem":
        text += f"\nCurrent: <b>{bot_limit({'premium': True})}</b>"
    elif action == "clone_price":
        text += f"\nCurrent: <b>₹{esc(get_setting('clone_price', CLONE_PRICE_DEFAULT))}</b>"
    elif action == "ref_cfg":
        text += f"\nCurrent: <b>{ref_reward()} coins / referral, {coin_cost()} coins = 1 slot</b>"
    elif action == "plan_price":
        text = f"💰 <b>Plan {extra} day(s) — naya price bhejo</b>\nSirf number bhejo (e.g. <code>99</code>). Currency: ₹"
    return text


def premium_expiry_text(u: dict) -> str:
    if not u["is_premium"]:
        return "—"
    if not u["premium_until"]:
        return "♾ Lifetime"
    try:
        until = datetime.fromisoformat(u["premium_until"])
    except ValueError:
        return "—"
    if until > datetime.now():
        return f"⏳ {until.strftime('%d %b %Y, %H:%M')}"
    return "⌛ Expired"


def _plan_label(u: dict) -> str:
    return f"{get_emoji('crown')} PREMIUM" if u["premium"] else f"{get_emoji('free')} FREE"


def _vps_label(u: dict) -> str:
    if not u.get("vps"):
        return "—"
    srv = get_server(u.get("vps_server_id") or 0)
    return f"✅ {esc(srv['name'])}" if srv else "✅ Active (server pending)"


def home_text(u: dict, name: str) -> str:
    return ui("home", v_brand=BRAND, v_version=VERSION, v_name=esc(name), v_id=u["user_id"],
              v_plan=_plan_label(u), v_slots=f"{user_bot_count(u['user_id'])}/{user_limit(u)}",
              v_coins=u["coins"], v_vps=_vps_label(u))


def profile_text(u: dict, name: str) -> str:
    return ui("profile", v_name=esc(name), v_id=u["user_id"], v_plan=_plan_label(u), v_till=premium_expiry_text(u),
              v_slots=f"{user_bot_count(u['user_id'])}/{user_limit(u)}", v_coins=u["coins"],
              v_invites=invite_count(u["user_id"]), v_vps=_vps_label(u), v_joined=u["joined_at"] or "—")


def refer_view(uid: int):
    u = get_user(uid) or {"coins": 0}
    link = f"https://t.me/{_BOT_USERNAME}?start=ref_{uid}" if _BOT_USERNAME else "(bot username load nahi hua)"
    text = ui("refer", v_coins=u["coins"], v_invites=invite_count(uid), v_reward=ref_reward(),
              v_cost=coin_cost(), v_link=link)
    rows = []
    if u["coins"] >= coin_cost():
        rows.append([btn(f"🪙 {coin_cost()} coins se +1 Slot lo", "redeem")])
    rows.append([btn("💰 My Coins", "my_coins"), btn("👥 My Refer", "my_refer")])
    rows.append([btn("🏆 Lead Board", "lead")])
    rows.append([btn("🔙 Back", "back_main")])
    return text, kb(*rows)


def server_stats_text() -> str:
    cpu = psutil.cpu_percent(interval=0.3)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    active = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    return ui("status", v_cpu=cpu, v_ram=ram, v_disk=disk, v_active=active)


def my_bots_view(uid: int):
    rows = db_query("SELECT id, bot_name, approval FROM hosted_bots WHERE user_id=? ORDER BY id DESC", (uid,), fetchall=True) or []
    if not rows:
        return (
            "📭 <b>Abhi koi bot hosted nahi hai.</b>\n\n📤 Pehla bot upload karo!",
            kb([btn("📤 Upload Bot (.py)", "upload")], [btn("🔙 Back", "back_main")]),
        )
    buttons = []
    for bid, name, approval in rows:
        inst = ACTIVE_BOTS.get(bid)
        if approval == "pending":
            icon = "⏳"
        elif approval == "rejected":
            icon = "❌"
        else:
            icon = "🟢" if inst and inst.is_running else "🔴"
        buttons.append([btn(f"{icon} {name}", f"view:{bid}")])
    buttons.append([btn("🔙 Back", "back_main")])
    text = f"📱 <b>My Hosted Bots</b> ({len(rows)})\n━━━━━━━━━━━━━━━━━━\n🟢 Running  |  🔴 Stopped  |  ⏳ Pending  |  ❌ Rejected"
    return text, kb(*buttons)


def stats_text() -> str:
    users = db_query("SELECT user_id, is_premium, premium_until, is_banned FROM users", fetchall=True) or []
    total = len(users)
    premium = sum(1 for r in users if is_premium_active({"is_premium": r[1], "premium_until": r[2]}))
    banned = sum(1 for r in users if r[3] == 1)
    bots_total = (db_query("SELECT COUNT(*) FROM hosted_bots", fetchone=True) or (0,))[0]
    pending = (db_query("SELECT COUNT(*) FROM hosted_bots WHERE approval='pending'", fetchone=True) or (0,))[0]
    pay_pending = (db_query("SELECT COUNT(*) FROM payments WHERE status='pending' AND proof_id!=''", fetchone=True) or (0,))[0]
    running = sum(1 for b in ACTIVE_BOTS.values() if b.is_running)
    vps_srv = (db_query("SELECT COUNT(*) FROM vps_servers", fetchone=True) or (0,))[0]
    return (
        f"📈 <b>Platform Stats</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: <b>{total}</b>\n"
        f"💎 Premium Users: <b>{premium}</b>\n"
        f"🚫 Banned Users: <b>{banned}</b>\n"
        f"🤖 Total Bots: <b>{bots_total}</b>\n"
        f"🟢 Running Bots: <b>{running}</b>\n"
        f"🛰 VPS Servers: <b>{vps_srv}</b>\n"
        f"⏳ Pending Bot Approvals: <b>{pending}</b>\n"
        f"💳 Pending Payment Approvals: <b>{pay_pending}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


async def safe_edit(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.message.answer(text, reply_markup=markup)


# ==============================================================================
# 🛡️ MIDDLEWARE
# ==============================================================================
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user and not user.is_bot and user.id != ADMIN_ID:
            existed = db_query("SELECT 1 FROM users WHERE user_id=?", (user.id,), fetchone=True) is not None
            ensure_user(user.id, user.first_name or "", user.username or "")
            if not existed and isinstance(event, Message) and event.text:
                m = REF_START_RE.match(event.text.strip())
                if m:
                    capture_referral(user.id, int(m.group(1)))
            row = get_user(user.id)
            if row and row["banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("🚫 Aap banned hain.", show_alert=True)
                else:
                    await event.answer(BANNED_TEXT)
                return None
        return await handler(event, data)


class RateLimitMiddleware(BaseMiddleware):
    WINDOW = 5.0
    MAX_EVENTS = 8

    def __init__(self):
        super().__init__()
        self._hits: Dict[int, deque] = {}

    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if user and user.id != ADMIN_ID:
            now = time.time()
            if len(self._hits) > 20000:
                self._hits.clear()
            q = self._hits.setdefault(user.id, deque())
            while q and now - q[0] > self.WINDOW:
                q.popleft()
            if len(q) >= self.MAX_EVENTS:
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer("⏳ Thoda ruko...")
                    except Exception:
                        pass
                return None
            q.append(now)
        return await handler(event, data)


dp.message.outer_middleware(RateLimitMiddleware())
dp.callback_query.outer_middleware(RateLimitMiddleware())
dp.message.outer_middleware(BanMiddleware())
dp.callback_query.outer_middleware(BanMiddleware())

# ---------------------------- Force-join channels ----------------------------
_join_cache: Dict[Tuple[int, str], float] = {}
_join_warned: set = set()


def normalize_channel(text: str) -> Optional[str]:
    t = text.strip()
    m = re.search(r"(?:t\.me/|@)?([A-Za-z0-9_]{5,32})/?$", t)
    return "@" + m.group(1) if m else None


async def _check_member(uid: int, channel: str) -> Optional[bool]:
    try:
        cm = await bot.get_chat_member(channel, uid)
    except Exception as e:
        if channel not in _join_warned:
            _join_warned.add(channel)
            logger.error("Force-join check failed for %s (%s)", channel, e)
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Force-join check fail: {esc(channel)}\n<code>{esc(e)}</code>\n"
                                                 f"Bot ko is channel me <b>admin</b> banao.")
            except Exception:
                pass
        return None
    status = str(getattr(cm.status, "value", cm.status))
    return status in ("member", "administrator", "creator") or (
        status == "restricted" and bool(getattr(cm, "is_member", False)))


async def _is_member(uid: int, channel: str, use_cache: bool = True) -> bool:
    key = (uid, channel)
    now = time.time()
    if use_cache and _join_cache.get(key, 0) > now:
        return True
    ok = await _check_member(uid, channel)
    if ok is None:
        return True
    if ok:
        _join_cache[key] = now + CHANNEL_CACHE_TTL
    else:
        _join_cache.pop(key, None)
    return ok


async def missing_channels(uid: int, use_cache: bool = True) -> List[str]:
    missing = []
    for ch in get_channels():
        if not await _is_member(uid, ch, use_cache):
            missing.append(ch)
    return missing


def join_kb(missing: List[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📌 Join {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in missing]
    rows.append([btn("✅ Verify", "check_join")])
    return kb(*rows)


class GateMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[..., Awaitable[Any]], event: Any, data: Dict[str, Any]) -> Any:
        user = getattr(event, "from_user", None)
        if not user or user.is_bot or user.id == ADMIN_ID:
            return await handler(event, data)

        if get_setting("maintenance", "OFF") == "ON":
            if isinstance(event, CallbackQuery):
                await event.answer("🔧 Maintenance chal raha hai. Thodi der baad try karo.", show_alert=True)
            else:
                await event.answer(ui("maintenance"))
            return None

        if isinstance(event, CallbackQuery) and event.data == "check_join":
            return await handler(event, data)

        missing = await missing_channels(user.id)
        if missing:
            text = ui("join_required")
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("❌ Pehle sare channels join karo!", show_alert=True)
                except Exception:
                    pass
                try:
                    await event.message.answer(text, reply_markup=join_kb(missing))
                except Exception:
                    pass
            else:
                await event.answer(text, reply_markup=join_kb(missing))
            return None
        await try_credit_referral(user.id)
        return await handler(event, data)


dp.message.outer_middleware(GateMiddleware())
dp.callback_query.outer_middleware(GateMiddleware())


async def notify_left_channels(uid: int, left: List[str], stopped: int) -> None:
    names = ", ".join(esc(c) for c in left)
    if stopped:
        head = f"🛑 <b>Aapke {stopped} bot(s) band kar diye gaye hain.</b>\n\nAapne channel leave kar diya: {names}\n\n"
    else:
        head = f"❌ <b>Aapne channel leave kar diya:</b> {names}\n\n"
    text = head + "<b>Pehle channel dobara join karo, uske baad hi bot chalega.</b>\nJoin karke <b>✅ Verify</b> dabao, phir apna bot dobara Start karo."
    try:
        await bot.send_message(uid, text, reply_markup=join_kb(left))
    except Exception:
        pass


async def enforce_channels_once() -> None:
    channels = get_channels()
    if not channels:
        return
    by_user: Dict[int, List[BotInstance]] = {}
    for inst in list(ACTIVE_BOTS.values()):
        if inst.user_id != ADMIN_ID and (inst.is_running or inst.should_run):
            by_user.setdefault(inst.user_id, []).append(inst)
    for uid, insts in by_user.items():
        left = []
        for ch in channels:
            if await _check_member(uid, ch) is False:
                left.append(ch)
                _join_cache.pop((uid, ch), None)
            await asyncio.sleep(0.05)
        if not left:
            continue
        n = 0
        for inst in insts:
            try:
                await inst.stop()
                ins.log("🛑 Owner ne required channel leave kiya, bot band kar diya gaya.")
                n += 1
            except Exception as e:
                logger.warning("Bot %s stop error: %s", inst.bot_id, e)
        await notify_left_channels(uid, left, n)


async def channel_watcher() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await enforce_channels_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("channel_watcher error")
        await asyncio.sleep(CHANNEL_WATCH_INTERVAL)


# ==============================================================================
# 🏠 GENERAL HANDLERS
# ==============================================================================
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    ensure_user(msg.from_user.id, msg.from_user.first_name or "", msg.from_user.username or "")
    m = REF_START_RE.match((msg.text or "").strip())
    if m:
        capture_referral(msg.from_user.id, int(m.group(1)))
    u = get_user(msg.from_user.id)
    await msg.answer(home_text(u, msg.from_user.first_name or "User"), reply_markup=main_menu(msg.from_user.id))


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("✖️ Cancelled.", reply_markup=main_menu(msg.from_user.id))


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(help_text())


@dp.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(admin_header(), reply_markup=admin_main_menu())


@dp.message(Command("loadpremium"))
async def cmd_loadpremium(msg: Message):
    """Admin: premium2.py ke saare premium emoji library me load karta hai.
    /loadpremium         -> naye emoji add, existing mapping same rehti hai
    /loadpremium overwrite -> mapping bhi premium2 ke hisaab se badal deta hai
    """
    if msg.from_user.id != ADMIN_ID:
        return
    if not os.path.exists(PREMIUM2_FILE):
        await msg.answer(
            f"❌ <b>premium2.py</b> nahi mili.\nIs file ko bot ke folder me rakho:\n<code>{html.escape(DATA_DIR)}</code>")
        return
    low = (msg.text or "").lower()
    overwrite = "overwrite" in low
    map_chars = "map" in low or overwrite
    try:
        t, n, m = await asyncio.to_thread(load_premium2, PREMIUM2_FILE, overwrite, map_chars)
    except Exception as e:
        await msg.answer(f"❌ Load fail: <code>{html.escape(str(e))[:300]}</code>")
        return
    await msg.answer(
        f"✅ <b>Premium2 load ho gaya</b>\n"
        f"• Total emoji file me: <b>{t}</b>\n"
        f"• Naye library me add: <b>{n}</b>\n"
        f"• Characters mapped: <b>{m}</b>\n"
        f"• Library total: <b>{lib_count()}</b>")


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer(stats_text())


@dp.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.first_name or "", call.from_user.username or "")
    u = get_user(call.from_user.id)
    await safe_edit(call, home_text(u, call.from_user.first_name or "User"), main_menu(call.from_user.id))


@dp.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    u = get_user(call.from_user.id)
    await safe_edit(call, profile_text(u, call.from_user.first_name or "User"), kb([btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "status")
async def cb_status(call: CallbackQuery):
    await safe_edit(call, server_stats_text(), kb([btn("🔙 Back", "back_main")]))


def help_text() -> str:
    return ui("help")


@dp.callback_query(F.data == "help")
async def cb_help(call: CallbackQuery):
    await safe_edit(call, help_text(), kb([owner_button()], [btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "check_join")
async def cb_check_join(call: CallbackQuery, state: FSMContext):
    for ch in get_channels():
        _join_cache.pop((call.from_user.id, ch), None)
    still = await missing_channels(call.from_user.id)
    if still:
        return await call.answer("❌ Abhi tak sare channels join nahi kiye! Join karke Verify dabao.", show_alert=True)
    await call.answer("✅ Verified! Welcome.")
    await try_credit_referral(call.from_user.id)
    try:
        await call.message.delete()
    except Exception:
        pass
    await state.clear()
    ensure_user(call.from_user.id, call.from_user.first_name or "", call.from_user.username or "")
    u = get_user(call.from_user.id)
    await call.message.answer(home_text(u, call.from_user.first_name or "User"), reply_markup=main_menu(call.from_user.id))


# ==============================================================================
# 💰 MY COINS / 👥 MY REFER / 🏆 LEAD BOARD
# ==============================================================================
def my_coins_text(uid: int) -> str:
    u = get_user(uid) or {"coins": 0}
    row = db_query("SELECT coins_earned FROM users WHERE user_id=?", (uid,), fetchone=True)
    earned = int(row[0] or 0) if row else 0
    return ("💰 <b>My Coins</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Abhi balance: <b>{int(u.get('coins') or 0)}</b> coins\n"
            f"Total kamaye: <b>{earned}</b> coins\n"
            f"Verified referrals: <b>{invite_count(uid)}</b>\n"
            f"{coin_cost()} coins = <b>+1 Bot Slot</b>\n━━━━━━━━━━━━━━━━━━")


def my_refer_text(uid: int) -> str:
    rows = db_query("SELECT first_name, user_id FROM users WHERE referred_by=? AND ref_credited=1 "
                    "ORDER BY user_id DESC LIMIT 15", (uid,), fetchall=True) or []
    lines = [f"{i}. {esc(n or 'User')} (<code>{rid}</code>)" for i, (n, rid) in enumerate(rows, 1)]
    body = "\n".join(lines) if lines else "Abhi koi verified referral nahi hai."
    return (f"👥 <b>My Refer</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"Verified referrals: <b>{invite_count(uid)}</b>\n"
            f"Total coins from refer: <b>{int((get_user(uid) or {}).get('coins_earned') or 0)}</b>\n\n"
            f"{body}\n━━━━━━━━━━━━━━━━━━")


def lead_rows() -> List[Tuple[str, int]]:
    rows = db_query("SELECT u.first_name, COUNT(r.user_id) AS c FROM users u "
                    "JOIN users r ON r.referred_by=u.user_id AND r.ref_credited=1 "
                    "GROUP BY u.user_id ORDER BY c DESC LIMIT 20", fetchall=True) or []
    out: List[Tuple[str, int]] = [(str(n or "User"), int(c)) for n, c in rows]
    try:
        manual = json.loads(get_setting("lead_manual", "[]") or "[]")
    except Exception:
        manual = []
    for item in manual:
        try:
            out.append((str(item["name"]), int(item["count"])))
        except Exception:
            continue
    out.sort(key=lambda x: -x[1])
    return out[:20]


def lead_text() -> str:
    rows = lead_rows()
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (name, cnt) in enumerate(rows, 1):
        mark = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{mark} {esc(name)} — <b>{cnt}</b> refer")
    body = "\n".join(lines) if lines else "Abhi koi entry nahi hai."
    return f"🏆 <b>Lead Board</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n━━━━━━━━━━━━━━━━━━"


@dp.callback_query(F.data == "my_coins")
async def cb_my_coins(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, my_coins_text(call.from_user.id), kb([btn("🎁 Refer & Earn", "refer")], [btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "my_refer")
async def cb_my_refer(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, my_refer_text(call.from_user.id), kb([btn("🎁 Refer & Earn", "refer")], [btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "lead")
async def cb_lead(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(call, lead_text(), kb([btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "adm_lead_clear")
async def cb_adm_lead_clear(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    set_setting("lead_manual", "[]")
    await call.answer("✅ Manual lead entries clear ho gayi")


# ==============================================================================
# 🎁 REFER & EARN
# ==============================================================================
@dp.callback_query(F.data == "refer")
async def cb_refer(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = refer_view(call.from_user.id)
    await safe_edit(call, text, markup)


@dp.callback_query(F.data == "redeem")
async def cb_redeem(call: CallbackQuery):
    uid = call.from_user.id
    cost = coin_cost()
    if db_update_count("UPDATE users SET coins=coins-?, extra_slots=extra_slots+1 WHERE user_id=? AND coins>=?",
                       (cost, uid, cost)) != 1:
        return await call.answer(f"❌ Itne coins nahi hain. {cost} coins chahiye.", show_alert=True)
    await call.answer("✅ +1 Bot Slot mil gaya!", show_alert=True)
    text, markup = refer_view(uid)
    await safe_edit(call, text, markup)


# ==============================================================================
# 🧬 CLONE: token + admin ID se auto-run (koi file nahi)
# ==============================================================================
CLONE_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


def build_clone_source(token: str, admin_id: int) -> bytes:
    with open(os.path.abspath(__file__), "r", encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r"^MAIN_BOT_TOKEN = .*$", lambda _m: f"MAIN_BOT_TOKEN = {json.dumps(token)}", src, count=1, flags=re.M)
    src = re.sub(r"^ADMIN_ID = .*$", lambda _m: f"ADMIN_ID = {int(admin_id)}", src, count=1, flags=re.M)
    src = re.sub(r"^PAY_API_KEY = .*$", lambda _m: 'PAY_API_KEY = ""', src, count=1, flags=re.M)
    return src.encode("utf-8")


@dp.message(CloneStates.wait_token)
async def clone_got_token(msg: Message, state: FSMContext):
    token = (msg.text or "").strip()
    if not CLONE_TOKEN_RE.fullmatch(token):
        return await msg.answer("❌ Ye valid BotFather token nahi lag raha. Dobara bhejo (ya /cancel).")
    if token == MAIN_BOT_TOKEN:
        return await msg.answer("❌ Ye hosting bot ka token hai. Apne <b>naye</b> bot ka token bhejo.")
    tmp = Bot(token=token)
    try:
        me = await tmp.get_me()
    except Exception:
        return await msg.answer("❌ Token check nahi hua. Token sahi hai ya nahi, dobara check karke bhejo.")
    finally:
        try:
            await tmp.session.close()
        except Exception:
            pass
    await state.update_data(clone_token=token, clone_username=me.username or "")
    await state.set_state(CloneStates.wait_admin)
    await msg.answer(f"✅ Token valid: @{esc(me.username or '')}\n\nAb apna <b>Telegram Admin ID</b> (numeric) bhejo.")


@dp.message(CloneStates.wait_admin)
async def clone_got_admin(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    if not raw.isdigit() or not (5 <= len(raw) <= 15):
        return await msg.answer("❌ Sirf numeric Telegram ID bhejo (e.g. <code>123456789</code>).")
    data = await state.get_data()
    token = data.get("clone_token") or ""
    username = data.get("clone_username") or "bot"
    if not token:
        await state.clear()
        return await msg.answer("❌ Session expire ho gaya. Clone dubara shuru karo.")
    buyer = msg.from_user.id
    bot_id = create_clone_bot(buyer, token, int(raw), username)
    await state.clear()
    if not bot_id:
        return await msg.answer("❌ Clone bot save nahi hua. Admin se contact karo.")
    inst = get_instance(bot_id)
    ok = await inst.start() if inst else False
    if ok:
        await msg.answer(f"✅ <b>Clone bot successfully run ho gaya!</b>\n🤖 @{esc(username)}\n"
                         f"🆔 Bot ID: <code>{bot_id}</code>\nAb is bot par /start karke use karo.")
    else:
        await msg.answer(f"⚠️ Clone save ho gaya (ID <code>{bot_id}</code>), lekin abhi start nahi hua. "
                         f"'My Hosted Bots' me Start dabao ya admin se contact karo.")
    try:
        await bot.send_message(ADMIN_ID, f"🧬 Clone bot ban gaya: user <code>{buyer}</code> · Bot ID <code>{bot_id}</code> · @{esc(username)} · "
                                         f"{'✅ running' if ok else '❌ start fail'}")
    except Exception:
        pass


def create_clone_bot(owner_id: int, token: str, admin_id: int, username: str) -> Optional[int]:
    src = build_clone_source(token, admin_id)
    user_dir = os.path.join(HOST_DIR, str(owner_id))
    os.makedirs(user_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_]", "_", username or "bot")[:30]
    file_path = os.path.join(user_dir, f"{int(time.time())}_clone_{safe}.py")
    with open(file_path, "wb") as fh:
        fh.write(src)
    sha = file_sha256(file_path)
    bot_id = db_insert(
        "INSERT INTO hosted_bots (user_id, bot_name, file_path, auto_restart, created_at, approval) VALUES (?,?,?,?,?,?)",
        (owner_id, f"clone_{safe}.py", file_path, 1, now_str(), "approved"),
    )
    if not bot_id:
        return None
    db_query("UPDATE hosted_bots SET file_sha=? WHERE id=?", (sha, bot_id))
    secure_bot_folder(user_dir, file_path)
    return bot_id


# ==============================================================================
# 🧬 CLONE BOT
# ==============================================================================
@dp.callback_query(F.data == "clone")
async def cb_clone(call: CallbackQuery, state: FSMContext):
    await state.clear()
    price = get_setting("clone_price", CLONE_PRICE_DEFAULT)
    await safe_edit(call, ui("clone", v_price=price),
                    kb([btn(f"💳 Pay ₹{price} & Get Clone", "clone_pay")], [btn("🔙 Back", "back_main")]))


@dp.callback_query(F.data == "clone_pay")
async def cb_clone_pay(call: CallbackQuery, state: FSMContext):
    price = get_setting("clone_price", CLONE_PRICE_DEFAULT)
    await open_payment(call, 0, price, "clone", "Clone Bot")


# ==============================================================================
# 💎 PAYMENTS
# ==============================================================================
UPI_RE = re.compile(r"^[A-Za-z0-9._\-]{2,}@[A-Za-z]{2,}$")


def pay_api_key() -> str:
    return (PAY_API_KEY or "").strip() or get_setting("pay_api_key", "")


def upi_link(upi: str, name: str, amount: str, note: str) -> str:
    return f"upi://pay?pa={quote(upi)}&pn={quote(name)}&am={quote(str(amount))}&cu=INR&tn={quote(note)}"


async def make_qr_png(data: str) -> Optional[bytes]:
    def _local() -> bytes:
        import qrcode
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _local)
    except Exception as e:
        logger.warning("Local QR fail (%s) -> online QR try", e)
    try:
        url = "https://api.qrserver.com/v1/create-qr-code/?size=500x500&margin=10&data=" + quote(data, safe="")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url) as r:
                if r.status == 200 and (r.content_type or "").startswith("image"):
                    return await r.read()
    except Exception as e:
        logger.warning("Online QR fail: %s", e)
    return None


async def create_api_payment(pid: int, uid: int, days: int, amount: str) -> Optional[str]:
    url = get_setting("pay_api_url", "")
    key = pay_api_key()
    if not url:
        return None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"order_id": f"PH{pid}", "amount": amount, "currency": "INR",
               "user_id": uid, "days": days, "description": f"Order {pid}"}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status >= 400:
                    logger.warning("Payment API HTTP %s", r.status)
                    return None
                data = await r.json(content_type=None)
        if not isinstance(data, dict):
            return None
        link = data.get("payment_url") or data.get("url") or data.get("link")
        return link if isinstance(link, str) and link.startswith("http") else None
    except Exception as e:
        logger.warning("Payment API error: %s", e)
        return None


async def open_payment(call: CallbackQuery, days: int, price: str, kind: str, title: str):
    uid = call.from_user.id
    ensure_user(uid, call.from_user.first_name or "", call.from_user.username or "")
    mode = get_setting("pay_mode", "upi")
    pid = db_insert(
        "INSERT INTO payments (user_id, days, amount, method, status, created_at, kind) VALUES (?,?,?,?, 'pending', ?, ?)",
        (uid, days, price, mode, now_str(), kind),
    )
    if not pid:
        return await call.answer("Database error, dobara try karo.", show_alert=True)

    back_cb = "buy" if kind == "premium" else "clone"
    rows: List[List[InlineKeyboardButton]] = []
    qr_data: Optional[str] = None
    if mode == "api":
        pay_url = await create_api_payment(pid, uid, days, price)
        if pay_url:
            text = (f"💳 <b>Payment — {esc(title)}</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Amount: <b>₹{esc(price)}</b>\n\n"
                    "QR scan karo ya <b>Pay Now</b> dabao, payment karo, phir <b>I Paid</b> dabakar screenshot bhejo.")
            rows.append([InlineKeyboardButton(text="💳 Pay Now", url=pay_url)])
            qr_data = pay_url
        else:
            text = "❌ Payment link abhi nahi bana. Admin ko contact karo."
            try:
                await bot.send_message(ADMIN_ID, f"⚠️ Payment API fail (payment #{pid}, user {uid}).")
            except Exception:
                pass
    else:
        upi = get_setting("upi_id", "")
        if not upi:
            text = "❌ Admin ne abhi UPI ID set nahi kiya. Thodi der baad try karo."
        else:
            text = (f"💳 <b>Payment — {esc(title)}</b>\n━━━━━━━━━━━━━━━━━━\n"
                    f"Amount: <b>₹{esc(price)}</b>\n"
                    f"UPI ID: <code>{esc(upi)}</code>\n"
                    f"Note: <code>PH{pid}</code>\n\n"
                    "📷 QR scan karo (amount pehle se bhara hoga) ya UPI ID par pay karo, "
                    "phir <b>I Paid</b> dabakar screenshot bhejo.")
            qr_data = upi_link(upi, BRAND, price, f"PH{pid}")
    rows.append([btn("📸 I Paid — Send Screenshot", f"paid:{pid}")])
    rows.append([btn("🔙 Back", back_cb)])
    markup = kb(*rows)

    if qr_data:
        qr_png = await make_qr_png(qr_data)
        if qr_png:
            try:
                await call.message.answer_photo(
                    BufferedInputFile(qr_png, filename=f"pay_PH{pid}.png"), caption=text, reply_markup=markup)
                try:
                    await call.message.delete()
                except Exception:
                    pass
                return
            except Exception as e:
                logger.warning("QR photo bhejne me error: %s", e)
        text += "\n\n<i>⚠️ QR abhi nahi ban paya, UPI ID se manually pay karo.</i>"
    await safe_edit(call, text, markup)


@dp.callback_query(F.data == "buy")
async def cb_buy(call: CallbackQuery, state: FSMContext):
    await state.clear()
    plans = get_plans()
    rows = [[btn(f"🗓 {p['days']} Day{'s' if p['days'] > 1 else ''} — ₹{p['price']}", f"plan:{p['days']}")] for p in plans]
    rows.append([btn("🔙 Back", "back_main")])
    text = ui("buy", v_free=get_setting("free_limit", str(FREE_BOT_LIMIT)),
              v_prem=get_setting("premium_limit", str(PREMIUM_BOT_LIMIT)))
    if not plans:
        text += "\n\n<i>Abhi koi plan available nahi hai.</i>"
    await safe_edit(call, text, kb(*rows))


@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery, state: FSMContext):
    try:
        days = int(call.data.split(":")[1])
    except ValueError:
        return await call.answer("Galat plan", show_alert=True)
    plan = get_plan(days)
    if not plan:
        return await call.answer("Ye plan ab available nahi hai.", show_alert=True)
    await open_payment(call, days, plan["price"], "premium", f"{days} Day(s)")


@dp.callback_query(F.data.startswith("paid:"))
async def cb_paid(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    pay = get_payment(pid)
    if not pay or pay["user_id"] != call.from_user.id or pay["status"] != "pending":
        return await call.answer("Ye payment valid nahi hai.", show_alert=True)
    await state.set_state(PayStates.wait_proof)
    await state.update_data(pay_id=pid)
    await safe_edit(
        call,
        f"📸 <b>Payment screenshot bhejo</b>\nPayment #{pid} — {esc(pay_title(pay))}, ₹{esc(pay['amount'])}\n\n"
        "Photo ya screenshot is chat me bhejo. Cancel: /cancel",
        kb([btn("🔙 Back", "buy")]),
    )


async def _submit_proof(msg: Message, state: FSMContext, file_id: str, is_photo: bool):
    data = await state.get_data()
    pid = data.get("pay_id")
    pay = get_payment(pid) if pid else None
    if not pay or pay["user_id"] != msg.from_user.id or pay["status"] != "pending":
        await state.clear()
        return await msg.answer("❌ Ye payment ab valid nahi hai. Dobara try karo.")
    uniq = msg.photo[-1].file_unique_id if is_photo else msg.document.file_unique_id
    if db_query("SELECT id FROM payments WHERE proof_unique=? AND status!='rejected'", (uniq,), fetchone=True):
        return await msg.answer("❌ Ye screenshot pehle hi kisi payment me use ho chuka hai. Naya screenshot bhejo.")
    db_query("UPDATE payments SET proof_id=?, proof_unique=? WHERE id=?", (file_id, uniq, pid))
    await state.clear()
    kind_label = "🧬 Clone Bot" if pay.get("kind") == "clone" else "💎 Premium"
    caption = (f"💳 <b>Payment Proof</b> #{pid} ({kind_label})\n👤 User: <code>{msg.from_user.id}</code> "
               f"{esc(msg.from_user.first_name or '')}\n🗓 Item: {esc(pay_title(pay))}\n💰 Amount: ₹{esc(pay['amount'])}")
    markup = kb([btn("✅ Approve", f"payok:{pid}"), btn("❌ Reject", f"payno:{pid}")])
    try:
        if is_photo:
            await bot.send_photo(ADMIN_ID, photo=file_id, caption=caption, reply_markup=markup)
        else:
            await bot.send_document(ADMIN_ID, document=file_id, caption=caption, reply_markup=markup)
    except Exception as e:
        logger.warning("Admin ko proof bhejne me error: %s", e)
    await msg.answer("⏳ Screenshot admin ko bhej diya. Approve hote hi aapka kaam ho jayega.",
                     reply_markup=kb([btn("🔙 Back", "back_main")]))


@dp.message(PayStates.wait_proof, F.photo)
async def handle_proof_photo(msg: Message, state: FSMContext):
    await _submit_proof(msg, state, msg.photo[-1].file_id, True)


@dp.message(PayStates.wait_proof, F.document)
async def handle_proof_doc(msg: Message, state: FSMContext):
    await _submit_proof(msg, state, msg.document.file_id, False)


@dp.message(PayStates.wait_proof)
async def handle_proof_other(msg: Message, state: FSMContext):
    await msg.answer("📸 Screenshot (photo) bhejo. Cancel ke liye /cancel.")


async def _finish_payment_message(call: CallbackQuery, label: str):
    try:
        await call.message.edit_caption(caption=(call.message.caption or "") + f"\n\n{label}", reply_markup=None)
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("payok:"))
async def cb_pay_ok(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    pid = int(call.data.split(":")[1])
    pay = get_payment(pid)
    if not pay or pay["status"] != "pending":
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    if db_update_count("UPDATE payments SET status='approved', decided_at=? WHERE id=? AND status='pending'",
                       (now_str(), pid)) != 1:
        return await call.answer("Ye payment already processed hai.", show_alert=True)

    if pay.get("kind") == "clone":
        try:
            fsm = dp.fsm.get_context(bot=bot, chat_id=pay["user_id"], user_id=pay["user_id"])
            await fsm.set_state(CloneStates.wait_token)
            await bot.send_message(
                pay["user_id"],
                "🧬 <b>Payment approve ho gaya!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "1️⃣ Apna <b>Bot Token</b> bhejo (@BotFather se banaya hua naya bot)\n"
                "2️⃣ Phir apna <b>Telegram Admin ID</b> (numeric) bhejo\n\n"
                "Bot apne aap chal jayega. Cancel: /cancel")
        except Exception as e:
            logger.warning("Clone step start error: %s", e)
    else:
        set_premium(pay["user_id"], pay["days"])
        u = get_user(pay["user_id"]) or {}
        try:
            await bot.send_message(pay["user_id"],
                                   f"🎉 <b>Premium Activate ho gaya!</b>\n🗓 +{pay['days']} day(s)\n⏳ Till: {premium_expiry_text(u)}")
        except Exception:
            pass
    await call.answer("✅ Approved")
    await _finish_payment_message(call, "✅ <b>APPROVED</b>")


@dp.callback_query(F.data.startswith("payno:"))
async def cb_pay_no(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    pid = int(call.data.split(":")[1])
    pay = get_payment(pid)
    if not pay or pay["status"] != "pending":
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    if db_update_count("UPDATE payments SET status='rejected', decided_at=? WHERE id=? AND status='pending'",
                       (now_str(), pid)) != 1:
        return await call.answer("Ye payment already processed hai.", show_alert=True)
    try:
        await bot.send_message(pay["user_id"], f"❌ Payment #{pid} reject ho gaya. Sahi screenshot ke saath dobara try karo.")
    except Exception:
        pass
    await call.answer("❌ Rejected")
    await _finish_payment_message(call, "❌ <b>REJECTED</b>")


# ==============================================================================
# 📤 UPLOAD & HOSTED BOT MANAGEMENT
# ==============================================================================
def approval_markup(bid: int) -> InlineKeyboardMarkup:
    return kb([btn("✅ Approve", f"bapp:{bid}"), btn("❌ Reject", f"brej:{bid}")])


async def notify_admin_bot(hb: dict, owner_id: int, owner_name: str, note: str,
                           markup: Optional[InlineKeyboardMarkup] = None, prefix: str = "📥 Bot file"):
    caption = (f"{prefix}\n👤 User: <code>{owner_id}</code> {esc(owner_name)}\n"
               f"🆔 Bot ID: <code>{hb['id']}</code>\n📄 {esc(hb['bot_name'])}\n{note}")
    try:
        if os.path.exists(hb["file_path"]):
            await bot.send_document(ADMIN_ID, FSInputFile(hb["file_path"], filename=hb["bot_name"]),
                                    caption=caption[:1000], reply_markup=markup)
        else:
            await bot.send_message(ADMIN_ID, caption + "\n❌ File server par nahi mili.", reply_markup=markup)
    except Exception as e:
        logger.warning("Admin ko file bhejne me error: %s", e)


def limit_reached_kb() -> InlineKeyboardMarkup:
    return kb([btn("🎁 Refer & Earn (coins kamao)", "refer")], [btn("💎 Buy Premium", "buy")], [btn("🔙 Back", "back_main")])


@dp.callback_query(F.data == "upload")
async def cb_upload(call: CallbackQuery, state: FSMContext):
    u = get_user(call.from_user.id)
    count = user_bot_count(call.from_user.id)
    limit = user_limit(u)
    if count >= limit:
        text = (f"🚫 <b>Slot limit poori ho gayi ({count}/{limit}).</b>\n\n"
                f"🎁 <b>Refer karo:</b> har referral par <b>{ref_reward()} coins</b>, "
                f"<b>{coin_cost()} coins</b> = <b>+1 slot</b>.\n"
                f"Aapke coins: <b>{u['coins']}</b>\n\nYa Premium lo 💎")
        return await safe_edit(call, text, limit_reached_kb())
    await safe_edit(call, ui("upload_prompt", v_slots=f"{count}/{limit}", v_max=MAX_UPLOAD_MB),
                    kb([btn("🔙 Cancel", "back_main")]))
    await state.set_state(HostingStates.wait_file)


@dp.message(HostingStates.wait_file, F.document)
async def handle_upload(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    u = get_user(uid)
    doc = msg.document

    if not doc.file_name or not doc.file_name.lower().endswith(".py"):
        return await msg.answer("❌ Sirf <b>.py</b> files allowed hain.")
    if doc.file_size and doc.file_size > MAX_UPLOAD_MB * 1024 * 1024:
        return await msg.answer(f"❌ File {MAX_UPLOAD_MB} MB se badi hai.")

    count = user_bot_count(uid)
    if count >= user_limit(u):
        await state.clear()
        return await msg.answer(f"🚫 Slot limit poori ({count}/{user_limit(u)}). Refer karke coins kamao 🎁 ya Premium lo 💎",
                                reply_markup=limit_reached_kb())

    user_dir = os.path.join(HOST_DIR, str(uid))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", doc.file_name)
    file_path = os.path.join(user_dir, f"{int(time.time())}_{safe_name}")

    status = await msg.answer("⏳ File download ho rahi hai...")
    try:
        await bot.download(doc, destination=file_path)
    except Exception as e:
        await state.clear()
        return await status.edit_text(f"❌ File download nahi hui: <code>{esc(e)}</code>")

    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if MAIN_BOT_TOKEN in content:
        os.remove(file_path)
        await state.clear()
        return await status.edit_text(
            "❌ <b>CRITICAL ERROR:</b>\nAapne Hosting Bot ka hi token file me daal diya hai!\n\n"
            "@BotFather se <b>NAYA BOT</b> banao aur uska token use karo."
        )

    sha = file_sha256(file_path)
    score, reasons, critical = scan_bot_source(content)
    risky = critical or score >= PENDING_SCORE
    fast = fast_approve_on() and not risky
    approval = "approved" if fast else "pending"
    auto = 1 if u["premium"] else 0
    bot_id = db_insert(
        "INSERT INTO hosted_bots (user_id, bot_name, file_path, auto_restart, created_at, approval) VALUES (?,?,?,?,?,?)",
        (uid, doc.file_name, file_path, auto, now_str(), approval),
    )
    await state.clear()
    if not bot_id:
        return await status.edit_text("❌ Database error. Dobara try karo.")

    db_query("UPDATE hosted_bots SET file_sha=? WHERE id=?", (sha, bot_id))
    secure_bot_folder(user_dir, file_path)

    hb = get_bot(bot_id)
    name = msg.from_user.first_name or ""
    prefix = "📥 New bot file upload"
    if risky:
        note = (f"🚨 <b>Security scan: risky (score {score})</b>\n"
                + "\n".join(f"• {esc(r)}" for r in reasons)
                + "\n⏳ <b>Pending</b> — admin approve ya reject karega")
        markup = approval_markup(bot_id)
        prefix = "🚨 Security alert: risky bot file"
    elif fast:
        note = "⚡ <b>Fast Approve ON</b> — auto approved"
        markup = None
    else:
        note = "⏳ <b>Pending</b> — approve ya reject karo"
        markup = approval_markup(bot_id)
    await notify_admin_bot(hb, uid, name, note, markup=markup, prefix=prefix)

    try:
        await msg.answer_document(FSInputFile(file_path, filename=doc.file_name),
                                  caption=f"📄 Aapki bot file (copy) — ID <code>{bot_id}</code>")
    except Exception as e:
        logger.warning("User ko file bhejne me error: %s", e)

    if fast:
        text = (f"✅ <b>File Uploaded Successfully!</b>\n\n📄 File: <code>{esc(doc.file_name)}</code>\n"
                f"🆔 Bot ID: <code>{bot_id}</code>")
    else:
        text = (f"✅ <b>File Uploaded!</b>\n\n📄 File: <code>{esc(doc.file_name)}</code>\n"
                f"🆔 Bot ID: <code>{bot_id}</code>\n\n{ui('pending')}")
    await status.edit_text(text, reply_markup=bot_panel_kb(bot_id, bool(hb["auto_restart"]), u["premium"], approval))


@dp.message(HostingStates.wait_file)
async def handle_upload_other(msg: Message, state: FSMContext):
    await msg.answer("📤 Apni <b>.py</b> file document ki tarah bhejo. Cancel: /cancel")


@dp.callback_query(F.data == "my_bots")
async def cb_my_bots(call: CallbackQuery):
    text, markup = my_bots_view(call.from_user.id)
    await safe_edit(call, text, markup)


async def fetch_owned(call: CallbackQuery, bot_id: int) -> Optional[dict]:
    hb = get_bot(bot_id)
    if not can_access(call.from_user.id, hb):
        await call.answer("Bot nahi mila ya access nahi hai.", show_alert=True)
        return None
    return hb


def build_panel(viewer_id: int, bot_id: int):
    hb = get_bot(bot_id)
    inst = get_instance(bot_id)
    viewer = get_user(viewer_id) or {"premium": False}
    approval = hb.get("approval") or "approved"
    if approval == "pending":
        status = "⏳ Pending Approval"
    elif approval == "rejected":
        status = "❌ Rejected by admin"
    else:
        status = "🟢 Running" if inst and inst.is_running else "🔴 Stopped"
        if inst and inst.is_running and inst.remote_sid:
            status += f" · 🛰 {esc(inst.server_name)}"
    owner_note = ""
    if viewer_id == ADMIN_ID and hb["user_id"] != ADMIN_ID:
        owner_note = f"👤 Owner: <code>{hb['user_id']}</code>\n"
    text = (
        f"🤖 <b>Bot Control Panel</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📄 Name: <code>{esc(hb['bot_name'])}</code>\n"
        f"🆔 Bot ID: <code>{bot_id}</code>\n"
        f"{owner_note}"
        f"📊 Status: {status}\n"
        f"━━━━━━━━━━━━━━━━━━"
    )
    return text, bot_panel_kb(bot_id, inst.auto_restart if inst else bool(hb["auto_restart"]),
                              viewer["premium"], approval)


async def render_panel(call: CallbackQuery, bot_id: int):
    text, markup = build_panel(call.from_user.id, bot_id)
    await safe_edit(call, text, markup)


@dp.callback_query(F.data.startswith("view:"))
async def cb_view(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if hb.get("approval") == "pending":
        return await call.answer("⏳ Admin approval pending hai.", show_alert=True)
    if hb.get("approval") == "rejected":
        return await call.answer("❌ Admin ne ye bot reject kiya hai.", show_alert=True)
    inst = get_instance(bot_id)
    if inst.is_running:
        return await call.answer("Bot pehle se running hai ✅", show_alert=True)

    if hb["user_id"] != ADMIN_ID:
        miss = await missing_channels(hb["user_id"])
        if miss:
            return await call.answer("❌ Pehle sare required channels join karo.", show_alert=True)

    await notify_admin_bot(hb, hb["user_id"], call.from_user.first_name or "", "▶️ Run request", prefix="▶️ Bot run by user")
    await call.answer("🚀 Starting...")
    await inst.start()
    await asyncio.sleep(3)

    if not inst.is_running:
        logs = "\n".join(list(inst.logs)[-20:])[-2500:]
        await safe_edit(
            call,
            f"❌ <b>Bot Crash Ho Gaya!</b>\n\n<b>Error:</b>\n<pre>{esc(logs)}</pre>",
            kb([btn("📦 Pip Install", f"pip:{bot_id}")], [btn("🔙 Back", f"view:{bot_id}")]),
        )
    else:
        await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("stop:"))
async def cb_stop(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    if inst.is_running:
        await inst.stop()
        await call.answer("⏹ Bot stopped.")
    else:
        await call.answer("Bot already stopped hai.")
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if hb.get("approval") != "approved":
        return await call.answer("⏳ Pehle admin approval chahiye.", show_alert=True)
    inst = get_instance(bot_id)
    await call.answer("🔁 Restarting...")
    await inst.stop()
    await inst.start()
    await asyncio.sleep(2)
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    viewer = get_user(call.from_user.id)
    if not viewer["premium"] and not viewer["vps"]:
        return await call.answer("💎 Auto-Restart sirf Premium / VPS users ke liye hai!", show_alert=True)
    inst = get_instance(bot_id)
    inst.auto_restart = not inst.auto_restart
    db_query("UPDATE hosted_bots SET auto_restart=? WHERE id=?", (1 if inst.auto_restart else 0, bot_id))
    await call.answer(f"Auto-Restart {'ON ⚡' if inst.auto_restart else 'OFF'}")
    await render_panel(call, bot_id)


@dp.callback_query(F.data.startswith("logs:"))
async def cb_logs(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    viewer = get_user(call.from_user.id)
    inst = get_instance(bot_id)
    limit = PREMIUM_LOG_LINES if viewer["premium"] else FREE_LOG_LINES
    if inst.remote_sid:
        log_text = await remote_logs(inst.remote_sid, bot_id)
        log_text = "\n".join(log_text.splitlines()[-limit:])
    else:
        lines = list(inst.logs)[-limit:]
        log_text = "\n".join(lines) if lines else "Abhi koi log nahi hai."
    log_text = log_text[-3500:]
    await safe_edit(
        call,
        f"📋 <b>Live Logs</b> (last {limit})\n<pre>{esc(log_text)}</pre>",
        kb([btn("🔄 Refresh", f"logs:{bot_id}")], [btn("🔙 Back", f"view:{bot_id}")]),
    )


@dp.callback_query(F.data.startswith("clearlog:"))
async def cb_clearlog(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    inst = get_instance(bot_id)
    inst.logs.clear()
    inst.log("🧹 Logs cleared!")
    await call.answer("🧹 Logs Cleared!")
    await cb_logs(call)


@dp.callback_query(F.data.startswith("dllog:"))
async def cb_dllog(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    viewer = get_user(call.from_user.id)
    if not viewer["premium"]:
        return await call.answer("💎 Full log download sirf Premium users ke liye hai!", show_alert=True)
    inst = get_instance(bot_id)
    if inst.remote_sid:
        data = (await remote_logs(inst.remote_sid, bot_id)).encode("utf-8")
    else:
        data = "\n".join(inst.logs).encode("utf-8") or b"No logs."
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"{hb['bot_name']}_logs.txt"),
        caption=f"📥 Logs of <code>{esc(hb['bot_name'])}</code>",
    )
    await call.answer()


@dp.callback_query(F.data.startswith("dlfile:"))
async def cb_dlfile(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    if not os.path.exists(hb["file_path"]):
        return await call.answer("File server par nahi mili.", show_alert=True)
    await call.message.answer_document(FSInputFile(hb["file_path"], filename=hb["bot_name"]),
                                       caption=f"📄 <code>{esc(hb['bot_name'])}</code>")
    await call.answer()


@dp.callback_query(F.data.startswith("pip:"))
async def cb_pip(call: CallbackQuery, state: FSMContext):
    bot_id = int(call.data.split(":")[1])
    if not await fetch_owned(call, bot_id):
        return
    await state.update_data(pip_bot_id=bot_id)
    inst = get_instance(bot_id)
    where = f"🛰 VPS <b>{esc(inst.server_name)}</b> par" if inst and inst.remote_sid else "Hosting server par"
    await safe_edit(
        call,
        f"📦 <b>Pip package ka naam bhejo</b> ({where} install hoga):\n(e.g. <code>pyrogram</code>, <code>requests==2.31.0</code>)",
        kb([btn("🔙 Cancel", f"view:{bot_id}")]),
    )
    await state.set_state(HostingStates.wait_pip_package)


PIP_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]<>=!~, ]{1,120}$")


@dp.message(HostingStates.wait_pip_package)
async def execute_pip(msg: Message, state: FSMContext):
    data = await state.get_data()
    bot_id = data.get("pip_bot_id")
    pkg = (msg.text or "").strip()
    if not PIP_RE.match(pkg) or any(p.startswith("-") for p in pkg.split()):
        return await msg.answer("❌ Galat package name. Sirf letters, numbers aur <code>=&lt;&gt;!~[]</code> allowed hain.")

    await state.clear()
    inst = get_instance(bot_id) if bot_id else None
    back = kb([btn("🔙 Back to Bot", f"view:{bot_id}")]) if bot_id else kb([btn("🔙 Back", "back_main")])
    status = await msg.answer(f"⏳ Installing <code>{esc(pkg)}</code>...")

    if inst and inst.remote_sid:
        ok, out = await remote_pip(inst.remote_sid, bot_id, pkg.split())
        if ok:
            return await status.edit_text(f"✅ <b>VPS par install ho gaya!</b>\n<code>{esc(pkg)}</code>", reply_markup=back)
        return await status.edit_text(f"❌ <b>VPS Install Failed:</b>\n<pre>{esc(out)}</pre>", reply_markup=back)

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", *pkg.split(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=child_env(HOST_DIR),
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        return await status.edit_text("⌛ Timeout (5 min). Dobara try karo.")
    if proc.returncode == 0:
        await status.edit_text(f"✅ <b>Package Installed!</b>\n<code>{esc(pkg)}</code> ready hai.", reply_markup=back)
    else:
        err = stderr.decode(errors="ignore")[-1000:]
        await status.edit_text(f"❌ <b>Install Failed:</b>\n<pre>{esc(err)}</pre>", reply_markup=back)


@dp.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery):
    bot_id = int(call.data.split(":")[1])
    hb = await fetch_owned(call, bot_id)
    if not hb:
        return
    inst = ACTIVE_BOTS.pop(bot_id, None)
    if inst:
        await inst.stop()
    if inst and inst.remote_sid:
        await remote_stop(inst.remote_sid, bot_id, wipe=True)
    if os.path.exists(hb["file_path"]):
        os.remove(hb["file_path"])
    db_query("DELETE FROM hosted_bots WHERE id=?", (bot_id,))
    await call.answer("🗑 Bot Deleted!", show_alert=True)
    text, markup = my_bots_view(call.from_user.id)
    await safe_edit(call, text, markup)


@dp.callback_query(F.data.startswith("bapp:"))
async def cb_bot_approve(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    bid = int(call.data.split(":")[1])
    hb = get_bot(bid)
    if not hb:
        return await call.answer("Bot nahi mila.", show_alert=True)
    set_approval(bid, "approved")
    approve_current_sha(bid)
    try:
        await bot.send_message(hb["user_id"], f"✅ <b>Aapka bot approve ho gaya!</b>\n📄 <code>{esc(hb['bot_name'])}</code>\nAb Start dabao.")
    except Exception:
        pass
    await call.answer("✅ Approved")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@dp.callback_query(F.data.startswith("brej:"))
async def cb_bot_reject(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    bid = int(call.data.split(":")[1])
    hb = get_bot(bid)
    if not hb:
        return await call.answer("Bot nahi mila.", show_alert=True)
    set_approval(bid, "rejected")
    inst = ACTIVE_BOTS.get(bid)
    if inst and inst.is_running:
        await inst.stop()
    try:
        await bot.send_message(hb["user_id"], f"❌ <b>Aapka bot admin ne reject kar diya.</b>\n📄 <code>{esc(hb['bot_name'])}</code>")
    except Exception:
        pass
    await call.answer("❌ Rejected")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ==============================================================================
# 👑 ADMIN PANEL
# ==============================================================================
def admin_header() -> str:
    return (
        "╔═══════════════════════╗\n"
        "   👑 <b>SUPER ADMIN PANEL</b> 👑\n"
        "╚═══════════════════════╝\n"
        f"🟢 Bots running: <b>{sum(1 for b in ACTIVE_BOTS.values() if b.is_running)}</b>  ·  "
        f"🛰 VPS: <b>{(db_query('SELECT COUNT(*) FROM vps_servers', fetchone=True) or (0,))[0]}</b>\n"
        "👇 Section chuno:"
    )


@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return await call.answer("⛔ Access denied", show_alert=True)
    await state.clear()
    await safe_edit(call, admin_header(), admin_main_menu())


@dp.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await safe_edit(call, stats_text(), kb([btn("🔙 Admin", "admin_main")]))


@dp.callback_query(F.data == "adm_users")
async def cb_adm_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query(
        "SELECT user_id, first_name, username, is_premium, premium_until, is_banned, is_vps, vps_until "
        "FROM users ORDER BY user_id DESC LIMIT 15", fetchall=True) or []
    lines = []
    for uid, name, uname, prem, until, banned, vps, vps_until in rows:
        p = "💎" if is_premium_active({"is_premium": prem, "premium_until": until}) else "·"
        v = "🛰" if vps and _future(vps_until) else ""
        b = "🚫" if banned else ""
        handle = f"@{uname}" if uname else (name or "—")
        lines.append(f"{p}{v}{b} <code>{uid}</code> {esc(handle)}")
    body = "\n".join(lines) if lines else "Koi user nahi."
    await safe_edit(call, f"👥 <b>Latest Users</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\n💎 Premium  🛰 VPS  🚫 Banned",
                    kb([btn("🔙 Admin", "admin_main")]))


@dp.callback_query(F.data == "adm_bots")
async def cb_adm_bots(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, bot_name, approval FROM hosted_bots ORDER BY id DESC LIMIT 20", fetchall=True) or []
    lines = []
    for bid, uid, name, approval in rows:
        inst = ACTIVE_BOTS.get(bid)
        icon = "⏳" if approval == "pending" else ("❌" if approval == "rejected" else ("🟢" if inst and inst.is_running else "🔴"))
        vp = "🛰" if inst and inst.remote_sid else ""
        lines.append(f"{icon}{vp} <code>{bid}</code> {esc(name)} · <code>{uid}</code>")
    body = "\n".join(lines) if lines else "Koi bot nahi."
    await safe_edit(call, f"🤖 <b>All Hosted Bots (latest 20)</b>\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Admin", "admin_main")]))


@dp.callback_query(F.data == "adm_pending")
async def cb_adm_pending(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, bot_name FROM hosted_bots WHERE approval='pending' ORDER BY id DESC LIMIT 15",
                    fetchall=True) or []
    buttons = []
    for bid, uid, name in rows:
        buttons.append([btn(f"✅ #{bid} {name[:18]}", f"bapp:{bid}"), btn("❌", f"brej:{bid}")])
    buttons.append([btn("🔙 Admin", "admin_main")])
    body = "\n".join(f"⏳ <code>{bid}</code> {esc(name)} · <code>{uid}</code>" for bid, uid, name in rows) or "Koi pending bot nahi."
    await safe_edit(call, f"⏳ <b>Pending Bot Approvals</b>\n━━━━━━━━━━━━━━━━━━\n{body}", kb(*buttons))


@dp.callback_query(F.data == "adm_stopall")
async def cb_adm_stopall(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    count = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.is_running:
            await inst.stop()
            count += 1
    await call.answer(f"🛑 {count} bots stopped.", show_alert=True)
    await safe_edit(call, admin_header(), admin_main_menu())


@dp.callback_query(F.data.startswith("adm_in:"))
async def cb_adm_input(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    action = call.data.split(":", 1)[1]
    if action not in ADMIN_PROMPTS:
        return await call.answer("Unknown action")
    await state.set_state(AdminStates.wait_input)
    await state.update_data(action=action)
    await safe_edit(call, admin_prompt_text(action), kb([btn("🔙 Cancel", "admin_main")]))


def parse_int(s: str) -> Optional[int]:
    s = s.strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


async def do_broadcast(msg: Message, only_premium: bool = False):
    rows = db_query("SELECT user_id, is_premium, premium_until FROM users WHERE is_banned=0", fetchall=True) or []
    if only_premium:
        rows = [r for r in rows if is_premium_active({"is_premium": r[1], "premium_until": r[2]})]
    users = [r[0] for r in rows]
    status = await msg.answer(f"⏳ Broadcasting to {len(users)} users...")
    sent = failed = 0
    for uid in users:
        try:
            await msg.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ <b>Broadcast Complete</b>\n📨 Sent: {sent}\n❌ Failed: {failed}")


async def stop_user_bots(uid: int) -> int:
    n = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.user_id == uid and inst.is_running:
            await inst.stop()
            n += 1
    return n


async def test_api_connection() -> str:
    url = get_setting("pay_api_url", "")
    key = pay_api_key()
    if not url:
        return "❌ Payment API URL set nahi hai."
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {"test": True, "order_id": "TEST", "amount": "1", "currency": "INR", "user_id": 0, "days": 0}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                body = (await r.text())[:300]
                return f"{'✅' if r.status < 400 else '⚠️'} HTTP <b>{r.status}</b>\n<pre>{esc(body)}</pre>"
    except Exception as e:
        return f"❌ Connect nahi hua: <code>{esc(e)}</code>"


@dp.message(AdminStates.wait_input)
async def admin_input_handler(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    action = data.get("action")
    text = (msg.text or "").strip()
    back = kb([btn("🔙 Admin Panel", "admin_main")])

    if action == "broadcast":
        await state.clear()
        return await do_broadcast(msg)

    if action == "broadcast_prem":
        await state.clear()
        return await do_broadcast(msg, only_premium=True)

    if action == "open_bot":
        bid = parse_int(text)
        if bid is None or not get_bot(bid):
            return await msg.answer("❌ Is ID ka koi bot nahi mila. Sahi Bot ID bhejo.")
        await state.clear()
        panel_text, panel_markup = build_panel(ADMIN_ID, bid)
        return await msg.answer(panel_text, reply_markup=panel_markup)

    if action in ("limit_free", "limit_prem"):
        n = parse_int(text)
        if n is None or not 1 <= n <= 1000:
            return await msg.answer("❌ 1 se 1000 ke beech ka number bhejo.")
        set_setting("free_limit" if action == "limit_free" else "premium_limit", n)
        await state.clear()
        return await msg.answer(f"✅ {'Free' if action == 'limit_free' else 'Premium'} limit ab <b>{n}</b> bots hai.", reply_markup=back)

    if action == "stop_user":
        target = parse_int(text)
        if target is None:
            return await msg.answer("❌ Sahi numeric User ID bhejo.")
        await state.clear()
        n = await stop_user_bots(target)
        return await msg.answer(f"🛑 User <code>{target}</code> ke <b>{n}</b> bots band kiye.", reply_markup=back)

    if action == "chan_add":
        ch = normalize_channel(text)
        if ch is None:
            return await msg.answer("❌ Sahi channel <code>@username</code> ya link bhejo.")
        await state.clear()
        chans = get_channels()
        if ch in chans:
            return await msg.answer("ℹ️ Ye channel pehle se added hai.", reply_markup=kb([btn("🔙 Channels", "adm_chans")]))
        chans.append(ch)
        save_channels(chans)
        note = ""
        try:
            me = await bot.me()
            cm = await bot.get_chat_member(ch, me.id)
            if str(getattr(cm.status, "value", cm.status)) not in ("administrator", "creator"):
                note = "\n⚠️ Bot is channel me admin nahi hai."
        except Exception as e:
            note = f"\n⚠️ Channel check nahi hua ({esc(e)})."
        return await msg.answer(f"✅ Channel add ho gaya: <b>{esc(ch)}</b>{note}", reply_markup=kb([btn("🔙 Channels", "adm_chans")]))

    if action == "upi_set":
        if not UPI_RE.match(text):
            return await msg.answer("❌ Sahi UPI ID bhejo (e.g. <code>name@okaxis</code>).")
        set_setting("upi_id", text)
        await state.clear()
        return await msg.answer(f"✅ UPI ID save: <code>{esc(text)}</code>", reply_markup=kb([btn("🔙 Payment Setup", "adm_paycfg")]))

    if action == "api_url":
        if not text.lower().startswith(("http://", "https://")):
            return await msg.answer("❌ https:// se shuru hone wala URL bhejo.")
        set_setting("pay_api_url", text)
        await state.clear()
        return await msg.answer("✅ Payment API URL save ho gaya.", reply_markup=kb([btn("🔙 Payment Setup", "adm_paycfg")]))

    if action == "api_key":
        set_setting("pay_api_key", text)
        await state.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        return await msg.answer("✅ Payment API key save ho gayi.", reply_markup=kb([btn("🔙 Payment Setup", "adm_paycfg")]))

    if action == "plan_add":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>DAYS PRICE</code> (e.g. <code>15 79</code>)")
        days, price = int(parts[0]), parts[1]
        if days <= 0:
            return await msg.answer("❌ Days 1 ya usse zyada hone chahiye.")
        plans = [p for p in get_plans() if p["days"] != days]
        plans.append({"days": days, "price": price})
        save_plans(plans)
        await state.clear()
        return await msg.answer(f"✅ Plan add/update: {days} day(s) — ₹{esc(price)}", reply_markup=kb([btn("🔙 Plans", "adm_plans")]))

    if action == "plan_price":
        days = data.get("plan_days")
        if not text.isdigit() or int(text) <= 0:
            return await msg.answer("❌ Sirf positive number bhejo (e.g. 99).")
        plans = get_plans()
        for p in plans:
            if p["days"] == days:
                p["price"] = text
        save_plans(plans)
        await state.clear()
        return await msg.answer(f"✅ {days} day plan ka price ab ₹{text} hai.", reply_markup=kb([btn("🔙 Plans", "adm_plans")]))

    if action == "clone_price":
        if not text.isdigit() or int(text) <= 0:
            return await msg.answer("❌ Sirf positive number bhejo (e.g. 60).")
        set_setting("clone_price", text)
        await state.clear()
        return await msg.answer(f"✅ Clone Bot ki price ab ₹{text} hai.", reply_markup=back)

    if action == "lead_add":
        parts = text.rsplit(None, 1)
        if len(parts) != 2 or parse_int(parts[1]) is None or int(parts[1]) < 0 or not parts[0].strip():
            return await msg.answer("❌ Format: <code>NAME COUNT</code> (e.g. <code>Rahul 25</code>)")
        try:
            manual = json.loads(get_setting("lead_manual", "[]") or "[]")
        except Exception:
            manual = []
        manual.append({"name": parts[0].strip()[:40], "count": int(parts[1])})
        set_setting("lead_manual", json.dumps(manual, ensure_ascii=False))
        await state.clear()
        return await msg.answer("✅ Lead Board me entry add ho gayi.", reply_markup=kb([btn("🏆 Lead Board", "lead")], [btn("🔙 Admin", "admin_main")]))

    if action == "ref_cfg":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>REWARD COST</code> (e.g. <code>3 5</code>)")
        reward, cost = int(parts[0]), int(parts[1])
        if reward <= 0 or cost <= 0:
            return await msg.answer("❌ Dono number 1 ya usse zyada hone chahiye.")
        set_setting("ref_reward", reward)
        set_setting("coin_cost", cost)
        await state.clear()
        return await msg.answer(f"✅ Referral: <b>{reward} coins</b> per invite, <b>{cost} coins = 1 slot</b>.", reply_markup=back)

    if action == "vps_give":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>USER_ID DAYS</code> (e.g. <code>123456 30</code>)")
        uid, days = int(parts[0]), int(parts[1])
        await state.clear()
        if days <= 0:
            set_vps(uid, 0)
            for inst in list(ACTIVE_BOTS.values()):
                if inst.user_id == uid:
                    inst.vps = False
            try:
                await bot.send_message(uid, "🛰 Aapka VPS plan khatam ho gaya.")
            except Exception:
                pass
            return await msg.answer(f"🛰 User <code>{uid}</code>: VPS hata diya gaya.", reply_markup=back)
        if not list_servers():
            return await msg.answer("❌ Pehle <b>🖥 VPS Manager</b> me ek server add karo.", reply_markup=back)
        rows = [[btn(f"{ {'ok': '🟢', 'fail': '🔴'}.get(s['status'], '⚪') } {s['name']}", f"vpsg:{uid}:{days}:{s['id']}")]
                for s in list_servers()]
        rows.append([btn("🔙 Admin Panel", "admin_main")])
        return await msg.answer(
            f"🛰 <b>VPS Grant — User <code>{uid}</code> ({days} din)</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Kaun sa VPS server is user ke bots ke liye use karna hai?",
            reply_markup=kb(*rows))

    if action == "ref_cfg_dummy":
        return

    if action == "give_prem":
        parts = text.split()
        if len(parts) != 2 or parse_int(parts[0]) is None or parse_int(parts[1]) is None:
            return await msg.answer("❌ Format: <code>USER_ID DAYS</code> (e.g. <code>123456 30</code>)")
        uid, days = int(parts[0]), int(parts[1])
        ensure_user(uid)
        set_premium(uid, days)
        await state.clear()
        period = "Lifetime ♾" if days <= 0 else f"{days} days"
        try:
            await bot.send_message(uid, f"💎 <b>Premium activate ho gaya!</b>\n🗓 {period}")
        except Exception:
            pass
        return await msg.answer(f"💎 User <code>{uid}</code> ab Premium hai ({period})!", reply_markup=back)

    uid = parse_int(text)
    if uid is None:
        return await msg.answer("❌ Sahi numeric User ID bhejo.")
    await state.clear()

    if action == "rm_prem":
        remove_premium(uid)
        await msg.answer(f"🚫 Premium removed for <code>{uid}</code>.", reply_markup=back)
    elif action == "ban":
        if uid == ADMIN_ID:
            return await msg.answer("😅 Admin ko ban nahi kar sakte.", reply_markup=back)
        ensure_user(uid)
        set_ban(uid, True)
        stopped = await stop_user_bots(uid)
        await msg.answer(f"⛔ User <code>{uid}</code> banned. ({stopped} running bots band kiye)", reply_markup=back)
    elif action == "unban":
        set_ban(uid, False)
        await msg.answer(f"✅ User <code>{uid}</code> unbanned.", reply_markup=back)
    elif action == "info":
        u = get_user(uid)
        if not u:
            return await msg.answer("❌ User database me nahi mila.", reply_markup=back)
        await msg.answer(
            f"🔍 <b>User Info</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: <code>{uid}</code>\n"
            f"📛 Name: {esc(u['first_name'])}\n"
            f"🔗 Username: @{esc(u['username']) if u['username'] else '—'}\n"
            f"💎 Plan: {'PREMIUM' if u['premium'] else 'FREE'}\n"
            f"📅 Premium Till: {premium_expiry_text(u)}\n"
            f"🛰 VPS: {_vps_label(u)}\n"
            f"🪙 Coins: {u['coins']} · 👥 Invites: {invite_count(uid)}\n"
            f"🚫 Banned: {'Yes' if u['banned'] else 'No'}\n"
            f"📦 Bots: {user_bot_count(uid)}/{user_limit(u)}\n"
            f"🗓 Joined: {u['joined_at'] or '—'}",
            reply_markup=back,
        )


@dp.callback_query(F.data.startswith("vpsg:"))
async def cb_vps_grant(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        _, uid_s, days_s, sid_s = call.data.split(":")
        uid, days, sid = int(uid_s), int(days_s), int(sid_s)
    except ValueError:
        return await call.answer("Invalid", show_alert=True)
    srv = get_server(sid)
    if not srv:
        return await call.answer("Server nahi mila.", show_alert=True)
    set_vps(uid, days, sid)
    restarted = 0
    for inst in list(ACTIVE_BOTS.values()):
        if inst.user_id == uid and inst.is_running:
            await inst.stop()
            await inst.start()
            restarted += 1
    try:
        await bot.send_message(uid, f"🛰 <b>VPS Activate ho gaya!</b>\n🖥 Server: <b>{esc(srv['name'])}</b>\n🗓 {days} din\n"
                                    "Ab aapke bots 24x7 us VPS par chalenge.")
    except Exception:
        pass
    await call.answer("✅ VPS grant ho gaya")
    await safe_edit(call, f"🛰 <b>Done!</b>\nUser <code>{uid}</code> → <b>{esc(srv['name'])}</b> · {days} din\n"
                          f"🔁 Restart hue bots: {restarted}",
                    kb([btn("🖥 VPS Manager", "adm_vps")], [btn("🔙 Admin", "admin_main")]))


@dp.callback_query(F.data == "adm_prem_list")
async def cb_adm_prem_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT user_id, first_name, username, is_premium, premium_until FROM users WHERE is_premium=1 ORDER BY user_id DESC LIMIT 60", fetchall=True) or []
    lines = []
    for uid, name, uname, prem, until in rows:
        if not is_premium_active({"is_premium": prem, "premium_until": until}):
            continue
        handle = f"@{uname}" if uname else (name or "—")
        lines.append(f"💎 <code>{uid}</code> {esc(handle)} · {'♾' if not until else until[:10]}")
    body = "\n".join(lines[:40]) if lines else "Koi active premium user nahi."
    await safe_edit(call, f"💎 <b>Premium Users</b> ({len(lines)})\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Admin", "admin_main")]))


@dp.callback_query(F.data == "adm_ban_list")
async def cb_adm_ban_list(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT user_id, first_name, username FROM users WHERE is_banned=1 ORDER BY user_id DESC LIMIT 40", fetchall=True) or []
    body = "\n".join(f"⛔ <code>{uid}</code> {esc(('@' + uname) if uname else (name or '—'))}" for uid, name, uname in rows) or "Koi banned user nahi."
    await safe_edit(call, f"⛔ <b>Banned Users</b> ({len(rows)})\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Admin", "admin_main")]))


# ---------------------------- VPS MANAGER ----------------------------
VPS_STEPS = [
    ("name", "🏷 <b>Server ka naam</b> bhejo (e.g. <code>Mumbai-1</code>)"),
    ("host", "🌐 <b>IP address ya domain</b> bhejo (e.g. <code>203.0.113.10</code>)"),
    ("port", "🔌 <b>SSH port</b> bhejo (default <code>22</code> ke liye <code>-</code> bhejo)"),
    ("username", "👤 <b>Username</b> bhejo (e.g. <code>root</code> ya <code>ubuntu</code>)"),
    ("secret", "🔑 <b>Password</b> bhejo, ya <b>SSH private key</b> poora paste karo.\n<i>Password message turant delete ho jayega.</i>"),
]


def server_card(srv: dict) -> str:
    icon = {"ok": "🟢 Connected", "fail": "🔴 Failed", "untested": "⚪ Untested"}.get(srv["status"], "⚪")
    return (
        f"🛰 <b>{esc(srv['name'])}</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"🌐 Host: <code>{esc(srv['host'])}:{srv['port']}</code>\n"
        f"👤 User: <code>{esc(srv['username'])}</code>\n"
        f"🔐 Auth: {'🗝 SSH Key' if srv['auth'] == 'key' else '🔑 Password'}\n"
        f"📶 Status: {icon}\n"
        f"🕒 Last check: {esc(srv['last_check'] or '—')}\n"
        f"👥 VPS users: <b>{server_user_count(srv['id'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def server_card_kb(sid: int) -> InlineKeyboardMarkup:
    return kb(
        [btn("🧪 Test Connection", f"vst:{sid}"), btn("🛠 Setup VPS", f"vss:{sid}")],
        [btn("👥 VPS Users", f"vsu:{sid}")],
        [btn("🗑 Delete Server", f"vsd:{sid}")],
        [btn("🔙 VPS Manager", "adm_vps")],
    )


@dp.callback_query(F.data == "adm_vps")
async def cb_adm_vps(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    servers = list_servers()
    body = f"🛰 <b>VPS Manager</b>\n━━━━━━━━━━━━━━━━━━\nServers: <b>{len(servers)}</b>\n"
    body += "🟢 Connected · 🔴 Failed · ⚪ Untested\n━━━━━━━━━━━━━━━━━━\n👇 Server chuno:" if servers else \
        "Abhi koi server nahi hai.\n\n➕ <b>Add VPS Server</b> se apna VPS connect karo."
    await safe_edit(call, body, vps_menu())


@dp.callback_query(F.data == "vps_add")
async def cb_vps_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.wait_vps_input)
    await state.update_data(vps_step=0, vps_draft={})
    step_text = VPS_STEPS[0][1]
    await safe_edit(call, f"➕ <b>VPS Connect Wizard</b> (1/{len(VPS_STEPS)})\n━━━━━━━━━━━━━━━━━━\n{step_text}\n\n"
                          "<i>Cancel: /cancel</i>", kb([btn("🔙 VPS Manager", "adm_vps")]))


@dp.message(AdminStates.wait_vps_input)
async def admin_vps_wizard(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    step = int(data.get("vps_step", 0))
    draft: Dict[str, Any] = dict(data.get("vps_draft") or {})
    raw = (msg.text or "").strip()
    field = VPS_STEPS[step][0]

    if field == "secret":
        try:
            await msg.delete()
        except Exception:
            pass
    if not raw and field != "port":
        return await msg.answer("❌ Khali nahi chalega. Dobara bhejo.")

    if field == "name":
        draft["name"] = raw[:40]
    elif field == "host":
        if " " in raw or len(raw) > 255:
            return await msg.answer("❌ Sahi IP / domain bhejo (space nahi hona chahiye).")
        draft["host"] = raw
    elif field == "port":
        port = 22 if raw in ("", "-") else parse_int(raw)
        if port is None or not 1 <= port <= 65535:
            return await msg.answer("❌ Port 1 se 65535 ke beech bhejo (ya <code>-</code> for 22).")
        draft["port"] = port
    elif field == "username":
        if " " in raw:
            return await msg.answer("❌ Username me space nahi hona chahiye.")
        draft["username"] = raw
    elif field == "secret":
        if raw.startswith("-----BEGIN"):
            draft["auth"] = "key"
        else:
            draft["auth"] = "password"
        draft["secret"] = raw

    step += 1
    if step < len(VPS_STEPS):
        await state.update_data(vps_step=step, vps_draft=draft)
        return await msg.answer(f"➕ <b>VPS Connect Wizard</b> ({step + 1}/{len(VPS_STEPS)})\n━━━━━━━━━━━━━━━━━━\n"
                                f"{VPS_STEPS[step][1]}")

    await state.clear()
    sid = save_server(draft["name"], draft["host"], draft["port"], draft["username"], draft["auth"], draft["secret"])
    if not sid:
        return await msg.answer("❌ Server save nahi hua.", reply_markup=kb([btn("🔙 VPS Manager", "adm_vps")]))
    srv = get_server(sid)
    wait = await msg.answer("🔌 SSH connection test ho raha hai...")
    ok, out = await ssh_test(srv)
    srv = get_server(sid)
    head = "✅ <b>VPS connect ho gaya!</b>" if ok else "❌ <b>Connect nahi hua</b>"
    await wait.edit_text(f"{head}\n\n{server_card(srv)}\n<pre>{esc(out[:900])}</pre>",
                         reply_markup=server_card_kb(sid))


@dp.callback_query(F.data.startswith("vsv:"))
async def cb_vps_view(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sid = int(call.data.split(":")[1])
    srv = get_server(sid)
    if not srv:
        return await call.answer("Server nahi mila.", show_alert=True)
    await safe_edit(call, server_card(srv), server_card_kb(sid))


@dp.callback_query(F.data.startswith("vst:"))
async def cb_vps_test(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sid = int(call.data.split(":")[1])
    srv = get_server(sid)
    if not srv:
        return await call.answer("Server nahi mila.", show_alert=True)
    await call.answer("🔌 Testing...")
    ok, out = await ssh_test(srv)
    srv = get_server(sid)
    head = "🟢 <b>Connection OK</b>" if ok else "🔴 <b>Connection FAIL</b>"
    await safe_edit(call, f"{head}\n\n{server_card(srv)}\n<pre>{esc(out[:900])}</pre>", server_card_kb(sid))


@dp.callback_query(F.data.startswith("vss:"))
async def cb_vps_setup(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sid = int(call.data.split(":")[1])
    srv = get_server(sid)
    if not srv:
        return await call.answer("Server nahi mila.", show_alert=True)
    await call.answer("🛠 Setup chal raha hai (2-5 min)...")
    await safe_edit(call, f"🛠 <b>Setup chal raha hai...</b>\n{server_card(srv)}\n"
                          "Python / pip install + <code>/opt/prime_bots</code> ready kar raha hun.",
                    kb([btn("🔙 VPS Manager", "adm_vps")]))
    ok, out = await ssh_setup(srv)
    srv = get_server(sid)
    head = "✅ <b>Setup complete!</b>" if ok else "❌ <b>Setup me problem</b> (log dekho)"
    await call.message.answer(f"{head}\n<pre>{esc(out[-1200:])}</pre>", reply_markup=server_card_kb(sid))


@dp.callback_query(F.data.startswith("vsu:"))
async def cb_vps_users(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sid = int(call.data.split(":")[1])
    rows = db_query("SELECT user_id, first_name, vps_until FROM users WHERE vps_server_id=? AND is_vps=1",
                    (sid,), fetchall=True) or []
    body = "\n".join(f"🛰 <code>{uid}</code> {esc(n or '—')} · till {esc((u or '')[:10])}" for uid, n, u in rows) \
        or "Is server par koi user nahi."
    await safe_edit(call, f"👥 <b>VPS Users</b>\n━━━━━━━━━━━━━━━━━━\n{body}", kb([btn("🔙 Server", f"vsv:{sid}")]))


@dp.callback_query(F.data.startswith("vsd:"))
async def cb_vps_del_confirm(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sid = int(call.data.split(":")[1])
    await safe_edit(call, "⚠️ <b>Server delete karna hai?</b>\nIs server ke bots band ho jayenge aur users ka VPS link hat jayega.",
                    kb([btn("✅ Haan, delete", f"vsdy:{sid}"), btn("❌ Nahi", f"vsv:{sid}")]))


@dp.callback_query(F.data.startswith("vsdy:"))
async def cb_vps_del(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    sid = int(call.data.split(":")[1])
    for inst in list(ACTIVE_BOTS.values()):
        if inst.remote_sid == sid:
            inst.should_run = False
            inst.is_running = False
            set_status(inst.bot_id, "Stopped")
    delete_server(sid)
    await call.answer("🗑 Server delete ho gaya")
    servers = list_servers()
    await safe_edit(call, f"🛰 <b>VPS Manager</b>\nServers: <b>{len(servers)}</b>", vps_menu())


# ---------------------------- Plans & prices ----------------------------
@dp.callback_query(F.data == "adm_plans")
async def cb_adm_plans(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    rows = []
    for p in get_plans():
        rows.append([btn(f"✏️ {p['days']}d — ₹{p['price']}", f"adm_plan:{p['days']}"), btn("🗑", f"adm_pdel:{p['days']}")])
    rows.append([btn("➕ Add Plan", "adm_in:plan_add")])
    rows.append([btn("🔙 Admin", "admin_main")])
    await safe_edit(call, "💰 <b>Plans & Prices</b>\nPlan pe tap karke price badlo, 🗑 se delete karo.", kb(*rows))


@dp.callback_query(F.data.startswith("adm_plan:"))
async def cb_adm_plan(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    days = int(call.data.split(":")[1])
    await state.set_state(AdminStates.wait_input)
    await state.update_data(action="plan_price", plan_days=days)
    await safe_edit(call, admin_prompt_text("plan_price", extra=str(days)), kb([btn("🔙 Cancel", "adm_plans")]))


@dp.callback_query(F.data.startswith("adm_pdel:"))
async def cb_adm_pdel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    days = int(call.data.split(":")[1])
    save_plans([p for p in get_plans() if p["days"] != days])
    await call.answer("Plan deleted")
    await cb_adm_plans(call, state)


@dp.callback_query(F.data == "adm_pay")
async def cb_adm_pay(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT id, user_id, days, amount, kind FROM payments WHERE status='pending' AND proof_id!='' ORDER BY id DESC LIMIT 15",
                    fetchall=True) or []
    buttons = []
    for pid, uid, days, amount, kind in rows:
        label = "Clone" if kind == "clone" else f"{days}d"
        buttons.append([btn(f"✅ #{pid} {label} ₹{amount}", f"payok:{pid}"), btn("❌", f"payno:{pid}")])
    buttons.append([btn("🔙 Admin", "admin_main")])
    body = "\n".join(f"💳 #{pid} · <code>{uid}</code> · {'Clone' if kind == 'clone' else str(days) + ' day(s)'} · ₹{esc(amount)}"
                     for pid, uid, days, amount, kind in rows) or "Koi pending payment nahi."
    await safe_edit(call, f"💳 <b>Pending Payments</b>\n━━━━━━━━━━━━━━━━━━\n{body}", kb(*buttons))


def paycfg_text() -> str:
    mode = get_setting("pay_mode", "upi")
    key = pay_api_key()
    masked = ("•" * 6 + key[-4:]) if key else "not set"
    return (
        "💳 <b>Payment Setup</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Mode: <b>{mode.upper()}</b>\n"
        f"UPI ID: <code>{esc(get_setting('upi_id', '') or 'not set')}</code>\n"
        f"API URL: <code>{esc(get_setting('pay_api_url', '') or 'not set')}</code>\n"
        f"API Key: <code>{esc(masked)}</code>\n━━━━━━━━━━━━━━━━━━"
    )


def paycfg_markup() -> InlineKeyboardMarkup:
    mode = get_setting("pay_mode", "upi")
    return kb(
        [btn(f"🔁 Mode: {mode.upper()} (tap to switch)", "adm_paymode")],
        [btn("✏️ Set UPI ID", "adm_in:upi_set"), btn("🧪 Test UPI", "adm_test_upi")],
        [btn("🔗 Set API URL", "adm_in:api_url"), btn("🔑 Set API Key", "adm_in:api_key")],
        [btn("🧪 Test API Connection", "adm_test_api")],
        [btn("🔙 Admin", "admin_main")],
    )


@dp.callback_query(F.data == "adm_paycfg")
async def cb_adm_paycfg(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await safe_edit(call, paycfg_text(), paycfg_markup())


@dp.callback_query(F.data == "adm_paymode")
async def cb_adm_paymode(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "api" if get_setting("pay_mode", "upi") == "upi" else "upi"
    set_setting("pay_mode", new)
    await call.answer(f"Mode: {new.upper()}")
    await safe_edit(call, paycfg_text(), paycfg_markup())


@dp.callback_query(F.data == "adm_test_upi")
async def cb_adm_test_upi(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    upi = get_setting("upi_id", "")
    if not upi:
        return await call.answer("Pehle UPI ID set karo.", show_alert=True)
    if not UPI_RE.match(upi):
        return await call.answer("UPI ID ka format galat hai.", show_alert=True)
    link = upi_link(upi, BRAND, "1", "TEST")
    await call.answer("✅ UPI format valid")
    await call.message.answer(f"🧪 <b>UPI Test</b>\n✅ Format valid: <code>{esc(upi)}</code>\n"
                              f"Test link (₹1):\n<code>{esc(link)}</code>")
    qr_png = await make_qr_png(link)
    if qr_png:
        await call.message.answer_photo(BufferedInputFile(qr_png, filename="test_upi_qr.png"),
                                        caption="🧪 Test QR (₹1)")


@dp.callback_query(F.data == "adm_test_api")
async def cb_adm_test_api(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("🧪 Testing...")
    result = await test_api_connection()
    await call.message.answer(f"🧪 <b>API Test</b>\n{result}")


# ---------------------------- Emoji backup ----------------------------
@dp.callback_query(F.data == "adm_emb_export")
async def cb_adm_emb_export(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    raw = build_emoji_backup()
    await call.answer("💾 Backup ban raha hai...")
    await call.message.answer_document(
        BufferedInputFile(raw, filename=f"emoji_backup_{datetime.now():%Y%m%d_%H%M}.json"),
        caption="💾 <b>Emoji Backup</b>\nIs file ko 📥 Import Emoji Backup se wapas load kar sakte ho.",
    )


@dp.callback_query(F.data == "adm_emb_import")
async def cb_adm_emb_import(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.wait_emoji_import)
    await safe_edit(call, "📥 <b>Emoji Backup Import</b>\n━━━━━━━━━━━━━━━━━━\n"
                          "Pehle wali <code>emoji_backup_*.json</code> file bhejo.\n\n<i>Cancel: /cancel</i>",
                    kb([btn("🔙 Admin", "admin_main")]))


@dp.message(AdminStates.wait_emoji_import, F.document)
async def admin_import_emoji_doc(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    doc = msg.document
    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        return await msg.answer("❌ Sirf <b>.json</b> backup file bhejo. Cancel: /cancel")
    try:
        buf = await bot.download(doc)
        n_set, n_lib = import_emoji_backup(buf.getvalue())
    except Exception as e:
        return await msg.answer(f"❌ Import fail: <code>{esc(str(e)[:300])}</code>")
    await state.clear()
    await msg.answer(f"✅ <b>Emoji backup import ho gaya!</b>\n⚙️ Settings: <b>{n_set}</b>\n🧰 Naye library emoji: <b>{n_lib}</b>\n"
                     f"🧰 Library total: <b>{lib_count()}</b>", reply_markup=kb([btn("🔙 Admin", "admin_main")]))


@dp.message(AdminStates.wait_emoji_import)
async def admin_import_emoji_other(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    await msg.answer("📥 Backup <b>.json</b> file document ki tarah bhejo. Cancel: /cancel")


# ---------------------------- Channels ----------------------------
@dp.callback_query(F.data == "adm_chans")
async def cb_adm_chans(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    chans = get_channels()
    rows = [[btn(f"🗑 {ch}", f"adm_cdel:{ch}")] for ch in chans]
    rows.append([btn("➕ Add Channel", "adm_in:chan_add")])
    rows.append([btn("🔙 Admin", "admin_main")])
    body = "\n".join(f"📌 {esc(c)}" for c in chans) or "<i>Koi channel nahi (sab open hai)</i>"
    await safe_edit(call, f"📌 <b>Force Join Channels</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\nChannel pe tap karke remove karo.", kb(*rows))


@dp.callback_query(F.data.startswith("adm_cdel:"))
async def cb_adm_cdel(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    ch = call.data.split(":", 1)[1]
    save_channels([c for c in get_channels() if c != ch])
    await call.answer("Removed")
    chans = get_channels()
    rows = [[btn(f"🗑 {c}", f"adm_cdel:{c}")] for c in chans]
    rows.append([btn("➕ Add Channel", "adm_in:chan_add")])
    rows.append([btn("🔙 Admin", "admin_main")])
    body = "\n".join(f"📌 {esc(c)}" for c in chans) or "<i>Koi channel nahi (sab open hai)</i>"
    await safe_edit(call, f"📌 <b>Force Join Channels</b>\n━━━━━━━━━━━━━━━━━━\n{body}\n\nChannel pe tap karke remove karo.", kb(*rows))


# ---------------------------- Toggles ----------------------------
@dp.callback_query(F.data == "adm_maint")
async def cb_adm_maint(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if get_setting("maintenance", "OFF") == "ON" else "ON"
    set_setting("maintenance", new)
    await call.answer(f"🔧 Maintenance {new}")
    await safe_edit(call, admin_header(), admin_main_menu())


@dp.callback_query(F.data == "adm_toggle_pe")
async def cb_adm_toggle_pe(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if get_setting("premium_emoji", "ON") == "ON" else "ON"
    set_setting("premium_emoji", new)
    await call.answer(f"✨ Premium Emoji {new}")
    await safe_edit(call, admin_header(), admin_main_menu())


@dp.callback_query(F.data == "adm_fast")
async def cb_adm_fast(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    new = "OFF" if fast_approve_on() else "ON"
    set_setting("fast_approve", new)
    await call.answer(f"⚡ Fast Approve {new}")
    await safe_edit(call, admin_header(), admin_main_menu())


@dp.callback_query(F.data == "adm_backup")
async def cb_adm_backup(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.answer("🗂 Backup ban raha hai...")
    name = f"hosting_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    tmp = os.path.join(tempfile.gettempdir(), name)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        await call.message.answer_document(FSInputFile(tmp, filename=name), caption="🗂 Database backup")
    finally:
        os.remove(tmp)


# ---------------------------- Edit global emojis ----------------------------
EMOJI_PAGE = 12


@dp.callback_query(F.data.startswith("adm_emoji"))
async def cb_adm_emoji(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    page = int(call.data.split(":")[1]) if ":" in call.data and call.data.split(":")[1].isdigit() else 0
    slots = list(DEFAULT_EMOJIS)
    pages = (len(slots) + EMOJI_PAGE - 1) // EMOJI_PAGE
    page = max(0, min(page, pages - 1))
    rows, pair = [], []
    for sl in slots[page * EMOJI_PAGE:(page + 1) * EMOJI_PAGE]:
        has_premium = bool(get_setting(f"emoji_{sl}", "") or DEFAULT_EMOJIS.get(sl))
        pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{' *' if has_premium else ''}", f"emo:{sl}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"adm_emoji:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"adm_emoji:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("🔙 Admin", "admin_main")])
    await safe_edit(call, f"🎨 <b>Edit Emojis</b> (page {page + 1}/{pages})\n\nSlot chuno, phir premium emoji (ya ID) bhejo.\n"
                          "<code>*</code> = premium ID set hai.", kb(*rows))


@dp.callback_query(F.data.startswith("emo:"))
async def cb_emoji_pick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    slot = call.data.split(":", 1)[1]
    if slot not in DEFAULT_EMOJIS:
        return await call.answer("Unknown slot", show_alert=True)
    await state.update_data(slot=slot)
    await state.set_state(AdminStates.wait_emoji_value)
    cur = get_setting(f"emoji_{slot}", "") or DEFAULT_EMOJIS.get(slot) or "—"
    await safe_edit(call, f"🎨 <b>{slot}</b> {EMOJI_FALLBACK[slot]}\nCurrent ID: <code>{cur}</code>\n\n"
                          "Premium emoji (ya numeric ID) bhejo.\n<code>default</code> = code wala default.\n<code>none</code> = sirf normal emoji.",
                    kb([btn("🔙 Back", "adm_emoji")]))


def _read_emoji_input(msg: Message) -> Tuple[Optional[str], bool]:
    txt = (msg.text or msg.caption or "").strip()
    ids = [e.custom_emoji_id for e in (msg.entities or []) if e.type == "custom_emoji" and e.custom_emoji_id]
    if txt.lower() in ("default", "reset"):
        return None, True
    if ids:
        return str(ids[0]), False
    if txt.isdigit():
        return txt, False
    return None, False


@dp.message(AdminStates.wait_emoji_value)
async def admin_save_emoji(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    slot = (await state.get_data()).get("slot")
    if slot not in DEFAULT_EMOJIS:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    txt = (msg.text or "").strip().lower()
    eid, reset = _read_emoji_input(msg)
    if txt == "none":
        set_setting(f"emoji_{slot}", "x")
        done = "ab normal emoji dikhayega"
    elif reset:
        del_setting(f"emoji_{slot}")
        done = "default par wapas"
    elif eid:
        set_setting(f"emoji_{slot}", eid)
        done = f"set: <code>{eid}</code>"
    else:
        return await msg.answer("❌ Premium emoji, numeric ID, <code>default</code> ya <code>none</code> bhejo.")
    await state.clear()
    await msg.answer(f"✅ <b>{slot}</b> {done}.", reply_markup=kb([btn("🔙 Back to emojis", "adm_emoji")]))


# ---------------------------- Edit UI texts ----------------------------
def _ui_slots_in(key: str) -> List[str]:
    tpl = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    out: List[str] = []
    for sl in re.findall(r"\{([a-z_]+)\}", tpl):
        if sl in DEFAULT_EMOJIS and sl not in out:
            out.append(sl)
    return out


def _ui_key(call: CallbackQuery) -> Optional[str]:
    key = call.data.split(":", 1)[1].split(":", 1)[0]
    return key if key in UI_EDITABLE else None


@dp.callback_query(F.data == "adm_ui")
async def cb_adm_ui(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    rows, pair = [], []
    for key, (label, _h) in UI_EDITABLE.items():
        tags = []
        if get_setting(f"ui_{key}", ""):
            tags.append("edited")
        if get_setting(f"ui_yt_{key}", ""):
            tags.append("video")
        pair.append(btn(label + (f" ({', '.join(tags)})" if tags else ""), f"uip:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔙 Admin", "admin_main")])
    await safe_edit(call, "✏️ <b>Edit UI Texts</b>\n\nHar screen ka alag admin panel hai. Ek screen chuno:", kb(*rows))


async def _show_ui_panel(call: CallbackQuery, key: str):
    yt = get_setting(f"ui_yt_{key}", "")
    edited = bool(get_setting(f"ui_{key}", ""))
    n_emoji = len([1 for sl in _ui_slots_in(key) if get_setting(f"uiemoji_{key}_{sl}", "")])
    rows = [
        [btn("✏️ Edit Text", f"uied:{key}"), btn("👁 Preview", f"uiprev:{key}")],
        [btn("▶️ " + ("Change YouTube Link" if yt else "Set YouTube Link"), f"uiyt:{key}")],
    ]
    if yt:
        rows.append([btn("🗑 Remove YouTube Link", f"uiytd:{key}")])
    rows.append([btn("🎨 Set Emojis of this screen", f"uiem:{key}")])
    rows.append([btn("🔄 Reset text to default", f"uirs:{key}")])
    rows.append([btn("🔙 Back to list", "adm_ui")])
    await safe_edit(call, f"✏️ <b>{UI_EDITABLE[key][0]}</b>\n\n▶️ YouTube: {esc(yt) if yt else '<i>not set</i>'}\n"
                          f"✅ Custom text: <b>{'yes' if edited else 'no (default)'}</b>\n🎨 Custom emojis: <b>{n_emoji}</b>",
                    kb(*rows))


@dp.callback_query(F.data.startswith("uip:"))
async def cb_ui_panel(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.clear()
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiprev:"))
async def cb_ui_preview(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await call.answer()
    text = re.sub(r"\{(v_\w+)\}", "—", ui(key))
    btn_ = ui_video_button(key)
    try:
        await call.message.answer(text, reply_markup=kb([btn_]) if btn_ else None)
    except Exception as e:
        await call.message.answer(f"❌ Is text me galat HTML / emoji hai:\n<code>{esc(str(e))[:300]}</code>")


@dp.callback_query(F.data.startswith("uied:"))
async def cb_ui_edit(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.wait_ui_text)
    current = get_setting(f"ui_{key}", "") or DEFAULT_UI.get(key, "")
    await safe_edit(call, f"✏️ <b>{UI_EDITABLE[key][0]}</b>\n\nNaya text bhejo (HTML / formatting / premium emoji chalega).\n"
                          f"Placeholders: <code>{esc(UI_EDITABLE[key][1])}</code>\n\nCurrent template:\n<pre>{esc(current)}</pre>",
                    kb([btn("🔙 Back", f"uip:{key}")]))


@dp.message(AdminStates.wait_ui_text)
async def admin_save_ui_text(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    key = (await state.get_data()).get("ui_key")
    new_text = (msg.html_text or "").strip()
    if key not in UI_EDITABLE or not new_text:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    if len(new_text) > 3500:
        return await msg.answer("❌ Bahut lamba (max 3500 characters).")
    set_setting(f"ui_{key}", new_text)
    await state.clear()
    await msg.answer(f"✅ <b>{UI_EDITABLE[key][0]}</b> update ho gaya!", reply_markup=kb([btn("🔙 Back to this screen", f"uip:{key}")]))


@dp.callback_query(F.data.startswith("uirs:"))
async def cb_ui_reset(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_setting(f"ui_{key}")
    await state.clear()
    await call.answer("Default par reset")
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiyt:"))
async def cb_ui_yt(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.update_data(ui_key=key)
    await state.set_state(AdminStates.wait_ui_video)
    cur = get_setting(f"ui_yt_{key}", "")
    await safe_edit(call, f"▶️ <b>YouTube link — {UI_EDITABLE[key][0]}</b>\n\nYouTube link bhejo.\nCurrent: {esc(cur) if cur else '<i>none</i>'}",
                    kb([btn("🔙 Back", f"uip:{key}")]))


@dp.message(AdminStates.wait_ui_video)
async def admin_save_ui_video(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    key = (await state.get_data()).get("ui_key")
    link = (msg.text or "").strip()
    if key not in UI_EDITABLE:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    if not YT_RE.match(link):
        return await msg.answer("❌ Sahi YouTube link bhejo (youtube.com ya youtu.be).")
    set_setting(f"ui_yt_{key}", link)
    await state.clear()
    await msg.answer(f"✅ YouTube link save ho gaya.", reply_markup=kb([btn("🔙 Back to this screen", f"uip:{key}")]))


@dp.callback_query(F.data.startswith("uiytd:"))
async def cb_ui_yt_delete(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_setting(f"ui_yt_{key}")
    await call.answer("YouTube link hata diya")
    await _show_ui_panel(call, key)


@dp.callback_query(F.data.startswith("uiem:"))
async def cb_ui_emoji_list(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    await state.clear()
    rows, pair = [], []
    for sl in _ui_slots_in(key):
        mark = " *" if get_setting(f"uiemoji_{key}_{sl}", "") else ""
        pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{mark}", f"uies:{key}:{sl}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([btn("🔄 Reset all emojis of this screen", f"uiemr:{key}")])
    rows.append([btn("🔙 Back", f"uip:{key}")])
    await safe_edit(call, f"🎨 <b>Emojis — {UI_EDITABLE[key][0]}</b>\n\nEmoji slot chuno, phir premium emoji bhejo.", kb(*rows))


@dp.callback_query(F.data.startswith("uies:"))
async def cb_ui_emoji_pick(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    try:
        _, key, slot = call.data.split(":", 2)
    except ValueError:
        return await call.answer("Unknown slot", show_alert=True)
    if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
        return await call.answer("Unknown slot", show_alert=True)
    await state.update_data(ui_key=key, slot=slot)
    await state.set_state(AdminStates.wait_ui_emoji)
    await safe_edit(call, f"🎨 <b>{slot}</b> — {UI_EDITABLE[key][0]}\n\nPremium emoji (ya ID) bhejo. <code>default</code> = global.",
                    kb([btn("🔙 Back", f"uiem:{key}")]))


@dp.message(AdminStates.wait_ui_emoji)
async def admin_save_ui_emoji(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key, slot = data.get("ui_key"), data.get("slot")
    if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
        await state.clear()
        return await msg.answer("❌ Kuch save nahi hua.", reply_markup=admin_main_menu())
    eid, reset = _read_emoji_input(msg)
    if reset:
        del_setting(f"uiemoji_{key}_{slot}")
        done = "global emoji par wapas"
    elif eid:
        set_setting(f"uiemoji_{key}_{slot}", eid)
        done = f"set: <code>{eid}</code>"
    else:
        return await msg.answer("❌ Premium emoji, numeric ID ya <code>default</code> bhejo.")
    await state.clear()
    await msg.answer(f"✅ <b>{slot}</b> {done}.", reply_markup=kb([btn("🔙 Back to emojis", f"uiem:{key}")]))


@dp.callback_query(F.data.startswith("uiemr:"))
async def cb_ui_emoji_reset(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = _ui_key(call)
    if not key:
        return await call.answer("Unknown screen", show_alert=True)
    del_settings_like(f"uiemoji_{key}_%")
    await call.answer("Emojis reset")
    await cb_ui_emoji_list(call, state)


# ---------------------------- Emoji Map ----------------------------
async def _render_emap(call: CallbackQuery):
    rows = []
    for ch, pid in list(custom_emoji_map().items())[:30]:
        rows.append([btn(f"{ch} → {pid[:10]}…", f"emapdel:{_emap_key(ch)}")])
    rows.append([btn("🔙 Admin", "admin_main")])
    await safe_edit(call, "🗺 <b>Emoji Map</b>\n━━━━━━━━━━━━━━━━━━\n"
                          "Koi bhi <b>premium emoji</b> chat me bhejo; ye har message / button me normal emoji ki jagah dikhega.\n"
                          "Delete karne ke liye tap karo.\n\n<i>Band: /cancel</i>", kb(*rows))


@dp.callback_query(F.data == "adm_emap")
async def cb_adm_emap(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.wait_emap)
    await _render_emap(call)


@dp.callback_query(F.data.startswith("emapdel:"))
async def cb_emap_del(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    key = call.data.split(":", 1)[1]
    if not key.startswith("emap_"):
        return await call.answer("Invalid", show_alert=True)
    del_setting(key)
    await call.answer("Mapping removed")
    await state.set_state(AdminStates.wait_emap)
    await _render_emap(call)


@dp.message(AdminStates.wait_emap)
async def admin_save_emap(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    txt = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    raw = txt.encode("utf-16-le")
    saved = []
    for e in ents:
        if e.type == "custom_emoji" and e.custom_emoji_id:
            ch = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", errors="ignore")
            if ch:
                set_setting(_emap_key(ch), e.custom_emoji_id)
                saved.append(f"{ch} → <code>{e.custom_emoji_id}</code>")
    if not saved:
        return await msg.answer("❌ Premium emoji bhejo (normal emoji nahi). Band karne ke liye /cancel.")
    await msg.answer("✅ <b>Mapping save ho gayi:</b>\n" + "\n".join(saved) + "\n\nAur bhejo, ya /cancel.")


# ==============================================================================
# 🧰 PREMIUM EMOJI LIBRARY
# ==============================================================================
LIB_PAGE = 10
LIB_SLOT_PAGE = 12
PACK_RE = re.compile(r"(?:t\.me|telegram\.me)/add(?:emoji|stickers)/([A-Za-z0-9_]+)", re.I)


def _tg_emoji(eid: str, ch: str = "") -> str:
    return f'<tg-emoji emoji-id="{eid}">{esc(ch or "✨")}</tg-emoji>'


def lib_seed_defaults() -> None:
    seen = set()
    for slot, fb, eid in EMOJI_SLOTS:
        if eid and eid not in seen:
            seen.add(eid)
            lib_add(eid, fb, name=slot, pack="default")


def lib_count() -> int:
    row = db_query("SELECT COUNT(*) FROM premium_emojis", fetchone=True)
    return int(row[0]) if row else 0


def lib_add(eid: Any, ch: str = "", name: str = "", pack: str = "") -> bool:
    eid = str(eid).strip()
    if not eid.isdigit():
        return False
    exists = db_query("SELECT id FROM premium_emojis WHERE emoji_id=?", (eid,), fetchone=True)
    if exists:
        if ch:
            db_query("UPDATE premium_emojis SET char=? WHERE emoji_id=? AND (char IS NULL OR char='')", (ch, eid))
        return False
    db_insert("INSERT OR IGNORE INTO premium_emojis (emoji_id, char, name, pack, added_at) VALUES (?,?,?,?,?)",
              (eid, ch or "", name or "", pack or "", now_str()))
    return True


def lib_get(rid: int) -> Optional[tuple]:
    return db_query("SELECT id, emoji_id, char, name, pack, added_at FROM premium_emojis WHERE id=?", (rid,), fetchone=True)


def lib_map_char(ch: str, eid: str, overwrite: bool = False) -> bool:
    base = (ch or "").replace("\ufe0f", "").strip()
    if not base or len(base) > 16 or base.isascii():
        return False
    key = _emap_key(base)
    if not overwrite and get_setting(key, "").isdigit():
        return False
    set_setting(key, eid)
    return True


PREMIUM2_FILE = os.path.join(APP_DIR, "premium2.py")
_P2_TAG_RE = re.compile(r"<tg-emoji\s+emoji-id=['\"](\d+)['\"]\s*>(.*?)</tg-emoji>", re.S)


def parse_premium2(path: str) -> List[Tuple[str, str]]:
    """premium2.py se (emoji_id, char) pairs nikalta hai. Duplicate ID hata deta hai."""
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        text = fh.read()
    out: List[Tuple[str, str]] = []
    seen = set()
    for eid, ch in _P2_TAG_RE.findall(text):
        if eid in seen:
            continue
        seen.add(eid)
        out.append((eid, ch.replace("\ufe0f", "").strip()))
    return out


def load_premium2(path: str = PREMIUM2_FILE, overwrite: bool = False, map_chars: bool = False) -> Tuple[int, int, int]:
    """Library me premium2 emoji dalta. Returns (total, naye_added, mapped)."""
    pairs = parse_premium2(path)
    new = mapped = 0
    first_for_char: Dict[str, str] = {}
    for idx, (eid, ch) in enumerate(pairs, 1):
        if lib_add(eid, ch, name=f"p2_{idx}", pack="premium2"):
            new += 1
        if ch:
            first_for_char.setdefault(ch, eid)
    for ch, eid in (first_for_char.items() if map_chars else []):
        if lib_map_char(ch, eid, overwrite=overwrite):
            mapped += 1
    return len(pairs), new, mapped


def lib_automap_on() -> bool:
    return get_setting("lib_automap", "ON") != "OFF"


def lib_usage(eid: str) -> List[str]:
    if _SETTINGS is None:
        _load_settings()
    out: List[str] = []
    for k, v in (_SETTINGS or {}).items():
        if v != eid:
            continue
        if k.startswith("emoji_"):
            out.append(f"slot {k[6:]}")
        elif k.startswith("uiemoji_"):
            out.append(f"screen {k[8:]}")
        elif k.startswith("emap_"):
            try:
                out.append("map " + "".join(chr(int(h, 16)) for h in k[5:].split("_")))
            except ValueError:
                pass
    return out


async def resolve_emoji_chars(ids: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for i in range(0, len(ids), 200):
            res = await bot.get_custom_emoji_stickers(custom_emoji_ids=ids[i:i + 200])
            for st in res:
                if st.custom_emoji_id:
                    out[st.custom_emoji_id] = st.emoji or ""
    except Exception as e:
        logger.warning("resolve_emoji_chars: %s", e)
    return out


async def import_pack(name: str) -> Tuple[int, int, int, str]:
    ss = await bot.get_sticker_set(name)
    total = added = mapped = 0
    auto = lib_automap_on()
    for st in ss.stickers:
        eid = getattr(st, "custom_emoji_id", None)
        if not eid:
            continue
        total += 1
        ch = st.emoji or ""
        if lib_add(eid, ch, pack=name):
            added += 1
        if auto and lib_map_char(ch, eid):
            mapped += 1
    return total, added, mapped, ss.title or name


def _lib_back_kb() -> InlineKeyboardMarkup:
    return kb([btn("🧰 Open Library", "adm_lib:0")], [btn("🔙 Admin", "admin_main")])


async def _lib_show(call: CallbackQuery, page: int, query: str = ""):
    if query:
        like = f"%{query}%"
        where, params = "WHERE name LIKE ? OR char LIKE ? OR pack LIKE ? OR emoji_id LIKE ?", (like, like, like, like)
    else:
        where, params = "", ()
    row = db_query(f"SELECT COUNT(*) FROM premium_emojis {where}", params, fetchone=True)
    total = int(row[0]) if row else 0
    pages = max(1, (total + LIB_PAGE - 1) // LIB_PAGE)
    page = max(0, min(page, pages - 1))
    rows = db_query(f"SELECT id, emoji_id, char, name, pack FROM premium_emojis {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                    params + (LIB_PAGE, page * LIB_PAGE), fetchall=True) or []
    lines, numbers = [], []
    for n, (rid, eid, ch, name, pack) in enumerate(rows, 1):
        label = name or pack or ""
        lines.append(f"{n}. {_tg_emoji(eid, ch)} <code>…{eid[-6:]}</code> {esc(label)}")
        numbers.append(btn(str(n), f"lib:i:{rid}"))
    body = "\n".join(lines) if lines else "<i>Library khali hai. ➕ Add se premium emoji jodo.</i>"
    am = "ON" if lib_automap_on() else "OFF"
    text = (f"🧰 <b>Premium Emoji Library</b> — {total} emoji (page {page + 1}/{pages})\n━━━━━━━━━━━━━━━━━━\n{body}\n━━━━━━━━━━━━━━━━━━\n"
            "Number dabao → emoji ko kahan lagana hai chuno.")
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"adm_lib:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"adm_lib:{page + 1}"))
    markup_rows = [numbers[i:i + 5] for i in range(0, len(numbers), 5)]
    if nav:
        markup_rows.append(nav)
    markup_rows += [
        [btn("➕ Add Emojis", "adm_lib_add"), btn("🔎 Find", "adm_lib_find")],
        [btn(f"⚙️ Auto-map on add: {am}", "adm_lib_am"), btn("🚀 Auto-map All", "adm_lib_amall")],
        [btn("🗑 Clear Library", "adm_lib_clear")],
        [btn("🔙 Admin", "admin_main")],
    ]
    await safe_edit(call, text, kb(*markup_rows))


@dp.callback_query(F.data.startswith("adm_lib:"))
async def cb_adm_lib(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    try:
        page = int(call.data.split(":", 1)[1])
    except ValueError:
        page = 0
    await _lib_show(call, page)


@dp.callback_query(F.data == "adm_lib_add")
async def cb_adm_lib_add(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.wait_lib_add)
    await safe_edit(call, "➕ <b>Premium Emojis add karo</b>\n━━━━━━━━━━━━━━━━━━\n"
                          "1️⃣ Premium emoji bhejo\n2️⃣ Numeric ID bhejo\n"
                          "3️⃣ Pack link bhejo: <code>https://t.me/addemoji/PackName</code>\n\n"
                          f"⚙️ Auto-map: <b>{'ON' if lib_automap_on() else 'OFF'}</b>\n<i>Band: /cancel</i>",
                    kb([btn("🔙 Back", "adm_lib:0")]))


@dp.message(AdminStates.wait_lib_add)
async def admin_lib_add(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    txt = msg.text or msg.caption or ""
    ents = msg.entities or msg.caption_entities or []
    raw = txt.encode("utf-16-le")
    found: Dict[str, str] = {}
    for e in ents:
        if e.type == "custom_emoji" and e.custom_emoji_id:
            ch = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", errors="ignore")
            found.setdefault(e.custom_emoji_id, ch)
    st = getattr(msg, "sticker", None)
    if st is not None and getattr(st, "custom_emoji_id", None):
        found.setdefault(st.custom_emoji_id, st.emoji or "")
    id_only = [t for t in re.findall(r"\d{10,25}", txt) if t not in found]
    if id_only:
        chars = await resolve_emoji_chars(id_only)
        for t in id_only:
            found.setdefault(t, chars.get(t, ""))
    packs = list(dict.fromkeys(PACK_RE.findall(txt)))
    if not packs and not found:
        t = txt.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,63}", t):
            packs = [t]
    if not found and not packs:
        return await msg.answer("❌ Premium emoji, numeric ID ya pack link bhejo. Band: /cancel")

    new = mapped = 0
    auto = lib_automap_on()
    for eid, ch in found.items():
        if lib_add(eid, ch):
            new += 1
        if auto and lib_map_char(ch, eid):
            mapped += 1
    report = []
    if found:
        report.append(f"✅ {len(found)} emoji mile · <b>{new}</b> naye · map: <b>{mapped}</b>")
    for name in packs:
        try:
            total, added, pm, title = await import_pack(name)
            if total == 0:
                report.append(f"❌ <code>{esc(name)}</code>: ye custom-emoji pack nahi hai.")
            else:
                report.append(f"📦 <b>{esc(title)}</b>: {total} emoji · <b>{added}</b> naye · map: <b>{pm}</b>")
        except Exception as e:
            report.append(f"❌ <code>{esc(name)}</code>: pack nahi mila ({esc(str(e))[:80]})")
    report.append(f"\n🧰 Library total: <b>{lib_count()}</b>\n<i>Aur bhejo, ya /cancel.</i>")
    await msg.answer("\n".join(report), reply_markup=_lib_back_kb())


@dp.callback_query(F.data == "adm_lib_find")
async def cb_adm_lib_find(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.wait_lib_find)
    await safe_edit(call, "🔎 <b>Find</b>\nNaam / emoji / pack / ID ka koi hissa bhejo.", kb([btn("🔙 Back", "adm_lib:0")]))


@dp.message(AdminStates.wait_lib_find)
async def admin_lib_find(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    q = (msg.text or "").strip()
    if not q:
        return await msg.answer("❌ Text bhejo.")
    await state.clear()
    like = f"%{q}%"
    rows = db_query("SELECT id, emoji_id, char, name, pack FROM premium_emojis "
                    "WHERE name LIKE ? OR char LIKE ? OR pack LIKE ? OR emoji_id LIKE ? ORDER BY id DESC LIMIT 20",
                    (like, like, like, like), fetchall=True) or []
    if not rows:
        return await msg.answer("❌ Kuch nahi mila.", reply_markup=_lib_back_kb())
    lines, btns = [], []
    for n, (rid, eid, ch, name, pack) in enumerate(rows, 1):
        lines.append(f"{n}. {_tg_emoji(eid, ch)} <code>…{eid[-6:]}</code> {esc(name or pack or '')}")
        btns.append(btn(str(n), f"lib:i:{rid}"))
    await msg.answer(f"🔎 <b>{len(rows)} result</b>\n" + "\n".join(lines),
                     reply_markup=kb(*[btns[i:i + 5] for i in range(0, len(btns), 5)], [btn("🧰 Library", "adm_lib:0")]))


@dp.callback_query(F.data == "adm_lib_am")
async def cb_adm_lib_am(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    set_setting("lib_automap", "OFF" if lib_automap_on() else "ON")
    await call.answer("Auto-map: " + ("ON" if lib_automap_on() else "OFF"))
    await _lib_show(call, 0)


@dp.callback_query(F.data == "adm_lib_amall")
async def cb_adm_lib_amall(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    rows = db_query("SELECT emoji_id, char FROM premium_emojis ORDER BY id ASC", fetchall=True) or []
    done = sum(1 for eid, ch in rows if lib_map_char(ch, eid))
    await call.answer(f"✅ {done} naye mapping bane", show_alert=True)


@dp.callback_query(F.data == "adm_lib_clear")
async def cb_adm_lib_clear(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await safe_edit(call, "⚠️ <b>Poori library delete karni hai?</b>",
                    kb([btn("✅ Haan, delete", "adm_lib_clear2"), btn("❌ Nahi", "adm_lib:0")]))


@dp.callback_query(F.data == "adm_lib_clear2")
async def cb_adm_lib_clear2(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    db_query("DELETE FROM premium_emojis")
    await call.answer("Library clear")
    await _lib_show(call, 0)


async def _lib_item(call: CallbackQuery, rid: int):
    row = lib_get(rid)
    if not row:
        await call.answer("Ye emoji ab library me nahi hai", show_alert=True)
        return await _lib_show(call, 0)
    _id, eid, ch, name, pack, added = row
    used = lib_usage(eid)
    text = (
        f"🧰 <b>Emoji #{rid}</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"{_tg_emoji(eid, ch)}  ←  premium\n"
        f"🆔 ID: <code>{eid}</code>\n"
        f"🔤 Normal emoji: {esc(ch) if ch else '<i>unknown</i>'}\n"
        f"📛 Naam: {esc(name) if name else '<i>—</i>'}\n"
        f"📦 Pack: {esc(pack) if pack else '<i>—</i>'}\n"
        f"📅 Added: {esc(added)}\n"
        f"📍 Lagaya hua: {esc(', '.join(used[:12])) if used else '<i>kahin nahi</i>'}\n"
        "━━━━━━━━━━━━━━━━━━\n👇 <b>Kahan lagana hai?</b>"
    )
    rows = [[btn("📍 Global Slot", f"lib:g:{rid}:0")], [btn("🖥 Sirf ek Screen me", f"lib:s:{rid}")]]
    if ch:
        rows.append([btn(f"🗺 Map: {ch} → ye premium", f"lib:m:{rid}")])
    rows.append([btn("🗺 Kisi aur emoji se Map", f"lib:mo:{rid}")])
    rows.append([btn("✏️ Rename", f"lib:rn:{rid}"), btn("🗑 Delete", f"lib:d:{rid}")])
    rows.append([btn("🔙 Library", "adm_lib:0")])
    await safe_edit(call, text, kb(*rows))


@dp.callback_query(F.data.startswith("lib:"))
async def cb_lib_router(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    a = call.data.split(":")
    action = a[1] if len(a) > 1 else ""
    try:
        rid = int(a[2]) if len(a) > 2 else 0
    except ValueError:
        return await call.answer("Invalid", show_alert=True)
    row = lib_get(rid)
    if not row:
        await call.answer("Ye emoji library me nahi hai", show_alert=True)
        return await _lib_show(call, 0)
    _id, eid, ch, name, pack, _added = row

    if action == "i":
        await state.clear()
        return await _lib_item(call, rid)

    if action == "g":
        page = int(a[3]) if len(a) > 3 and a[3].isdigit() else 0
        slots = list(DEFAULT_EMOJIS)
        pages = (len(slots) + LIB_SLOT_PAGE - 1) // LIB_SLOT_PAGE
        page = max(0, min(page, pages - 1))
        rows, pair = [], []
        for sl in slots[page * LIB_SLOT_PAGE:(page + 1) * LIB_SLOT_PAGE]:
            cur = get_setting(f"emoji_{sl}", "") or DEFAULT_EMOJIS.get(sl, "")
            mark = " ✓" if cur == eid else ""
            pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{mark}", f"lib:gs:{rid}:{sl}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", f"lib:g:{rid}:{page - 1}"))
        if page < pages - 1:
            nav.append(btn("Next ➡️", f"lib:g:{rid}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([btn("🔙 Back", f"lib:i:{rid}")])
        return await safe_edit(call, f"📍 <b>Global Slot</b> {_tg_emoji(eid, ch)}\n\nSlot chuno (page {page + 1}/{pages}).", kb(*rows))

    if action == "gs":
        slot = a[3] if len(a) > 3 else ""
        if slot not in DEFAULT_EMOJIS:
            return await call.answer("Unknown slot", show_alert=True)
        set_setting(f"emoji_{slot}", eid)
        await call.answer(f"✅ {slot} set ho gaya")
        return await _lib_item(call, rid)

    if action == "s":
        rows, pair = [], []
        for key, (label, _h) in UI_EDITABLE.items():
            pair.append(btn(label, f"lib:ss:{rid}:{key}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([btn("🔙 Back", f"lib:i:{rid}")])
        return await safe_edit(call, f"🖥 <b>Screen chuno</b> {_tg_emoji(eid, ch)}", kb(*rows))

    if action == "ss":
        key = a[3] if len(a) > 3 else ""
        if key not in UI_EDITABLE:
            return await call.answer("Unknown screen", show_alert=True)
        rows, pair = [], []
        for sl in _ui_slots_in(key):
            mark = " ✓" if get_setting(f"uiemoji_{key}_{sl}", "") == eid else ""
            pair.append(btn(f"{EMOJI_FALLBACK[sl]} {sl}{mark}", f"lib:sv:{rid}:{key}:{sl}"))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([btn("🔙 Back", f"lib:s:{rid}")])
        return await safe_edit(call, f"🖥 <b>{esc(UI_EDITABLE[key][0])}</b> {_tg_emoji(eid, ch)}\n\nKaun sa slot badalna hai?", kb(*rows))

    if action == "sv":
        key, slot = (a[3], a[4]) if len(a) > 4 else ("", "")
        if key not in UI_EDITABLE or slot not in DEFAULT_EMOJIS:
            return await call.answer("Unknown", show_alert=True)
        set_setting(f"uiemoji_{key}_{slot}", eid)
        await call.answer(f"✅ {UI_EDITABLE[key][0]} › {slot}")
        return await _lib_item(call, rid)

    if action == "m":
        if not lib_map_char(ch, eid, overwrite=True):
            return await call.answer("Normal rup pata nahi — 'Kisi aur emoji se Map' use karo", show_alert=True)
        await call.answer(f"✅ {ch} → premium (har jagah)")
        return await _lib_item(call, rid)

    if action == "mo":
        await state.update_data(lib_rid=rid)
        await state.set_state(AdminStates.wait_lib_char)
        return await safe_edit(call, f"🗺 {_tg_emoji(eid, ch)}\n\nJis normal emoji ki jagah ye dikhana hai wo bhejo (🔥 🚀 …).",
                               kb([btn("🔙 Back", f"lib:i:{rid}")]))

    if action == "rn":
        await state.update_data(lib_rid=rid)
        await state.set_state(AdminStates.wait_lib_name)
        return await safe_edit(call, f"✏️ {_tg_emoji(eid, ch)}\n\nNaya naam bhejo (max 30 characters).", kb([btn("🔙 Back", f"lib:i:{rid}")]))

    if action == "d":
        db_query("DELETE FROM premium_emojis WHERE id=?", (rid,))
        await call.answer("🗑 Library se hata diya")
        return await _lib_show(call, 0)

    await call.answer("Unknown action", show_alert=True)


@dp.message(AdminStates.wait_lib_char)
async def admin_lib_char(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
    rid = (await state.get_data()).get("lib_rid")
    row = lib_get(int(rid)) if rid else None
    if not row:
        await state.clear()
        return await msg.answer("❌ Emoji nahi mila.", reply_markup=_lib_back_kb())
    eid = row[1]
    chars = []
    for token in re.split(r"\s+", (msg.text or "").strip()):
        t = token.replace("\ufe0f", "")
        if t and not t.isascii() and len(t) <= 16 and t not in chars:
            chars.append(t)
    if not chars:
        return await msg.answer("❌ Ek normal emoji bhejo (jaise 🔥).")
    ok = [c for c in chars if lib_map_char(c, eid, overwrite=True)]
    await state.clear()
    await msg.answer("✅ Map ho gaya: " + " ".join(ok) + f" → {_tg_emoji(eid, row[2])}",
                     reply_markup=kb([btn("🔙 Is emoji par", f"lib:i:{rid}")], [btn("🧰 Library", "adm_lib:0")]))


@dp.message(AdminStates.wait_lib_name)
async def admin_lib_name(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID:
        return
   txt = msg.text or ""
    raw = txt.encode("utf-16-le")
    found: Dict[str, str] = {}
    for e in (msg.entities or []):
        if e.type == "custom_emoji" and e.custom_emoji_id:
            ch = raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le", errors="ignore")
            found.setdefault(e.custom_emoji_id, ch)
    if not found:
        return
    new = 0
    for eid, ch in found.items():
        if lib_add(eid, ch):
            new += 1
        if lib_automap_on():
            lib_map_char(ch, eid)
    lines = "\n".join(f"{_tg_emoji(i, c)} <code>{i}</code>" for i, c in found.items())
    await msg.answer(f"🎨 <b>Premium emoji ID(s):</b>\n{lines}\n\n🧰 Library me save: <b>{new}</b> naye (total {lib_count()}).",
                     reply_markup=kb([btn("🧰 Library kholo", "adm_lib:0")]))


# ==============================================================================
# 🚀 STARTUP / SHUTDOWN
# ==============================================================================
async def restore_bots():
    rows = db_query("SELECT id, user_id FROM hosted_bots WHERE status='Running' AND approval='approved'", fetchall=True) or []
    for bid, owner in rows:
        inst = get_instance(bid)
        if not inst:
            continue
        if owner != ADMIN_ID:
            miss = await missing_channels(owner, use_cache=False)
            if miss:
                set_status(bid, "Stopped")
                await notify_left_channels(owner, miss, 1)
                continue
        await inst.start()
        logger.info("♻️ Restored bot %s", bid)


async def on_startup():
    global _BOT_USERNAME
    init_db()
    harden_data_dirs()
    try:
        os.chmod(HOST_DIR, 0o700)
    except OSError:
        pass
    try:
        me = await bot.me()
        _BOT_USERNAME = me.username or ""
    except Exception as e:
        logger.warning("Bot username load nahi hua: %s", e)
    logger.info("🚀 %s v%s started!", BRAND, VERSION)
    if os.path.exists(PREMIUM2_FILE):
        async def _p2_boot():
            try:
                t, n, m = await asyncio.to_thread(load_premium2, PREMIUM2_FILE, False, False)
                logger.info("premium2.py loaded: total=%s new=%s mapped=%s", t, n, m)
            except Exception as e:
                logger.warning("premium2.py load fail: %s", e)
        _BG_TASKS.add(asyncio.create_task(_p2_boot()))
    for coro in (restore_bots(), channel_watcher(), vps_watchdog(), emoji_backup_watcher()):
        t = asyncio.create_task(coro)
        _BG_TASKS.add(t)
        t.add_done_callback(_BG_TASKS.discard)


async def on_shutdown():
    for inst in ACTIVE_BOTS.values():
        inst.kill_silently()
    logger.info("🛑 Hosting bot shutting down.")


async def polling_forever(stop: asyncio.Event):
    while not stop.is_set():
        try:
            await dp.start_polling(bot, handle_signals=False, close_bot_session=False)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Polling crash: %s, 5s me restart", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass


async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    poller = asyncio.create_task(polling_forever(stop))
    try:
        await stop.wait()
    finally:
        try:
            await dp.stop_polling()
        except Exception:
            pass
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)
        await on_shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
