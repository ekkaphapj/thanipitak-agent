"""Focused checks for the separate ComfyUI draw service."""
import base64
import io
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import draw_server


class DrawGraphTest(unittest.TestCase):
    def test_qwen_profiles_follow_official_recipe(self):
        graph = draw_server.build_graph("portrait", "blur", 768, 123, "quality",
                                        "draw_refs/example.png")
        self.assertEqual(graph["1"]["class_type"], "UnetLoaderGGUF")
        self.assertEqual(graph["4"]["inputs"]["steps"], 20)
        self.assertEqual(graph["4"]["inputs"]["cfg"], 2.5)
        self.assertEqual(graph["3"]["inputs"]["images.image_1"], ["8", 0])
        self.assertEqual(graph["3"]["inputs"]["vae"], ["5", 0])
        fast = draw_server.build_graph("portrait", "", 768, 123, "fast")
        medium = draw_server.build_graph("portrait", "", 768, 123, "medium")
        self.assertEqual((fast["4"]["inputs"]["steps"], fast["4"]["inputs"]["cfg"]), (12, 1.0))
        self.assertEqual((medium["4"]["inputs"]["steps"], medium["4"]["inputs"]["cfg"]), (20, 1.0))

    def test_flux_uses_distilled_four_step_graph(self):
        graph = draw_server.build_graph("golfing puppy", "", 768, 123, "standard",
                                        model_id=draw_server.FLUX_MODEL_ID)
        self.assertEqual(graph["1"]["inputs"]["unet_name"], draw_server.FLUX_MODEL)
        self.assertEqual(graph["2"]["inputs"]["type"], "flux2")
        self.assertEqual(graph["3"]["inputs"]["text"], "golfing puppy")
        self.assertEqual(graph["5"]["inputs"]["cfg"], 1.0)
        self.assertEqual(graph["6"]["inputs"], {"steps": 4, "width": 768, "height": 768})
        self.assertEqual(graph["8"]["inputs"]["noise_seed"], 123)
        self.assertEqual(graph["10"]["inputs"]["latent_image"], ["9", 0])
        self.assertEqual(graph["12"]["inputs"]["samples"], ["10", 0])


class EnhancePromptTest(unittest.TestCase):
    def setUp(self):
        draw_server.PROMPT_CACHE.clear()

    def test_passes_through_without_enricher_or_thai(self):
        with mock.patch.object(draw_server, "ENRICHER_URL", ""):
            self.assertEqual(draw_server.enhance_prompt("แมวส้ม"), "แมวส้ม")
        with mock.patch.object(draw_server, "ENRICHER_URL", "http://127.0.0.1:11434/v1/chat/completions"):
            self.assertEqual(draw_server.enhance_prompt("orange cat"), "orange cat")

    def test_translates_thai_and_strips_think_block(self):
        reply = {"choices": [{"message": {"content":
            "<think>reasoning</think>\nA photorealistic orange cat reading a book, warm library light"}}]}
        with mock.patch.object(draw_server, "ENRICHER_URL", "http://127.0.0.1:1/v1/chat/completions"):
            opener = mock.MagicMock()
            opener.return_value.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            with mock.patch.object(urllib.request, "urlopen", opener):
                result = draw_server.enhance_prompt("แมวส้มอ่านหนังสือ")
        self.assertEqual(result, "A photorealistic orange cat reading a book, warm library light")
        sent = json.loads(opener.call_args.args[0].data)
        self.assertEqual(sent["messages"][1]["content"], "แมวส้มอ่านหนังสือ")
        self.assertIn("double quotes", sent["messages"][0]["content"])

    def test_enricher_failure_falls_back_to_original(self):
        with mock.patch.object(draw_server, "ENRICHER_URL", "http://127.0.0.1:1/v1/chat/completions"):
            with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("down")):
                self.assertEqual(draw_server.enhance_prompt("แมวส้ม"), "แมวส้ม")
        self.assertFalse(draw_server.PROMPT_CACHE)

    def test_cache_avoids_second_call(self):
        reply = {"choices": [{"message": {"content": "orange cat on a roof"}}]}
        with mock.patch.object(draw_server, "ENRICHER_URL", "http://127.0.0.1:1/v1/chat/completions"):
            opener = mock.MagicMock()
            opener.return_value.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            with mock.patch.object(urllib.request, "urlopen", opener):
                first = draw_server.enhance_prompt("แมวส้มบนหลังคา")
                second = draw_server.enhance_prompt("แมวส้มบนหลังคา")
        self.assertEqual(first, second)
        self.assertEqual(opener.call_count, 1)


class RunJobTest(unittest.TestCase):
    def test_translates_then_builds_graph_and_completes(self):
        draw_server.JOBS.clear()
        history = {"pid1": {"status": {"status_str": "success", "completed": True},
                            "outputs": {"7": {"images": [
                                {"filename": "out.png", "type": "output"}]}}}}

        def fake_post(path, payload):
            if path == "/prompt":
                self.graph = payload["prompt"]
                return {"prompt_id": "pid1"}
            return {}

        spec = {"prompt": "แมวส้มอ่านหนังสือ", "negative": "", "resolution": 768,
                "seed": 5, "profile": "medium", "reference": None,
                "model_id": draw_server.DEFAULT_MODEL_ID}
        draw_server.JOBS["job1"] = {"status": "queued", "ts": 0}
        with mock.patch.object(draw_server, "enhance_prompt", return_value="an orange cat reading a book"), \
             mock.patch.object(draw_server, "comfy_post", side_effect=fake_post), \
             mock.patch.object(draw_server, "comfy_get", return_value=history), \
             mock.patch.object(draw_server.time, "sleep"):
            draw_server.run_job("job1", spec)
        self.assertEqual(draw_server.JOBS["job1"]["status"], "done")
        self.assertEqual(draw_server.JOBS["job1"]["file"], "out.png")
        self.assertIn("started", draw_server.JOBS["job1"])
        self.assertIn("finished", draw_server.JOBS["job1"])
        self.assertGreaterEqual(draw_server.JOBS["job1"]["finished"],
                                draw_server.JOBS["job1"]["started"])
        self.assertEqual(draw_server.JOBS["job1"]["prompt_enhanced"],
                         "an orange cat reading a book")
        self.assertEqual(self.graph["3"]["inputs"]["prompt"], "an orange cat reading a book")
        self.assertEqual(self.graph["4"]["inputs"]["steps"], 20)

    def test_comfy_error_marks_job_failed(self):
        draw_server.JOBS.clear()
        draw_server.JOBS["job2"] = {"status": "queued", "ts": 0}
        spec = {"prompt": "cat", "negative": "", "resolution": 512, "seed": 1,
                "profile": "fast", "reference": None,
                "model_id": draw_server.DEFAULT_MODEL_ID}
        with mock.patch.object(draw_server, "comfy_post", side_effect=RuntimeError("comfy down")), \
             mock.patch.object(draw_server.time, "sleep"):
            draw_server.run_job("job2", spec)
        self.assertEqual(draw_server.JOBS["job2"]["status"], "error")
        self.assertIn("comfy down", draw_server.JOBS["job2"]["error"])


class AttachmentExtractTest(unittest.TestCase):
    def test_txt_docx_xlsx(self):
        text, err = draw_server.extract_attachment("notes.txt", "hello สวัสดี".encode())
        self.assertIsNone(err)
        self.assertIn("สวัสดี", text)

        doc_xml = ("<w:document><w:body><w:p>Hello <w:r><w:t>world</w:t></w:r></w:p>"
                   "<w:p>second line</w:p></w:body></w:document>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", doc_xml)
        text, err = draw_server.extract_attachment("a.docx", buf.getvalue())
        self.assertIsNone(err)
        self.assertIn("Hello world", text)
        self.assertIn("second line", text)

        shared = ('<sst><si><t>ชื่อ</t></si><si><t><r>ยอดขาย</r></t></si></sst>')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/sharedStrings.xml", shared)
        text, err = draw_server.extract_attachment("a.xlsx", buf.getvalue())
        self.assertIsNone(err)
        self.assertIn("ชื่อ", text)
        self.assertIn("ยอดขาย", text)

    def test_pdf_uncompressed_and_unreadable(self):
        pdf = b"BT (hello pdf text extractor) Tj ET"
        text, err = draw_server.extract_attachment("a.pdf", pdf)
        self.assertIsNone(err)
        self.assertIn("hello pdf text extractor", text)
        text, err = draw_server.extract_attachment("a.pdf", b"not a pdf")
        self.assertIsNotNone(err)

    def test_unsupported_type(self):
        text, err = draw_server.extract_attachment("a.exe", b"MZ")
        self.assertIsNotNone(err)


class ChatMessageTest(unittest.TestCase):
    def base64_(self, raw):
        return base64.b64encode(raw).decode()

    def test_builds_messages_with_doc_inlined_and_keeps_history(self):
        doc = base64.b64encode(b"quarterly numbers: 42").decode()
        payload = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "สรุปไฟล์นี้",
             "images": ["data:image/jpeg;base64,QUJD"],
             "docs": [{"name": "report.txt", "data": doc}]},
        ]}
        messages, has_images, notes = draw_server.build_chat_messages(payload)
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertTrue(has_images)
        self.assertEqual(notes, [])
        self.assertEqual(messages[-1]["images"], ["QUJD"])
        self.assertIn("สรุปไฟล์นี้", messages[-1]["content"])
        self.assertIn("ไฟล์แนบ report.txt", messages[-1]["content"])
        self.assertIn("quarterly numbers: 42", messages[-1]["content"])

    def test_rejects_bad_history_and_limits(self):
        for messages in ([], "nope",
                         [{"role": "system", "content": "x"}],
                         [{"role": "user", "content": "x", "images": ["a"] * 5}]):
            with self.subTest(messages=messages):
                with self.assertRaises(ValueError):
                    draw_server.build_chat_messages({"messages": messages})


class OllamaStream:
    """File-like stand-in for a streaming Ollama chat response."""

    def __init__(self, chunks):
        self.chunks = [json.dumps(c).encode() + b"\n" for c in chunks]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self.chunks)

    def read(self):
        return b"".join(self.chunks)


class FreeComfyVramTest(unittest.TestCase):
    def test_frees_only_when_idle(self):
        with mock.patch.object(draw_server, "comfy_get",
                               return_value={"queue_running": [[1, "x"]]}), \
             mock.patch.object(draw_server, "comfy_post") as post:
            self.assertFalse(draw_server.free_comfy_vram())
            post.assert_not_called()

        with mock.patch.object(draw_server, "comfy_get",
                               return_value={"queue_running": []}), \
             mock.patch.object(draw_server, "comfy_post") as post:
            self.assertTrue(draw_server.free_comfy_vram())
            post.assert_called_with("/free", {"unload_models": True,
                                             "free_memory": True})

    def test_comfy_down_is_not_fatal(self):
        with mock.patch.object(draw_server, "comfy_get", side_effect=OSError):
            self.assertFalse(draw_server.free_comfy_vram())


class WarmModelTest(unittest.TestCase):
    def test_warmup_loads_model_with_keep_alive(self):
        stream = OllamaStream([{"done": True}])
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=stream) as urlopen:
            draw_server.warm_chat_model("typhoon2.5-4b")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["model"], "typhoon2.5-4b")
        self.assertEqual(sent["keep_alive"], draw_server.CHAT_KEEP_ALIVE)
        self.assertNotIn("prompt", sent)


class ChatHttpTest(unittest.TestCase):
    def setUp(self):
        draw_server.JOBS.clear()
        self.server = draw_server.ThreadingHTTPServer(("127.0.0.1", 0), draw_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tags = mock.patch.object(draw_server, "ollama_tags", return_value=[
            {"model": "qwen3:8b", "capabilities": ["completion", "thinking"]},
            {"model": "vl:4b", "capabilities": ["completion", "vision"]},
            {"model": "emb", "capabilities": ["embedding"]}])
        self.tags.start()

    def tearDown(self):
        self.tags.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.JOBS.clear()

    def test_models_endpoint_filters_embeddings_and_flags_vision(self):
        with urllib.request.urlopen(self.base + "/api/chat/models") as response:
            data = json.load(response)
        self.assertEqual([m["id"] for m in data["models"]], ["qwen3:8b", "vl:4b"])
        self.assertEqual([m["vision"] for m in data["models"]], [False, True])

    def ollama_mock(self, chunks):
        """Patch urlopen only for Ollama URLs so the test's own calls pass through."""
        real_urlopen = urllib.request.urlopen

        def selective(url, *args, **kwargs):
            target = url.full_url if isinstance(url, urllib.request.Request) else url
            if target.startswith(draw_server.OLLAMA):
                return OllamaStream(chunks)
            return real_urlopen(url, *args, **kwargs)
        return mock.patch.object(urllib.request, "urlopen", side_effect=selective)

    def chat_post(self, body):
        request = urllib.request.Request(
            self.base + "/api/chat", json.dumps(body).encode(),
            {"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(request, timeout=5)

    def test_chat_streams_deltas_and_user_content(self):
        lines = [
            {"message": {"content": "สวัสดี"}, "done": False},
            {"message": {"content": " ครับ"}, "done": False},
            {"message": {"content": ""}, "done": True,
             "prompt_eval_count": 120, "eval_count": 8},
        ]
        body = {"model": "qwen3:8b", "messages": [
            {"role": "user", "content": "hi",
             "docs": [{"name": "n.txt", "data": base64.b64encode(b"payload").decode()}]}]}
        with self.ollama_mock(lines) as urlopen, \
             mock.patch.object(draw_server, "free_comfy_vram", return_value=True) as free, \
             mock.patch.object(draw_server, "warm_chat_model") as warm:
            with self.chat_post(body) as response:
                streamed = response.read().decode()
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["model"], "qwen3:8b")
        self.assertEqual(sent["stream"], True)
        self.assertEqual(sent["keep_alive"], draw_server.CHAT_KEEP_ALIVE)
        self.assertEqual(sent["options"]["num_ctx"], draw_server.CHAT_NUM_CTX)
        self.assertEqual(sent["options"]["num_predict"], -1)
        self.assertIn("payload", sent["messages"][-1]["content"])
        free.assert_called_once()
        warm.assert_called_once_with("qwen3:8b")
        rows = [json.loads(l) for l in streamed.splitlines() if l.strip()]
        stages = [r["stage"] for r in rows if "stage" in r]
        self.assertEqual(stages, ["freeing", "loading"])
        self.assertEqual(rows[0]["user_content"], sent["messages"][-1]["content"])
        self.assertEqual("".join(r["delta"] for r in rows if "delta" in r), "สวัสดี ครับ")
        usage = rows[-2]["usage"]
        self.assertEqual(usage, {"ctx": draw_server.CHAT_NUM_CTX,
                                 "prompt": 120, "eval": 8})
        self.assertTrue(rows[-1]["done"])

    def test_heartbeat_keeps_connection_alive_while_ollama_is_silent(self):
        class SlowStream:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                time.sleep(0.25)  # model "cold load": no tokens for a while
                yield json.dumps({"message": {"content": "ok"}, "done": True}).encode() + b"\n"

        real_urlopen = urllib.request.urlopen

        def selective(url, *args, **kwargs):
            target = url.full_url if isinstance(url, urllib.request.Request) else url
            if target.startswith(draw_server.OLLAMA):
                return SlowStream()
            return real_urlopen(url, *args, **kwargs)

        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(draw_server, "CHAT_HEARTBEAT", 0.05), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"), \
             mock.patch.object(urllib.request, "urlopen", side_effect=selective):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines() if l.strip()]
        self.assertGreaterEqual(len([r for r in rows if "beat" in r]), 1)
        self.assertTrue(rows[-1]["done"])

    def test_length_cut_reports_truncation(self):
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        cut = [{"message": {"content": "partial"}, "done": False},
               {"message": {"content": ""}, "done": True, "done_reason": "length"}]
        with self.ollama_mock(cut), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines() if l.strip()]
        kinds = [k for r in rows for k in r]
        self.assertIn("usage", kinds)
        self.assertTrue(rows[-2]["truncated"])
        self.assertTrue(rows[-1]["done"])

    def test_image_without_vision_model_is_rejected(self):
        body = {"model": "qwen3:8b", "messages": [
            {"role": "user", "content": "ดูรูป", "images": ["QUJD"]}]}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.chat_post(body)
        self.assertEqual(error.exception.code, 400)
        self.assertIn("ไม่รองรับรูปภาพ",
                      json.loads(error.exception.read().decode("utf-8"))["error"])
        error.exception.close()

        body["model"] = "vl:4b"
        ok = [{"message": {"content": "ok"}, "done": True}]
        with self.ollama_mock(ok), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                self.assertTrue(json.loads(response.read().decode().splitlines()[-1])["done"])


class QueuePositionTest(unittest.TestCase):
    def test_counts_running_and_pending(self):
        queue = {"queue_running": [[1, "a"]],
                 "queue_pending": [[2, "b"], [3, "c"], [4, "a2"]]}
        with mock.patch.object(draw_server, "comfy_get", return_value=queue):
            self.assertEqual(draw_server.comfy_jobs_ahead("c"), 2)
            self.assertEqual(draw_server.comfy_jobs_ahead("a"), 0)
            self.assertIsNone(draw_server.comfy_jobs_ahead("zzz"))

    def test_handles_string_entries_and_errors(self):
        with mock.patch.object(draw_server, "comfy_get",
                               return_value={"queue_running": ["x"], "queue_pending": ["x2"]}):
            self.assertEqual(draw_server.comfy_jobs_ahead("x2"), 1)
        with mock.patch.object(draw_server, "comfy_get", side_effect=OSError):
            self.assertIsNone(draw_server.comfy_jobs_ahead("x"))


class DrawHttpTest(unittest.TestCase):
    def setUp(self):
        draw_server.JOBS.clear()
        self.server = draw_server.ThreadingHTTPServer(("127.0.0.1", 0), draw_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.available = mock.patch.object(draw_server, "available_models", return_value={
            draw_server.DEFAULT_MODEL_ID: True, draw_server.FLUX_MODEL_ID: True})
        self.available.start()

    def tearDown(self):
        self.available.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.JOBS.clear()

    def post(self, body):
        request = urllib.request.Request(
            self.base + "/api/generate", body,
            {"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(request, timeout=2)

    def test_catalog_and_model_selection(self):
        with urllib.request.urlopen(self.base + "/api/models") as response:
            catalog = json.load(response)
        self.assertEqual(catalog["default"], draw_server.DEFAULT_MODEL_ID)
        self.assertIn("prompt_enhancer", catalog)
        self.assertEqual({m["id"] for m in catalog["models"]},
                         {draw_server.DEFAULT_MODEL_ID, draw_server.FLUX_MODEL_ID})
        with mock.patch.object(draw_server, "run_job") as run_job:
            with self.post(json.dumps({"prompt": "bird"}).encode()) as response:
                qwen_job = json.load(response)
            with self.post(json.dumps({"prompt": "bird", "model": draw_server.FLUX_MODEL_ID,
                                       "profile": "standard", "seed": 12}).encode()) as response:
                flux_job = json.load(response)
        self.assertEqual((qwen_job["model"], qwen_job["profile"]),
                         (draw_server.DEFAULT_MODEL_ID, "medium"))
        self.assertEqual((flux_job["model"], flux_job["profile"]),
                         (draw_server.FLUX_MODEL_ID, "standard"))
        self.assertEqual(run_job.call_count, 2)
        qwen_spec = run_job.call_args_list[0].args[1]
        self.assertEqual(qwen_spec["prompt"], "bird")
        self.assertEqual(qwen_spec["model_id"], draw_server.DEFAULT_MODEL_ID)
        flux_spec = run_job.call_args_list[1].args[1]
        self.assertEqual(flux_spec["model_id"], draw_server.FLUX_MODEL_ID)

    def test_unavailable_unknown_and_unsupported_options_fail_before_queue(self):
        bad = [
            ({"prompt": "bird", "model": "../../other.safetensors"}, 400),
            ({"prompt": "bird", "model": draw_server.FLUX_MODEL_ID,
              "profile": "quality"}, 400),
            ({"prompt": "bird", "model": draw_server.FLUX_MODEL_ID,
              "negative": "blur"}, 400),
            ({"prompt": "bird", "model": draw_server.FLUX_MODEL_ID,
              "reference": "data:image/png;base64,abc"}, 400),
        ]
        with mock.patch.object(draw_server, "run_job") as run_job:
            for body, expected in bad:
                with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                    self.post(json.dumps(body).encode())
                self.assertEqual(error.exception.code, expected)
                error.exception.close()
        run_job.assert_not_called()
        self.assertFalse(draw_server.JOBS)

        with mock.patch.object(draw_server, "available_models", return_value={
            draw_server.DEFAULT_MODEL_ID: True, draw_server.FLUX_MODEL_ID: False}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post(json.dumps({"prompt": "bird", "model": draw_server.FLUX_MODEL_ID}).encode())
            self.assertEqual(error.exception.code, 503)
            error.exception.close()

    def test_status_reports_comfy_queue_position(self):
        draw_server.JOBS["jq"] = {"status": "running", "prompt_id": "p1", "ts": time.time()}
        queue = {"queue_running": [[1, "other"]], "queue_pending": [[2, "p2"], [3, "p1"]]}
        with mock.patch.object(draw_server, "comfy_get", return_value=queue):
            with urllib.request.urlopen(self.base + "/api/status/jq") as response:
                data = json.load(response)
        self.assertEqual(data["queue_ahead"], 2)

        draw_server.JOBS["jd"] = {"status": "done", "prompt_id": "p9", "ts": 1.0,
                                  "started": 1.5, "finished": 2.0}
        with mock.patch.object(draw_server, "comfy_get") as comfy_get:
            with urllib.request.urlopen(self.base + "/api/status/jd") as response:
                data = json.load(response)
        comfy_get.assert_not_called()
        self.assertNotIn("queue_ahead", data)

    def test_invalid_utf8_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(b'{"prompt":"\xff"}')
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
