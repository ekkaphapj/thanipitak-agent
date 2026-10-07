"""Per-user library: chat auto-archiving, media files, quota and deletion."""
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import draw_server
from test_draw_server import ChatServerTest, approve_user_via_api, patch_draw_db


def chat_stream_lines(text="ตอบกลับแล้ว"):
    return [
        {"message": {"content": text}, "done": False},
        {"message": {"content": ""}, "done": True,
         "prompt_eval_count": 3, "eval_count": 2},
    ]


class ChatArchiveTest(ChatServerTest):
    def chat(self, body):
        with self.ollama_mock(chat_stream_lines()), \
                mock.patch.object(draw_server, "chat_model_ready", return_value=True), \
                mock.patch.object(draw_server, "free_comfy_vram"), \
                mock.patch.object(draw_server, "warm_chat_model"):
            with self.chat_post(body) as response:
                return [json.loads(line)
                        for line in response.read().decode().splitlines() if line.strip()]

    def library(self):
        request = urllib.request.Request(self.base + "/api/library", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def storage(self):
        request = urllib.request.Request(self.base + "/api/storage", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_chat_auto_saves_into_history_sidebar(self):
        rows = self.chat({"model": "qwen3:8b",
                          "messages": [{"role": "user", "content": "สวัสดี"}]})
        session_id = next(r["chat_session"] for r in rows if "chat_session" in r)
        library = self.library()
        self.assertEqual(len(library["chats"]), 1)
        chat = library["chats"][0]
        self.assertEqual(chat["id"], session_id)
        self.assertEqual(chat["messages"], 2)
        self.assertEqual(chat["title"], "สวัสดี")
        request = urllib.request.Request(self.base + "/api/chats/" + session_id,
                                         headers=self.auth)
        with urllib.request.urlopen(request) as response:
            messages = json.load(response)["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "สวัสดี")
        self.assertEqual(messages[1]["content"], "ตอบกลับแล้ว")
        storage = self.storage()
        self.assertGreater(storage["used"], 0)
        self.assertEqual(storage["chat_messages"], 2)
        self.assertEqual(storage["quota"], draw_server.USER_QUOTA)
        self.assertLessEqual(storage["remaining"], storage["quota"] - storage["used"] + 1)

        # A follow-up question reusing the session id appends to the same chat
        rows = self.chat({"model": "qwen3:8b",
                          "messages": [{"role": "user", "content": "ถามต่อ"}],
                          "chat_session": session_id})
        self.assertEqual(next(r["chat_session"] for r in rows if "chat_session" in r),
                         session_id)
        library = self.library()
        self.assertEqual(len(library["chats"]), 1)
        self.assertEqual(library["chats"][0]["messages"], 4)

        # Deleting the session removes its messages and frees the quota bytes
        request = urllib.request.Request(self.base + "/api/chats/" + session_id,
                                         method="DELETE", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.load(response)["removed"])
        self.assertEqual(self.library()["chats"], [])
        self.assertEqual(self.storage()["used"], 0)

    def test_new_sessions_stay_separate(self):
        first = self.chat({"model": "qwen3:8b",
                           "messages": [{"role": "user", "content": "คนละเรื่อง"}]})
        second = self.chat({"model": "qwen3:8b",
                            "messages": [{"role": "user", "content": "อีกบทสนทนา"}]})
        first_session = next(r["chat_session"] for r in first if "chat_session" in r)
        second_session = next(r["chat_session"] for r in second if "chat_session" in r)
        self.assertNotEqual(first_session, second_session)
        titles = sorted(c["title"] for c in self.library()["chats"])
        self.assertEqual(titles, ["คนละเรื่อง", "อีกบทสนทนา"])

class MediaLibraryTest(ChatServerTest):
    def archive(self, kind, source):
        user = draw_server.get_user_by_username("tester")
        return draw_server.archive_job_output((user["id"], "tester"), kind,
                                              source, "คำบรรยายทดสอบ")

    def make_output(self, name, size=2048):
        source = Path(self.tmp.name) / name
        source.write_bytes(b"DATA" + b"\0" * size)
        return source

    def library(self):
        request = urllib.request.Request(self.base + "/api/library", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            return json.load(response)

    def test_archived_files_use_username_prefix_and_are_served(self):
        self.assertTrue(self.archive("image", self.make_output("a.png")))
        self.assertTrue(self.archive("image", self.make_output("b.png")))
        self.assertTrue(self.archive("music", self.make_output("s.mp3")))
        library = self.library()
        self.assertEqual(sorted(i["filename"] for i in library["images"]),
                         ["tester+picture-thanipitak-2.png",
                          "tester+picture-thanipitak.png"])
        self.assertEqual([i["filename"] for i in library["music"]],
                         ["tester+music-thanipitak.mp3"])
        item = library["images"][0]
        request = urllib.request.Request(self.base + item["url"], headers=self.auth)
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertGreater(len(response.read()), 0)

    def test_media_is_private_and_deletable(self):
        media_id = self.archive("image", self.make_output("a.png"))
        other_token = approve_user_via_api(self.base, "other")
        url = self.base + "/api/media/" + media_id + "/file"
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(
                url, headers={"Authorization": "Bearer " + other_token}))
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

        owner = self.auth["Authorization"]
        request = urllib.request.Request(url, headers={"Authorization": owner})
        with urllib.request.urlopen(request) as response:
            response.read()
        images_dir = Path(self.tmp.name) / "users" / "tester" / "images"
        self.assertEqual([p.name for p in images_dir.iterdir()],
                         ["tester+picture-thanipitak.png"])
        request = urllib.request.Request(self.base + "/api/media/" + media_id,
                                         method="DELETE", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            self.assertTrue(json.load(response)["removed"])
        self.assertEqual(self.library()["images"], [])
        self.assertFalse(images_dir.exists() and any(images_dir.iterdir()))
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(
                url, headers={"Authorization": owner}))
        self.assertEqual(error.exception.code, 404)
        error.exception.close()

    def test_clear_all_media_of_a_kind(self):
        self.archive("image", self.make_output("a.png"))
        self.archive("music", self.make_output("s.mp3"))
        request = urllib.request.Request(self.base + "/api/media?kind=images",
                                         method="DELETE", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            removed = json.load(response)["removed"]
        self.assertEqual(removed, 1)
        library = self.library()
        self.assertEqual(library["images"], [])
        self.assertEqual(len(library["music"]), 1)


class QuotaTest(ChatServerTest):
    def make_output(self, name, size):
        source = Path(self.tmp.name) / name
        source.write_bytes(b"\0" * size)
        return source

    def test_archive_refuses_when_quota_is_full(self):
        user = draw_server.get_user_by_username("tester")
        owner = (user["id"], "tester")
        with mock.patch.object(draw_server, "USER_QUOTA", 1024):
            self.assertTrue(draw_server.archive_job_output(
                owner, "image", self.make_output("a.png", 800), "x"))
            self.assertIsNone(draw_server.archive_job_output(
                owner, "image", self.make_output("b.png", 800), "x"))
        request = urllib.request.Request(self.base + "/api/storage", headers=self.auth)
        with urllib.request.urlopen(request) as response:
            storage = json.load(response)
        self.assertEqual(storage["image_count"], 1)
        self.assertGreater(storage["remaining"], 0)

    def test_run_job_archives_output_into_user_folder(self):
        user = draw_server.get_user_by_username("tester")
        owner = (user["id"], "tester")
        output_dir = Path(self.tmp.name) / "comfy-out"
        output_dir.mkdir()
        (output_dir / "out.png").write_bytes(b"PNGDATA" + b"\0" * 500)
        history = {"pid1": {"status": {"status_str": "success", "completed": True},
                            "outputs": {"7": {"images": [
                                {"filename": "out.png", "type": "output"}]}}}}
        spec = {"prompt": "แมวส้ม", "negative": "", "resolution": 512, "seed": 1,
                "profile": "fast", "reference": None,
                "model_id": draw_server.DEFAULT_MODEL_ID}
        draw_server.JOBS["j1"] = {"status": "queued", "ts": 0}
        with mock.patch.object(draw_server, "OUTPUT_DIR", output_dir), \
                mock.patch.object(draw_server, "USER_QUOTA", 1024 * 1024), \
                mock.patch.object(draw_server, "enhance_prompt", return_value="an orange cat"), \
                mock.patch.object(draw_server, "release_ollama_vram"), \
                mock.patch.object(draw_server, "comfy_post",
                                  return_value={"prompt_id": "pid1"}), \
                mock.patch.object(draw_server, "comfy_get", return_value=history), \
                mock.patch.object(draw_server.time, "sleep"):
            draw_server.run_job("j1", spec, None, owner)
        self.assertEqual(draw_server.JOBS["j1"]["status"], "done")
        self.assertTrue(draw_server.JOBS["j1"]["media_id"])
        archived = output_dir.parent / "users" / "tester" / "images" / \
            "tester+picture-thanipitak.png"
        self.assertTrue(archived.is_file())

    def test_run_job_flags_quota_full_instead_of_failing(self):
        user = draw_server.get_user_by_username("tester")
        owner = (user["id"], "tester")
        output_dir = Path(self.tmp.name) / "comfy-out2"
        output_dir.mkdir()
        (output_dir / "out.png").write_bytes(b"\0" * 4096)
        history = {"pid1": {"status": {"status_str": "success", "completed": True},
                            "outputs": {"7": {"images": [
                                {"filename": "out.png", "type": "output"}]}}}}
        spec = {"prompt": "แมวส้ม", "negative": "", "resolution": 512, "seed": 1,
                "profile": "fast", "reference": None,
                "model_id": draw_server.DEFAULT_MODEL_ID}
        draw_server.JOBS["j2"] = {"status": "queued", "ts": 0}
        with mock.patch.object(draw_server, "OUTPUT_DIR", output_dir), \
                mock.patch.object(draw_server, "USER_QUOTA", 64), \
                mock.patch.object(draw_server, "enhance_prompt", side_effect=lambda p: p), \
                mock.patch.object(draw_server, "release_ollama_vram"), \
                mock.patch.object(draw_server, "comfy_post",
                                  return_value={"prompt_id": "pid1"}), \
                mock.patch.object(draw_server, "comfy_get", return_value=history), \
                mock.patch.object(draw_server.time, "sleep"):
            draw_server.run_job("j2", spec, None, owner)
        self.assertEqual(draw_server.JOBS["j2"]["status"], "done")
        self.assertTrue(draw_server.JOBS["j2"]["quota_full"])
        self.assertNotIn("media_id", draw_server.JOBS["j2"])


class RegisterModelTest(unittest.TestCase):
    def setUp(self):
        patch_draw_db(self)

    def test_username_rules_and_duplicates(self):
        for bad in ({"fullname": "", "username": "a", "password": "x"},
                    {"fullname": "คน", "username": "", "password": "x"},
                    {"fullname": "คน", "username": "abc", "password": ""},
                    {"fullname": "คน", "username": "SP ACE", "password": "x"},
                    {"fullname": "คน", "username": "th/ร้าน", "password": "x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    draw_server.register_user(**bad)
        draw_server.register_user("คน หนึ่ง", "abc", "pw")
        with self.assertRaises(ValueError):
            draw_server.register_user("คน สอง", "ABC", "pw")

    def test_password_hash_round_trip(self):
        draw_server.register_user("คน หนึ่ง", "abc", "pw1")
        user = draw_server.get_user_by_username("abc")
        self.assertNotIn("pw1", user["password_hash"])
        self.assertTrue(draw_server.verify_password("pw1", user["password_hash"]))
        self.assertFalse(draw_server.verify_password("pw2", user["password_hash"]))

    def test_pending_blocks_login_until_approved(self):
        draw_server.register_user("คน หนึ่ง", "abc", "pw")
        with self.assertRaises(draw_server.PendingAccount):
            draw_server.login_user("abc", "pw")
        self.assertTrue(draw_server.approve_user("abc"))
        self.assertEqual(draw_server.login_user("abc", "pw")["username"], "abc")
        with self.assertRaises(ValueError):
            draw_server.login_user("abc", "wrong")


if __name__ == "__main__":
    unittest.main()
