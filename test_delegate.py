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


CACHE = {"models": [
    {"slug": "big", "visibility": "list", "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}]},
    {"slug": "small", "visibility": "list", "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
    {"slug": "secret", "visibility": "hide", "supported_reasoning_levels": [{"effort": "low"}]},
]}


class Options(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        (home / "models_cache.json").write_text(json.dumps(CACHE), encoding="utf-8")
        self.old = os.environ.get("CODEX_HOME")
        os.environ["CODEX_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            del os.environ["CODEX_HOME"]
        else:
            os.environ["CODEX_HOME"] = self.old
        self.tmp.cleanup()

    def test_codex_cache(self):
        self.assertEqual([m["model"] for m in d.codex_models()], ["big", "small"])
        self.assertEqual(len(d.codex_models(include_hidden=True)), 3)

    def test_unreadable_cache_means_no_validation(self):
        os.environ["CODEX_HOME"] = self.tmp.name + "-missing"
        self.assertEqual(d.codex_models(), [])
        self.assertIsNone(d.check_options("codex", {"model": "anything", "effort": "whatever"}))

    def test_codex_validation(self):
        self.assertIsNone(d.check_options("codex", {"model": "small", "effort": "medium"}))
        self.assertIn("no model", d.check_options("codex", {"model": "nope"}))
        self.assertIn("does not support", d.check_options("codex", {"model": "small", "effort": "ultra"}))
        self.assertIsNone(d.check_options("codex", {"effort": "ultra"}))  # no model given: Codex's own default, efforts unknown
        self.assertIsNone(d.check_options("codex", {"model": "secret"}))  # hidden models stay usable

    def test_agy_effort_validation(self):
        self.assertIsNone(d.check_options("agy", {"effort": "max"}))
        self.assertIn("not one of", d.check_options("agy", {"effort": "ultra"}))

    def test_per_provider_forms(self):
        self.assertEqual(d.per_provider(["m1"], ["agy"], "--model"), {"agy": "m1"})
        self.assertEqual(d.per_provider(["codex=a", "agy=b=c"], ["agy", "codex"], "--model"),
                         {"codex": "a", "agy": "b=c"})

    def test_per_provider_errors(self):
        for vals, names in ((["m"], ["agy", "codex"]), (["codex=m"], ["agy"]), (["a", "b"], ["agy"])):
            with self.assertRaises(ValueError):
                d.per_provider(vals, names, "--model")

    def test_build_options_shape(self):
        o = d.build_options(["agy", "codex"], ["codex=small"], ["agy=low", "codex=low"])
        self.assertEqual(o, {"agy": {"effort": "low"}, "codex": {"model": "small", "effort": "low"}})

    def test_build_options_rejects_bad_pair(self):
        with self.assertRaises(ValueError):
            d.build_options(["codex"], ["small"], ["ultra"])

    def run_with_fake_process(self, runner, opts):
        seen = {}

        class P:
            returncode, stderr, stdout = 0, "", CODEX_OK if runner is d.run_codex else AGY_OK
        real_run, real_find = d._run, d.find_exe
        d._run = lambda cmd, *a, **k: (seen.setdefault("cmd", cmd), P)[1]
        d.find_exe = lambda n: n
        try:
            self.assertTrue(runner("q", ".", False, opts, 5).ok)
        finally:
            d._run, d.find_exe = real_run, real_find
        return seen["cmd"]

    def test_codex_command_line(self):
        cmd = self.run_with_fake_process(d.run_codex, {"model": "small", "effort": "low"})
        self.assertEqual(cmd[cmd.index("-m") + 1], "small")
        self.assertEqual(cmd[cmd.index("-c") + 1], 'model_reasoning_effort="low"')
        self.assertEqual(cmd[-1], "-")  # the prompt marker stays last
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")

    def test_agy_command_line(self):
        cmd = self.run_with_fake_process(d.run_agy, {"model": "m", "effort": "max"})
        self.assertEqual(cmd[cmd.index("--model") + 1], "m")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")

    def test_no_options_adds_no_model_flags(self):
        cmd = self.run_with_fake_process(d.run_codex, {})
        self.assertNotIn("-m", cmd)
        self.assertNotIn("-c", cmd)


class FindExe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.old_path, self.old_cwd = os.environ.get("PATH"), os.getcwd()
        self.exe = "agy.exe" if os.name == "nt" else "agy"

    def tearDown(self):
        os.chdir(self.old_cwd)
        os.environ["PATH"] = self.old_path
        self.tmp.cleanup()

    def make(self, sub, name):
        p = self.root / sub / name
        p.parent.mkdir(exist_ok=True)
        p.write_text("")
        p.chmod(0o755)
        return p

    def test_finds_exe_in_absolute_path_entry(self):
        p = self.make("bin", self.exe)
        os.environ["PATH"] = str(p.parent)
        self.assertEqual(Path(d.find_exe("agy")), p)

    def test_current_and_relative_entries_are_skipped(self):
        p = self.make("planted", self.exe)
        os.chdir(p.parent)
        os.environ["PATH"] = os.pathsep.join(["", ".", "planted", str(p.parent)])
        self.assertIsNone(d.find_exe("agy"))

    def test_batch_file_is_not_accepted(self):
        if os.name != "nt":
            self.skipTest("only Windows resolves .cmd")
        p = self.make("bin", "agy.cmd")
        os.environ["PATH"] = str(p.parent)
        self.assertIsNone(d.find_exe("agy"))

    def test_agy_empty_success_is_failure(self):
        class P:
            returncode, stderr = 0, ""
            stdout = '{"status":"SUCCESS","response":"\\n","usage":{"total_tokens":5}}'
        real_run, real_find = d._run, d.find_exe
        d._run, d.find_exe = lambda *a, **k: P, lambda n: "agy"
        try:
            self.assertFalse(d.run_agy("q", ".", False, {}, 5).ok)
        finally:
            d._run, d.find_exe = real_run, real_find


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
