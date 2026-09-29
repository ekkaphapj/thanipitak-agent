#!/usr/bin/env python3
"""Thai image-generation UI + local AI webchat backed by ComfyUI and Ollama.

Stdlib only. Binds to 127.0.0.1:8190 — reach it through the Cloudflare tunnel.
Draw jobs: POST /api/generate -> poll GET /api/status/<id> -> GET /api/image/<id>.
Chat: POST /api/chat streams NDJSON deltas while Ollama generates.
Both async patterns keep HTTP responses short so Cloudflare never times out.
"""
import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
import zlib
import xml.etree.ElementTree as ET
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
FLUX_TEXT_ENCODER = "qwen_3_4b.safetensors"
FLUX_VAE = "flux2-vae.safetensors"
MODEL_ROOT = Path(os.environ.get("COMFY_MODEL_ROOT", str(COMFY_HOME / "models")))
HOST = os.environ.get("DRAW_HOST", "127.0.0.1")
PORT = int(os.environ.get("DRAW_PORT", "8190"))
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
CHAT_MAX_DOCS = 4
CHAT_TIMEOUT = 600
CHAT_HEARTBEAT = 15  # seconds between keepalive lines while Ollama is silent
CHAT_KEEP_ALIVE = os.environ.get("CHAT_KEEP_ALIVE", "30m")
CHAT_NUM_CTX = int(os.environ.get("CHAT_NUM_CTX", "32768"))  # default 4k cuts long code
WARMUP_TIMEOUT = 300
PDF_PAGES_MAX = int(os.environ.get("CHAT_PDF_PAGES", "6"))
SHEET_ROWS_MAX = int(os.environ.get("CHAT_SHEET_ROWS", "400"))
DEFAULT_MODEL_ID = "qwen-image-2.1"
FLUX_MODEL_ID = "flux.2-klein-4b"
RESOLUTIONS = [512, 640, 768, 896, 1024]
DEFAULT_NEGATIVE = ""
JOB_TIMEOUT = 900  # seconds
MAX_BODY = 12 * 1024 * 1024  # allows a base64 reference image
MAX_REFERENCE = 10 * 1024 * 1024
# Qwen-Image-2.1 official recipe is euler/simple at 20 steps; CFG 2.5 (with a
# negative prompt) buys prompt adherence at 2x UNET passes, CFG 1.0 buys speed.
PROFILES = {
    "fast": {"steps": 12, "cfg": 1.0},
    "medium": {"steps": 20, "cfg": 1.0},
    "quality": {"steps": 20, "cfg": 2.5},
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
HERE = Path(__file__).resolve().parent
PROMPT_CACHE = {}
PROMPT_CACHE_MAX = 256

ENHANCER_SYSTEM = (
    "You translate Thai image-generation prompts into vivid English prompts for a "
    "text-to-image model. Keep any text inside double quotes exactly as written; "
    "that text must appear in the picture, so never translate or alter it. Translate "
    "everything else into natural, detailed English that describes the subject, "
    "composition, setting, lighting and style. Never add new subjects or change the "
    "meaning. Reply with only the final prompt, no explanations."
)


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
        "temperature": 0.3,
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
        if not text or len(text) > 8000:
            return prompt
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
    """Chat-capable Ollama models, flagging which can see images."""
    models = []
    for m in ollama_tags():
        caps = m.get("capabilities") or []
        if caps and "completion" not in caps:  # embeddings etc.
            continue
        models.append({"id": m["model"], "label": m["model"],
                       "vision": "vision" in caps})
    return {"models": models}


def _xml_text(xml):
    return re.sub(r"<[^>]+>", "", xml)


def extract_docx(data):
    """Body text from a Word file (it is a zip of XML)."""
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    xml = re.sub(r"</w:p>", "\n", xml)
    return _xml_text(xml).strip()


def extract_xlsx(data):
    """Readable table text from an Excel workbook: one block per sheet.

    Reads cell grids via the sheet XML (values, numbers, booleans, inline and
    shared strings) rather than only the shared-strings pool, so models see
    the actual table layout. Capped at SHEET_ROWS_MAX rows per sheet.
    """
    def local(e):
        return e.tag.rsplit("}", 1)[-1]

    def col_index(ref):
        letters = re.match(r"([A-Z]+)", ref or "")
        n = 0
        if not letters:
            return 0
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
    with zipfile.ZipFile(__import__("io").BytesIO(data)) as z:
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
        except (KeyError, ET.ParseError):
            return ""
        rid_target = {rel.get("Id"): rel.get("Target") for rel in rels}
        sheets = [(s.get("name", "Sheet"),
                   rid_target.get(s.get("{%s}id" % ns["r"], "")))
                  for s in wb.findall(".//m:sheet", ns)]
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
                cells = sorted(
                    ((col_index(c.get("r")), cell_text(c, shared))
                     for c in row if local(c) == "c"),
                    key=lambda p: p[0])
                if cells:
                    width = max(width, cells[-1][0] + 1)
                    grid.append(cells)
                if len(grid) >= SHEET_ROWS_MAX:
                    truncated = True
                    break
            block = [f"=== ชีต: {sheet_name} ==="]
            for cells in grid:
                line = [""] * width
                for idx, text in cells:
                    line[idx] = text.replace("\t", " ").replace("\n", " ")
                block.append("\t".join(line).rstrip())
            if truncated:
                block.append(f"… (แสดง {SHEET_ROWS_MAX} แถวแรก)")
            out.append("\n".join(block))
    return "\n\n".join(out)


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
    images = last.setdefault("images", [])
    notes = []
    for doc in list(docs):
        name = str(doc.get("name", "file"))
        if not name.lower().endswith(".pdf"):
            continue
        try:
            data = base64.b64decode(doc.get("data") or "", validate=False)
        except (binascii.Error, ValueError):
            continue  # build_chat_messages will report the broken file
        if vision:
            pages = pdf_page_images(data, PDF_PAGES_MAX)
            if pages:
                room = max(0, 8 - len(images))
                take = pages[:room]
                if take:
                    images.extend(take)
                    docs.remove(doc)
                    notes.append(f"{name}: แปลงเป็นภาพ {len(take)} หน้า "
                                 f"ให้โมเดลอ่านจากรูปโดยตรง")
                    continue
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
            return extract_xlsx(data) or None, None
        if ext in ("pdf",):
            text = extract_pdf(data)
            if len(text) < 20:
                return None, "อ่านข้อความจาก PDF นี้ไม่ได้ (ฟอนต์ฝังแบบพิเศษ) ลองคัดลอกข้อความมาแนบเป็น .txt แทน"
            return text, None
    except Exception:
        return None, "เปิดไฟล์ไม่สำเร็จ"
    return None, "ชนิดไฟล์นี้ยังไม่รองรับ"


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


def chat_model_ready(model_id):
    """True when the model sits in VRAM right now (Ollama /api/ps)."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10) as r:
            models = json.loads(r.read().decode()).get("models", [])
        for m in models:
            if model_id in (m.get("name"), m.get("model")):
                total = m.get("size") or 1
                return (m.get("size_vram") or 0) >= 0.5 * total
    except Exception:
        pass
    return False


def warm_chat_model(model_id):
    """Load a chat model into VRAM without generating anything."""
    payload = {"model": model_id, "keep_alive": CHAT_KEEP_ALIVE,
               # must match the chat runner options or Ollama treats it as a
               # different instance and reloads on every message
               "options": {"num_ctx": CHAT_NUM_CTX}}
    req = urllib.request.Request(
        OLLAMA + "/api/generate", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=WARMUP_TIMEOUT) as r:
        r.read()


def build_chat_messages(payload):
    """Validate a chat request and turn it into Ollama messages.

    History entries pass through as {role, content}; the final user entry may
    carry images (base64) and doc attachments whose extracted text is inlined
    into the content so it stays in context for later turns.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 80:
        raise ValueError("invalid message history")
    out = []
    for m in messages[:-1]:
        if (not isinstance(m, dict) or m.get("role") not in ("user", "assistant")
                or not isinstance(m.get("content"), str)):
            raise ValueError("invalid message history")
        out.append({"role": m["role"], "content": m["content"]})
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user" \
            or not isinstance(last.get("content"), str):
        raise ValueError("invalid message history")
    content = last["content"].strip()
    images = last.get("images") or []
    docs = last.get("docs") or []
    if not isinstance(images, list) or len(images) > CHAT_MAX_IMAGES:
        raise ValueError(f"too many images (max {CHAT_MAX_IMAGES})")
    if not isinstance(docs, list) or len(docs) > CHAT_MAX_DOCS:
        raise ValueError(f"too many documents (max {CHAT_MAX_DOCS})")
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
        blocks.append(f"ไฟล์แนบ {doc.get('name')}:\n{text[:60000]}")
    if not content and not blocks and not images:
        raise ValueError("message is empty")
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
            "models": [
        {"id": model_id, "label": spec["label"], "available": available[model_id],
         "profiles": spec["profiles"], "default_profile": spec["default_profile"],
         "supports_reference": spec["supports_reference"],
         "supports_negative": spec["supports_negative"]}
        for model_id, spec in MODELS.items()
    ]}


def build_qwen_graph(prompt, negative, resolution, seed, profile, reference=None):
    params = PROFILES[profile]
    graph = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": MODEL}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": TEXT_ENCODER, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": prompt, "negative_prompt": negative,
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
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": FLUX_MODEL, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
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


def run_job(job_id, spec, ref_path=None):
    try:
        try:
            with LOCK:
                JOBS[job_id]["stage"] = "translating"
            final_prompt = enhance_prompt(spec["prompt"])
            if final_prompt != spec["prompt"]:
                with LOCK:
                    JOBS[job_id]["prompt_enhanced"] = final_prompt
            with LOCK:
                JOBS[job_id]["stage"] = "sampling"
            graph = build_graph(final_prompt, spec["negative"], spec["resolution"],
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
                                return
                    raise RuntimeError("finished but no output image found")
            raise RuntimeError("timeout waiting for ComfyUI")
        except Exception as e:  # surface the failure to the browser
            with LOCK:
                JOBS[job_id]["status"] = "error"
                JOBS[job_id]["error"] = str(e)[:500]
    finally:
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

    def _stream_chat(self, model_id, messages, notes):
        payload = {"model": model_id, "messages": messages,
                   "stream": True, "think": False,
                   "keep_alive": CHAT_KEEP_ALIVE,
                   # context knobs only apply inside options{} — top-level
                   # fields are silently ignored and 4k ctx cuts long code
                   "options": {"num_ctx": CHAT_NUM_CTX, "num_predict": -1}}
        req = urllib.request.Request(
            OLLAMA + "/api/chat", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        wlock = threading.Lock()
        state = {"done": False}

        def emit(obj):
            with wlock:
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
                self.wfile.flush()

        if notes:
            emit({"notes": notes})
        # exact user content (docs inlined) so the browser history matches
        # what the model saw and follow-up questions keep the file context
        emit({"user_content": messages[-1]["content"]})

        def heartbeat():
            # Cloudflare and middleboxes drop connections that sit silent
            # while Ollama cold-loads a model (up to minutes on this GPU)
            while not state["done"]:
                time.sleep(CHAT_HEARTBEAT)
                if not state["done"]:
                    try:
                        emit({"beat": True})
                    except OSError:
                        return

        threading.Thread(target=heartbeat, daemon=True).start()
        if not chat_model_ready(model_id):
            emit({"stage": "freeing"})
            free_comfy_vram()
            emit({"stage": "loading"})
            warm_chat_model(model_id)
        try:
            with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT) as r:
                for line in r:
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("message", {}).get("content", "")
                    if chunk.get("done"):
                        state["done"] = True
                        usage = {"ctx": CHAT_NUM_CTX}
                        if isinstance(chunk.get("prompt_eval_count"), int):
                            usage["prompt"] = chunk["prompt_eval_count"]
                        if isinstance(chunk.get("eval_count"), int):
                            usage["eval"] = chunk["eval_count"]
                        emit({"usage": usage})
                        if chunk.get("done_reason") == "length" or \
                                chunk.get("finish_reason") == "length":
                            emit({"truncated": True})
                        emit({"done": True})
                        return
                    if delta:
                        emit({"delta": delta})
            state["done"] = True
            emit({"done": True})
        except Exception as e:  # tell the browser instead of dying mid-stream
            state["done"] = True
            try:
                emit({"error": str(e)[:300]})
            except OSError:
                pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8", cache="no-cache")
        elif self.path == "/api/health":
            self._send(200, {"ok": True, "model": MODEL,
                             "available_models": [m["id"] for m in model_catalog()["models"]
                                                  if m["available"]]})
        elif self.path == "/api/models":
            self._send(200, model_catalog())
        elif self.path == "/api/chat/models":
            try:
                self._send(200, chat_models())
            except Exception:
                self._send(503, {"error": "ollama ไม่พร้อมใช้งาน"})
        elif self.path.startswith("/api/status/"):
            jid = self.path.rsplit("/", 1)[-1]
            with LOCK:
                job = dict(JOBS.get(jid, {}))
            job.setdefault("status", "unknown")
            if job.get("status") in ("queued", "running") and job.get("prompt_id"):
                ahead = comfy_jobs_ahead(job["prompt_id"])
                if ahead is not None:
                    job["queue_ahead"] = ahead
            self._send(200, job)
        elif self.path.startswith("/api/image/"):
            jid = self.path.rsplit("/", 1)[-1]
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
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/chat":
            self._handle_chat()
            return
        if self.path != "/api/generate":
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
            seed = int(body.get("seed") or time.time_ns() % (2 ** 31))
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid generation option"})
            return
        if resolution not in RESOLUTIONS:
            resolution = 1024
        job_id = uuid.uuid4().hex[:12]
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
                            "model": model_id, "ts": time.time()}
            for old in [j for j, v in JOBS.items() if time.time() - v["ts"] > 3600]:
                JOBS.pop(old, None)
        threading.Thread(target=run_job, args=(job_id, spec, ref_path), daemon=True).start()
        self._send(200, {"id": job_id, "profile": profile, "model": model_id})

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
        if model_id not in known:
            self._send(400, {"error": "ไม่พบโมเดลนี้"})
            return
        try:
            messages, has_images, notes = build_chat_messages(body)
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        if has_images and not known[model_id]["vision"]:
            self._send(400, {"error": "โมเดลที่เลือกไม่รองรับรูปภาพ กรุณาเลือกโมเดลที่มีป้าย 'เห็นภาพ'"})
            return
        pdf_notes = preprocess_pdf_docs(body, known[model_id]["vision"])
        try:
            messages, has_images, notes = build_chat_messages(body)
        except ValueError as e:
            self._send(400, {"error": str(e)})
            return
        notes = pdf_notes + (notes or [])
        self._stream_chat(model_id, messages, notes)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
