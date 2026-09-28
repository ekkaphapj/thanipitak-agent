"""Focused checks for the separate ComfyUI draw service."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
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

    def test_invalid_utf8_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(b'{"prompt":"\xff"}')
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
