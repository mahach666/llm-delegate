import json
import os
import tempfile
import unittest
from pathlib import Path

import delegate as d

CODEX_OK = "\n".join([
    '{"type":"thread.started","thread_id":"x"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":21683,"cached_input_tokens":13440,"output_tokens":5}}',
])
AGY_OK = '{"conversation_id":"c","status":"SUCCESS","response":"OK\\n","usage":{"total_tokens":12702}}'


class Parsing(unittest.TestCase):
    def test_codex_success(self):
        self.assertEqual(d.parse_codex("noise\n" + CODEX_OK), (True, "OK", 21688))

    def test_codex_without_turn_completed_is_failure(self):
        self.assertFalse(d.parse_codex(CODEX_OK.rsplit("\n", 1)[0])[0])

    def test_codex_error_messages(self):
        out = ('{"type":"thread.started","thread_id":"x"}\n'
               '{"type":"item.completed","item":{"type":"error","message":"usage limit reached"}}\n'
               '{"type":"turn.failed","error":{"message":"try again in 1h"}}')
        self.assertEqual(d.codex_errors(out), "usage limit reached\ntry again in 1h")
        self.assertEqual(d.codex_errors("plain text"), "plain text")

    def test_agy_json_after_log_lines(self):
        self.assertEqual(d.parse_agy("log line\n" + AGY_OK)["usage"]["total_tokens"], 12702)

    def test_agy_garbage(self):
        self.assertIsNone(d.parse_agy("no json here"))

    def test_cooldown(self):
        self.assertEqual(d.parse_cooldown("You've hit your usage limit. Try again in 3h 20m."), 3 * 3600 + 20 * 60)
        self.assertEqual(d.parse_cooldown("retry in 45 seconds"), 60)
        self.assertIsNone(d.parse_cooldown("quota exceeded"))

    def test_quota_pattern(self):
        for s in ("You've hit your usage limit", "429 Too Many Requests", "RESOURCE_EXHAUSTED", "Quota exceeded"):
            self.assertTrue(d.QUOTA_RE.search(s), s)
        self.assertFalse(d.QUOTA_RE.search("syntax error in file"))


class FindExe(unittest.TestCase):
    def test_batch_file_is_refused(self):
        real, d.shutil.which = d.shutil.which, lambda n: r"C:\tools\agy.cmd"
        try:
            self.assertFalse(d.run_agy("q", ".", False, None, 5).ok)
            self.assertIn("batch file", d.run_agy("q", ".", False, None, 5).err)
        finally:
            d.shutil.which = real

    def test_agy_empty_success_is_failure(self):
        class P:
            returncode, stderr = 0, ""
            stdout = '{"status":"SUCCESS","response":"\\n","usage":{"total_tokens":5}}'
        real_run, real_which = d._run, d.shutil.which
        d._run, d.shutil.which = lambda *a, **k: P, lambda n: r"C:\tools\agy.exe"
        try:
            self.assertFalse(d.run_agy("q", ".", False, None, 5).ok)
        finally:
            d._run, d.shutil.which = real_run, real_which

    def test_cwd_is_not_searched(self):
        d.find_exe("python")
        self.assertEqual(os.environ["NoDefaultCurrentDirectoryInExePath"], "1")


class Fallback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d.STATE = Path(self.tmp.name) / "state.json"
        self.saved = dict(d.RUNNERS)
        self.t = 1000.0

    def tearDown(self):
        d.RUNNERS.clear()
        d.RUNNERS.update(self.saved)
        self.tmp.cleanup()

    def go(self, providers=("agy", "codex")):
        return d.delegate("q", list(providers), ".", now=lambda: self.t)

    def test_quota_falls_through_and_is_remembered(self):
        calls = []
        d.RUNNERS["agy"] = lambda *a: (calls.append("agy"), d.Result(False, err="429 try again in 2h"))[1]
        d.RUNNERS["codex"] = lambda *a: (calls.append("codex"), d.Result(True, "A", 7))[1]
        self.assertEqual(self.go(), (0, "A"))
        self.assertEqual(self.go(), (0, "A"))
        self.assertEqual(calls, ["agy", "codex", "codex"])  # second run skips agy
        self.assertEqual(d.load_state()["agy"]["blocked_until"], 1000 + 7200)
        self.t += 7201
        self.go()
        self.assertEqual(calls[-2:], ["agy", "codex"])  # block expired

    def test_all_out_of_quota_is_exit_3(self):
        for n in ("agy", "codex"):
            d.RUNNERS[n] = lambda *a: d.Result(False, err="usage limit")
        self.assertEqual(self.go()[0], 3)

    def test_other_error_is_exit_4_and_not_blocked(self):
        d.RUNNERS["agy"] = lambda *a: d.Result(False, err="boom")
        self.assertEqual(self.go(("agy",))[0], 4)
        self.assertNotIn("blocked_until", d.load_state()["agy"])

    def test_status_counts_windows(self):
        d.RUNNERS["agy"] = lambda *a: d.Result(True, "A", 10)
        self.go(("agy",))
        r = d.describe(d.load_state(), "agy", now=self.t + 6 * 3600)
        self.assertEqual((r["calls_5h"], r["calls_24h"], r["tokens_24h"]), (0, 1, 10))


if __name__ == "__main__":
    unittest.main()
