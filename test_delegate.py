import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
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
        self.assertIn("--ignore-user-config", cmd)  # keeps the user's MCP servers and notify command out of the run
        self.assertIn("--ignore-rules", cmd)
        self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")

    def test_agy_command_line(self):
        cmd = self.run_with_fake_process(d.run_agy, {"model": "m", "effort": "max"})
        self.assertEqual(cmd[cmd.index("--model") + 1], "m")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "plan")

    def write_config(self, text):
        (Path(self.tmp.name) / "config.toml").write_text(text, encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "[windows] sandbox only applies on Windows")
    def test_windows_sandbox_is_passed_back_from_user_config(self):
        self.write_config("\n".join(['model = "x"', "[windows]", 'sandbox = "elevated"', "[mcp_servers.figma]", 'url = "u"']))
        self.assertEqual(d.codex_windows_sandbox(), "elevated")
        cmd = self.run_with_fake_process(d.run_codex, {})
        self.assertEqual(cmd[cmd.index("-c") + 1], 'windows.sandbox="elevated"')

    @unittest.skipUnless(os.name == "nt", "[windows] sandbox only applies on Windows")
    def test_no_sandbox_override_without_the_setting(self):
        self.write_config('model = "x"')
        self.assertIsNone(d.codex_windows_sandbox())
        self.assertNotIn("-c", self.run_with_fake_process(d.run_codex, {}))

    @unittest.skipUnless(os.name == "nt", "[windows] sandbox only applies on Windows")
    def test_unusable_config_is_reported_and_skipped(self):
        import contextlib
        import io
        for text in ("this is = not [valid", "[windows]\nsandbox = 'a b; rm'"):
            self.write_config(text)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertIsNone(d.codex_windows_sandbox())
            self.assertIn("not passed on", err.getvalue())

    def test_missing_config_is_not_an_error(self):
        self.assertIsNone(d.codex_windows_sandbox())

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


class Timeouts(unittest.TestCase):
    def test_timeout_kills_grandchildren_and_returns_on_time(self):
        marker = Path(tempfile.mkdtemp()) / "grandchild-survived.txt"
        grandchild = f"import time; time.sleep(4); open({str(marker)!r}, 'w').close()"
        parent = (f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(60)")
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            d._run([sys.executable, "-c", parent], None, 1)
        self.assertLess(time.time() - started, 3.5)  # plain subprocess.run waits for the grandchild's pipe, ~4 s here
        time.sleep(5)
        self.assertFalse(marker.exists(), "the grandchild kept running after the timeout")

    def test_zero_means_no_limit(self):
        self.assertEqual(d._run([sys.executable, "-c", "print('hi')"], None, 0).stdout.strip(), "hi")

    def test_seconds_argument(self):
        self.assertEqual(d.seconds("0"), 0)
        self.assertEqual(d.seconds("600"), 600)
        with self.assertRaises(argparse.ArgumentTypeError):
            d.seconds("-1")

    def test_agy_gets_zero_print_timeout_for_no_limit(self):
        seen = {}
        real_run, real_find = d._run, d.find_exe
        d._run = lambda cmd, *a, **k: (seen.setdefault("cmd", cmd), type("R", (), {"returncode": 0, "stderr": "", "stdout": AGY_OK}))[1]
        d.find_exe = lambda n: "agy"
        try:
            d.run_agy("q", ".", False, {}, 0)
        finally:
            d._run, d.find_exe = real_run, real_find
        cmd = seen["cmd"]
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "0s")


class AgyRuns(unittest.TestCase):
    def fake(self, outputs):
        """Replace _run/find_exe; returns the list of command lines that were run."""
        calls = []
        queue = list(outputs)

        class P:
            returncode, stderr = 0, ""

        def run(cmd, *a, **k):
            calls.append(cmd)
            return type("R", (P,), {"stdout": queue.pop(0)})
        self.real = (d._run, d.find_exe)
        d._run, d.find_exe = run, lambda n: "agy"
        self.addCleanup(lambda: setattr(d, "_run", self.real[0]) or setattr(d, "find_exe", self.real[1]))
        return calls

    def test_read_only_prompt_gets_answer_directly_suffix(self):
        calls = self.fake([AGY_OK])
        d.run_agy("question", ".", False, {}, 5)
        self.assertTrue(calls[0][1].startswith("-p=question"))
        self.assertIn("Do not write a plan", calls[0][1])

    def test_write_prompt_is_left_alone(self):
        calls = self.fake([AGY_OK])
        d.run_agy("edit it", ".", True, {}, 5)
        self.assertEqual(calls[0][1], "-p=edit it")

    def test_empty_success_is_retried_once(self):
        empty = '{"status":"SUCCESS","response":"","usage":{"total_tokens":1}}'
        calls = self.fake([empty, AGY_OK])
        self.assertTrue(d.run_agy("q", ".", False, {}, 5).ok)
        self.assertEqual(len(calls), 2)

    def test_two_empty_successes_fail(self):
        empty = '{"status":"SUCCESS","response":" ","usage":{"total_tokens":1}}'
        calls = self.fake([empty, empty])
        self.assertFalse(d.run_agy("q", ".", False, {}, 5).ok)
        self.assertEqual(len(calls), 2)

    def test_real_failure_is_not_retried(self):
        calls = self.fake(['{"status":"ERROR","response":""}'])
        self.assertFalse(d.run_agy("q", ".", False, {}, 5).ok)
        self.assertEqual(len(calls), 1)


class ExternalData(unittest.TestCase):
    """Output and files written by other programs are not guaranteed to have the shape we expect."""

    def test_codex_stream_with_odd_fields_does_not_crash(self):
        stream = "\n".join(['{"item": null}', '{"error": "rate limit reached"}', '{"error": null}', "123", '"text"',
                            '{"type":"item.completed","item":null}', '{"type":"turn.completed","usage":null}'])
        self.assertEqual(d.parse_codex(stream), (False, "", 0))
        self.assertEqual(d.codex_errors(stream), "rate limit reached")

    def test_agy_result_found_among_noise(self):
        out = "\n".join(["{debug: not json", AGY_OK, "{another broken line", "[1, 2]"])
        self.assertEqual(d.parse_agy(out)["usage"]["total_tokens"], 12702)
        self.assertIsNone(d.parse_agy("[1]\nnothing here"))

    def test_damaged_state_file_is_reported_not_fatal(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            real = d.STATE
            d.STATE = Path(tmp) / "state.json"
            try:
                for text in ("", "{not json", "[1, 2]"):
                    d.STATE.write_text(text, encoding="utf-8")
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(d.load_state(), {})
                    self.assertIn("empty state", err.getvalue())
            finally:
                d.STATE = real

    def test_models_cache_with_null_and_incomplete_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "models_cache.json").write_text(json.dumps({"models": [
                {"slug": "a", "visibility": "list", "supported_reasoning_levels": None},
                {"visibility": "list"}, "junk", {"slug": "b", "visibility": "list", "supported_reasoning_levels": [{"x": 1}, {"effort": "low"}]},
            ]}), encoding="utf-8")
            old = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = tmp
            try:
                self.assertEqual([(m["model"], m["efforts"]) for m in d.codex_models()], [("a", []), ("b", ["low"])])
            finally:
                if old is None:
                    del os.environ["CODEX_HOME"]
                else:
                    os.environ["CODEX_HOME"] = old

    @unittest.skipUnless(os.name == "nt", "[windows] sandbox only applies on Windows")
    def test_non_table_windows_key_in_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text("windows = false\n", encoding="utf-8")
            old = os.environ.get("CODEX_HOME")
            os.environ["CODEX_HOME"] = tmp
            try:
                self.assertIsNone(d.codex_windows_sandbox())
            finally:
                if old is None:
                    del os.environ["CODEX_HOME"]
                else:
                    os.environ["CODEX_HOME"] = old


class Robustness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state, d.STATE = d.STATE, Path(self.tmp.name) / "state.json"
        self.saved = dict(d.RUNNERS)

    def tearDown(self):
        d.STATE = self.real_state
        d.RUNNERS.clear()
        d.RUNNERS.update(self.saved)
        self.tmp.cleanup()

    def test_provider_that_cannot_start_falls_through_to_the_next(self):
        def cannot_start(*a):
            raise NotADirectoryError("bad cwd")
        d.RUNNERS["agy"] = cannot_start
        d.RUNNERS["codex"] = lambda *a: d.Result(True, "A", 1)
        self.assertEqual(d.delegate("q", ["agy", "codex"], "."), (0, "A"))
        self.assertIn("could not start agy", d.load_state()["agy"]["last_error"])

    def test_state_entry_without_history_is_repaired(self):
        d.STATE.write_text(json.dumps({"agy": {"last_error": "old"}}), encoding="utf-8")
        d.RUNNERS["agy"] = lambda *a: d.Result(True, "A", 3)
        self.assertEqual(d.delegate("q", ["agy"], "."), (0, "A"))
        self.assertEqual(len(d.load_state()["agy"]["history"]), 1)

    def test_unknown_provider_is_exit_code_2(self):
        args = argparse.Namespace(providers="nope", model=None, effort=None, prompt="q", cwd=".", write=False,
                                  timeout=5, cooldown=1)
        self.assertEqual(d.cmd_run(args), 2)
        self.assertEqual(d.cmd_check(argparse.Namespace(providers="nope", model=None, effort=None, timeout=5)), 2)

    @unittest.skipUnless(os.name == "nt", "the command line limit is a Windows one")
    def test_quoting_that_overflows_the_windows_command_line_is_refused(self):
        called = []
        real_run, real_find = d._run, d.find_exe
        d._run, d.find_exe = lambda *a, **k: called.append(1), lambda n: "agy"
        try:
            res = d.run_agy('"' * 20000, ".", False, {}, 5)  # 20 000 characters, over 40 000 once quoted
        finally:
            d._run, d.find_exe = real_run, real_find
        self.assertFalse(res.ok)
        self.assertIn("too long", res.err)
        self.assertEqual(called, [])

    def agy_runs(self, outputs):
        calls = []
        queue = list(outputs)
        real_run, real_find = d._run, d.find_exe
        d._run = lambda cmd, *a, **k: (calls.append(cmd), type("R", (), {"returncode": 0, "stderr": "", "stdout": queue.pop(0)}))[1]
        d.find_exe = lambda n: "agy"
        self.addCleanup(lambda: (setattr(d, "_run", real_run), setattr(d, "find_exe", real_find)))
        return calls

    def test_quick_empty_answer_is_retried(self):
        empty = '{"status":"SUCCESS","response":"","duration_seconds":12}'
        calls = self.agy_runs([empty, AGY_OK])
        self.assertTrue(d.run_agy("q", ".", False, {}, 5).ok)
        self.assertEqual(len(calls), 2)

    def test_slow_empty_answer_is_not_paid_for_twice(self):
        empty = '{"status":"SUCCESS","response":"","duration_seconds":250}'
        calls = self.agy_runs([empty, AGY_OK])
        self.assertFalse(d.run_agy("q", ".", False, {}, 5).ok)
        self.assertEqual(len(calls), 1)

    def test_suffix_forbids_shell_commands(self):
        self.assertIn("cannot run shell commands", d.AGY_SUFFIX)


class Check(unittest.TestCase):
    def setUp(self):
        self.saved = dict(d.RUNNERS)
        self.addCleanup(lambda: (d.RUNNERS.clear(), d.RUNNERS.update(self.saved)))

    def reader(self, prompt, cwd, write, o, timeout):
        return d.Result(True, "the word is " + (Path(cwd) / "note.txt").read_text().split()[-1].rstrip("."), 5)

    def test_pass_when_the_file_content_is_returned(self):
        d.RUNNERS["agy"] = self.reader
        self.assertTrue(d.check_provider("agy", {})[0])

    def test_fail_when_the_provider_refuses(self):
        d.RUNNERS["agy"] = lambda *a: d.Result(True, "I cannot read files here", 5)
        ok, detail = d.check_provider("agy", {})
        self.assertFalse(ok)
        self.assertIn("without the word", detail)

    def test_fail_when_the_provider_errors(self):
        d.RUNNERS["agy"] = lambda *a: d.Result(False, err="boom")
        self.assertEqual(d.check_provider("agy", {}), (False, "boom"))

    def test_runs_read_only(self):
        seen = {}
        d.RUNNERS["agy"] = lambda p, c, write, o, t: (seen.setdefault("write", write), d.Result(False, err="x"))[1]
        d.check_provider("agy", {})
        self.assertFalse(seen["write"])


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
