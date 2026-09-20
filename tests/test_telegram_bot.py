"""telegram_bot.main() intake with a faked Telegram API: dates come from content, not send time."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _guard  # noqa: F401 -- installs the real-data guard

import pending_store
import support
import telegram_bot as tb

NOW = datetime.now(tb.LOCAL_TZ)
YDAY = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
YDAY_DDMM = (NOW - timedelta(days=1)).strftime("%d/%m")


def update(uid, **msg):
    return {"update_id": uid, "message": {"chat": {"id": 1}, "date": int(NOW.timestamp()), **msg}}


class BotIntakeTests(unittest.TestCase):
    def run_bot(self, updates, paused=False, seed=None):
        tmp = Path(tempfile.mkdtemp())
        support.patch_pending(self, tmp)
        replies, popen = [], mock.Mock()

        def fake_get(url, **kw):
            r = mock.Mock()
            if url.endswith("/getUpdates"):
                r.json.return_value = {"ok": True, "result": updates}
            elif url.endswith("/getFile"):
                r.json.return_value = {"ok": True, "result": {"file_path": "p/x.jpg"}}
            else:
                r.content = b"jpgbytes"
            return r

        flag = tmp / "paused.flag"
        if paused:
            flag.write_text("x")
        for p in (
            mock.patch.object(tb, "STATE_PATH", tmp / "state.json"),
            mock.patch.object(tb, "BOARDS_DIR", tmp / "boards"),
            mock.patch.object(tb, "PAUSE_FLAG", flag),
            mock.patch.object(tb, "ROOT", tmp),
            mock.patch.object(tb, "load_env", return_value={"TELEGRAM_BOT_TOKEN": "t"}),
            mock.patch.object(tb.requests, "get", side_effect=fake_get),
            mock.patch.object(tb.requests, "post", side_effect=lambda url, json=None, **kw: replies.append(json["text"])),
            mock.patch.object(tb.subprocess, "Popen", popen),
        ):
            p.start()
            self.addCleanup(p.stop)
        if seed:
            seed()  # only after the store is redirected to temp files
        tb.main()
        return replies, popen, tmp

    def test_photo_caption_date_then_text_then_start(self):
        replies, popen, tmp = self.run_bot([
            update(1, photo=[{"file_id": "f"}], caption=f"{YDAY_DDMM} board"),
            update(2, text="amrap 2.5 rounds"),
            update(3, text="/start"),
        ])
        ((eid, e),) = pending_store.entries()
        self.assertEqual((e["workout_date"], e["date_source"]), (YDAY, "text"))  # from the caption, not the send date
        self.assertRegex(e["photo"], r"^boards/\d{4}-\d{2}-\d{2}_\d{6}\.jpg$")  # named by send time
        self.assertTrue((tmp / e["photo"]).exists())
        self.assertIn("amrap 2.5 rounds", e["texts"])
        self.assertIn(YDAY_DDMM, replies[0])  # photo reply says which workout date was detected
        popen.assert_called_once()  # /start spawned the analysis

    def test_start_spawns_the_analysis_with_utf8_output(self):
        # The detached child must not inherit a cp1252 console encoding.
        _, popen, _ = self.run_bot([update(1, text="/start")])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONIOENCODING"], "utf-8")

    def test_text_without_a_date_stays_undated(self):
        replies, _, _ = self.run_bot([update(1, text="First amrap with a 18 kg dumbbell, I did 2.5 rounds")])
        ((_, e),) = pending_store.entries()
        self.assertIsNone(e["workout_date"])
        self.assertNotIn("תאריך האימון", replies[0])

    def test_date_reply_answers_the_question_and_is_not_stored_as_text(self):
        ids = []

        def seed():
            ids.append(pending_store.add_photo(NOW - timedelta(minutes=30), "boards/a.jpg"))
            pending_store.update(ids[0], asked_date=True)

        replies, _, _ = self.run_bot([update(1, text=YDAY_DDMM)], seed=seed)
        e = pending_store.load()[ids[0]]
        self.assertEqual((e["workout_date"], e["date_source"], e["texts"]), (YDAY, "user_reply", []))
        self.assertIn("/start", replies[0])

    def test_paused_photo_then_text_then_start_are_both_saved_and_then_processed(self):
        photo = update(1, photo=[{"file_id": "f"}])
        text = update(2, text=f"{YDAY_DDMM} front squat 60kg, amrap 2.5 rounds")
        replies, popen, tmp = self.run_bot([photo, text, update(3, text="/start")], paused=True)

        # Nothing sent before /start was dropped, and the paused bot said nothing about it.
        ((eid, e),) = pending_store.entries()
        self.assertTrue(e["photo"])
        self.assertEqual((e["workout_date"], e["texts"]), (YDAY, [text["message"]["text"]]))
        self.assertTrue((tmp / e["photo"]).exists())
        self.assertEqual(len(replies), 1)  # only the /start acknowledgement
        self.assertIn("▶️", replies[0])
        self.assertFalse((tmp / "paused.flag").exists())  # /start resumed normal operation
        popen.assert_called_once()  # the analysis is only spawned by /start

        # ...and the analysis then processes both the photo and the text.
        r = support.run_analyze(self, pending_store.load(), [], activities=[support.workout(7, YDAY)])
        (call,) = r.calls.llm
        self.assertEqual((call.date, call.entry["photo"], call.entry["texts"]), (YDAY, e["photo"], e["texts"]))
        self.assertEqual(r.pending, {})

    def test_paused_messages_are_saved_silently_and_nothing_runs(self):
        replies, popen, tmp = self.run_bot([
            update(1, photo=[{"file_id": "f"}], caption=f"{YDAY_DDMM} board"),
            update(2, text="amrap 2.5 rounds"),
            update(3, text="/status"),  # commands are ignored while paused
        ], paused=True)
        ((_, e),) = pending_store.entries()
        self.assertEqual((bool(e["photo"]), e["workout_date"]), (True, YDAY))
        self.assertIn("amrap 2.5 rounds", e["texts"])
        self.assertNotIn("/status", e["texts"])  # not stored as a result text either
        self.assertEqual(replies, [])  # no notifications while paused
        popen.assert_not_called()  # no Garmin / LLM / vision run
        self.assertTrue((tmp / "paused.flag").exists())  # still paused

    def test_stop_pauses_and_later_messages_are_still_kept(self):
        replies, popen, tmp = self.run_bot([update(1, text="STOP"), update(2, text="19/9 amrap")])
        self.assertTrue((tmp / "paused.flag").exists())
        self.assertEqual(len(replies), 1)  # just the STOP acknowledgement
        self.assertEqual(len(pending_store.entries()), 1)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
