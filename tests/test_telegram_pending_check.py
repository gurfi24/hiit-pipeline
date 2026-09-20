"""scripts/telegram_pending_check.py: read-only by construction. Faked Telegram, no network."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _guard  # noqa: F401 -- installs the real-data guard

import telegram_pending_check as tpc

NOW = int(datetime.now().timestamp())
SECRET_TEXT = "amrap 2.5 rounds with 18 kg secret-workout-text"
SECRET_FILE_ID = "AgACAgQAAxkBAAsecretfileid"
TOKEN = "123456:SECRET-TOKEN"


def msg(uid, **fields):
    return {"update_id": uid, "message": {"chat": {"id": 999888}, "date": NOW, **fields}}


UPDATES = [
    msg(5, text="old already-processed text"),
    msg(11, photo=[{"file_id": SECRET_FILE_ID}], caption="caption with private words"),
    msg(12, text=SECRET_TEXT),
    msg(13, text="/status"),
    msg(14, text="Start"),
    msg(15, text="/stop"),
    {"update_id": 16, "edited_message": {"text": "x"}},
]


class PendingCheckTests(unittest.TestCase):
    def run_check(self, result=None, status=200, get_side_effect=None, cursor=10):
        tmp = Path(tempfile.mkdtemp())
        state = tmp / "telegram_state.json"
        if cursor is not None:
            state.write_text(json.dumps({"last_update_id": cursor, "chat_id": 999888}), encoding="utf-8")
        before = state.read_bytes() if state.exists() else None
        resp = mock.Mock(status_code=status)
        resp.json.return_value = {"ok": True, "result": UPDATES if result is None else result}
        out, get = io.StringIO(), mock.Mock(return_value=resp, side_effect=get_side_effect)
        exit_msg = None
        with mock.patch.object(tpc, "STATE_PATH", state), \
                mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": TOKEN}), \
                mock.patch.object(tpc.requests, "get", get), \
                mock.patch.object(tpc.requests, "post") as post, \
                contextlib.redirect_stdout(out):
            try:
                tpc.main()
            except SystemExit as e:
                exit_msg = str(e)
        return SimpleResult(out.getvalue(), get, post, state, before, exit_msg)

    def test_sends_one_getupdates_with_no_offset_and_never_posts_or_writes_state(self):
        r = self.run_check()
        r.get.assert_called_once()
        self.assertTrue(r.get.call_args.args[0].endswith("/getUpdates"))
        self.assertEqual(r.get.call_args.kwargs["params"], {"timeout": 0})  # no offset => nothing consumed
        r.post.assert_not_called()  # sends no Telegram messages either
        self.assertEqual(r.state.read_bytes(), r.before)  # cursor file untouched

    def test_prints_counts_types_times_and_start_stop_flags_only(self):
        r = self.run_check()
        self.assertIn("Updates returned by Telegram: 7", r.out)
        self.assertIn("already processed by the poller (Telegram redelivers until the next poll): 1", r.out)
        self.assertIn("WAITING for the next poll: 6", r.out)
        self.assertIn("Waiting summary: photo=1, text=2, command=2, other=1, START-like=1, STOP-like=1", r.out)
        self.assertIn("[looks like START]", r.out)
        self.assertIn("[looks like STOP]", r.out)
        self.assertRegex(r.out, r"waiting +#11 +\d{4}-\d{2}-\d{2} \d{2}:\d{2} +photo")

    def test_never_prints_message_text_captions_file_ids_chat_id_or_token(self):
        r = self.run_check()
        for secret in (SECRET_TEXT, "secret-workout-text", "private words", "already-processed text",
                       SECRET_FILE_ID, "999888", TOKEN, "SECRET-TOKEN"):
            self.assertNotIn(secret, r.out)

    def test_unreadable_cursor_still_reports_without_guessing(self):
        r = self.run_check(cursor=None)
        self.assertIn("cannot tell processed from waiting", r.out)
        self.assertIn("Waiting summary:", r.out)

    def test_empty_queue(self):
        r = self.run_check(result=[])
        self.assertIn("Updates returned by Telegram: 0", r.out)
        self.assertIn("Waiting summary: nothing", r.out)

    def test_http_error_exits_without_the_token(self):
        r = self.run_check(status=409)
        self.assertIn("409", r.exit_msg)
        self.assertNotIn("SECRET-TOKEN", r.exit_msg)

    def test_request_failure_does_not_leak_the_token_url(self):
        boom = tpc.requests.ConnectionError(f"failed for https://api.telegram.org/bot{TOKEN}/getUpdates")
        r = self.run_check(get_side_effect=boom)
        self.assertIn("ConnectionError", r.exit_msg)
        self.assertNotIn("SECRET-TOKEN", r.exit_msg)


class SimpleResult:
    def __init__(self, out, get, post, state, before, exit_msg):
        self.out, self.get, self.post, self.state, self.before, self.exit_msg = out, get, post, state, before, exit_msg


if __name__ == "__main__":
    unittest.main()
