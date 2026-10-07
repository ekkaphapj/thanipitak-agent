#!/usr/bin/env python3
"""Thai image-generation UI + local AI webchat backed by ComfyUI and Ollama.

Stdlib only. Binds to 127.0.0.1:8190 — reach it through the Cloudflare tunnel.
Draw jobs: POST /api/generate -> poll GET /api/status/<id> -> GET /api/image/<id>.
Chat: POST /api/chat streams NDJSON deltas while Ollama generates.
Both async patterns keep HTTP responses short so Cloudflare never times out.
"""
import base64
import binascii
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMFY = os.environ.get("COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_HOME = Path(os.environ.get("COMFY_HOME", str(Path.home() / "ComfyUI")))
OUTPUT_DIR = Path(os.environ.get("COMFY_OUTPUT_DIR", str(COMFY_HOME / "output")))
INPUT_DIR = Path(os.environ.get("COMFY_INPUT_DIR", str(COMFY_HOME / "input")))
MODEL = "qwen-image-2.1-UC-Q4_K_M.gguf"
TEXT_ENCODER = "qwen3vl_8b_int8_convrot.safetensors"
VAE = "qwen_image_2.1_vae_bf16.safetensors"
FLUX_MODEL = "flux-2-klein-4b-fp8.safetensors"
FLUX_TEXT_ENCODER = os.environ.get(
    "FLUX_TEXT_ENCODER", "qwen_3_4b.safetensors")
FLUX_VAE = "flux2-vae.safetensors"
MODEL_ROOT = Path(os.environ.get("COMFY_MODEL_ROOT", str(COMFY_HOME / "models")))
HOST = os.environ.get("DRAW_HOST", "127.0.0.1")
PORT = int(os.environ.get("DRAW_PORT", "8190"))
# Personal accounts replace the old shared password. Registration lands in a
# pending state; an admin opens /approve, enters APPROVE_CODE and approves.
APPROVE_CODE = os.environ.get("APPROVE_CODE") \
    or os.environ.get("DRAW_PASSWORD") or "thanipitak1"
SESSION_TTL = 30 * 24 * 3600  # seconds before the browser must log in again
# Optional OpenAI-compatible chat endpoint (Ollama/LM Studio/vLLM) that turns
# Thai prompts into detailed English ones. Empty = prompts pass through as-is.
ENRICHER_URL = os.environ.get("ENRICHER_URL", "").rstrip("/")
ENRICHER_MODEL = os.environ.get("ENRICHER_MODEL", "")
ENRICHER_TIMEOUT = float(os.environ.get("ENRICHER_TIMEOUT", "60"))
THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
# Local Ollama powering the webchat tab; models must already be pulled.
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
CHAT_MAX_BODY = 40 * 1024 * 1024
CHAT_MAX_FILE = 10 * 1024 * 1024
CHAT_MAX_IMAGES = 4
CHAT_MAX_MODEL_IMAGES = 8  # user images plus rendered PDF pages
CHAT_MAX_DOCS = 4
CHAT_TIMEOUT = 600
CHAT_HEARTBEAT = 15  # seconds between keepalive lines while Ollama is silent
CHAT_KEEP_ALIVE = os.environ.get("CHAT_KEEP_ALIVE", "30m")
CHAT_NUM_CTX = int(os.environ.get("CHAT_NUM_CTX", "32768"))  # default 4k cuts long code
# Per-model ctx overrides as a json map, e.g.
#   CHAT_NUM_CTX_OVERRIDES='{"ministral3-14b-heresy": 16384}'
# A 14B model at 32k ctx spills its KV cache onto the CPU on a 12GB card
# and generates ~3x slower; a smaller ctx keeps it fully in VRAM.
def _parse_ctx_overrides(raw):
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"CHAT_NUM_CTX_OVERRIDES is not valid json: {e}")
    if not isinstance(data, dict):
        raise SystemExit("CHAT_NUM_CTX_OVERRIDES must be a json object mapping model id to integer")
    parsed = {}
    for model_id, ctx in data.items():
        if not isinstance(model_id, str) or not model_id:
            raise SystemExit("CHAT_NUM_CTX_OVERRIDES keys must be non-empty model ids")
        if isinstance(ctx, bool) or not isinstance(ctx, int) or ctx <= 0:
            raise SystemExit(f"CHAT_NUM_CTX_OVERRIDES[{model_id!r}] must be a positive integer")
        parsed[model_id] = ctx
    return parsed


CHAT_NUM_CTX_OVERRIDES = _parse_ctx_overrides(
    os.environ.get("CHAT_NUM_CTX_OVERRIDES", ""))
WARMUP_TIMEOUT = 300
PDF_PAGES_MAX = int(os.environ.get("CHAT_PDF_PAGES", "6"))
SHEET_ROWS_MAX = int(os.environ.get("CHAT_SHEET_ROWS", "400"))
DEFAULT_MODEL_ID = "qwen-image-2.1"
FLUX_MODEL_ID = "flux.2-klein-4b"
RESOLUTIONS = [512, 640, 768, 896, 1024]
DEFAULT_NEGATIVE = ""
JOB_TIMEOUT = 900  # seconds
# YuE2 text-to-song runs through the same ComfyUI instance as image jobs.
# The int8 checkpoint fits a 12GB card; a full song is much slower than an
# image, so music jobs get their own, larger timeout.
YUE2_CHECKPOINT = os.environ.get("YUE2_CHECKPOINT", "yue2_3b_int8_convrot.safetensors")
MUSIC_JOB_TIMEOUT = 3600  # seconds
MUSIC_MAX_SECONDS = 240
# One GPU, many users: each lane caps how many jobs run at once and the rest
# wait in a FIFO with live queue positions. Chat (Ollama) and ComfyUI jobs
# queue separately so simultaneous users never thrash the 12GB card.
QUEUE_MAX_COMFY = max(1, int(os.environ.get("QUEUE_MAX_COMFY", "1")))
QUEUE_MAX_CHAT = max(1, int(os.environ.get("QUEUE_MAX_CHAT", "1")))
QUEUE_WAIT_TIMEOUT = float(os.environ.get("QUEUE_WAIT_TIMEOUT", "900"))
QUEUE_EVENT_INTERVAL = float(os.environ.get("QUEUE_EVENT_INTERVAL", "3"))
# Image/music jobs unload the chat model before sampling; give an in-flight
# answer this many seconds to finish first so it is not cut off mid-stream.
CHAT_GRACE_SECONDS = float(os.environ.get("CHAT_GRACE_SECONDS", "90"))
MAX_BODY = 12 * 1024 * 1024  # allows a base64 reference image
MAX_REFERENCE = 10 * 1024 * 1024
# Qwen-Image-2.1 is distilled. The ComfyUI template samples euler/simple at
# CFG 1.0 for 25 steps; CFG above 1 blurs the picture and costs a second UNET
# pass. Profiles only change the step count. At CFG 1 the sampler ignores
# negative conditioning, so exclusions are appended to the positive prompt.
PROFILES = {
    "fast": {"steps": 12, "cfg": 1.0},
    "medium": {"steps": 20, "cfg": 1.0},
    "quality": {"steps": 25, "cfg": 1.0},
}
MODELS = {
    DEFAULT_MODEL_ID: {
        "label": "Qwen-Image-2.1 · GGUF Q4",
        "files": (("diffusion_models", MODEL), ("text_encoders", TEXT_ENCODER), ("vae", VAE)),
        "profiles": tuple(PROFILES),
        "default_profile": "medium",
        "supports_reference": True,
        "supports_negative": True,
    },
    FLUX_MODEL_ID: {
        "label": "FLUX.2 [klein] 4B · FP8",
        "files": (("diffusion_models", FLUX_MODEL), ("text_encoders", FLUX_TEXT_ENCODER), ("vae", FLUX_VAE)),
        "profiles": ("standard",),
        "default_profile": "standard",
        "supports_reference": False,
        "supports_negative": False,
    },
}

JOBS = {}
LOCK = threading.Lock()
SESSIONS = {}  # bearer token -> {exp, username, admin}; restart logs everyone out
SESSION_LOCK = threading.Lock()
HERE = Path(__file__).resolve().parent
PROMPT_CACHE = {}
PROMPT_CACHE_MAX = 256
CHAT_CAPABILITY_CACHE = {}

# ---- accounts & per-user library (SQLite + one folder per user) -------------
# Everything a user produces is kept forever in DATA_DIR: chat text in the
# database, images/music as files under users/<username>/. The 50MB quota
# counts both, and the web UI shows what is left.
DATA_DIR = Path(os.environ.get("THANIPITAK_DATA_DIR", str(HERE / "user_data")))
DB_PATH = DATA_DIR / "users.db"
USERS_DIR = DATA_DIR / "users"
USER_QUOTA = max(1, int(os.environ.get("USER_QUOTA_MB", "50"))) * 1024 * 1024
# Reentrant: helpers like save_chat_exchange hold it across db() calls,
# and db() itself re-acquires it inside init_db().
DB_LOCK = threading.RLock()
DB_READY = False
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fullname TEXT NOT NULL,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id);
CREATE TABLE IF NOT EXISTS media(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  rel_path TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_user ON media(user_id, kind);
"""


def init_db():
    global DB_READY
    with DB_LOCK:
        if DB_READY:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()
        DB_READY = True


@contextmanager
def db():
    init_db()
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return salt.hex() + "$" + digest.hex()


def verify_password(password, stored):
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return hmac.compare_digest(actual, expected)


class PendingAccount(Exception):
    """Raised at login when the account exists but is not approved yet."""


def register_user(fullname, username, password):
    """Create a pending account; raise ValueError with a Thai message."""
    fullname = (fullname or "").strip()
    username = (username or "").strip().lower()
    password = password or ""
    if not fullname:
        raise ValueError("กรุณากรอกชื่อ-นามสกุล")
    if len(fullname) > 120:
        raise ValueError("ชื่อ-นามสกุลยาวเกิน 120 ตัวอักษร")
    if not username:
        raise ValueError("กรุณากรอก username")
    if not USERNAME_RE.match(username):
        raise ValueError("username ต้องเป็น a-z, 0-9, จุด, ขีด หรือ ขีดล่าง ความยาว 3-32 ตัวอักษร")
    if not password:
        raise ValueError("กรุณากรอกรหัสผ่าน")
    if len(password) > 128:
        raise ValueError("รหัสผ่านยาวเกิน 128 ตัวอักษร")
    with DB_LOCK, db() as conn:
        try:
            conn.execute(
                "INSERT INTO users(fullname, username, password_hash, status, created_at)"
                " VALUES(?,?,?,?,?)",
                (fullname, username, hash_password(password), "pending", time.time()))
        except sqlite3.IntegrityError:
            raise ValueError("username นี้ถูกใช้ไปแล้ว กรุณาเลือกใหม่")
    return username


def login_user(username, password):
    """Return the user row for valid, approved credentials."""
    username = (username or "").strip().lower()
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not isinstance(row, sqlite3.Row) or not verify_password(password or "", row["password_hash"]):
        time.sleep(0.8)  # blunt credential guessing through the tunnel
        raise ValueError("username หรือรหัสผ่านไม่ถูกต้อง")
    if row["status"] != "approved":
        raise PendingAccount()
    return row


def get_user_by_username(username):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def pending_users():
    with db() as conn:
        return conn.execute(
            "SELECT id, fullname, username, created_at FROM users WHERE status='pending'"
            " ORDER BY created_at").fetchall()


def approve_user(username, approved=True):
    with DB_LOCK, db() as conn:
        if approved:
            cur = conn.execute("UPDATE users SET status='approved' WHERE username=?",
                               (username,))
        else:
            cur = conn.execute("DELETE FROM users WHERE username=? AND status='pending'",
                               (username,))
    return cur.rowcount > 0


def create_session(username=None, admin=False):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with SESSION_LOCK:
        for stale, sess in list(SESSIONS.items()):
            if sess["exp"] <= now:
                SESSIONS.pop(stale, None)
        SESSIONS[token] = {"exp": now + SESSION_TTL, "username": username,
                           "admin": admin}
    return token


def session_info(token):
    with SESSION_LOCK:
        sess = SESSIONS.get(token or "")
    if not sess or sess["exp"] <= time.time():
        return None
    return dict(sess)


def session_valid(token):
    return session_info(token) is not None


def used_bytes(user_id):
    """Storage the user's library takes: media files plus chat text."""
    with db() as conn:
        row = conn.execute(
            "SELECT (SELECT COALESCE(SUM(bytes),0) FROM media WHERE user_id=?)"
            " + (SELECT COALESCE(SUM(LENGTH(CAST(content AS BLOB))),0)"
            "    FROM chat_messages WHERE user_id=?) AS used", (user_id, user_id)).fetchone()
    return int(row["used"])


def storage_summary(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='image' THEN bytes END),0) AS images,"
            " COALESCE(SUM(CASE WHEN kind='music' THEN bytes END),0) AS music,"
            " COUNT(CASE WHEN kind='image' THEN 1 END) AS image_count,"
            " COUNT(CASE WHEN kind='music' THEN 1 END) AS music_count"
            " FROM media WHERE user_id=?", (user_id,)).fetchone()
        chat = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(content AS BLOB))),0) AS bytes,"
            " COUNT(*) AS messages FROM chat_messages WHERE user_id=?",
            (user_id,)).fetchone()
    used = int(row["images"]) + int(row["music"]) + int(chat["bytes"])
    return {"quota": USER_QUOTA, "used": used, "remaining": max(0, USER_QUOTA - used),
            "images_bytes": int(row["images"]), "music_bytes": int(row["music"]),
            "chats_bytes": int(chat["bytes"]),
            "image_count": int(row["image_count"]), "music_count": int(row["music_count"]),
            "chat_messages": int(chat["messages"])}


def user_library(user_id):
    """Chat sessions plus saved images/music for the sidebar and manage tab."""
    with db() as conn:
        chats = conn.execute(
            "SELECT s.id, s.title, s.updated_at,"
            " (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS messages"
            " FROM chat_sessions s WHERE s.user_id=? ORDER BY s.updated_at DESC",
            (user_id,)).fetchall()
        media = conn.execute(
            "SELECT id, kind, filename, bytes, note, created_at FROM media"
            " WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return {
        "chats": [{"id": r["id"], "title": r["title"], "updated_at": r["updated_at"],
                   "messages": int(r["messages"])} for r in chats],
        "images": [_media_row(r) for r in media if r["kind"] == "image"],
        "music": [_media_row(r) for r in media if r["kind"] == "music"],
    }


def _media_row(row):
    return {"id": row["id"], "filename": row["filename"], "bytes": int(row["bytes"]),
            "note": row["note"], "created_at": row["created_at"],
            "url": "/api/media/" + row["id"] + "/file"}


def unique_media_name(conn, username, kind, ext):
    """username+picture-thanipitak.png / username+music-thanipitak.mp3, then -2, -3…"""
    base = f"{username}+{'picture' if kind == 'image' else 'music'}-thanipitak"
    name = base + ext
    n = 2
    while conn.execute("SELECT 1 FROM media WHERE filename=?", (name,)).fetchone() \
            or (USERS_DIR / username / ("images" if kind == "image" else "music") / name).exists():
        name = f"{base}-{n}{ext}"
        n += 1
    return name


def archive_job_output(user, kind, source_path, note):
    """Copy a finished ComfyUI output into the user's library under quota.

    Returns the media id, or None when there is no room left (the job still
    succeeds — the browser just warns the file is not in the library).
    """
    user_id, username = user
    try:
        size = source_path.stat().st_size
    except OSError:
        return None
    if used_bytes(user_id) + size > USER_QUOTA:
        return None
    ext = source_path.suffix.lower() or (".png" if kind == "image" else ".mp3")
    sub = "images" if kind == "image" else "music"
    dest_dir = USERS_DIR / username / sub
    dest_dir.mkdir(parents=True, exist_ok=True)
    media_id = uuid.uuid4().hex[:12]
    with DB_LOCK, db() as conn:
        filename = unique_media_name(conn, username, kind, ext)
        dest = dest_dir / filename
        try:
            shutil.copy2(source_path, dest)
        except OSError:
            return None
        conn.execute(
            "INSERT INTO media(id, user_id, kind, filename, rel_path, bytes, note, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (media_id, user_id, kind, filename,
             f"{username}/{sub}/{filename}", dest.stat().st_size,
             (note or "").strip()[:200], time.time()))
    return media_id


def delete_media(user_id, media_id=None, kind=None):
    """Remove one media row (or every row of a kind) plus its file."""
    with DB_LOCK, db() as conn:
        if media_id is not None:
            rows = conn.execute("SELECT * FROM media WHERE user_id=? AND id=?",
                                (user_id, media_id)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM media WHERE user_id=? AND kind=?",
                                (user_id, kind)).fetchall()
        for row in rows:
            conn.execute("DELETE FROM media WHERE id=?", (row["id"],))
    for row in rows:
        target = DATA_DIR / "users" / row["rel_path"]
        try:
            target.unlink()
        except OSError:
            pass
    return len(rows)


def chat_title(text):
    line = (text or "").strip().splitlines()[0].strip() if (text or "").strip() else ""
    return (line[:60] or "แชตใหม่")


def ensure_chat_session(user_id, session_id, title_seed):
    """Validate a session id from the browser or open a new one."""
    with DB_LOCK, db() as conn:
        if session_id:
            row = conn.execute("SELECT id FROM chat_sessions WHERE id=? AND user_id=?",
                               (session_id, user_id)).fetchone()
            if row:
                return session_id
        sid = uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO chat_sessions(id, user_id, title, created_at, updated_at)"
            " VALUES(?,?,?,?,?)", (sid, user_id, chat_title(title_seed), time.time(), time.time()))
    return sid


def save_chat_exchange(user_id, session_id, user_text, assistant_text):
    """Store one question+answer pair; skip silently when the quota is full."""
    needed = len(user_text.encode("utf-8")) + len(assistant_text.encode("utf-8"))
    with DB_LOCK, db() as conn:
        used = used_bytes(user_id)
        if used + needed > USER_QUOTA:
            return False
        now = time.time()
        conn.execute(
            "INSERT INTO chat_messages(session_id, user_id, role, content, created_at)"
            " VALUES(?,?,?,?,?), (?,?,?,?,?)",
            (session_id, user_id, "user", user_text, now,
             session_id, user_id, "assistant", assistant_text, now))
        conn.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id))
    return True


def chat_messages(user_id, session_id):
    with db() as conn:
        owner = conn.execute("SELECT id FROM chat_sessions WHERE id=? AND user_id=?",
                             (session_id, user_id)).fetchone()
        if not owner:
            return None
        rows = conn.execute(
            "SELECT role, content FROM chat_messages WHERE session_id=? ORDER BY id",
            (session_id,)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def delete_chat_session(user_id, session_id=None):
    with DB_LOCK, db() as conn:
        if session_id is not None:
            cur = conn.execute("DELETE FROM chat_sessions WHERE id=? AND user_id=?",
                               (session_id, user_id))
            conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        else:
            cur = conn.execute("DELETE FROM chat_sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM chat_messages WHERE user_id=?", (user_id,))
    return cur.rowcount



class QueueGate:
    """FIFO ticket gate that caps concurrent jobs and reports queue positions."""

    def __init__(self, name, max_active):
        self.name = name
        self.max_active = max_active
        self.cond = threading.Condition()
        self.active = 0
        self.waiting = []  # ticket numbers in arrival order
        self._next_ticket = 0

    def reserve(self):
        """Join the queue; pair with try_promote() then leave()/cancel()."""
        with self.cond:
            ticket = self._next_ticket
            self._next_ticket += 1
            self.waiting.append(ticket)
            return ticket

    def try_promote(self, ticket, timeout):
        """Grant the slot to the queue head within `timeout` seconds."""
        with self.cond:
            deadline = time.time() + max(0.0, timeout)
            while True:
                if self.waiting and self.waiting[0] == ticket \
                        and self.active < self.max_active:
                    self.waiting.pop(0)
                    self.active += 1
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self.cond.wait(min(remaining, 0.5))

    def leave(self, ticket):
        with self.cond:
            self.active = max(0, self.active - 1)
            self.cond.notify_all()

    def cancel(self, ticket):
        with self.cond:
            if ticket in self.waiting:
                self.waiting.remove(ticket)
            self.cond.notify_all()

    def position(self, ticket):
        """How many jobs run before this one; 0 once the slot is granted."""
        with self.cond:
            if ticket in self.waiting:
                return self.active + self.waiting.index(ticket)
            return 0

    def snapshot(self):
        with self.cond:
            return {"active": self.active, "waiting": len(self.waiting),
                    "max": self.max_active}


COMFY_GATE = QueueGate("comfy", QUEUE_MAX_COMFY)
CHAT_GATE = QueueGate("chat", QUEUE_MAX_CHAT)


def wait_chat_idle(timeout):
    """Wait for in-flight chats so their model is not yanked mid-answer."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if CHAT_GATE.snapshot()["active"] == 0:
            return True
        time.sleep(1.0)
    return CHAT_GATE.snapshot()["active"] == 0

ENHANCER_SYSTEM = (
    "You translate Thai image-generation prompts into English prompts for a "
    "text-to-image model. Keep the same subjects, counts, colors, positions and "
    "negations. Never add a subject that was not asked for. "
    "Keep any text inside double quotes exactly as written; that text must appear "
    "in the picture, so never translate or alter it. "
    "ตัวหนังสือ and ตัวอักษร mean rendered letters or text in the image, not books "
    "(หนังสือ). \"ไม่มีตัวหนังสือ\" means no rendered text or letters are visible. "
    "A line that starts with \"Avoid:\" lists things that must NOT appear. Keep the "
    "word \"Avoid:\" exactly, translate only that list, and never turn those items "
    "into things that are present. "
    "Reply with only the final prompt, no explanations.\n"
    "Examples:\n"
    "แมวสีส้มหนึ่งตัวนั่งอ่านหนังสือ ไม่มีตัวหนังสือในภาพ\n"
    "-> One orange cat sitting and reading a book. No rendered text or letters are visible.\n"
    "ป้ายเขียนว่า \"สวัสดี\"\n"
    "-> A sign that reads \"สวัสดี\".\n"
    "เสื้อสีแดงสามตัวบนโต๊ะไม้\n"
    "-> Three red shirts on a wooden table."
)
QUOTED_RE = re.compile(r'"([^"\n]+)"')
# The lyrics tab asks a local chat model for YuE2-ready lyrics: a [Style]
# line first (the browser pipes it into the song-form style box), then tagged
# sections only, no preamble, so the text can drop straight into the form.
LYRICS_SYSTEM = (
    "คุณเป็นนักแต่งเนื้อร้องมืออาชีพ ช่วยเขียนเนื้อร้องภาษาไทยสำหรับโมเดลสร้างเพลง YuE2 "
    "ตอบเป็นสองส่วนตามลำดับนี้เท่านั้น ห้ามอธิบายอื่นใด ห้ามใช้ markdown "
    "ส่วนแรก: บรรทัดแรกเขียนว่า [Style] แล้วบรรทัดถัดมาเขียนสไตล์เพลงภาษาอังกฤษบรรทัดเดียว "
    "ที่เหมาะกับเพลงนี้ที่สุด ระบุภาษา แนวเพลง เสียงร้อง จังหวะ BPM อารมณ์ และเครื่องดนตรี "
    "ตัวอย่างรูปแบบ: Thai, upbeat acoustic pop, warm female vocal, 96 BPM, heartfelt, "
    "acoustic guitar and soft piano "
    "ส่วนที่สอง: เนื้อร้องภาษาไทย จัดทุกท่อนด้วยแท็กวงเล็บเหลี่ยมบนบรรทัดของตัวเองในรูปแบบ "
    "[Verse 1], [Chorus], [Verse 2], [Bridge], [Outro] "
    "ต้องเริ่มด้วย [Verse 1] และมีท่อน [Chorus] อย่างน้อยหนึ่งท่อน "
    "แต่ละท่อนมี 4-8 บรรทัด รวมความยาวพอเหมาะกับเพลง 1-2 นาที "
    "เขียนให้ร้องได้จริง มีคำสัมผัส จำง่าย และเข้ากับสไตล์ที่เลือก"
)


def compose_prompt(prompt, negative):
    """Append exclusions where CFG 1 can see them: on the positive prompt."""
    negative = (negative or "").strip()
    if not negative:
        return prompt
    return prompt.rstrip() + "\nAvoid: " + negative


def _translation_ok(source, text):
    """Reject a rewrite that drops quoted text, the Avoid line, or lettering."""
    if not text or len(text) > 8000:
        return False
    for quoted in QUOTED_RE.findall(source):
        if quoted not in text:
            return False
    if "\nAvoid:" in "\n" + source and "Avoid:" not in text:
        return False
    if "ตัวหนังสือ" in source or "ตัวอักษร" in source:
        lowered = text.lower()
        if not any(word in lowered for word in (
                "letter", "letters", "text", "writing", "glyph",
                "inscription", "caption", "typography")):
            return False
    return True


def enhance_prompt(prompt):
    """Translate a Thai prompt into a detailed English one via the configured LLM.

    Returns the original prompt untouched when no enricher is configured, when the
    prompt contains no Thai, or when anything goes wrong — generation must never
    fail because of translation.
    """
    if not ENRICHER_URL or not THAI_RE.search(prompt):
        return prompt
    with LOCK:
        cached = PROMPT_CACHE.get(prompt)
    if cached is not None:
        return cached
    payload = {
        "messages": [
            {"role": "system", "content": ENHANCER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "think": False,  # ollama reasoning models: skip <think>, others ignore it
    }
    if ENRICHER_MODEL:
        payload["model"] = ENRICHER_MODEL
    try:
        req = urllib.request.Request(
            ENRICHER_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=ENRICHER_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        text = data["choices"][0]["message"]["content"]
        text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()  # qwen3 reasoning
        text = re.sub(r"^(prompt|translation|english)\s*:\s*", "", text, flags=re.I).strip()
        if not _translation_ok(prompt, text):
            text = prompt
    except Exception:
        return prompt
    with LOCK:
        if len(PROMPT_CACHE) >= PROMPT_CACHE_MAX:
            PROMPT_CACHE.clear()
        PROMPT_CACHE[prompt] = text
    return text


def comfy_post(path, payload):
    req = urllib.request.Request(
        COMFY + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def comfy_get(path):
    with urllib.request.urlopen(COMFY + path, timeout=30) as r:
        return json.loads(r.read().decode())


def comfy_jobs_ahead(prompt_id):
    """How many ComfyUI jobs finish before this one; None when unknown."""
    try:
        data = comfy_get("/queue")
    except Exception:
        return None

    def ids(entries):
        out = []
        for e in entries if isinstance(entries, list) else []:
            out.append(e[1] if isinstance(e, list) and len(e) > 1 else e)
        return out

    running = ids(data.get("queue_running"))
    pending = ids(data.get("queue_pending"))
    if prompt_id in pending:
        return len([i for i in running if i != prompt_id]) + pending.index(prompt_id)
    if prompt_id in running:
        return 0
    return None


def save_reference(data_url, job_id):
    """Persist an uploaded reference image (data URL) into ComfyUI's input dir.

    Returns the LoadImage value (path relative to the input root).
    """
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        raise ValueError("reference must be an image data URL")
    match = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,", data_url)
    if not match:
        raise ValueError("reference must be base64 png/jpeg/webp")
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
    except Exception:
        raise ValueError("reference base64 is malformed")
    if not raw:
        raise ValueError("reference image is empty")
    if len(raw) > MAX_REFERENCE:
        raise ValueError("reference image is too large (max 10MB)")
    if not (raw.startswith(b"\x89PNG") or raw.startswith(b"\xff\xd8") or raw[:4] == b"RIFF"):
        raise ValueError("unsupported image format")
    ref_dir = INPUT_DIR / "draw_refs"
    ref_dir.mkdir(parents=True, exist_ok=True)
    path = ref_dir / (job_id + ".png")
    path.write_bytes(raw)
    return "draw_refs/" + path.name


def available_models():
    return {
        model_id: all((MODEL_ROOT / folder / name).is_file()
                      and (MODEL_ROOT / folder / name).stat().st_size > 0
                      for folder, name in spec["files"])
        for model_id, spec in MODELS.items()
    }


# ---- local webchat (Ollama) ------------------------------------------------

def ollama_tags():
    with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=15) as r:
        return json.loads(r.read().decode()).get("models", [])


def chat_models():
    """Chat-capable Ollama models, flagging which can see images.

    /api/tags sometimes omits capabilities. Treating that as "no vision" hides
    vision models and lets embedding models through, so fall back to /api/show.
    """
    models = []
    for m in ollama_tags():
        model_id = m["model"]
        caps = m.get("capabilities") or []
        if not caps:
            key = (OLLAMA, model_id, m.get("digest"), m.get("modified_at"))
            with LOCK:
                cached = CHAT_CAPABILITY_CACHE.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                caps = cached[1]
            else:
                try:
                    req = urllib.request.Request(
                        OLLAMA + "/api/show",
                        data=json.dumps({"model": model_id}).encode(),
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        caps = json.loads(r.read().decode()).get("capabilities") or []
                    with LOCK:
                        if len(CHAT_CAPABILITY_CACHE) >= 256:
                            CHAT_CAPABILITY_CACHE.clear()
                        CHAT_CAPABILITY_CACHE[key] = (time.monotonic(), caps)
                except Exception:
                    caps = []  # allow text chat when capability discovery fails
        if caps and "completion" not in caps:  # embeddings etc.
            continue
        models.append({"id": model_id, "label": model_id,
                       "vision": "vision" in caps})
    return {"models": models}


def _xml_text(xml):
    # Strip tags first so an escaped "<" does not become a tag, then unescape.
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


def extract_docx(data):
    """Body text from a Word file (it is a zip of XML)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return _xml_text(xml).strip()


def extract_xlsx(data):
    """Readable table text from an Excel workbook: one block per sheet.

    Reads cell grids via the sheet XML (values, numbers, booleans, inline and
    shared strings) rather than only the shared-strings pool, so models see
    the actual table layout. Capped at SHEET_ROWS_MAX rows per sheet. A file
    with no workbook map still yields any worksheet XML it does contain.
    Returns "" when nothing usable is present.
    """
    def local(e):
        return e.tag.rsplit("}", 1)[-1]

    def col_index(ref):
        letters = re.match(r"([A-Z]+)", ref or "")
        n = 0
        if not letters:
            return None
        for ch in letters.group(1):
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    def cell_text(c, shared):
        t = c.get("t")
        if t == "inlineStr":
            return "".join(x.text or "" for x in c.iter() if local(x) == "t")
        v = next((x for x in c if local(x) == "v"), None)
        if v is None or v.text is None:
            return ""
        raw = v.text
        if t == "s":
            try:
                return shared[int(raw)]
            except (IndexError, ValueError):
                return raw
        if t == "b":
            return "TRUE" if raw.strip() == "1" else "FALSE"
        if t in ("str", "e"):
            return raw
        try:  # plain number: keep ints tidy
            f = float(raw)
            return str(int(f)) if f.is_integer() else str(f)
        except ValueError:
            return raw

    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(x.text or "" for x in si.iter()
                                      if local(x) == "t"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rid_target = {rel.get("Id"): rel.get("Target") for rel in rels}
            sheets = [(s.get("name", "Sheet"),
                       rid_target.get(s.get("{%s}id" % ns["r"], "")))
                      for s in wb.findall(".//m:sheet", ns)]
        except (KeyError, ET.ParseError):
            sheets = [(name.rsplit("/", 1)[-1], name) for name in names
                      if name.startswith("xl/worksheets/") and name.endswith(".xml")]
        for sheet_name, target in sheets:
            if not target:
                continue
            path = target.lstrip("/")
            if not path.startswith("xl/"):
                path = "xl/" + path
            if path not in names:
                continue
            grid = []
            width = 0
            truncated = False
            for row in ET.fromstring(z.read(path)).iter():
                if local(row) != "row":
                    continue
                cells = []
                nxt = 0
                for c in row:
                    if local(c) != "c":
                        continue
                    idx = col_index(c.get("r"))
                    if idx is None:
                        idx = nxt
                    nxt = idx + 1
                    cells.append((idx, cell_text(c, shared)))
                cells.sort(key=lambda p: p[0])
                # The row that fills the cap is kept; only a further data row
                # means the sheet was actually cut.
                if len(grid) >= SHEET_ROWS_MAX:
                    if cells:
                        truncated = True
                        break
                    continue
                if cells:
                    width = max(width, cells[-1][0] + 1)
                    grid.append(cells)
            if not grid:
                continue
            block = [f"=== ชีต: {sheet_name} ==="]
            for cells in grid:
                line = [""] * width
                for idx, text in cells:
                    if 0 <= idx < width:
                        line[idx] = text.replace("\t", " ").replace("\n", " ")
                block.append("\t".join(line).rstrip())
            if truncated:
                block.append(f"… (แสดง {SHEET_ROWS_MAX} แถวแรก)")
            out.append("\n".join(block))
    if out:
        return "\n\n".join(out)
    return "\n".join(s for s in shared if s)


def extract_pdf(data):
    """Best-effort stdlib PDF text: inflate streams, read text operators.

    Embedded/custom encodings (very common for Thai) can defeat this; the
    caller tells the user when nothing readable came out.
    """
    out = []
    scanned = []

    def scan(raw):
        for op in re.finditer(rb"\((?:\\.|[^\\()])*\)\s*Tj", raw):
            s = op.group(0)[1:op.group(0).rindex(b")")]
            s = s.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
            out.append(s.decode("latin-1", "replace"))

    for m in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = m.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        scanned.append(raw)
        scan(raw)
    if not out:  # uncompressed PDFs keep text operators outside streams
        scan(data)
    return " ".join(out).strip()


def _rescue_mojibake(text):
    """Fix Thai text whose UTF-8 bytes were decoded with a Thai codepage.

    PDFs whose embedded fonts lack proper ToUnicode maps extract as junk like
    'เธฃเธฒเธข'. U+0E00 never occurs in real Thai, so when it shows up we try
    re-encoding through ISO-8859-11 (TIS-620) back to UTF-8.
    """
    if text.count("\u0e00") < 3:
        return text
    try:
        fixed = text.encode("iso-8859-11", "ignore").decode("utf-8", "ignore")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    if fixed.count("\u0e00") < text.count("\u0e00") and len(fixed) > 0.5 * len(text):
        return fixed
    return text


def pdf_page_images(data, max_pages):
    """Render PDF pages to JPEG base64 via poppler's pdftoppm; [] when absent."""
    if not shutil.which("pdftoppm"):
        return []
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "doc.pdf"
        src.write_bytes(data)
        try:
            subprocess.run(
                ["pdftoppm", "-jpeg", "-r", "110", "-l", str(max_pages),
                 str(src), str(Path(td) / "p")],
                check=True, timeout=120, capture_output=True)
        except (subprocess.SubprocessError, OSError):
            return []
        pages = []
        for p in sorted(Path(td).glob("p-*.jpg")):
            raw = p.read_bytes()
            if raw and len(raw) <= CHAT_MAX_FILE:
                pages.append(base64.b64encode(raw).decode())
        return pages


def pdf_text_via_poppler(data):
    """pdftotext extraction (handles proper text PDFs incl. Thai) -> str."""
    if not shutil.which("pdftotext"):
        return ""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "doc.pdf"
        src.write_bytes(data)
        try:
            subprocess.run(
                ["pdftotext", "-layout", str(src), str(Path(td) / "out.txt")],
                check=True, timeout=120, capture_output=True)
            return _rescue_mojibake(
                (Path(td) / "out.txt").read_text(encoding="utf-8", errors="replace"))
        except (subprocess.SubprocessError, OSError):
            return ""


def preprocess_pdf_docs(body, vision):
    """Give PDFs a real reader before generic extraction.

    With a vision model, render pages to images (charts/scans have no usable
    text layer). Otherwise extract text with poppler; whatever succeeds is
    attached as pre-extracted text. Returns extra user-facing notes.
    """
    last = body["messages"][-1]
    docs = last.get("docs") or []
    if not docs:
        return []
    images = last.get("images") or []
    last["images"] = images
    notes = []
    for doc in list(docs):
        name = str(doc.get("name", "file"))
        if not name.lower().endswith(".pdf"):
            continue
        if isinstance(doc.get("text"), str):
            continue
        try:
            data = base64.b64decode(doc.get("data") or "", validate=False)
        except (binascii.Error, ValueError):
            continue  # build_chat_messages will report the broken file
        if len(data) > CHAT_MAX_FILE:
            continue  # reject before spawning poppler; report during extraction
        room = max(0, CHAT_MAX_MODEL_IMAGES - len(images))
        if vision and room:
            pages = pdf_page_images(data, PDF_PAGES_MAX)
            if pages:
                take = pages[:room]
                if take:
                    images.extend(take)
                    docs.remove(doc)
                    notes.append(f"{name}: แปลงเป็นภาพ {len(take)} หน้า "
                                 f"ให้โมเดลอ่านจากรูปโดยตรง")
                    if len(take) < len(pages):
                        notes.append(f"{name}: ไม่ได้ส่งอีก {len(pages) - len(take)} หน้า "
                                     f"เพราะส่งภาพรวมได้สูงสุด {CHAT_MAX_MODEL_IMAGES} รูป")
                    if len(pages) >= PDF_PAGES_MAX:
                        notes.append(f"{name}: อ่านเป็นภาพสูงสุด {PDF_PAGES_MAX} หน้าแรก")
                    continue
        elif vision:
            notes.append(f"{name}: พื้นที่รูปเต็มแล้ว ลองอ่าน PDF เป็นข้อความแทน")
        text = pdf_text_via_poppler(data)
        if len(text.strip()) >= 20:
            doc.pop("data", None)
            doc["text"] = text[:60000]
    return notes


def extract_attachment(name, data):
    """-> (text, error message). Exactly one is None."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    try:
        if ext in ("txt", "md", "csv", "json", "py", "log", "xml", "html"):
            return data.decode("utf-8", "replace"), None
        if ext in ("docx",):
            return extract_docx(data), None
        if ext in ("xlsx", "xlsm"):
            text = extract_xlsx(data)
            if not text.strip():
                return None, "ไม่พบตารางที่อ่านได้ในไฟล์ Excel นี้"
            return text, None
        if ext in ("pdf",):
            text = extract_pdf(data)
            if len(text) < 20:
                return None, "อ่านข้อความจาก PDF นี้ไม่ได้ (ฟอนต์ฝังแบบพิเศษ) ลองคัดลอกข้อความมาแนบเป็น .txt แทน"
            return text, None
    except Exception:
        return None, "เปิดไฟล์ไม่สำเร็จ"
    return None, "ชนิดไฟล์นี้ยังไม่รองรับ"


def release_ollama_vram():
    """Unload chat models so ComfyUI gets the whole GPU.

    An empty prompt with keep_alive 0 is Ollama's unload request. It does not
    load a model that is not already resident. Failure must not fail the image.
    """
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=5) as r:
            models = json.loads(r.read().decode()).get("models", [])
    except Exception:
        return
    names = []
    for model in models:
        name = model.get("name") or model.get("model")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    for name in names:
        payload = {"model": name, "prompt": "", "keep_alive": 0, "stream": False}
        req = urllib.request.Request(
            OLLAMA + "/api/generate", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
        except Exception:
            continue


def free_comfy_vram():
    """Unload ComfyUI's models so Ollama can own the whole GPU.

    Never touches anything while a generation is executing. Returns True when
    ComfyUI was asked to free memory.
    """
    try:
        queue = comfy_get("/queue")
        if queue.get("queue_running"):
            return False
        comfy_post("/free", {"unload_models": True, "free_memory": True})
        return True
    except Exception:
        return False


def _canonical_model_id(model_id):
    """Ollama treats a bare name as the implicit :latest tag."""
    return model_id if ":" in model_id else f"{model_id}:latest"


def chat_num_ctx(model_id):
    """Context for a chat request, honoring per-model overrides.

    The lookup canonicalizes both the requested id and every override key
    to Ollama's full name form, so "mini" and "mini:latest" resolve to the
    same override while a different tag ("mini:1") stays a different model.
    """
    canonical = _canonical_model_id(model_id)
    if canonical in CHAT_NUM_CTX_OVERRIDES:
        return CHAT_NUM_CTX_OVERRIDES[canonical]
    for key, ctx in CHAT_NUM_CTX_OVERRIDES.items():
        if _canonical_model_id(key) == canonical:
            return ctx
    return CHAT_NUM_CTX


def chat_model_ready(model_id):
    """True when the model sits in VRAM right now (Ollama /api/ps)."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10) as r:
            models = json.loads(r.read().decode()).get("models", [])
        for m in models:
            if model_id in (m.get("name"), m.get("model")):
                total = m.get("size") or 1
                return (m.get("context_length") == chat_num_ctx(model_id)
                        and (m.get("size_vram") or 0) >= 0.5 * total)
    except Exception:
        pass
    return False


def warm_chat_model(model_id):
    """Load a chat model into VRAM without generating anything."""
    payload = {"model": model_id, "keep_alive": CHAT_KEEP_ALIVE,
               # must match the chat runner options or Ollama treats it as a
               # different instance and reloads on every message
               "options": {"num_ctx": chat_num_ctx(model_id)}}
    req = urllib.request.Request(
        OLLAMA + "/api/generate", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=WARMUP_TIMEOUT) as r:
        for line in r.read().splitlines():
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" in chunk:
                raise RuntimeError(str(chunk["error"]))


def validate_chat_payload(payload, image_limit=CHAT_MAX_IMAGES):
    """Check request structure before any file conversion or extraction."""
    if not isinstance(payload, dict):
        raise ValueError("invalid chat request")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 80:
        raise ValueError("invalid message history")
    for m in messages[:-1]:
        if (not isinstance(m, dict) or m.get("role") not in ("user", "assistant")
                or not isinstance(m.get("content"), str)):
            raise ValueError("invalid message history")
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user" \
            or not isinstance(last.get("content"), str):
        raise ValueError("invalid message history")
    images = last.get("images")
    docs = last.get("docs")
    images = [] if images is None else images
    docs = [] if docs is None else docs
    if not isinstance(images, list) or len(images) > image_limit:
        raise ValueError(f"too many images (max {image_limit})")
    if not isinstance(docs, list) or len(docs) > CHAT_MAX_DOCS:
        raise ValueError(f"too many documents (max {CHAT_MAX_DOCS})")
    if any(not isinstance(img, str) for img in images):
        raise ValueError("invalid image")
    for doc in docs:
        if not isinstance(doc, dict) or not (
                isinstance(doc.get("data"), str) or isinstance(doc.get("text"), str)):
            raise ValueError("invalid attachment")
    if not last["content"].strip() and not docs and not images:
        raise ValueError("message is empty")
    return messages


def build_chat_messages(payload, image_limit=CHAT_MAX_IMAGES):
    """Extract attachments after validation and inline document context."""
    messages = validate_chat_payload(payload, image_limit)
    out = [{"role": m["role"], "content": m["content"]} for m in messages[:-1]]
    last = messages[-1]
    content = last["content"].strip()
    images = last.get("images") or []
    docs = last.get("docs") or []
    blocks, errors = [], []
    for doc in docs:
        if not isinstance(doc, dict):
            raise ValueError("invalid attachment")
        if isinstance(doc.get("text"), str):  # pre-extracted (poppler path)
            blocks.append(f"ไฟล์แนบ {doc.get('name')}:\n{doc['text'][:60000]}")
            continue
        if not isinstance(doc.get("data"), str):
            raise ValueError("invalid attachment")
        try:
            data = base64.b64decode(doc["data"], validate=False)
        except (binascii.Error, ValueError):
            errors.append(f"{doc.get('name', '?')}: ไฟล์เสียหาย"); continue
        if len(data) > CHAT_MAX_FILE:
            errors.append(f"{doc.get('name', '?')}: ใหญ่เกิน 10MB"); continue
        text, err = extract_attachment(str(doc.get("name", "file")), data)
        if err:
            errors.append(f"{doc.get('name', '?')}: {err}"); continue
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{doc.get('name', '?')}: ไม่พบข้อความที่อ่านได้"); continue
        blocks.append(f"ไฟล์แนบ {doc.get('name')}:\n{text[:60000]}")
    if not content and not blocks and not images:
        raise ValueError("; ".join(errors) if errors else "message is empty")
    if blocks:
        content = (content + "\n\n" if content else "") + "\n\n".join(blocks)
    msg = {"role": "user", "content": content or "(ดูรูปภาพที่แนบมา)"}
    if images:
        clean = []
        for img in images:
            if not isinstance(img, str):
                raise ValueError("invalid image")
            clean.append(img.split(",", 1)[-1])  # tolerate data URLs
        msg["images"] = clean
    out.append(msg)
    return out, bool(images), errors


def model_catalog():
    available = available_models()
    return {"default": DEFAULT_MODEL_ID, "prompt_enhancer": bool(ENRICHER_URL),
            "music_available": music_available(),
            "models": [
        {"id": model_id, "label": spec["label"], "available": available[model_id],
         "profiles": spec["profiles"], "default_profile": spec["default_profile"],
         "supports_reference": spec["supports_reference"],
         "supports_negative": spec["supports_negative"]}
        for model_id, spec in MODELS.items()
    ]}


def build_qwen_graph(prompt, negative, resolution, seed, profile, reference=None):
    params = PROFILES[profile]
    prompt = compose_prompt(prompt, negative)
    graph = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": MODEL}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": TEXT_ENCODER, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": prompt, "negative_prompt": "",
            "resolution": int(resolution)}},
        "4": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["3", 0], "negative": ["3", 1],
            "latent_image": ["3", 2], "seed": int(seed), "steps": params["steps"],
            "cfg": params["cfg"], "sampler_name": "euler", "scheduler": "simple",
            "denoise": 1.0}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["5", 0]}},
        "7": {"class_type": "SaveImage", "inputs": {
            "images": ["6", 0], "filename_prefix": "webui_gen"}},
    }
    if reference:
        # Native Qwen-Image-2.1 reference pathway: the node encodes the image as
        # vision tokens and splices VAE reference latents into the conditioning.
        # Autogrow inputs are addressed by their nested path name (images.image_N).
        graph["8"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
        graph["3"]["inputs"]["images.image_1"] = ["8", 0]
        graph["3"]["inputs"]["vae"] = ["5", 0]
    return graph


def build_flux_graph(prompt, resolution, seed):
    """ComfyUI's distilled FLUX.2 Klein path: four Euler steps, CFG 1."""
    # GGUF text encoders load through ComfyUI-GGUF's loader node
    loader = "CLIPLoaderGGUF" if FLUX_TEXT_ENCODER.endswith(".gguf") else "CLIPLoader"
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": FLUX_MODEL, "weight_dtype": "default"}},
        "2": {"class_type": loader, "inputs": {
            "clip_name": FLUX_TEXT_ENCODER, "type": "flux2", "device": "default"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {
            "clip": ["2", 0], "text": prompt}},
        "4": {"class_type": "ConditioningZeroOut", "inputs": {
            "conditioning": ["3", 0]}},
        "5": {"class_type": "CFGGuider", "inputs": {
            "model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0],
            "cfg": 1.0}},
        "6": {"class_type": "Flux2Scheduler", "inputs": {
            "steps": 4, "width": int(resolution), "height": int(resolution)}},
        "7": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "8": {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}},
        "9": {"class_type": "EmptyFlux2LatentImage", "inputs": {
            "width": int(resolution), "height": int(resolution), "batch_size": 1}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["8", 0], "guider": ["5", 0], "sampler": ["7", 0],
            "sigmas": ["6", 0], "latent_image": ["9", 0]}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX_VAE}},
        "12": {"class_type": "VAEDecode", "inputs": {
            "samples": ["10", 0], "vae": ["11", 0]}},
        "13": {"class_type": "SaveImage", "inputs": {
            "images": ["12", 0], "filename_prefix": "webui_flux2_klein"}},
    }


def build_graph(prompt, negative, resolution, seed, profile, reference=None,
                model_id=DEFAULT_MODEL_ID):
    if model_id == DEFAULT_MODEL_ID:
        return build_qwen_graph(prompt, negative, resolution, seed, profile, reference)
    if model_id == FLUX_MODEL_ID:
        return build_flux_graph(prompt, resolution, seed)
    raise ValueError("unknown model")


def music_available():
    return (MODEL_ROOT / "checkpoints" / YUE2_CHECKPOINT).exists()


def build_yue2_graph(style, lyrics, seconds, seed, planning=True):
    """Text-to-song graph mirroring the ComfyUI 'Text to Music (YuE2)' template."""
    graph = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": YUE2_CHECKPOINT}},
        "3": {"class_type": "YuE2GenerateMusic", "inputs": {
            "clip": ["1", 1], "style": style, "lyrics": lyrics, "abc": "",
            "seed": seed, "mode": "full", "max_duration": float(seconds),
            "temperature": 1.0, "top_p": 0.95, "top_k": 100,
            "repetition_penalty": 1.2}},
        "4": {"class_type": "ConditioningZeroOut", "inputs": {
            "conditioning": ["3", 0]}},
        "5": {"class_type": "EmptyYuE2LatentAudio", "inputs": {
            "seconds": ["3", 1], "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["3", 0], "negative": ["4", 0],
            "latent_image": ["5", 0], "seed": seed, "steps": 32, "cfg": 1.0,
            "sampler_name": "dpm_2", "scheduler": "sgm_uniform", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudioMP3", "inputs": {
            "audio": ["7", 0], "filename_prefix": "music/thanipitak",
            "quality": "V0"}},
    }
    if planning:  # ABC plan (melody + chords) steers the music pass
        graph["2"] = {"class_type": "YuE2GenerateABC", "inputs": {
            "clip": ["1", 1], "style": style, "lyrics": lyrics, "seed": seed,
            "mode": "full", "max_abc_tokens": 8192, "temperature": 0.7,
            "top_p": 0.9, "top_k": 30, "repetition_penalty": 1.005,
            "penalty_window": 100}}
        graph["3"]["inputs"]["abc"] = ["2", 0]
    return graph


def run_music_job(job_id, spec, user=None):
    ticket = COMFY_GATE.reserve()
    granted = False
    queue_deadline = time.time() + QUEUE_WAIT_TIMEOUT
    try:
        while not COMFY_GATE.try_promote(ticket, QUEUE_EVENT_INTERVAL):
            with LOCK:
                JOBS[job_id]["queue_position"] = COMFY_GATE.position(ticket)
            if time.time() >= queue_deadline:
                raise RuntimeError("รอคิวนานเกินไป (มากกว่า "
                                   f"{int(QUEUE_WAIT_TIMEOUT)} วินาที) กรุณาลองใหม่ภายหลัง")
        granted = True
        with LOCK:
            JOBS[job_id]["queue_position"] = 0
        if CHAT_GATE.snapshot()["active"]:
            with LOCK:
                JOBS[job_id]["stage"] = "waiting_chat"
            wait_chat_idle(CHAT_GRACE_SECONDS)
        with LOCK:
            JOBS[job_id]["stage"] = "freeing"
        release_ollama_vram()  # YuE2 shares the 12GB card with the chat model
        with LOCK:
            JOBS[job_id]["stage"] = "composing"
        graph = build_yue2_graph(spec["style"], spec["lyrics"], spec["seconds"],
                                 spec["seed"], spec["planning"])
        resp = comfy_post("/prompt", {"prompt": graph, "client_id": job_id})
        pid = resp.get("prompt_id")
        if not pid:
            raise RuntimeError("comfy rejected prompt: " + json.dumps(resp)[:300])
        with LOCK:
            JOBS[job_id]["prompt_id"] = pid
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["started"] = time.time()
        deadline = time.time() + MUSIC_JOB_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            history = comfy_get(f"/history/{pid}")
            if history == {}:
                continue
            item = next(iter(history.values()))
            status = item.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                detail = json.dumps(msgs[-1])[:400] if msgs else "comfy execution error"
                raise RuntimeError(detail)
            if status.get("completed"):
                for output in item.get("outputs", {}).values():
                    for audio in output.get("audio", output.get("images", [])):
                        if audio.get("type") == "output":
                            subfolder = audio.get("subfolder", "")
                            with LOCK:
                                JOBS[job_id]["file"] = audio["filename"]
                                JOBS[job_id]["subfolder"] = subfolder
                                JOBS[job_id]["status"] = "done"
                                JOBS[job_id]["finished"] = time.time()
                            if user:
                                source = (OUTPUT_DIR / subfolder / audio["filename"]) \
                                    if subfolder else (OUTPUT_DIR / audio["filename"])
                                note = "สไตล์: " + spec["style"]
                                if spec.get("lyrics"):
                                    note += "\nเนื้อร้อง: " + spec["lyrics"]
                                media_id = archive_job_output(user, "music", source, note)
                                with LOCK:
                                    if media_id:
                                        JOBS[job_id]["media_id"] = media_id
                                    else:
                                        JOBS[job_id]["quota_full"] = True
                            return
                raise RuntimeError("finished but no output audio found")
        raise RuntimeError("timeout waiting for ComfyUI")
    except Exception as e:  # surface the failure to the browser
        with LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)[:500]
    finally:
        if granted:
            COMFY_GATE.leave(ticket)
        else:
            COMFY_GATE.cancel(ticket)


def run_job(job_id, spec, ref_path=None, user=None):
    ticket = COMFY_GATE.reserve()
    granted = False
    queue_deadline = time.time() + QUEUE_WAIT_TIMEOUT
    try:
        while not COMFY_GATE.try_promote(ticket, QUEUE_EVENT_INTERVAL):
            with LOCK:
                JOBS[job_id]["queue_position"] = COMFY_GATE.position(ticket)
            if time.time() >= queue_deadline:
                raise RuntimeError("รอคิวนานเกินไป (มากกว่า "
                                   f"{int(QUEUE_WAIT_TIMEOUT)} วินาที) กรุณาลองใหม่ภายหลัง")
        granted = True
        with LOCK:
            JOBS[job_id]["queue_position"] = 0
        source = spec["prompt"]
        if spec.get("model_id", DEFAULT_MODEL_ID) == DEFAULT_MODEL_ID:
            source = compose_prompt(source, spec.get("negative") or "")
        with LOCK:
            JOBS[job_id]["stage"] = "translating"
        final_prompt = enhance_prompt(source)
        with LOCK:
            if final_prompt != spec["prompt"]:
                JOBS[job_id]["prompt_enhanced"] = final_prompt
        if CHAT_GATE.snapshot()["active"]:
            with LOCK:
                JOBS[job_id]["stage"] = "waiting_chat"
            wait_chat_idle(CHAT_GRACE_SECONDS)
        with LOCK:
            # Drop the chat model after translation. The enricher itself
            # may be the model now sitting on the GPU.
            JOBS[job_id]["stage"] = "freeing"
        release_ollama_vram()
        with LOCK:
            JOBS[job_id]["stage"] = "sampling"
        graph = build_graph(final_prompt, "", spec["resolution"],
                            spec["seed"], spec["profile"], spec["reference"],
                            spec["model_id"])
        resp = comfy_post("/prompt", {"prompt": graph, "client_id": job_id})
        pid = resp.get("prompt_id")
        if not pid:
            raise RuntimeError("comfy rejected prompt: " + json.dumps(resp)[:300])
        with LOCK:
            JOBS[job_id]["prompt_id"] = pid
            JOBS[job_id]["status"] = "running"
            JOBS[job_id]["started"] = time.time()
        deadline = time.time() + JOB_TIMEOUT
        while time.time() < deadline:
            time.sleep(2)
            history = comfy_get(f"/history/{pid}")
            if history == {}:
                continue
            item = next(iter(history.values()))
            status = item.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                detail = json.dumps(msgs[-1])[:400] if msgs else "comfy execution error"
                raise RuntimeError(detail)
            if status.get("completed"):
                for output in item.get("outputs", {}).values():
                    for img in output.get("images", []):
                        if img.get("type") == "output":
                            with LOCK:
                                JOBS[job_id]["file"] = img["filename"]
                                JOBS[job_id]["status"] = "done"
                                JOBS[job_id]["finished"] = time.time()
                            if user:
                                media_id = archive_job_output(
                                    user, "image", OUTPUT_DIR / img["filename"],
                                    spec["prompt"])
                                with LOCK:
                                    if media_id:
                                        JOBS[job_id]["media_id"] = media_id
                                    else:
                                        JOBS[job_id]["quota_full"] = True
                            return
                raise RuntimeError("finished but no output image found")
        raise RuntimeError("timeout waiting for ComfyUI")
    except Exception as e:  # surface the failure to the browser
        with LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)[:500]
    finally:
        if granted:
            COMFY_GATE.leave(ticket)
        else:
            COMFY_GATE.cancel(ticket)
        if ref_path:  # uploaded references are single-use
            try:
                (INPUT_DIR / ref_path).unlink()
            except OSError:
                pass


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8",
              cache=None):
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        header = self.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return session_info(token.strip())

    def _authorized(self):
        return self._session() is not None

    def _is_admin(self):
        info = self._session()
        return bool(info and info["admin"])

    def _read_json(self, limit):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > limit:
                raise ValueError("invalid body length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid body")
            return body
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return None

    def _current_user(self):
        """The approved account behind this request's token, or None."""
        info = self._session()
        if not info or not info.get("username"):
            return None
        return get_user_by_username(info["username"])

    def _handle_login(self):
        body = self._read_json(65536)
        if body is None:
            return
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not isinstance(password, str) \
                or not username.strip() or not password:
            self._send(400, {"error": "กรุณากรอก username และรหัสผ่าน"})
            return
        try:
            user = login_user(username, password)
        except PendingAccount:
            self._send(403, {"error": "บัญชีนี้ยังรอการอนุมัติจากผู้ดูแลระบบ"})
            return
        except ValueError as e:
            self._send(401, {"error": str(e)})
            return
        self._send(200, {"token": create_session(username=user["username"]),
                         "expires_in": SESSION_TTL,
                         "username": user["username"], "fullname": user["fullname"]})

    def _handle_register(self):
        body = self._read_json(65536)
        if body is None:
            return
        try:
            username = register_user(body.get("fullname"), body.get("username"),
                                     body.get("password"))
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        self._send(200, {"ok": True, "username": username,
                         "message": "ส่งคำขอสมัครแล้ว รอผู้ดูแลระบบอนุมัติก่อนจึงจะเข้าสู่ระบบได้"})

    def _handle_approve_login(self):
        body = self._read_json(65536)
        if body is None:
            return
        code = body.get("code")
        if not isinstance(code, str) or not hmac.compare_digest(
                code.encode(), APPROVE_CODE.encode()):
            time.sleep(0.8)
            self._send(401, {"error": "รหัสหน้าอนุมัติไม่ถูกต้อง"})
            return
        self._send(200, {"token": create_session(admin=True), "expires_in": SESSION_TTL})

    def _handle_admin_pending(self):
        rows = pending_users()
        self._send(200, {"pending": [
            {"id": r["id"], "fullname": r["fullname"], "username": r["username"],
             "created_at": r["created_at"]} for r in rows]})

    def _handle_admin_decision(self, approved):
        body = self._read_json(65536)
        if body is None:
            return
        username = str(body.get("username") or "").strip().lower()
        if not username or not approve_user(username, approved):
            self._send(404, {"error": "ไม่พบบัญชีนี้"})
            return
        self._send(200, {"ok": True,
                         "message": "อนุมัติบัญชีแล้ว" if approved else "ลบคำขอสมัครแล้ว"})

    def _stream_chat(self, model_id, messages, notes, persist=None):
        """Stream one Ollama answer as NDJSON.

        persist (chat tab only) = {"user_id", "session", "user_text"}: the
        exchange is written to the user's history once the answer completes.
        """
        payload = {"model": model_id, "messages": messages,
                   "stream": True, "think": False,
                   "keep_alive": CHAT_KEEP_ALIVE,
                   # context knobs only apply inside options{} — top-level
                   # fields are silently ignored and 4k ctx cuts long code
                   "options": {"num_ctx": chat_num_ctx(model_id), "num_predict": -1}}
        req = urllib.request.Request(
            OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        wlock = threading.Lock()
        stopped = threading.Event()
        full_parts = []

        def emit(obj):
            with wlock:
                if obj.get("beat") and stopped.is_set():
                    return
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()
                if obj.get("done") or "error" in obj:
                    stopped.set()

        def heartbeat():
            # Cloudflare and middleboxes drop connections that sit silent
            # while Ollama cold-loads a model (up to minutes on this GPU)
            while not stopped.wait(CHAT_HEARTBEAT):
                try:
                    emit({"beat": True})
                except OSError:
                    stopped.set()
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True,
                                             name="chat-heartbeat")
        heartbeat_thread.start()
        try:
            if notes:
                emit({"notes": notes})
            # Keep browser document context identical to what Ollama sees.
            emit({"user_content": messages[-1]["content"]})
            if persist:
                # Resolve (or open) the history session up front so the browser
                # can keep sending follow-up turns under the same id.
                persist["session"] = ensure_chat_session(
                    persist["user_id"], persist.get("session"), persist.get("user_text"))
                emit({"chat_session": persist["session"]})
            # Chat lane: one generation at a time; report the live position
            # while waiting so the browser can show "you are Nth in queue".
            ticket = None
            granted = False
            ticket = CHAT_GATE.reserve()
            try:
                queue_deadline = time.time() + QUEUE_WAIT_TIMEOUT
                while not CHAT_GATE.try_promote(ticket, QUEUE_EVENT_INTERVAL):
                    if stopped.is_set():
                        return
                    if time.time() >= queue_deadline:
                        emit({"error": "คิวแชตยาวเกินไป (รอเกิน "
                                       f"{int(QUEUE_WAIT_TIMEOUT)} วินาที) กรุณาลองใหม่ภายหลัง"})
                        return
                    emit({"stage": "queue", "position": CHAT_GATE.position(ticket)})
                granted = True
            except Exception:
                CHAT_GATE.cancel(ticket)
                raise
            if not chat_model_ready(model_id):
                emit({"stage": "freeing"})
                free_comfy_vram()
                emit({"stage": "loading"})
                warm_chat_model(model_id)
            if stopped.is_set():
                return
            with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT) as r:
                for line in r:
                    if stopped.is_set():
                        return
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise RuntimeError(str(chunk["error"]))
                    delta = chunk.get("message", {}).get("content", "")
                    if delta:
                        full_parts.append(delta)
                        emit({"delta": delta})
                    if chunk.get("done"):
                        usage = {"ctx": chat_num_ctx(model_id)}
                        if isinstance(chunk.get("prompt_eval_count"), int):
                            usage["prompt"] = chunk["prompt_eval_count"]
                        if isinstance(chunk.get("eval_count"), int):
                            usage["eval"] = chunk["eval_count"]
                        emit({"usage": usage})
                        if chunk.get("done_reason") == "length" or \
                                chunk.get("finish_reason") == "length":
                            emit({"truncated": True})
                        emit({"done": True})
                        if persist:
                            saved = save_chat_exchange(
                                persist["user_id"], persist["session"],
                                persist.get("user_text") or "", "".join(full_parts))
                            if not saved:
                                emit({"chat_saved": False})
                        return
            raise RuntimeError("Ollama ปิดการเชื่อมต่อก่อนตอบเสร็จ กรุณาลองอีกครั้ง")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser disconnected; there is nowhere to send an error
        except Exception as e:  # tell the browser instead of dying mid-stream
            try:
                emit({"error": str(e)[:300]})
            except OSError:
                pass
        finally:
            if granted:
                CHAT_GATE.leave(ticket)
            elif ticket is not None:
                CHAT_GATE.cancel(ticket)
            stopped.set()
            heartbeat_thread.join(timeout=1)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8", cache="no-cache")
        elif route in ("/approve", "/approve.html"):
            html = (HERE / "approve.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8", cache="no-cache")
        elif route == "/api/session":
            info = self._session()
            if not info:
                self._send(401, {"error": "ต้องเข้าสู่ระบบก่อนใช้งาน"})
                return
            user = get_user_by_username(info["username"]) if info.get("username") else None
            self._send(200, {"ok": True,
                             "username": info.get("username"),
                             "fullname": user["fullname"] if user else None,
                             "admin": bool(info.get("admin"))})
        elif route.startswith("/api/") and not self._authorized():
            self._send(401, {"error": "ต้องเข้าสู่ระบบก่อนใช้งาน"})
        elif route == "/api/health":
            self._send(200, {"ok": True, "model": MODEL,
                             "available_models": [m["id"] for m in model_catalog()["models"]
                                                  if m["available"]]})
        elif route == "/api/models":
            self._send(200, model_catalog())
        elif route == "/api/chat/models":
            try:
                self._send(200, chat_models())
            except Exception:
                self._send(503, {"error": "ollama ไม่พร้อมใช้งาน"})
        elif route == "/api/queue":
            self._send(200, {"comfy": COMFY_GATE.snapshot(),
                             "chat": CHAT_GATE.snapshot()})
        elif route == "/api/storage":
            user = self._current_user()
            if not user:
                self._send(403, {"error": "บัญชีนี้ใช้งานห้องแชตเท่านั้น"})
                return
            self._send(200, storage_summary(user["id"]))
        elif route == "/api/library":
            user = self._current_user()
            if not user:
                self._send(403, {"error": "บัญชีนี้ใช้งานห้องแชตเท่านั้น"})
                return
            self._send(200, user_library(user["id"]))
        elif route == "/api/admin/pending":
            if not self._is_admin():
                self._send(403, {"error": "ต้องเข้าสู่ระบบหน้าอนุมัติก่อน"})
                return
            self._handle_admin_pending()
        elif route.startswith("/api/chats/"):
            user = self._current_user()
            jid = route.rsplit("/", 1)[-1]
            messages = chat_messages(user["id"], jid) if user else None
            if messages is None:
                self._send(404, {"error": "ไม่พบบทสนทนานี้"})
                return
            self._send(200, {"id": jid, "messages": messages})
        elif route.startswith("/api/media/") and route.endswith("/file"):
            user = self._current_user()
            mid = route[len("/api/media/"):-len("/file")]
            with db() as conn:
                row = conn.execute("SELECT * FROM media WHERE id=? AND user_id=?",
                                   (mid, user["id"] if user else -1)).fetchone()
            if not row:
                self._send(404, {"error": "ไม่พบไฟล์นี้"})
                return
            target = (DATA_DIR / "users" / row["rel_path"]).resolve()
            if not str(target).startswith(str((DATA_DIR / "users").resolve())) or not target.is_file():
                self._send(404, {"error": "ไฟล์หายไปแล้ว"})
                return
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                     ".webp": "image/webp", ".mp3": "audio/mpeg",
                     ".wav": "audio/wav"}.get(target.suffix.lower(),
                                              "application/octet-stream")
            self._send(200, target.read_bytes(), ctype, cache="no-cache")
        elif route.startswith("/api/status/"):
            jid = route.rsplit("/", 1)[-1]
            with LOCK:
                job = dict(JOBS.get(jid, {}))
            for private in ("user_id", "username"):
                job.pop(private, None)
            job.setdefault("status", "unknown")
            if job.get("status") in ("queued", "running") and job.get("prompt_id"):
                ahead = comfy_jobs_ahead(job["prompt_id"])
                if ahead is not None:
                    job["queue_ahead"] = ahead
            self._send(200, job)
        elif route.startswith("/api/image/"):
            jid = route.rsplit("/", 1)[-1]
            with LOCK:
                filename = JOBS.get(jid, {}).get("file")
            if not filename:
                self._send(404, {"error": "image not ready"})
                return
            try:
                data = (OUTPUT_DIR / filename).read_bytes()
            except OSError:
                self._send(404, {"error": "image file missing"})
                return
            self._send(200, data, "image/png")
        elif route.startswith("/api/audio/"):
            jid = route.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(jid, {})
                filename = job.get("file")
                subfolder = job.get("subfolder", "")
            if job.get("status") != "done" or not filename:
                self._send(404, {"error": "audio not ready"})
                return
            target = (OUTPUT_DIR / subfolder / filename) if subfolder \
                else (OUTPUT_DIR / filename)
            try:
                data = target.read_bytes()
            except OSError:
                self._send(404, {"error": "audio file missing"})
                return
            self._send(200, data, "audio/mpeg", cache="no-cache")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route == "/api/login":
            self._handle_login()
            return
        if route == "/api/register":
            self._handle_register()
            return
        if route == "/api/approve/login":
            self._handle_approve_login()
            return
        if route.startswith("/api/") and not self._authorized():
            self._send(401, {"error": "ต้องเข้าสู่ระบบก่อนใช้งาน"})
            return
        if route in ("/api/admin/approve", "/api/admin/reject"):
            if not self._is_admin():
                self._send(403, {"error": "ต้องเข้าสู่ระบบหน้าอนุมัติก่อน"})
                return
            self._handle_admin_decision(route.endswith("approve"))
            return
        if route == "/api/chat":
            self._handle_chat()
            return
        if route == "/api/lyrics":
            self._handle_lyrics()
            return
        if route == "/api/music/generate":
            self._handle_music()
            return
        if route == "/api/memory/clear":
            self._handle_memory_clear()
            return
        if route != "/api/generate":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("invalid body length")
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid body")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return
        prompt = str(body.get("prompt", "")).strip()
        if not prompt or len(prompt) > 4000:
            self._send(400, {"error": "prompt is required (max 4000 characters)"})
            return
        negative = str(body.get("negative") or DEFAULT_NEGATIVE).strip()
        if len(negative) > 1000:
            self._send(400, {"error": "negative prompt is too long"})
            return
        model_id = body.get("model") or DEFAULT_MODEL_ID
        if not isinstance(model_id, str) or model_id not in MODELS:
            self._send(400, {"error": "unknown model"})
            return
        spec = MODELS[model_id]
        if not available_models()[model_id]:
            self._send(503, {"error": "selected model is not installed"})
            return
        profile = str(body.get("profile") or spec["default_profile"])
        if profile == "auto" and model_id == DEFAULT_MODEL_ID:  # legacy clients
            profile = "medium"
        if profile not in spec["profiles"]:
            self._send(400, {"error": "invalid profile for selected model"})
            return
        if negative and not spec["supports_negative"]:
            self._send(400, {"error": "selected model does not support negative prompts"})
            return
        if body.get("reference") and not spec["supports_reference"]:
            self._send(400, {"error": "selected model does not support reference images"})
            return
        try:
            resolution = int(body.get("resolution") or 1024)
            # 0 is a real seed; only a missing value should be randomized.
            raw_seed = body.get("seed")
            if raw_seed is None or raw_seed == "":
                seed = time.time_ns() % (2 ** 31)
            else:
                seed = int(raw_seed)
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid generation option"})
            return
        if resolution not in RESOLUTIONS:
            resolution = 1024
        job_id = uuid.uuid4().hex[:12]
        owner = self._job_owner()
        reference = None
        if body.get("reference"):
            try:
                reference = save_reference(body.get("reference"), job_id)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
        ref_path = reference
        spec = {"prompt": prompt, "negative": negative, "resolution": resolution,
                "seed": seed, "profile": profile, "reference": reference,
                "model_id": model_id}
        with LOCK:
            JOBS[job_id] = {"status": "queued", "profile": profile,
                            "model": model_id, "ts": time.time(),
                            "user_id": owner and owner[0],
                            "username": owner and owner[1]}
            for old in [j for j, v in JOBS.items() if time.time() - v["ts"] > 3600]:
                JOBS.pop(old, None)
        threading.Thread(target=run_job, args=(job_id, spec, ref_path, owner),
                         daemon=True).start()
        self._send(200, {"id": job_id, "profile": profile, "model": model_id})

    def _job_owner(self):
        """(user_id, username) of the caller, or None for admin-only tokens."""
        user = self._current_user()
        return (user["id"], user["username"]) if user else None

    def _handle_music(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("invalid body length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid body")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return
        if not music_available():
            self._send(503, {"error": "ยังไม่ได้ติดตั้งโมเดลสร้างเพลง (YuE2)"})
            return
        style = str(body.get("style", "")).strip()
        if not style or len(style) > 800:
            self._send(400, {"error": "กรุณาใส่สไตล์เพลง (ไม่เกิน 800 ตัวอักษร)"})
            return
        lyrics = str(body.get("lyrics", "")).strip()
        if len(lyrics) > 8000:
            self._send(400, {"error": "เนื้อร้องยาวเกิน 8000 ตัวอักษร"})
            return
        try:
            seconds = int(body.get("seconds") or 60)
            raw_seed = body.get("seed")
            if raw_seed in (None, ""):
                seed = time.time_ns() % (2 ** 31)
            else:
                seed = int(raw_seed)
            if seed < 0:
                seed = abs(seed)
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid music option"})
            return
        if not 15 <= seconds <= MUSIC_MAX_SECONDS:
            self._send(400, {"error": f"ความยาวต้องอยู่ระหว่าง 15-{MUSIC_MAX_SECONDS} วินาที"})
            return
        planning = body.get("planning", True) is not False
        job_id = uuid.uuid4().hex[:12]
        owner = self._job_owner()
        with LOCK:
            JOBS[job_id] = {"status": "queued", "kind": "music", "ts": time.time(),
                            "user_id": owner and owner[0],
                            "username": owner and owner[1]}
            for old in [j for j, v in JOBS.items()
                        if time.time() - v["ts"] > 4 * 3600]:
                JOBS.pop(old, None)
        threading.Thread(target=run_music_job, args=(job_id, {
            "style": style, "lyrics": lyrics, "seconds": seconds,
            "seed": seed, "planning": planning}, owner), daemon=True).start()
        self._send(200, {"id": job_id, "seed": seed})

    def _handle_memory_clear(self):
        """Unload every model from the GPU: ComfyUI first, then Ollama.

        Refuses while ComfyUI is mid-job; if ComfyUI is unreachable there is
        nothing to free there, so still unload Ollama and say so.
        """
        try:
            queue = comfy_get("/queue")
            if queue.get("queue_running"):
                self._send(409, {"error": "มีงานสร้างภาพ/เพลงกำลังทำอยู่ รอให้เสร็จก่อนแล้วค่อยล้าง"})
                return
        except Exception:
            pass  # ComfyUI down: its VRAM is gone with it, Ollama still holds models
        comfy_freed = free_comfy_vram()
        release_ollama_vram()
        self._send(200, {"ok": True, "comfy_freed": comfy_freed})

    def _handle_chat(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > CHAT_MAX_BODY:
                raise ValueError("invalid body length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid body")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return
        model_id = body.get("model")
        if not isinstance(model_id, str) or not model_id:
            self._send(400, {"error": "กรุณาเลือกโมเดล"})
            return
        try:
            known = {m["id"]: m for m in chat_models()["models"]}
        except Exception:
            self._send(503, {"error": "ollama ไม่พร้อมใช้งาน"})
            return
        # Ollama resolves a bare name to its :latest tag; accept both spellings
        # so curl callers match what chat_num_ctx already accepts.
        if model_id not in known and ":" not in model_id \
                and f"{model_id}:latest" in known:
            model_id = f"{model_id}:latest"
        if model_id not in known:
            self._send(400, {"error": "ไม่พบโมเดลนี้"})
            return
        try:
            raw_messages = validate_chat_payload(body)
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        if raw_messages[-1].get("images") and not known[model_id]["vision"]:
            self._send(400, {"error": "โมเดลที่เลือกไม่รองรับรูปภาพ กรุณาเลือกโมเดลที่มีป้าย 'เห็นภาพ'"})
            return
        try:
            pdf_notes = preprocess_pdf_docs(body, known[model_id]["vision"])
            messages, has_images, notes = build_chat_messages(
                body, image_limit=CHAT_MAX_MODEL_IMAGES)
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        if has_images and not known[model_id]["vision"]:
            self._send(400, {"error": "โมเดลที่เลือกไม่รองรับรูปภาพ กรุณาเลือกโมเดลที่มีป้าย 'เห็นภาพ'"})
            return
        notes = pdf_notes + (notes or [])
        # Auto-archive: keep the question (plus a note about attachments) and,
        # once the stream completes, the answer in the user's history.
        persist = None
        user = self._current_user()
        if user:
            last = raw_messages[-1] if raw_messages else {}
            user_text = str(last.get("content") or "").strip()
            markers = []
            if last.get("images"):
                markers.append(f"รูป {len(last['images'])} ไฟล์")
            if last.get("docs"):
                markers.append(f"เอกสาร {len(last['docs'])} ไฟล์")
            if markers:
                user_text = (user_text + "\n" if user_text else "") + "[แนบ: " + ", ".join(markers) + "]"
            session_id = body.get("chat_session")
            persist = {"user_id": user["id"],
                       "session": session_id if isinstance(session_id, str) else None,
                       "user_text": user_text}
        self._stream_chat(model_id, messages, notes, persist)

    def _handle_lyrics(self):
        """Write YuE2-ready lyrics with a local chat model; streams like /api/chat.

        The browser plays the deltas into the lyrics textarea as they arrive,
        so the finished text lands there with [Verse]/[Chorus] tags already.
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > 65536:
                raise ValueError("invalid body length")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("invalid body")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._send(400, {"error": "invalid json"})
            return
        model_id = body.get("model")
        if not isinstance(model_id, str) or not model_id:
            self._send(400, {"error": "กรุณาเลือกโมเดล"})
            return
        try:
            known = {m["id"]: m for m in chat_models()["models"]}
        except Exception:
            self._send(503, {"error": "ollama ไม่พร้อมใช้งาน"})
            return
        if model_id not in known and ":" not in model_id \
                and f"{model_id}:latest" in known:
            model_id = f"{model_id}:latest"
        if model_id not in known:
            self._send(400, {"error": "ไม่พบโมเดลนี้"})
            return
        genre = str(body.get("genre", "")).strip()
        if not genre or len(genre) > 400:
            self._send(400, {"error": "กรุณาพิมพ์แนวเพลง (ไม่เกิน 400 ตัวอักษร)"})
            return
        topic = str(body.get("topic", "")).strip()
        if len(topic) > 400:
            self._send(400, {"error": "หัวข้อยาวเกิน 400 ตัวอักษร"})
            return
        user_content = "แนวเพลง: " + genre
        if topic:
            user_content += "\nหัวข้อ/ธีมของเพลง: " + topic
        user_content += "\nเขียนเนื้อร้องตามรูปแบบที่กำหนดไว้"
        self._stream_chat(model_id, [
            {"role": "system", "content": LYRICS_SYSTEM},
            {"role": "user", "content": user_content},
        ], [])

    def do_DELETE(self):
        """Manage-content endpoints: remove chats or library files."""
        route, _, query = self.path.partition("?")
        if route.startswith("/api/") and not self._authorized():
            self._send(401, {"error": "ต้องเข้าสู่ระบบก่อนใช้งาน"})
            return
        user = self._current_user()
        if not user:
            self._send(403, {"error": "บัญชีนี้ใช้งานห้องแชตเท่านั้น"})
            return
        params = urllib.parse.parse_qs(query)
        if route == "/api/chats":
            self._send(200, {"removed": delete_chat_session(user["id"])})
        elif route.startswith("/api/chats/"):
            jid = route.rsplit("/", 1)[-1]
            removed = delete_chat_session(user["id"], jid)
            if not removed:
                self._send(404, {"error": "ไม่พบบทสนทนานี้"})
                return
            self._send(200, {"removed": removed})
        elif route == "/api/media":
            kind = (params.get("kind") or [""])[0]
            if kind not in ("images", "music"):
                self._send(400, {"error": "ต้องระบุ kind=images หรือ music"})
                return
            self._send(200, {"removed": delete_media(user["id"], kind=kind[:-1])})
        elif route.startswith("/api/media/"):
            mid = route.rsplit("/", 1)[-1]
            if not delete_media(user["id"], media_id=mid):
                self._send(404, {"error": "ไม่พบไฟล์นี้"})
                return
            self._send(200, {"removed": 1})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    init_db()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
