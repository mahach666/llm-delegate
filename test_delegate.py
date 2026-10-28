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

REAL_HERMES_LIMITS = d.hermes_limits


def setUpModule():
    # No test may call the real `hermes usage`: it takes seconds and reads the real subscription.
    d.hermes_limits = lambda provider, timeout=30: {"plan": None, "windows": []}


def tearDownModule():
    d.hermes_limits = REAL_HERMES_LIMITS


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


class CooldownAndCmdline(unittest.TestCase):
    def test_cooldown_reads_whole_time_words_only(self):
        cases = {
            "You've hit your usage limit. Try again in 3h 20m.": 3 * 3600 + 20 * 60,
            "retry in 500 milliseconds": 60,  # sub-second: the one-minute floor, not 500 minutes
            "retry in 5 messages": None, "quota exceeded": None, "try again in 1.5 hours": 5400,
            "reset after 2 days": 172800, "retry in 2 minutes and 30 seconds": 150, "resets in 90s": 90,
            "try again in 5 hrs": 18000, "retry in 2 min": 120, "retry in 45 seconds": 60,
        }
        for text, expected in cases.items():
            self.assertEqual(d.parse_cooldown(text), expected, text)

    def test_cmdline_counts_utf16_units(self):
        self.assertEqual(d.cmdline_units(["a", "\U0001F600"]), 4)  # "a", space, and two units for the emoji

    @unittest.skipUnless(os.name == "nt", "the command line limit is a Windows one")
    def test_emoji_prompt_that_overflows_the_limit_is_refused(self):
        called = []
        real_run, real_find = d._run, d.find_exe
        d._run, d.find_exe = lambda *a, **k: called.append(1), lambda n: "agy"
        try:
            res = d.run_agy("\U0001F600" * 20000, ".", False, {}, 5)  # 20 000 characters, 40 000 UTF-16 units
        finally:
            d._run, d.find_exe = real_run, real_find
        self.assertFalse(res.ok)
        self.assertEqual(called, [])


ALLOW = {"allow_unsandboxed": True}


class HermesRuns(unittest.TestCase):
    USAGE = {"completed": True, "failed": False, "total_tokens": 14494}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "project"
        (self.src / ".git").mkdir(parents=True)
        (self.src / ".git" / "config").write_text("secret history")
        (self.src / "sub").mkdir()
        (self.src / "note.txt").write_text("hello")
        (self.src / "sub" / "a.py").write_text("print(1)")
        self.real = (d._run, d.find_exe)
        d.find_exe = lambda n: "hermes"
        self.addCleanup(lambda: (setattr(d, "_run", self.real[0]), setattr(d, "find_exe", self.real[1]), self.tmp.cleanup()))

    def fake(self, stdout="the answer", returncode=0, usage=USAGE, touch=None):
        seen = {}

        def run(cmd, cwd, timeout, stdin_text=None):
            seen.update(cmd=cmd, cwd=Path(cwd), timeout=timeout,
                        copied=sorted(str(p.relative_to(cwd)).replace(os.sep, "/") for p in Path(cwd).rglob("*")))
            if touch:
                (Path(cwd) / touch).write_text("changed by the agent")
            if usage is not None:
                Path(cmd[cmd.index("--usage-file") + 1]).write_text(json.dumps(usage), encoding="utf-8")
            return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": "err text"})
        d._run = run
        return seen

    def test_command_line_and_result(self):
        seen = self.fake()
        res = d.run_hermes("question", self.src, False, {**ALLOW, "effort": "low"}, 77)
        self.assertEqual((res.ok, res.text, res.tokens), (True, "the answer", 14494))
        cmd = seen["cmd"]
        self.assertEqual(cmd[1:3], ["-z", "question"])
        self.assertEqual(cmd[cmd.index("--provider") + 1], "openai-codex")
        self.assertEqual(cmd[cmd.index("-m") + 1], d.HERMES_DEFAULT_MODEL)
        self.assertEqual(cmd[cmd.index("-t") + 1], "file")  # no terminal toolset
        self.assertIn("--safe-mode", cmd)
        self.assertEqual(cmd[cmd.index("--reasoning") + 1], "low")
        self.assertEqual(seen["timeout"], 77)

    def test_model_is_passed_through_and_reasoning_omitted_without_effort(self):
        seen = self.fake()
        d.run_hermes("q", self.src, False, {**ALLOW, "model": "gpt-6-astra"}, 5)
        self.assertEqual(seen["cmd"][seen["cmd"].index("-m") + 1], "gpt-6-astra")
        self.assertNotIn("--reasoning", seen["cmd"])

    def test_runs_on_a_copy_and_the_original_stays_untouched(self):
        seen = self.fake(touch="note.txt")
        d.run_hermes("q", self.src, False, ALLOW, 5)
        self.assertNotEqual(seen["cwd"], self.src)
        self.assertEqual((self.src / "note.txt").read_text(), "hello")  # the agent changed the copy only
        self.assertFalse(seen["cwd"].exists())  # and the copy is gone afterwards

    def test_copy_skips_ignored_directories(self):
        seen = self.fake()
        d.run_hermes("q", self.src, False, ALLOW, 5)
        self.assertEqual(seen["copied"], ["note.txt", "sub", "sub/a.py"])

    def test_write_is_refused_without_running_anything(self):
        seen = self.fake()
        res = d.run_hermes("q", self.src, True, ALLOW, 5)
        self.assertFalse(res.ok)
        self.assertIn("codex for --write", res.err)
        self.assertEqual(seen, {})

    def test_too_big_a_directory_is_refused(self):
        seen = self.fake()
        real, d.HERMES_MAX_COPY_BYTES = d.HERMES_MAX_COPY_BYTES, 5
        try:
            res = d.run_hermes("q", self.src, False, ALLOW, 5)
        finally:
            d.HERMES_MAX_COPY_BYTES = real
        self.assertFalse(res.ok)
        self.assertIn("MB", res.err)
        self.assertEqual(seen, {})

    def test_failures_are_not_reported_as_answers(self):
        for kwargs in ({"returncode": 1}, {"stdout": "  \n"}, {"usage": {"failed": True, "completed": True}},
                       {"usage": {"completed": False}}):
            self.fake(**kwargs)
            self.assertFalse(d.run_hermes("q", self.src, False, ALLOW, 5).ok, kwargs)

    def test_missing_usage_file_does_not_fail_a_good_answer(self):
        self.fake(usage=None)
        res = d.run_hermes("q", self.src, False, ALLOW, 5)
        self.assertEqual((res.ok, res.tokens), (True, 0))

    def test_hermes_is_registered(self):
        self.assertIn("hermes", d.RUNNERS)

    def test_effort_validation_for_hermes(self):
        self.assertIsNone(d.check_options("hermes", {"effort": "ultra"}))
        self.assertIn("not one of", d.check_options("hermes", {"effort": "extreme"}))

    def test_not_installed(self):
        d.find_exe = lambda n: None
        self.assertEqual(d.run_hermes("q", self.src, False, ALLOW, 5).err, "hermes not installed")


USAGE_JSON = json.dumps({
    "provider": "openai-codex", "plan": "Plus", "unavailable_reason": None,
    "windows": [{"label": "Session", "used_percent": 7.0, "resets_at": "2026-10-28T16:02:37+00:00", "detail": None},
                {"label": "Weekly", "used_percent": 1.0, "resets_at": "2026-11-04T11:02:37+00:00", "detail": None}]})


class HermesUsage(unittest.TestCase):
    """`hermes usage --json` gives the usage windows of the ChatGPT subscription before any call is spent."""

    def run_limits(self, stdout="", returncode=0, stderr="", exc=None):
        real = (d._run, d.find_exe)
        seen = {}

        def run(cmd, cwd, timeout, stdin_text=None):
            seen["cmd"] = cmd
            if exc:
                raise exc
            return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})
        d._run, d.find_exe = run, lambda n: "hermes"
        try:
            return REAL_HERMES_LIMITS("openai-codex"), seen
        finally:
            d._run, d.find_exe = real

    def test_windows_are_parsed(self):
        limits, seen = self.run_limits(USAGE_JSON)
        self.assertEqual(seen["cmd"][1:], ["usage", "--provider", "openai-codex", "--json"])
        self.assertEqual(limits["plan"], "Plus")
        self.assertEqual([(w["label"], w["used_percent"]) for w in limits["windows"]], [("Session", 7.0), ("Weekly", 1.0)])
        self.assertEqual(limits["windows"][0]["resets_at"], 1793203357.0)  # 2026-10-28T16:02:37Z

    def test_unreadable_windows_are_skipped(self):
        data = json.dumps({"windows": [{"label": "x"}, {"used_percent": "n/a", "resets_at": "bad"}, "junk",
                                       {"label": "ok", "used_percent": 50, "resets_at": "2026-10-28T16:02:37+00:00"}]})
        self.assertEqual([w["label"] for w in self.run_limits(data)[0]["windows"]], ["ok"])

    def test_every_way_of_not_knowing_is_reported(self):
        cases = [dict(returncode=1, stderr="no credential is configured"), dict(stdout="not json"),
                 dict(stdout=json.dumps({"unavailable_reason": "token expired"})),
                 dict(exc=subprocess.TimeoutExpired("hermes", 30))]
        for kwargs in cases:
            with self.assertRaises(d.LimitsUnavailable, msg=kwargs):
                self.run_limits(**kwargs)

    def test_hermes_not_installed(self):
        real = d.find_exe
        d.find_exe = lambda n: None
        try:
            with self.assertRaises(d.LimitsUnavailable):
                REAL_HERMES_LIMITS("openai-codex")
        finally:
            d.find_exe = real

    def test_limit_block_picks_the_window_that_lasts_longest(self):
        limits = {"windows": [{"label": "Session", "used_percent": 97.0, "resets_at": 5000.0},
                              {"label": "Weekly", "used_percent": 96.0, "resets_at": 90000.0},
                              {"label": "Other", "used_percent": 10.0, "resets_at": 99999.0}]}
        self.assertEqual(d.limit_block(limits, 95), (90000.0, "Weekly", 96.0))
        self.assertEqual(d.limit_block(limits, 96)[1], "Weekly")  # the threshold itself counts
        self.assertIsNone(d.limit_block(limits, 98))

    def test_format_limits(self):
        text = d.format_limits("chatgpt", {"plan": "Plus", "windows": [{"label": "Session", "used_percent": 7.0, "resets_at": 1793203357.0}]})
        self.assertTrue(text.startswith("chatgpt (Plus): Session 7% used, resets "))
        self.assertEqual(d.format_limits("anthropic", {"unavailable": "no credential"}), "anthropic: unavailable (no credential)")


class LimitPrecheck(unittest.TestCase):
    NOW = 1000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state, d.STATE = d.STATE, Path(self.tmp.name) / "state.json"
        self.saved = dict(d.RUNNERS)
        self.ran = []
        for n in ("hermes", "codex", "agy"):
            d.RUNNERS[n] = (lambda name: lambda *a: (self.ran.append(name), d.Result(True, name, 1))[1])(n)
        self.asked = []

    def tearDown(self):
        d.STATE = self.real_state
        d.RUNNERS.clear()
        d.RUNNERS.update(self.saved)
        self.tmp.cleanup()

    def limits(self, pct, resets=5000.0):
        def fn(provider):
            self.asked.append(provider)
            return {"plan": "Plus", "windows": [{"label": "Session", "used_percent": pct, "resets_at": resets}]}
        return fn

    def go(self, providers=("hermes", "codex", "agy"), **kw):
        return d.delegate("q", list(providers), ".", now=lambda: self.NOW, **kw)

    def test_full_window_skips_both_chatgpt_providers_without_spending_a_call(self):
        self.assertEqual(self.go(limits_fn=self.limits(97.0)), (0, "agy"))
        self.assertEqual(self.ran, ["agy"])
        self.assertEqual(self.asked, ["openai-codex"])  # read once for both
        st = d.load_state()
        self.assertEqual((st["hermes"]["blocked_until"], st["codex"]["blocked_until"]), (5000.0, 5000.0))
        self.assertIn("Session window 97% used", st["codex"]["block_reason"])

    def test_room_left_runs_the_first_provider(self):
        self.assertEqual(self.go(limits_fn=self.limits(40.0)), (0, "hermes"))
        self.assertEqual(self.ran, ["hermes"])

    def test_a_window_that_has_already_reset_does_not_block(self):
        self.assertEqual(self.go(limits_fn=self.limits(99.0, resets=900.0)), (0, "hermes"))

    def test_stop_at_zero_does_not_look(self):
        self.assertEqual(self.go(limits_fn=self.limits(99.0), stop_at=0), (0, "hermes"))
        self.assertEqual(self.asked, [])

    def test_no_chatgpt_provider_no_look(self):
        self.assertEqual(self.go(("agy",), limits_fn=self.limits(99.0)), (0, "agy"))
        self.assertEqual(self.asked, [])

    def test_unknown_limits_do_not_stop_the_call(self):
        import contextlib
        import io

        def unknown(provider):
            raise d.LimitsUnavailable("hermes not installed")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.go(limits_fn=unknown), (0, "hermes"))
        self.assertIn("not checked: hermes not installed", err.getvalue())

    def test_everything_blocked_is_exit_3(self):
        self.assertEqual(self.go(("hermes", "codex"), limits_fn=self.limits(99.0))[0], 3)
        self.assertEqual(self.ran, [])

    def test_reset_clears_the_reason_too(self):
        self.go(limits_fn=self.limits(97.0))
        d.cmd_reset(argparse.Namespace(provider=None))
        self.assertNotIn("block_reason", d.load_state()["codex"])
        self.assertNotIn("blocked_until", d.load_state()["codex"])

    def test_expired_mark_loses_its_reason(self):
        self.go(limits_fn=self.limits(97.0, resets=1500.0))
        d.RUNNERS["hermes"] = lambda *a: d.Result(True, "hermes", 1)
        later = d.delegate("q", ["hermes"], ".", now=lambda: 2000.0, limits_fn=self.limits(10.0))
        self.assertEqual(later, (0, "hermes"))
        self.assertNotIn("block_reason", d.load_state()["hermes"])

    def test_status_json_shape(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            d.cmd_status(argparse.Namespace(json=True, limits=False))
        data = json.loads(out.getvalue())
        self.assertEqual(sorted(data), ["limits", "providers"])
        self.assertEqual(data["limits"], {})
        self.assertIn("nous", [r["provider"] for r in data["providers"]])


class UnsandboxedGate(unittest.TestCase):
    """hermes and nous have no sandbox, so they only run when the caller says the input is trusted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name) / "project"
        self.src.mkdir()
        (self.src / "a.txt").write_text("x")
        self.called = []
        self.real = (d._run, d.find_exe)
        d._run = lambda *a, **k: self.called.append(1)
        d.find_exe = lambda n: "hermes"
        self.addCleanup(lambda: (setattr(d, "_run", self.real[0]), setattr(d, "find_exe", self.real[1]), self.tmp.cleanup()))

    def test_refused_without_the_flag_and_nothing_is_run_or_copied(self):
        for runner in (d.run_hermes, d.run_nous):
            res = runner("q", self.src, False, {}, 5)
            self.assertFalse(res.ok)
            self.assertIn("--allow-unsandboxed", res.err)
            self.assertIn("not sandboxed", res.err)
        self.assertEqual(self.called, [])

    def test_flag_reaches_every_provider_option(self):
        self.assertEqual(d.build_options(["agy", "hermes", "nous"], None, None, True),
                         {n: {"allow_unsandboxed": True} for n in ("agy", "hermes", "nous")})
        self.assertEqual(d.build_options(["hermes"], None, None), {"hermes": {}})

    def test_cli_flag_is_parsed_for_run_and_check(self):
        for command in ("run", "check"):
            text = subprocess.run([sys.executable, "-I", d.__file__, command, "-h"], capture_output=True, text=True).stdout
            self.assertIn("--allow-unsandboxed", text)

    def test_unsandboxed_providers_fall_through_to_a_sandboxed_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_state, d.STATE = d.STATE, Path(tmp) / "state.json"
            saved = dict(d.RUNNERS)
            d.RUNNERS["agy"] = lambda *a: d.Result(True, "from agy", 1)
            try:
                self.assertEqual(d.delegate("q", ["hermes", "nous", "agy"], self.src, opts={"hermes": {}, "nous": {}}),
                                 (0, "from agy"))
            finally:
                d.STATE = real_state
                d.RUNNERS.clear()
                d.RUNNERS.update(saved)

    def test_links_and_junctions_are_not_copied(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside")
        (self.src / "real").mkdir()
        (self.src / "real" / "b.txt").write_text("b")
        made = []
        if os.name == "nt":
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(self.src / "junction"), str(outside)], capture_output=True)
            made.append("junction") if r.returncode == 0 else None
        else:
            os.symlink(outside, self.src / "junction")
            made.append("junction")
        try:
            os.symlink(outside / "secret.txt", self.src / "filelink")
            made.append("filelink")
        except OSError:  # creating file symlinks needs a privilege on Windows
            pass
        if not made:
            self.skipTest("could not create any link on this system")
        dst = Path(self.tmp.name) / "copy"
        d.copy_workdir(self.src, dst)
        names = sorted(str(p.relative_to(dst)).replace(os.sep, "/") for p in dst.rglob("*"))
        self.assertEqual(names, ["a.txt", "real", "real/b.txt"])
        self.assertTrue((outside / "secret.txt").exists())  # and nothing was touched through the link


class NousFreeTier(unittest.TestCase):
    def test_only_free_models_are_accepted(self):
        self.assertIsNone(d.check_options("nous", {"model": "stepfun/step-3.7-flash:free", "effort": "low"}))
        self.assertIsNone(d.check_options("nous", {}))
        self.assertIn("free tier only", d.check_options("nous", {"model": "vendor/paid-model"}))
        self.assertIn("not one of", d.check_options("nous", {"effort": "extreme"}))

    def test_default_model_is_a_free_one_and_the_provider_is_nous(self):
        self.assertTrue(d.NOUS_DEFAULT_MODEL.endswith(":free"))
        seen = {}
        real = d.run_hermes_as
        d.run_hermes_as = lambda provider, model, *a: seen.update(provider=provider, model=model) or d.Result(True, "x", 1)
        try:
            d.run_nous("q", ".", False, {}, 5)
        finally:
            d.run_hermes_as = real
        self.assertEqual(seen, {"provider": "nous", "model": d.NOUS_DEFAULT_MODEL})

    def test_nous_is_registered_but_not_a_default_provider(self):
        self.assertIn("nous", d.RUNNERS)
        for command in ("run", "check"):
            help_text = subprocess.run([sys.executable, "-I", d.__file__, command, "-h"], capture_output=True, text=True).stdout
            self.assertIn("default: agy,codex", help_text)  # neither unsandboxed provider is tried unless named
            self.assertNotIn("default: hermes", help_text)
            self.assertNotIn(",nous", help_text)


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
