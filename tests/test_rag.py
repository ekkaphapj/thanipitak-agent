"""Per-user knowledge base: uploads, chunking, retrieval and chat injection."""
import base64
import io
import json
import sys
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import draw_server
from test_draw_server import ChatServerTest, patch_draw_db


def b64(data):
    return base64.b64encode(data).decode()


def docx_bytes(paragraphs):
    """Minimal Word file: one paragraph per entry in word/document.xml."""
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    document = ('<?xml version="1.0"?><w:document xmlns:w="w">'
                f"<w:body>{body}</w:body></w:document>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("word/document.xml", document)
    return out.getvalue()


def pptx_bytes(slides):
    """Minimal PowerPoint: one text box per slide."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for i, text in enumerate(slides, 1):
            shape = ('<p:sp xmlns:a="a" xmlns:p="p"><p:txBody>'
                     f'<a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>')
            z.writestr(f"ppt/slides/slide{i}.xml",
                       f'<?xml version="1.0"?><p:sld xmlns:p="p">{shape}</p:sld>')
    return out.getvalue()


PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8ffff3f0005fe02fea735c9cb"
    "0000000049454e44ae426082")


class DocsTestBase(ChatServerTest):
    def upload(self, name, data, headers=None):
        request = urllib.request.Request(
            self.base + "/api/docs", json.dumps({"name": name, "data": b64(data)}).encode(),
            {"Content-Type": "application/json", **(headers or self.auth)}, method="POST")
        return urllib.request.urlopen(request, timeout=5)

    def docs_list(self, headers=None):
        request = urllib.request.Request(
            self.base + "/api/docs", headers=headers or self.auth)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)["documents"]


class KnowledgeDocsTest(DocsTestBase):
    def test_upload_txt_lists_chunks_and_serves_original_file(self):
        text = "ธานีพิทักษ์คือระบบ AI ภายในประเทศ " + ("ข้อมูลส่วนเพิ่ม " * 80)
        with self.upload("knowledge.txt", text.encode()) as response:
            data = json.load(response)
        self.assertTrue(data["ok"])
        self.assertGreaterEqual(data["chunks"], 1)
        docs = self.docs_list()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["name"], "knowledge.txt")
        self.assertEqual(docs[0]["kind"], "text")
        self.assertEqual(docs[0]["chunks"], data["chunks"])

        # the original file is downloadable byte for byte
        request = urllib.request.Request(
            self.base + f"/api/docs/{docs[0]['id']}/file", headers=self.auth)
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.read(), text.encode())
        self.assertEqual(response.headers["Content-Type"], "text/plain")

    def test_upload_office_formats_extract_text(self):
        with self.upload("รายงาน.docx",
                         docx_bytes(["แผนงานธานีพิทักษ์", "งบประมาณปี 2569"])) as response:
            self.assertEqual(json.load(response)["chunks"], 1)
        with self.upload("deck.pptx", pptx_bytes(["สไลด์แรก", "สรุปงาน"])) as response:
            self.assertGreaterEqual(json.load(response)["chunks"], 1)
        docs = {d["name"]: d for d in self.docs_list()}
        self.assertIn("รายงาน.docx", docs)
        self.assertIn("deck.pptx", docs)

    def test_upload_image_counts_as_image_kind(self):
        with self.upload("photo.png", PNG_1PX) as response:
            data = json.load(response)
        self.assertEqual(data["chunks"], 0)
        doc = self.docs_list()[0]
        self.assertEqual(doc["kind"], "image")

    def test_rejects_unsupported_and_oversize_and_quota(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.upload("virus.exe", b"MZ")
        self.assertEqual(error.exception.code, 400)
        error.exception.close()

        with mock.patch.object(draw_server, "CHAT_MAX_FILE", 8):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.upload("big.txt", b"x" * 16)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()

        # quota: fill almost everything, then a doc that does not fit
        user = draw_server.get_user_by_username("tester")
        with mock.patch.object(draw_server, "USER_QUOTA", 200):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.upload("huge.txt", b"y" * 500)
            self.assertEqual(error.exception.code, 400)
            body = json.loads(error.exception.read())
            self.assertIn("พื้นที่", body["error"])
        self.assertEqual(user["username"], "tester")

    def test_docs_are_private_between_users(self):
        with self.upload("mine.txt", b"my secret knowledge"):
            pass
        self.assertEqual(len(self.docs_list()), 1)

        # a second approved account sees nothing and cannot delete ours
        request = urllib.request.Request(
            self.base + "/api/register",
            json.dumps({"fullname": "B", "username": "userb", "password": "pw"}).encode(),
            {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5):
            pass
        request = urllib.request.Request(
            self.base + "/api/approve/login",
            json.dumps({"code": draw_server.APPROVE_CODE}).encode(),
            {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            admin = json.load(response)
        request = urllib.request.Request(
            self.base + "/api/admin/approve",
            json.dumps({"username": "userb"}).encode(),
            {"Content-Type": "application/json",
             "Authorization": "Bearer " + admin["token"]}, method="POST")
        with urllib.request.urlopen(request, timeout=5):
            pass
        request = urllib.request.Request(
            self.base + "/api/login",
            json.dumps({"username": "userb", "password": "pw"}).encode(),
            {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            other = {"Authorization": "Bearer " + json.load(response)["token"]}

        self.assertEqual(self.docs_list(other), [])
        doc_id = self.docs_list()[0]["id"]
        request = urllib.request.Request(
            self.base + f"/api/docs/{doc_id}", headers=other, method="DELETE")
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()
        self.assertEqual(len(self.docs_list()), 1)

    def test_delete_one_and_all_docs_frees_quota(self):
        with self.upload("a.txt", b"aaaa"):
            pass
        with self.upload("b.txt", b"bbbb"):
            pass
        request = urllib.request.Request(self.base + "/api/storage", headers=self.auth)
        with urllib.request.urlopen(request, timeout=5) as response:
            before = json.load(response)
        self.assertEqual(before["doc_count"], 2)
        self.assertGreaterEqual(before["docs_bytes"], 8)

        first = self.docs_list()[0]["id"]
        request = urllib.request.Request(
            self.base + f"/api/docs/{first}", headers=self.auth, method="DELETE")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["removed"], 1)
        self.assertEqual(len(self.docs_list()), 1)

        request = urllib.request.Request(
            self.base + "/api/docs", headers=self.auth, method="DELETE")
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response)["removed"], 1)
        request = urllib.request.Request(self.base + "/api/storage", headers=self.auth)
        with urllib.request.urlopen(request, timeout=5) as response:
            after = json.load(response)
        self.assertEqual(after["doc_count"], 0)
        self.assertEqual(after["docs_bytes"], 0)


class RagRetrieveTest(unittest.TestCase):
    def setUp(self):
        patch_draw_db(self)

    def test_chunk_text_splits_with_overlap(self):
        text = "\n".join(f"บรรทัดที่ {i} " + "ก" * 50 for i in range(60))
        chunks = draw_server.chunk_text(text, size=200, overlap=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 200 for c in chunks))
        self.assertTrue(all(c.strip() for c in chunks))
        joined = "".join(chunks)
        self.assertIn("บรรทัดที่ 0", joined)   # first line kept
        self.assertIn("บรรทัดที่ 59", joined)  # last line kept
        self.assertGreater(len(joined), len(text) * 0.95)  # nothing lost
        # blank-line runs are collapsed
        self.assertNotIn("\n\n\n", joined)

    def test_retrieval_ranks_the_matching_thai_chunk_first(self):
        user = draw_server.register_user("คน ทดสอบ", "raguser", "pw1234")
        draw_server.approve_user(user)
        with mock.patch.object(draw_server, "used_bytes", return_value=0):
            draw_server.add_knowledge_doc((draw_server.get_user_by_username("raguser")["id"],
                                           "raguser"), "a.txt",
                                          "แผนฉุกเฉินธานีพิทักษ์ใช้เลขหมาย 191 ในการแจ้งเหตุ".encode())
            draw_server.add_knowledge_doc((draw_server.get_user_by_username("raguser")["id"],
                                           "raguser"), "b.txt",
                                          "สูตรขนมปังกินธัชมีแป้ง 500 กรัม".encode())
        hits = draw_server.rag_retrieve(
            draw_server.get_user_by_username("raguser")["id"],
            "เกิดเหตุฉุกเฉินต้องโทรหมายเลขอะไร")
        self.assertTrue(hits)
        self.assertIn("191", hits[0][1])
        self.assertEqual(hits[0][0], "a.txt")

    def test_retrieval_is_scoped_to_the_owner(self):
        for name in ("raguser2", "other"):
            draw_server.register_user("คน " + name, name, "pw1234")
            draw_server.approve_user(name)
        owner = draw_server.get_user_by_username("raguser2")
        with mock.patch.object(draw_server, "used_bytes", return_value=0):
            draw_server.add_knowledge_doc((owner["id"], "raguser2"), "s.txt",
                                          b"secret thanipitak tunnel code")
        self.assertEqual(draw_server.rag_retrieve(
            draw_server.get_user_by_username("other")["id"], "thanipitak tunnel"), [])


class ChatRagTest(DocsTestBase):
    def chat(self, body):
        lines = [
            {"message": {"content": "ตอบแล้ว"}, "done": False},
            {"message": {"content": ""}, "done": True,
             "prompt_eval_count": 3, "eval_count": 2},
        ]
        with self.ollama_mock(lines) as urlopen, \
                mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
                mock.patch.object(draw_server, "free_comfy_vram"), \
                mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                rows = [json.loads(l) for l in response.read().decode().splitlines()
                        if l.strip()]
        sent = json.loads(urlopen.call_args.args[0].data)
        return rows, sent

    def test_rag_flag_injects_knowledge_into_the_model_message(self):
        with self.upload("handbook.txt", "หมายเลขฉุกเฉินของธานีพิทักษ์คือ 191 และ 1669".encode()):
            pass
        rows, sent = self.chat({"model": "qwen3:8b", "rag": True,
                                "messages": [{"role": "user", "content": "เบอร์ฉุกเฉินคืออะไร"}]})
        self.assertIn("แหล่งความรู้", sent["messages"][-1]["content"])
        self.assertIn("191", sent["messages"][-1]["content"])
        notes = [n for r in rows if "notes" in r for n in r["notes"]]
        self.assertTrue(any("แหล่งความรู้" in n for n in notes))
        # the saved history keeps the human question, not the injected context
        session = next(r["chat_session"] for r in rows if "chat_session" in r)
        request = urllib.request.Request(self.base + "/api/chats/" + session,
                                         headers=self.auth)
        with urllib.request.urlopen(request, timeout=5) as response:
            saved = json.load(response)["messages"]
        self.assertEqual(saved[0]["content"], "เบอร์ฉุกเฉินคืออะไร\n[ใช้แหล่งความรู้]")

    def test_without_rag_flag_nothing_is_injected(self):
        with self.upload("handbook.txt", "รหัสลับคือ 4451".encode()):
            pass
        _, sent = self.chat({"model": "qwen3:8b",
                             "messages": [{"role": "user", "content": "รหัสลับคืออะไร"}]})
        self.assertNotIn("แหล่งความรู้", sent["messages"][-1]["content"])
        self.assertNotIn("4451", sent["messages"][-1]["content"])

    def test_rag_images_attach_only_for_vision_models(self):
        with self.upload("clue.png", PNG_1PX * 64):
            pass
        _, sent = self.chat({"model": "vl:4b", "rag": True,
                             "messages": [{"role": "user", "content": "ดูรูปใบ้"}]})
        self.assertEqual(len(sent["messages"][-1]["images"]), 1)
        self.assertEqual(sent["messages"][-1]["images"][0], b64(PNG_1PX * 64))

        # a non-vision model never receives the image bytes
        _, sent = self.chat({"model": "qwen3:8b", "rag": True,
                             "messages": [{"role": "user", "content": "x"}]})
        self.assertNotIn("images", sent["messages"][-1])

    def test_rag_with_no_uploads_reports_a_hint(self):
        rows, _ = self.chat({"model": "qwen3:8b", "rag": True,
                             "messages": [{"role": "user", "content": "ทดสอบ"}]})
        notes = [n for r in rows if "notes" in r for n in r["notes"]]
        self.assertTrue(any("อัปโหลด" in n for n in notes))


class MusicLyricsNoteTest(unittest.TestCase):
    def test_long_lyrics_are_kept_in_the_media_note(self):
        patch_draw_db(self)
        user = draw_server.register_user("คน เพลง", "songuser", "pw1234")
        draw_server.approve_user(user)
        user = draw_server.get_user_by_username("songuser")
        src = Path(self.tmp.name) / "song.mp3"
        src.write_bytes(b"ID3" + b"0" * 100)
        long_lyrics = "[Verse]\n" + ("บรรทัดเนื้อร้องยาวๆ\n" * 60)
        note = "สไตล์: ลูกทุ่ง\nเนื้อร้อง: " + long_lyrics
        media_id = draw_server.archive_job_output((user["id"], "songuser"),
                                                  "music", src, note)
        self.assertTrue(media_id)
        with draw_server.db() as conn:
            saved = conn.execute("SELECT note FROM media WHERE id=?",
                                 (media_id,)).fetchone()["note"]
        self.assertEqual(saved, note.strip()[:8000])
        self.assertIn("เนื้อร้อง:", saved)
        self.assertGreater(len(saved), 200)


if __name__ == "__main__":
    unittest.main()
