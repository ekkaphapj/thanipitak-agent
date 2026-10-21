"""Checks for the Codex tasking tab (แท็บ ธานีพิทักษ์)."""
import json
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import draw_server
from test_draw_server import approve_user_via_api, patch_draw_db, post_json


def get_json(base, path, token=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + path, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def admin_token(base):
    with post_json(base, "/api/approve/login",
                   {"code": draw_server.APPROVE_CODE}) as r:
        return json.load(r)["token"]


class CodexServerTest(unittest.TestCase):
    """Boots the real Handler with the codex worker stubbed out."""

    def setUp(self):
        patch_draw_db(self)
        self._restore = {
            "thread_id": draw_server.CODEX_STATE["thread_id"],
            "job": draw_server.CODEX_STATE["job"],
        }
        draw_server.CODEX_STATE["thread_id"] = None
        draw_server.CODEX_STATE["job"] = None
        self.server = draw_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), draw_server.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        draw_server.CODEX_STATE.update(self._restore)
        draw_server.SESSIONS.clear()

    def fake_worker(self, events, thread_id=None, status="done", error=None):
        """Return a stand-in for run_codex_job that replays canned events."""
        def worker(job):
            for evt in events:
                draw_server.codex_apply_event(job, evt)
            if thread_id:
                with draw_server.CODEX_LOCK:
                    draw_server.CODEX_STATE["thread_id"] = thread_id
            with draw_server.CODEX_LOCK:
                job["status"] = status
                if error:
                    job["error"] = error
                job["finished"] = time.time()
        return worker

    def patch_project(self):
        """Make the binary+project checks pass without a real codex install."""
        tmp = Path(self.tmp.name)
        (tmp / "codex").write_text("")            # fake binary
        (tmp / "project").mkdir(exist_ok=True)    # fake project checkout
        return mock.patch.multiple(
            draw_server, CODEX_BIN=str(tmp / "codex"),
            CODEX_PROJECT_DIR=str(tmp / "project"))


class CodexArgsTest(unittest.TestCase):
    def test_fresh_session(self):
        args = draw_server.codex_command_args("ทำงาน A")
        self.assertEqual(args[0], draw_server.CODEX_BIN)
        self.assertEqual(args[1], "exec")
        self.assertIn("--json", args)
        self.assertIn("workspace-write", args)
        self.assertEqual(args[-1], "ทำงาน A")
        self.assertNotIn("resume", args)

    def test_resume_reuses_thread_id(self):
        args = draw_server.codex_command_args("งานต่อ", "abc-123")
        self.assertEqual(args[1:4], ["exec", "resume", "abc-123"])
        self.assertEqual(args[-1], "งานต่อ")
        # exec resume rejects -C/-s: session keeps its own workspace/sandbox
        self.assertNotIn("-C", args)
        self.assertNotIn("-s", args)


class CodexEventTest(unittest.TestCase):
    def job(self):
        return {"events": [], "truncated": False, "thread_id": None,
                "usage": None, "error": None}

    def test_thread_started_records_id_but_not_shown(self):
        job = self.job()
        draw_server.codex_apply_event(job, {"type": "thread.started",
                                            "thread_id": "t1"})
        self.assertEqual(job["thread_id"], "t1")
        self.assertEqual(job["events"], [])

    def test_token_count_filtered(self):
        job = self.job()
        draw_server.codex_apply_event(job, {"type": "token_count", "n": 5})
        self.assertEqual(job["events"], [])

    def test_usage_captured_on_turn_completed(self):
        job = self.job()
        draw_server.codex_apply_event(job, {"type": "turn.completed",
                                            "usage": {"input_tokens": 9}})
        self.assertEqual(job["usage"], {"input_tokens": 9})

    def test_command_output_trimmed(self):
        job = self.job()
        draw_server.codex_apply_event(job, {"type": "item.completed", "item": {
            "type": "command_execution", "command": "ls",
            "aggregated_output": "x" * 5000, "exit_code": 0}})
        item = job["events"][0]["item"]
        self.assertLessEqual(len(item["aggregated_output"]), 1200)

    def test_event_cap_marks_truncated(self):
        job = self.job()
        real_max = draw_server.CODEX_MAX_EVENTS
        draw_server.CODEX_MAX_EVENTS = 3
        try:
            for i in range(6):
                draw_server.codex_apply_event(
                    job, {"type": "item.completed",
                          "item": {"type": "agent_message", "text": str(i)}})
        finally:
            draw_server.CODEX_MAX_EVENTS = real_max
        self.assertEqual(len(job["events"]), 3)
        self.assertTrue(job["truncated"])

    def test_non_json_line_becomes_raw(self):
        job = self.job()
        draw_server.codex_apply_event(job, "not-a-dict")
        self.assertEqual(job["events"], [])


class CodexHttpTest(CodexServerTest):
    def test_requires_login(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            get_json(self.base, "/api/codex/status")
        self.assertEqual(ctx.exception.code, 401)

    def test_normal_user_denied_and_admin_allowed(self):
        user = approve_user_via_api(self.base, "codex-plain")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            post_json(self.base, "/api/codex/send", {"prompt": "hi"}, token=user)
        self.assertEqual(ctx.exception.code, 403)

        admin = admin_token(self.base)
        data = get_json(self.base, "/api/codex/status", token=admin)
        self.assertTrue(data["allowed"])

    def test_allowed_users_env_grants_plain_user(self):
        user = approve_user_via_api(self.base, "codex-friend")
        with mock.patch.object(draw_server, "CODEX_ALLOWED_USERS",
                               {"codex-friend"}):
            data = get_json(self.base, "/api/codex/status", token=user)
        self.assertTrue(data["allowed"])

    def test_send_missing_binary_is_503(self):
        admin = admin_token(self.base)
        tmp = Path(self.tmp.name)
        with mock.patch.object(draw_server, "CODEX_BIN", str(tmp / "nope")):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                post_json(self.base, "/api/codex/send",
                          {"prompt": "hi"}, token=admin)
        self.assertEqual(ctx.exception.code, 503)

    def test_send_runs_worker_and_status_streams_events(self):
        admin = admin_token(self.base)
        events = [
            {"type": "thread.started", "thread_id": "th-1"},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "เสร็จแล้วครับ"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10,
                                                 "output_tokens": 4}},
        ]
        worker = self.fake_worker(events, thread_id="th-1")
        with self.patch_project(), \
             mock.patch.object(draw_server, "run_codex_job", worker):
            with post_json(self.base, "/api/codex/send",
                           {"prompt": "ทำงานทดสอบ"}, token=admin) as r:
                self.assertEqual(json.load(r)["id"][:1] != "", True)
            data = get_json(self.base, "/api/codex/status", token=admin)
        self.assertEqual(data["job"]["status"], "done")
        self.assertEqual(data["job"]["prompt"], "ทำงานทดสอบ")
        texts = [e["item"]["text"] for e in data["job"]["events"]
                 if e.get("item", {}).get("type") == "agent_message"]
        self.assertEqual(texts, ["เสร็จแล้วครับ"])
        self.assertTrue(data["session_active"])  # thread_id kept for resume
        self.assertEqual(draw_server.CODEX_STATE["thread_id"], "th-1")

    def test_busy_returns_409(self):
        admin = admin_token(self.base)
        draw_server.CODEX_STATE["job"] = {
            "id": "running-job", "status": "running", "prompt": "x",
            "events": [], "started": time.time(), "user": "admin",
            "thread_id": None, "usage": None, "error": None,
            "truncated": False}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            post_json(self.base, "/api/codex/send", {"prompt": "y"},
                      token=admin)
        self.assertEqual(ctx.exception.code, 409)
        draw_server.CODEX_STATE["job"] = None

    def test_prompt_validation(self):
        admin = admin_token(self.base)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            post_json(self.base, "/api/codex/send", {"prompt": "  "},
                      token=admin)
        self.assertEqual(ctx.exception.code, 400)

    def test_reset_clears_session_but_not_mid_job(self):
        admin = admin_token(self.base)
        draw_server.CODEX_STATE["thread_id"] = "keep"
        with post_json(self.base, "/api/codex/reset", {}, token=admin):
            pass
        self.assertIsNone(draw_server.CODEX_STATE["thread_id"])
        draw_server.CODEX_STATE["job"] = {
            "id": "j", "status": "running", "prompt": "x", "events": [],
            "started": time.time(), "thread_id": None, "usage": None,
            "error": None, "truncated": False}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            post_json(self.base, "/api/codex/reset", {}, token=admin)
        self.assertEqual(ctx.exception.code, 409)
        draw_server.CODEX_STATE["job"] = None


if __name__ == "__main__":
    unittest.main()
