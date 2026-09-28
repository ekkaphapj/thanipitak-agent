#!/usr/bin/env python3
"""Thai image-generation UI backed by allowlisted local ComfyUI models.

Stdlib only. Binds to 127.0.0.1:8190 — reach it through the Cloudflare tunnel.
Job flow: POST /api/generate -> poll GET /api/status/<id> -> GET /api/image/<id>.
The async job pattern keeps every HTTP response short so Cloudflare never times out.
"""
import base64
import json
import os
import re
import threading
import time
import urllib.request
import uuid
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
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._send(200, {"ok": True, "model": MODEL,
                             "available_models": [m["id"] for m in model_catalog()["models"]
                                                  if m["available"]]})
        elif self.path == "/api/models":
            self._send(200, model_catalog())
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

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
