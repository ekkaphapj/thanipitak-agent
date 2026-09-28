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
    def test_qwen_keeps_its_sampler_and_reference_path(self):
        graph = draw_server.build_graph("portrait", "blur", 768, 123, "quality",
                                        "draw_refs/example.png")
        self.assertEqual(graph["1"]["class_type"], "UnetLoaderGGUF")
        self.assertEqual(graph["4"]["inputs"]["steps"], 30)
        self.assertEqual(graph["4"]["inputs"]["cfg"], 2.5)
        self.assertEqual(graph["3"]["inputs"]["images.image_1"], ["8", 0])
        self.assertEqual(graph["3"]["inputs"]["vae"], ["5", 0])

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
        flux_graph = run_job.call_args.args[1]
        self.assertEqual(flux_graph["1"]["inputs"]["unet_name"], draw_server.FLUX_MODEL)

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
