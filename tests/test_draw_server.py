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
        self.assertEqual(graph["4"]["inputs"]["steps"], 25)
        self.assertEqual(graph["4"]["inputs"]["cfg"], 1.0)
        self.assertEqual(graph["3"]["inputs"]["prompt"], "portrait\nAvoid: blur")
        self.assertEqual(graph["3"]["inputs"]["negative_prompt"], "")
        self.assertEqual(graph["3"]["inputs"]["images.image_1"], ["8", 0])
        self.assertEqual(graph["3"]["inputs"]["vae"], ["5", 0])
        fast = draw_server.build_graph("portrait", "", 768, 123, "fast")
        medium = draw_server.build_graph("portrait", "", 768, 123, "medium")
        self.assertEqual((fast["4"]["inputs"]["steps"], fast["4"]["inputs"]["cfg"]), (12, 1.0))
        self.assertEqual((medium["4"]["inputs"]["steps"], medium["4"]["inputs"]["cfg"]), (20, 1.0))
        self.assertNotIn("Avoid:", fast["3"]["inputs"]["prompt"])

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
        self.assertIn("ตัวหนังสือ", sent["messages"][0]["content"])
        self.assertEqual(sent["temperature"], 0)

    def test_rejects_translation_that_drops_quotes_or_lettering(self):
        bad_quotes = {"choices": [{"message": {"content": "A sign that says hello"}}]}
        bad_books = {"choices": [{"message": {"content":
            "No actual books are visible in the image."}}]}
        kept = {"choices": [{"message": {"content":
            'One orange cat reading a book. No rendered text is visible. The sign reads "สวัสดี".'}}]}
        with mock.patch.object(draw_server, "ENRICHER_URL", "http://127.0.0.1:1/v1/chat/completions"):
            opener = mock.MagicMock()
            opener.return_value.__enter__.return_value.read.side_effect = [
                json.dumps(bad_quotes).encode(),
                json.dumps(bad_books).encode(),
                json.dumps(kept).encode(),
            ]
            with mock.patch.object(urllib.request, "urlopen", opener):
                quoted = 'ป้ายเขียนว่า "สวัสดี"'
                self.assertEqual(draw_server.enhance_prompt(quoted), quoted)
                lettering = "แมวอ่านหนังสือ ไม่มีตัวหนังสือในภาพ"
                self.assertEqual(draw_server.enhance_prompt(lettering), lettering)
                good_source = 'แมวอ่านหนังสือ ไม่มีตัวหนังสือ ป้ายเขียนว่า "สวัสดี"'
                self.assertIn("rendered text", draw_server.enhance_prompt(good_source))
                self.assertIn('"สวัสดี"', draw_server.enhance_prompt(good_source))
        self.assertEqual(opener.call_count, 3)

    def test_rejects_translation_that_drops_the_avoid_line(self):
        source = "แมวส้ม\nAvoid: watermark"
        reply = {"choices": [{"message": {"content": "An orange cat sitting on a chair"}}]}
        with mock.patch.object(draw_server, "ENRICHER_URL", "http://127.0.0.1:1/v1/chat/completions"):
            opener = mock.MagicMock()
            opener.return_value.__enter__.return_value.read.return_value = json.dumps(reply).encode()
            with mock.patch.object(urllib.request, "urlopen", opener):
                self.assertEqual(draw_server.enhance_prompt(source), source)
                self.assertEqual(draw_server.enhance_prompt(source), source)
        self.assertEqual(opener.call_count, 1)

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
        order = []
        with mock.patch.object(draw_server, "enhance_prompt", return_value="an orange cat reading a book"), \
             mock.patch.object(draw_server, "release_ollama_vram", side_effect=lambda: order.append("release")), \
             mock.patch.object(draw_server, "comfy_post", side_effect=lambda *a, **k: order.append("comfy") or fake_post(*a, **k)), \
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
        self.assertEqual(self.graph["3"]["inputs"]["negative_prompt"], "")
        self.assertEqual(self.graph["4"]["inputs"]["steps"], 20)
        self.assertEqual(order, ["release", "comfy"])

    def test_comfy_error_marks_job_failed(self):
        draw_server.JOBS.clear()
        draw_server.JOBS["job2"] = {"status": "queued", "ts": 0}
        spec = {"prompt": "cat", "negative": "", "resolution": 512, "seed": 1,
                "profile": "fast", "reference": None,
                "model_id": draw_server.DEFAULT_MODEL_ID}
        with mock.patch.object(draw_server, "release_ollama_vram"), \
             mock.patch.object(draw_server, "comfy_post", side_effect=RuntimeError("comfy down")), \
             mock.patch.object(draw_server.time, "sleep"):
            draw_server.run_job("job2", spec)
        self.assertEqual(draw_server.JOBS["job2"]["status"], "error")
        self.assertIn("comfy down", draw_server.JOBS["job2"]["error"])


def make_xlsx():
    """Minimal two-sheet workbook: headers, a string row, a number row."""
    shared = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
              'spreadsheetml/2006/main"><si><t>ชื่อสินค้า</t></si><si><t>ยอดขาย</t></si>'
              '<si><t>หมากฮอสชุดพรีเมียม</t></si></sst>')
    wb = ('<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
          'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
          'officeDocument/2006/relationships"><sheets>'
          '<sheet name="ยอดขาย" sheetId="1" r:id="rId1"/>'
          '<sheet name="สรุป" sheetId="2" r:id="rId2"/></sheets></workbook>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="t" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="t" Target="worksheets/sheet2.xml"/>'
            '</Relationships>')
    sheet1 = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.'
              'openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
              '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
              '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>1500.5</v></c></row>'
              '</sheetData></worksheet>')
    sheet2 = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.'
              'openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
              '<row r="1"><c r="A1"><v>42</v></c><c r="B1" t="b"><v>1</v></c></row>'
              '</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
        z.writestr("xl/worksheets/sheet2.xml", sheet2)
    return buf.getvalue()


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

        text, err = draw_server.extract_attachment("a.xlsx", make_xlsx())
        self.assertIsNone(err)
        self.assertIn("=== ชีต: ยอดขาย ===", text)
        self.assertIn("ชื่อสินค้า\tยอดขาย", text)
        self.assertIn("หมากฮอสชุดพรีเมียม\t1500.5", text)
        self.assertIn("=== ชีต: สรุป ===", text)
        self.assertIn("42\tTRUE", text)

    def test_docx_decodes_xml_entities(self):
        doc_xml = "<w:document><w:p>A &amp; B &lt;C&gt;</w:p></w:document>"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", doc_xml)
        text, err = draw_server.extract_attachment("a.docx", buf.getvalue())
        self.assertIsNone(err)
        self.assertIn("A & B <C>", text)

    def test_xlsx_reads_inline_text_numbers_and_shared_cells(self):
        sheet = """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <sheetData>
            <row r="1">
              <c r="A1" t="inlineStr"><is><t>ชื่อ</t></is></c>
              <c r="B1"><v>42</v></c>
              <c r="C1" t="s"><v>0</v></c>
            </row>
          </sheetData>
        </worksheet>"""
        book = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="ขาย" sheetId="1" r:id="rId1"/></sheets></workbook>"""
        rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
        </Relationships>"""
        shared = "<sst><si><t>จากตาราง</t></si></sst>"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", sheet)
            z.writestr("xl/workbook.xml", book)
            z.writestr("xl/_rels/workbook.xml.rels", rels)
            z.writestr("xl/sharedStrings.xml", shared)
        text, err = draw_server.extract_attachment("sales.xlsx", buf.getvalue())
        self.assertIsNone(err)
        self.assertIn("=== ชีต: ขาย ===", text)
        self.assertIn("ชื่อ\t42\tจากตาราง", text)

    def test_xlsx_without_cells_is_an_error(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/workbook.xml", "<workbook/>")
        text, err = draw_server.extract_attachment("empty.xlsx", buf.getvalue())
        self.assertIsNone(text)
        self.assertIn("Excel", err)

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

    def test_xlsx_without_shared_strings_does_not_crash(self):
        sheet = """<worksheet><sheetData><row>
          <c t="inlineStr"><is><t>เฉพาะในเซลล์</t></is></c><c><v>9</v></c>
        </row></sheetData></worksheet>"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", sheet)
        payload = {"messages": [{"role": "user", "content": "",
            "docs": [{"name": "n.xlsx",
                      "data": base64.b64encode(buf.getvalue()).decode()}]}]}
        messages, has_images, notes = draw_server.build_chat_messages(payload)
        self.assertFalse(has_images)
        self.assertEqual(notes, [])
        self.assertIn("เฉพาะในเซลล์", messages[-1]["content"])
        self.assertIn("9", messages[-1]["content"])

        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("xl/workbook.xml", "<workbook/>")
        payload["messages"][-1]["docs"][0]["data"] = base64.b64encode(empty.getvalue()).decode()
        with self.assertRaises(ValueError) as error:
            draw_server.build_chat_messages(payload)
        self.assertIn("Excel", str(error.exception))

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


class ReleaseOllamaTest(unittest.TestCase):
    def test_unloads_only_resident_models(self):
        class Resp:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.payload

        calls = []

        def fake_urlopen(url, *a, **k):
            target = url.full_url if isinstance(url, urllib.request.Request) else url
            if str(target).endswith("/api/ps"):
                body = {"models": [{"name": "typhoon2.5-4b"}, {"model": "qwen3:8b"},
                                   {"name": "typhoon2.5-4b"}]}
                return Resp(json.dumps(body).encode())
            calls.append(json.loads(url.data.decode()))
            return Resp(b'{"done": true, "done_reason": "unload"}')

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            draw_server.release_ollama_vram()
        self.assertEqual([c["model"] for c in calls], ["typhoon2.5-4b", "qwen3:8b"])
        self.assertTrue(all(c["keep_alive"] == 0 and c["prompt"] == "" and c["stream"] is False
                            for c in calls))

    def test_ollama_down_is_not_fatal(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=OSError("down")):
            draw_server.release_ollama_vram()


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


class ChatCatalogTest(unittest.TestCase):
    def test_missing_capabilities_use_show_and_cache_model_details(self):
        draw_server.CHAT_CAPABILITY_CACHE.clear()
        tags = [{"model": "vl:4b", "digest": "vision"},
                {"model": "embed:1", "digest": "embedding"}]

        def show(req, **kwargs):
            self.assertEqual(req.full_url, draw_server.OLLAMA + "/api/show")
            model = json.loads(req.data)["model"]
            caps = ["completion", "vision"] if model == "vl:4b" else ["embedding"]
            return io.BytesIO(json.dumps({"capabilities": caps}).encode())

        try:
            with mock.patch.object(draw_server, "ollama_tags", return_value=tags), \
                 mock.patch.object(urllib.request, "urlopen", side_effect=show) as upstream:
                catalog = draw_server.chat_models()
                self.assertEqual(catalog["models"], [{"id": "vl:4b", "label": "vl:4b", "vision": True}])
                self.assertEqual(draw_server.chat_models(), catalog)
                self.assertEqual(upstream.call_count, 2)
        finally:
            draw_server.CHAT_CAPABILITY_CACHE.clear()


class WarmModelTest(unittest.TestCase):
    def test_warmup_matches_chat_runner_options(self):
        stream = OllamaStream([{"done": True}])
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=stream) as urlopen:
            draw_server.warm_chat_model("typhoon2.5-4b")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["model"], "typhoon2.5-4b")
        self.assertEqual(sent["keep_alive"], draw_server.CHAT_KEEP_ALIVE)
        self.assertEqual(sent["options"]["num_ctx"], draw_server.CHAT_NUM_CTX)
        self.assertNotIn("prompt", sent)

    def test_chat_model_ready_reads_ps(self):
        class PsResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"models": [
                    {"name": "hot:1", "size": 10, "size_vram": 8,
                     "context_length": draw_server.CHAT_NUM_CTX},
                    {"name": "cpu:1", "size": 10, "size_vram": 2,
                     "context_length": draw_server.CHAT_NUM_CTX},
                    {"name": "wrongctx:1", "size": 10, "size_vram": 8,
                     "context_length": draw_server.CHAT_NUM_CTX // 2},
                ]}).encode()

        real = urllib.request.urlopen
        def selective(url, *a, **k):
            target = url.full_url if isinstance(url, urllib.request.Request) else url
            if target.startswith(draw_server.OLLAMA):
                return PsResponse()
            return real(url, *a, **k)
        with mock.patch.object(urllib.request, "urlopen", side_effect=selective):
            self.assertTrue(draw_server.chat_model_ready("hot:1"))
            self.assertFalse(draw_server.chat_model_ready("cpu:1"))
            self.assertFalse(draw_server.chat_model_ready("wrongctx:1"))
            self.assertFalse(draw_server.chat_model_ready("missing:1"))

    def test_warmup_json_error_is_not_treated_as_ready(self):
        with mock.patch.object(urllib.request, "urlopen", return_value=OllamaStream([{"error": "cannot load model"}])):
            with self.assertRaisesRegex(RuntimeError, "cannot load model"):
                draw_server.warm_chat_model("qwen3:8b")


class ChatNumCtxOverrideTest(unittest.TestCase):
    def test_parse_valid_and_empty(self):
        self.assertEqual(draw_server._parse_ctx_overrides(""), {})
        self.assertEqual(draw_server._parse_ctx_overrides("   "), {})
        self.assertEqual(draw_server._parse_ctx_overrides('{"m:1": 4096}'),
                         {"m:1": 4096})

    def test_parse_rejects_malformed(self):
        for raw in ('{not json', '[1, 2]', '{"m": 0}', '{"m": -4096}',
                    '{"m": 1.5}', '{"m": "big"}', '{"": 4096}'):
            with self.assertRaises(SystemExit, msg=raw):
                draw_server._parse_ctx_overrides(raw)

    def test_lookup_matches_canonical_forms(self):
        overrides = {"mini:1": 4096, "bare": 8192, "late:latest": 2048}
        with mock.patch.object(draw_server, "CHAT_NUM_CTX_OVERRIDES", overrides):
            self.assertEqual(draw_server.chat_num_ctx("mini:1"), 4096)
            # a different tag is a different model, so it keeps the default
            self.assertEqual(draw_server.chat_num_ctx("mini"),
                             draw_server.CHAT_NUM_CTX)
            self.assertEqual(draw_server.chat_num_ctx("bare"), 8192)
            self.assertEqual(draw_server.chat_num_ctx("bare:latest"), 8192)
            self.assertEqual(draw_server.chat_num_ctx("late:latest"), 2048)
            self.assertEqual(draw_server.chat_num_ctx("other:9"),
                             draw_server.CHAT_NUM_CTX)

    def test_warmup_uses_override(self):
        stream = OllamaStream([{"done": True}])
        with mock.patch.object(draw_server, "CHAT_NUM_CTX_OVERRIDES",
                               {"typhoon2.5-4b": 4096}), \
             mock.patch.object(urllib.request, "urlopen",
                               return_value=stream) as urlopen:
            draw_server.warm_chat_model("typhoon2.5-4b:latest")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["options"]["num_ctx"], 4096)

    def test_ready_check_uses_override(self):
        class PsResponse:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"models": [
                    {"name": "hot:latest", "size": 10, "size_vram": 8,
                     "context_length": 4096},
                    {"name": "wrongctx:latest", "size": 10, "size_vram": 8,
                     "context_length": 8192},
                ]}).encode()

        with mock.patch.object(draw_server, "CHAT_NUM_CTX_OVERRIDES",
                               {"hot": 4096}), \
             mock.patch.object(urllib.request, "urlopen",
                               return_value=PsResponse()):
            self.assertTrue(draw_server.chat_model_ready("hot:latest"))
            self.assertFalse(draw_server.chat_model_ready("wrongctx:latest"))


def login(base):
    """Exchange the shared password for a bearer token against a live server."""
    request = urllib.request.Request(
        base + "/api/login", json.dumps({"password": draw_server.DRAW_PASSWORD}).encode(),
        {"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)["token"]


class ChatHttpTest(unittest.TestCase):
    def setUp(self):
        draw_server.JOBS.clear()
        self.server = draw_server.ThreadingHTTPServer(("127.0.0.1", 0), draw_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.auth = {"Authorization": "Bearer " + login(self.base)}
        self.tags = mock.patch.object(draw_server, "ollama_tags", return_value=[
            {"model": "qwen3:8b", "capabilities": ["completion", "thinking"]},
            {"model": "vl:4b", "capabilities": ["completion", "vision"]},
            {"model": "emb", "capabilities": ["embedding"]}])
        self.tags.start()

    def tearDown(self):
        draw_server.CHAT_CAPABILITY_CACHE.clear()
        self.tags.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.JOBS.clear()
        draw_server.SESSIONS.clear()

    def test_models_endpoint_filters_embeddings_and_flags_vision(self):
        request = urllib.request.Request(self.base + "/api/chat/models",
                                         headers=self.auth)
        with urllib.request.urlopen(request) as response:
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
            {"Content-Type": "application/json", **self.auth}, method="POST")
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
             mock.patch.object(draw_server, "chat_model_ready", return_value=False), \
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

    def test_per_model_ctx_override_applies_to_stream_and_usage(self):
        lines = [
            {"message": {"content": "ok"}, "done": False},
            {"message": {"content": ""}, "done": True,
             "prompt_eval_count": 10, "eval_count": 2},
        ]
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(draw_server, "CHAT_NUM_CTX_OVERRIDES",
                               {"qwen3:8b": 4096}), \
             self.ollama_mock(lines) as urlopen, \
             mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                streamed = response.read().decode()
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["options"]["num_ctx"], 4096)
        rows = [json.loads(l) for l in streamed.splitlines() if l.strip()]
        self.assertEqual(rows[-2]["usage"]["ctx"], 4096)

    def test_untagged_model_id_resolves_to_latest(self):
        lines = [
            {"message": {"content": "ok"}, "done": False},
            {"message": {"content": ""}, "done": True, "eval_count": 1},
        ]
        body = {"model": "typhoon2-8b",
                "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(draw_server, "ollama_tags", return_value=[
                {"model": "typhoon2-8b:latest", "capabilities": ["completion"]}]), \
             self.ollama_mock(lines) as urlopen, \
             mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                response.read()
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["model"], "typhoon2-8b:latest")

    def test_unknown_model_is_rejected(self):
        body = {"model": "nope", "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.chat_post(body)
        self.assertEqual(caught.exception.code, 400)

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

    def test_loaded_model_skips_free_and_warm(self):
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        lines = [{"message": {"content": "ok"}, "done": True,
                  "prompt_eval_count": 5, "eval_count": 3}]
        with self.ollama_mock(lines) as urlopen, \
             mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
             mock.patch.object(draw_server, "free_comfy_vram") as free, \
             mock.patch.object(draw_server, "warm_chat_model") as warm:
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines() if l.strip()]
        free.assert_not_called()
        warm.assert_not_called()
        self.assertNotIn("stage", [k for r in rows for k in r])
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

    def test_upstream_error_is_forwarded_without_a_false_done(self):
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        chunks = [
            {"message": {"content": "บางส่วน"}, "done": False},
            {"error": "model failed during inference"},
        ]
        with self.ollama_mock(chunks), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines() if l.strip()]
        self.assertEqual("".join(r.get("delta", "") for r in rows), "บางส่วน")
        self.assertEqual(rows[-1]["error"], "model failed during inference")
        self.assertFalse(any(r.get("done") for r in rows))

    def test_content_on_the_done_chunk_is_kept(self):
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        chunks = [{"message": {"content": "ok"}, "done": True, "eval_count": 1}]
        with self.ollama_mock(chunks), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines() if l.strip()]
        self.assertEqual("".join(r.get("delta", "") for r in rows), "ok")
        self.assertTrue(rows[-1]["done"])

    def test_tags_without_capabilities_use_show_and_cache(self):
        draw_server.CHAT_CAPABILITY_CACHE.clear()
        tags = [{"model": "vl:4b"}, {"model": "emb"}]
        shows = {"vl:4b": ["completion", "vision"], "emb": ["embedding"]}

        class Resp:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.payload

        def fake_urlopen(url, *args, **kwargs):
            target = url.full_url if isinstance(url, urllib.request.Request) else url
            self.assertTrue(str(target).endswith("/api/show"))
            model_id = json.loads(url.data.decode())["model"]
            return Resp(json.dumps({"capabilities": shows[model_id]}).encode())

        with mock.patch.object(draw_server, "ollama_tags", return_value=tags), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen) as show:
            first = draw_server.chat_models()
            second = draw_server.chat_models()
        self.assertEqual([(m["id"], m["vision"]) for m in first["models"]], [("vl:4b", True)])
        self.assertEqual(second, first)
        self.assertEqual(show.call_count, 2)  # one show per model, then the cache
        draw_server.CHAT_CAPABILITY_CACHE.clear()

    def test_multipage_pdf_is_accepted_on_a_vision_model(self):
        pages = ["QQ=="] * 6
        body = {"model": "vl:4b", "messages": [{"role": "user", "content": "อ่าน",
            "docs": [{"name": "a.pdf", "data": base64.b64encode(b"%PDF-1.4").decode()}]}]}
        chunks = [{"message": {"content": "ok"}, "done": True}]
        with mock.patch.object(draw_server, "pdf_page_images", return_value=pages), \
             self.ollama_mock(chunks) as urlopen, \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines() if l.strip()]
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(len(sent["messages"][-1]["images"]), 6)
        self.assertTrue(rows[-1]["done"])

        body["model"] = "vl:4b"
        ok = [{"message": {"content": "ok"}, "done": True}]
        with self.ollama_mock(ok), \
             mock.patch.object(draw_server, "free_comfy_vram"), \
             mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                self.assertTrue(json.loads(response.read().decode().splitlines()[-1])["done"])


    def test_pdf_only_reaches_poppler_before_empty_message_check(self):
        for model, vision in (("vl:4b", True), ("qwen3:8b", False)):
            with self.subTest(model=model):
                body = {"model": model, "messages": [{"role": "user", "content": "",
                        "docs": [{"name": "scan.pdf", "data": "QUJD"}]}]}
                with self.ollama_mock([{"message": {"content": "อ่านได้"}, "done": True}]) as upstream, \
                     mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
                     mock.patch.object(draw_server, "pdf_page_images", return_value=["AAA"]) as render, \
                     mock.patch.object(draw_server, "pdf_text_via_poppler", return_value="ข้อความจาก PDF ภาษาไทยที่อ่านได้") as extract:
                    with self.chat_post(body) as response:
                        rows = [json.loads(l) for l in response.read().decode().splitlines()]
                sent = json.loads(upstream.call_args.args[0].data)
                self.assertTrue(rows[-1]["done"])
                if vision:
                    render.assert_called_once()
                    self.assertEqual(sent["messages"][-1]["images"], ["AAA"])
                else:
                    extract.assert_called_once()
                    self.assertIn("ข้อความจาก PDF", sent["messages"][-1]["content"])

    def test_six_page_pdf_streams_and_preserves_final_content(self):
        pages = [f"page{i}" for i in range(6)]
        body = {"model": "vl:4b", "messages": [{"role": "user", "content": "สรุป",
                "docs": [{"name": "report.pdf", "data": "QUJD"}]}]}
        with self.ollama_mock([{"message": {"content": "ครบหกหน้า"}, "done": True}]) as upstream, \
             mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
             mock.patch.object(draw_server, "pdf_page_images", return_value=pages):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines()]
        sent = json.loads(upstream.call_args.args[0].data)
        self.assertEqual(sent["messages"][-1]["images"], pages)
        self.assertEqual("".join(r.get("delta", "") for r in rows), "ครบหกหน้า")
        self.assertTrue(rows[-1]["done"])

    def test_warmup_failure_emits_error_and_stops_heartbeat(self):
        before = set(threading.enumerate())
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(draw_server, "CHAT_HEARTBEAT", 0.01), \
             mock.patch.object(draw_server, "chat_model_ready", return_value=False), \
             mock.patch.object(draw_server, "free_comfy_vram", return_value=True), \
             mock.patch.object(draw_server, "warm_chat_model", side_effect=OSError("warmup failed")):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines()]
        self.assertIn("warmup failed", rows[-1].get("error", ""))
        self.assertFalse(any(r.get("done") for r in rows))
        self.assertFalse(any(t not in before and t.name == "chat-heartbeat"
                             for t in threading.enumerate()))

    def test_stream_errors_and_premature_eof_are_not_success(self):
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "hi"}]}
        for chunks in ([{"message": {"content": "partial"}, "done": False},
                        {"error": "runner failed"}],
                       [{"message": {"content": "partial"}, "done": False}]):
            with self.subTest(chunks=chunks), self.ollama_mock(chunks), \
                 mock.patch.object(draw_server, "chat_model_ready", return_value=True):
                with self.chat_post(body) as response:
                    rows = [json.loads(l) for l in response.read().decode().splitlines()]
                self.assertIn("error", rows[-1])
                self.assertFalse(any(r.get("done") for r in rows))
                self.assertEqual("".join(r.get("delta", "") for r in rows), "partial")

    def test_invalid_pdf_request_is_rejected_before_rendering(self):
        for docs in ([None], [{"name": "x.pdf", "data": 123}],
                     [{"name": "x.pdf", "data": "QUJD"}] * 5):
            with self.subTest(docs=docs), mock.patch.object(draw_server, "pdf_page_images") as render:
                body = {"model": "vl:4b", "messages": [{"role": "user", "content": "hi", "docs": docs}]}
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.chat_post(body)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                render.assert_not_called()

    def test_empty_workbook_returns_attachment_error_instead_of_disconnect(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("xl/worksheets/sheet1.xml", "<worksheet/>")
        body = {"model": "qwen3:8b", "messages": [{"role": "user", "content": "",
                "docs": [{"name": "broken.xlsx", "data": base64.b64encode(buf.getvalue()).decode()}]}]}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.chat_post(body)
        self.assertEqual(error.exception.code, 400)
        self.assertIn("broken.xlsx", json.loads(error.exception.read().decode())["error"])
        error.exception.close()


class PdfPreprocessTest(unittest.TestCase):
    def test_combined_image_budget_caps_pages_and_reports_omissions(self):
        body = {"messages": [{"role": "user", "content": "",
                "images": ["user"] * 4, "docs": [
                    {"name": "one.pdf", "data": "QUJD"},
                    {"name": "two.pdf", "data": "QUJD"}]}]}
        with mock.patch.object(draw_server, "pdf_page_images", return_value=["page"] * 6) as render, \
             mock.patch.object(draw_server, "pdf_text_via_poppler", return_value="ข้อความจากเอกสารไฟล์ที่สองที่อ่านได้"):
            notes = draw_server.preprocess_pdf_docs(body, vision=True)
        self.assertEqual(len(body["messages"][-1]["images"]), draw_server.CHAT_MAX_MODEL_IMAGES)
        render.assert_called_once()
        self.assertTrue(any("ไม่ได้ส่งอีก 2 หน้า" in n for n in notes))
        messages, has_images, errors = draw_server.build_chat_messages(
            body, image_limit=draw_server.CHAT_MAX_MODEL_IMAGES)
        self.assertTrue(has_images)
        self.assertEqual(errors, [])
        self.assertIn("ไฟล์แนบ two.pdf", messages[-1]["content"])

    def test_oversized_pdf_is_not_passed_to_poppler(self):
        body = {"messages": [{"role": "user", "content": "read",
                "docs": [{"name": "big.pdf", "data": "QUJD"}]}]}
        with mock.patch.object(draw_server, "CHAT_MAX_FILE", 2), \
             mock.patch.object(draw_server, "pdf_page_images") as render, \
             mock.patch.object(draw_server, "pdf_text_via_poppler") as extract:
            draw_server.preprocess_pdf_docs(body, vision=True)
            _, _, errors = draw_server.build_chat_messages(body)
        render.assert_not_called()
        extract.assert_not_called()
        self.assertTrue(any("big.pdf" in n for n in errors))

    def test_rescue_mojibake(self):
        clean = "รายงานภาพรวมองค์กรประจำปี"
        self.assertEqual(draw_server._rescue_mojibake(clean), clean)
        broken = "เธฃเธฒเธขเธเธฒเธฃเธญเธเธดเธเธ"  # U+0E00 mojibake of รายงาน
        fixed = draw_server._rescue_mojibake(broken + " ok")
        self.assertEqual(fixed.count("\u0e00"), 0)

    def test_vision_model_gets_page_images(self):
        body = {"messages": [{"role": "user", "content": "ตีความ",
                              "docs": [{"name": "chart.pdf", "data": "QUJD"}]}]}
        with mock.patch.object(draw_server, "pdf_page_images",
                               return_value=["AAA", "BBB"]):
            notes = draw_server.preprocess_pdf_docs(body, vision=True)
        self.assertEqual(body["messages"][-1]["images"], ["AAA", "BBB"])
        self.assertEqual(body["messages"][-1]["docs"], [])
        self.assertEqual(len(notes), 1)
        self.assertIn("แปลงเป็นภาพ 2 หน้า", notes[0])

    def test_text_model_gets_poppler_text(self):
        body = {"messages": [{"role": "user", "content": "สรุป",
                              "docs": [{"name": "doc.pdf", "data": "QUJD"}]}]}
        with mock.patch.object(draw_server, "pdf_text_via_poppler",
                               return_value="หน้าหนึ่ง\nหน้าสอง\nข้อมูลเพิ่ม"):
            notes = draw_server.preprocess_pdf_docs(body, vision=False)
        doc = body["messages"][-1]["docs"][0]
        self.assertNotIn("data", doc)
        self.assertEqual(doc["text"], "หน้าหนึ่ง\nหน้าสอง\nข้อมูลเพิ่ม")
        self.assertEqual(notes, [])
        messages, has_images, err_notes = draw_server.build_chat_messages(body)
        self.assertIn("หน้าหนึ่ง", messages[-1]["content"])
        self.assertIn("ไฟล์แนบ doc.pdf", messages[-1]["content"])

    def test_no_poppler_and_no_text_keeps_doc_for_standard_note(self):
        body = {"messages": [{"role": "user", "content": "ดูไฟล์",
                              "docs": [{"name": "x.pdf", "data": "QUJD"}]}]}
        with mock.patch.object(draw_server, "pdf_page_images", return_value=[]), \
             mock.patch.object(draw_server, "pdf_text_via_poppler", return_value=""):
            notes = draw_server.preprocess_pdf_docs(body, vision=True)
        self.assertEqual(notes, [])
        self.assertEqual(body["messages"][-1]["docs"][0]["data"], "QUJD")


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
        self.auth = {"Authorization": "Bearer " + login(self.base)}
        self.available = mock.patch.object(draw_server, "available_models", return_value={
            draw_server.DEFAULT_MODEL_ID: True, draw_server.FLUX_MODEL_ID: True})
        self.available.start()

    def tearDown(self):
        self.available.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.JOBS.clear()
        draw_server.SESSIONS.clear()

    def post(self, body):
        request = urllib.request.Request(
            self.base + "/api/generate", body,
            {"Content-Type": "application/json", **self.auth}, method="POST")
        return urllib.request.urlopen(request, timeout=2)

    def get(self, path):
        request = urllib.request.Request(self.base + path, headers=self.auth)
        return urllib.request.urlopen(request, timeout=5)

    def test_catalog_and_model_selection(self):
        with self.get("/api/models") as response:
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
            with self.get("/api/status/jq") as response:
                data = json.load(response)
        self.assertEqual(data["queue_ahead"], 2)

        draw_server.JOBS["jd"] = {"status": "done", "prompt_id": "p9", "ts": 1.0,
                                  "started": 1.5, "finished": 2.0}
        with mock.patch.object(draw_server, "comfy_get") as comfy_get:
            with self.get("/api/status/jd") as response:
                data = json.load(response)
        comfy_get.assert_not_called()
        self.assertNotIn("queue_ahead", data)

    def test_explicit_zero_seed_is_kept(self):
        with mock.patch.object(draw_server, "run_job") as run_job:
            with self.post(json.dumps({"prompt": "bird", "seed": 0}).encode()) as response:
                self.assertEqual(response.status, 200)
            for _ in range(50):
                if run_job.called:
                    break
                time.sleep(0.01)
            self.assertEqual(run_job.call_args.args[1]["seed"], 0)

    def test_invalid_utf8_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post(b'{"prompt":"\xff"}')
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


class MusicGraphTest(unittest.TestCase):
    def test_graph_follows_yue2_template(self):
        graph = draw_server.build_yue2_graph(
            "pop ballad", "[Verse]\nhello", 60, 123, planning=True)
        self.assertEqual(graph["1"]["class_type"], "CheckpointLoaderSimple")
        self.assertEqual(graph["1"]["inputs"]["ckpt_name"], draw_server.YUE2_CHECKPOINT)
        self.assertEqual(graph["2"]["class_type"], "YuE2GenerateABC")
        self.assertEqual(graph["3"]["inputs"]["abc"], ["2", 0])
        self.assertEqual(graph["3"]["inputs"]["max_duration"], 60.0)
        self.assertEqual(graph["5"]["inputs"]["seconds"], ["3", 1])
        sampler = graph["6"]["inputs"]
        self.assertEqual((sampler["steps"], sampler["cfg"],
                          sampler["sampler_name"], sampler["scheduler"]), (32, 1.0, "dpm_2", "sgm_uniform"))
        self.assertEqual(sampler["negative"], ["4", 0])
        self.assertEqual(graph["8"]["class_type"], "SaveAudioMP3")

    def test_graph_without_planning_skips_abc_node(self):
        graph = draw_server.build_yue2_graph(
            "lofi", "", 30, 7, planning=False)
        self.assertNotIn("2", graph)
        self.assertEqual(graph["3"]["inputs"]["abc"], "")


class MusicHttpTest(unittest.TestCase):
    def setUp(self):
        draw_server.JOBS.clear()
        self.server = draw_server.ThreadingHTTPServer(("127.0.0.1", 0), draw_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.auth = {"Authorization": "Bearer " + login(self.base)}
        self.music_on = mock.patch.object(draw_server, "music_available", return_value=True)
        self.music_on.start()

    def tearDown(self):
        self.music_on.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.JOBS.clear()
        draw_server.SESSIONS.clear()

    def post_music(self, body):
        request = urllib.request.Request(
            self.base + "/api/music/generate", json.dumps(body).encode(),
            {"Content-Type": "application/json", **self.auth}, method="POST")
        return urllib.request.urlopen(request, timeout=5)

    def test_generate_queues_job_and_reports_music_catalog(self):
        with urllib.request.urlopen(urllib.request.Request(
                self.base + "/api/models", headers=self.auth)) as response:
            catalog = json.load(response)
        self.assertTrue(catalog["music_available"])
        with mock.patch.object(draw_server, "run_music_job") as run_music:
            with self.post_music({"style": "pop", "lyrics": "[Verse]\nla",
                                  "seconds": 30, "planning": False,
                                  "seed": 5}) as response:
                job = json.load(response)
        self.assertTrue(job["id"])
        self.assertEqual(job["seed"], 5)
        for _ in range(50):
            if run_music.called:
                break
            time.sleep(0.01)
        spec = run_music.call_args.args[1]
        self.assertEqual(spec, {"style": "pop", "lyrics": "[Verse]\nla",
                                "seconds": 30, "seed": 5, "planning": False})

    def test_validation_and_missing_model_fail_before_queue(self):
        with mock.patch.object(draw_server, "run_music_job") as run_music:
            for body in [{"lyrics": "no style"},
                         {"style": "x" * 801},
                         {"style": "pop", "seconds": 10},
                         {"style": "pop", "seconds": 9999}]:
                with self.subTest(body=body), \
                        self.assertRaises(urllib.error.HTTPError) as error:
                    self.post_music(body)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
        run_music.assert_not_called()
        self.assertFalse(draw_server.JOBS)

        self.music_on.stop()
        with mock.patch.object(draw_server, "music_available", return_value=False):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post_music({"style": "pop"})
        self.assertEqual(error.exception.code, 503)
        error.exception.close()


class LoginFlowTest(unittest.TestCase):
    def setUp(self):
        self.server = draw_server.ThreadingHTTPServer(("127.0.0.1", 0), draw_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.SESSIONS.clear()

    def post_json(self, path, body, headers=None):
        request = urllib.request.Request(
            self.base + path, json.dumps(body).encode(),
            {"Content-Type": "application/json", **(headers or {})}, method="POST")
        return urllib.request.urlopen(request, timeout=5)

    def test_login_rejects_wrong_password(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post_json("/api/login", {"password": "not-the-password"})
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        self.assertFalse(draw_server.SESSIONS)

    def test_api_locked_without_token_and_opened_by_login(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base + "/api/models")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        with self.post_json("/api/login", {"password": draw_server.DRAW_PASSWORD}) as response:
            data = json.load(response)
        self.assertTrue(data["token"])
        auth = {"Authorization": "Bearer " + data["token"]}

        with self.assertRaises(urllib.error.HTTPError) as error:
            self.post_json("/api/generate", {"prompt": "bird"})
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        request = urllib.request.Request(self.base + "/api/session", headers=auth)
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.load(response)["ok"])

        request = urllib.request.Request(self.base + "/api/session")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
