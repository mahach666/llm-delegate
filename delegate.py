#!/usr/bin/env python3
"""Hand a prompt to Codex or Antigravity (agy) and fall back when a subscription runs out.

    delegate.py run [--providers agy,codex] [--cwd DIR] [--write] [--model [P=]M] [--effort [P=]E] PROMPT|-
    delegate.py models [--json]
    delegate.py check [--providers agy,codex] [--model [P=]M] [--effort [P=]E]
    delegate.py status [--json]
    delegate.py reset [PROVIDER]

--model and --effort are repeatable. With several providers prefix the value with the provider
(`--model codex=gpt-6.1-sol --model agy=gemini-3.8-flash-low`); with one provider the prefix is optional.
`models` lists what each provider accepts. Codex runs with --ignore-user-config and --ignore-rules, so your MCP
servers, `notify` command and rules do not apply and Codex's own default model is used unless you pass --model. The
one setting passed back is `[windows] sandbox` (without it Codex cannot run commands on Windows).
`check` runs a real read-the-file test on each provider; run it after changing flags, models or either CLI.

`--timeout SECONDS` (default 600; 0 = no limit) is per provider; when it expires the provider's whole process tree is
killed and the next provider is tried. For tasks that run longer than your shell tool allows in the foreground,
start the whole command in the background and use `--timeout 0` or a large value.

stdout of `run` is only the model's answer; diagnostics go to stderr.
Exit codes: 0 answered, 2 bad arguments, 3 every provider is out of quota, 4 a provider failed for another reason.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

STATE = Path(os.environ.get("LLM_DELEGATE_STATE")
             or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "llm-delegate" / "state.json")
DEFAULT_COOLDOWN = 3600  # shortcut: used when the CLI gives no reset time, one retry per hour costs one cheap failed call
HISTORY_CAP = 1000
MAX_ARG_PROMPT = 30000  # agy takes the prompt as an argument; Windows caps a command line near 32k
MAX_WINDOWS_CMDLINE = 32000  # CreateProcess fails above 32767 characters
EMPTY_RETRY_MAX_SECONDS = 60  # an empty agy answer is retried once only if the first try was this quick
# shortcut: patterns are guesses from public reports, not captured from a real exhausted account; extend on first real miss
QUOTA_RE = re.compile(r"usage limit|rate.?limit|quota|resource.?exhausted|\b429\b|too many requests"
                      r"|exceeded your|out of credits", re.I)
RETRY_RE = re.compile(r"(?:try again|retry|resets?)\s+(?:in|after)\s+((?:\d+\s*[dhms]\w*[\s,]*(?:and\s+)?)+)", re.I)
UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}
AGY_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # from `agy --help`; per-model support is not published
# In --mode plan agy often answers with "I prepared a plan, please approve it" instead of the answer. And when it tries
# to run a shell command nobody can approve it, the permission is denied and the turn ends with an empty response
# (seen in agy's conversation database: "user denied permission to run command"). This steers it away from both.
AGY_SUFFIX = ("\n\nReply with the answer as plain text in this message. Do not write a plan or create any files. "
              "You cannot run shell commands here: any command is denied and ends the session without an answer. "
              "Read files with your file-viewing tool only.")


class Result:
    def __init__(self, ok, text="", tokens=0, err=""):
        self.ok, self.text, self.tokens, self.err = ok, text, tokens, err


def parse_cooldown(text):
    """Seconds from 'try again in 3h 20m' style hints, or None."""
    m = RETRY_RE.search(text)
    if not m:
        return None
    total = sum(int(n) * UNIT[u.lower()] for n, u in re.findall(r"(\d+)\s*([dhms])", m.group(1), re.I))
    return max(total, 60) or None


def find_exe(name):
    """Path of a real executable found in an absolute PATH entry other than the current directory.

    Only `<name>.exe` is accepted on Windows: a .cmd/.bat would be reparsed by cmd.exe with the prompt in it.
    """
    cwd = Path.cwd().resolve()
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or not Path(entry).is_absolute() or Path(entry).resolve() == cwd:
            continue
        cand = Path(entry) / (name + ".exe" if os.name == "nt" else name)
        if cand.is_file() and (os.name == "nt" or os.access(cand, os.X_OK)):
            return str(cand)
    return None


def kill_tree(proc):
    """Kill proc and everything it started. proc.kill() alone leaves grandchildren running."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session, see _run


def _run(cmd, cwd, timeout, stdin_text=None):
    """subprocess.run with a timeout (0 or None means no limit) that kills the whole process tree when it expires.

    subprocess.run only kills the direct child, and then waits for the pipes to close: a grandchild that still holds
    them keeps the call blocked far past the timeout (measured: a 3 s timeout returned after 25 s).
    """
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", start_new_session=(os.name != "nt"))
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout or None)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            proc.communicate(timeout=10)  # drain the pipes; the TimeoutExpired below is what the caller sees either way
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def json_objects(out):
    """The lines of `out` that are JSON objects; everything else (log noise, other JSON values) is skipped."""
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


def obj(value):
    """value if it is a JSON object, else {}: fields of external data are not guaranteed to be objects."""
    return value if isinstance(value, dict) else {}


def parse_codex(out):
    text, tokens, done = "", 0, False
    for ev in json_objects(out):
        item = obj(ev.get("item"))
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or ""
        elif ev.get("type") == "turn.completed":
            done = True
            u = obj(ev.get("usage"))
            tokens = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
    return done and bool(text), text, tokens


def run_codex(prompt, cwd, write, o, timeout):
    exe = find_exe("codex")
    if not exe:
        return Result(False, err="codex not installed")
    # Your config.toml would start your MCP servers and `notify` command inside a delegated run, so it is ignored. That
    # also drops [windows] sandbox, without which Codex cannot run any command, so that one setting is passed back.
    cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules",
           "-s", "workspace-write" if write else "read-only", "-C", str(cwd)]
    sandbox = codex_windows_sandbox()
    if sandbox:
        cmd += ["-c", f'windows.sandbox="{sandbox}"']
    if o.get("model"):
        cmd += ["-m", o["model"]]
    if o.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{o["effort"]}"']
    p = _run(cmd + ["-"], cwd, timeout, stdin_text=prompt)
    ok, text, tokens = parse_codex(p.stdout)
    if ok and p.returncode == 0:
        return Result(True, text, tokens)
    return Result(False, err=f"exit {p.returncode}\n{codex_errors(p.stdout)}\n{p.stderr}")


def codex_errors(out):
    """Readable error messages from the JSONL stream; the raw stream if none are found."""
    msgs = []
    for ev in json_objects(out):
        item = obj(ev.get("item"))
        err = ev.get("error")
        msg = ev.get("message") or obj(err).get("message") or (err if isinstance(err, str) else None) or (
            item.get("message") if item.get("type") == "error" else None)
        if msg:
            msgs.append(str(msg))
    return "\n".join(msgs) or out


def parse_agy(out):
    """The last line of `out` that is a JSON object (agy prints its result as one), or None."""
    return next(iter(reversed(list(json_objects(out)))), None)


def run_agy(prompt, cwd, write, o, timeout):
    exe = find_exe("agy")
    if not exe:
        return Result(False, err="agy not installed")
    if not write:
        prompt += AGY_SUFFIX
    if len(prompt) > MAX_ARG_PROMPT:
        return Result(False, err=f"prompt is {len(prompt)} chars; put the context in files and refer to their paths")
    cmd = [exe, f"-p={prompt}", "--output-format", "json", "--print-timeout", f"{timeout}s",
           "--mode", "accept-edits" if write else "plan"]
    if o.get("model"):
        cmd += ["--model", o["model"]]
    if o.get("effort"):
        cmd += ["--effort", o["effort"]]
    if os.name == "nt" and len(subprocess.list2cmdline(cmd)) > MAX_WINDOWS_CMDLINE:
        # quoting can grow the command line well past the prompt length (every " becomes \")
        return Result(False, err="prompt is too long for a Windows command line after quoting; put the context in files")
    for _ in range(2):
        p = _run(cmd, cwd, timeout)
        data = parse_agy(p.stdout) or {}
        success = data.get("status") == "SUCCESS" and p.returncode == 0
        if success and str(data.get("response") or "").strip():
            return Result(True, data["response"], obj(data.get("usage")).get("total_tokens") or 0)
        if not success or (data.get("duration_seconds") or 0) > EMPTY_RETRY_MAX_SECONDS:
            break
        # agy sometimes reports SUCCESS with an empty response (usually after a shell command it tried was denied).
        # A quick one is worth one more try; one that took minutes is not worth paying for twice.
    return Result(False, err=f"exit {p.returncode}\n{p.stdout}\n{p.stderr}")


RUNNERS = {"codex": run_codex, "agy": run_agy}


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_windows_sandbox():
    """The `[windows] sandbox` value from Codex's config.toml (e.g. "elevated"), or None (also off Windows)."""
    if os.name != "nt":
        return None
    try:
        with open(codex_home() / "config.toml", "rb") as f:
            value = obj(tomllib.load(f).get("windows")).get("sandbox")
    except FileNotFoundError:
        return None
    except tomllib.TOMLDecodeError as e:
        print(f"[delegate] codex config.toml is not valid TOML, windows.sandbox not passed on: {e}", file=sys.stderr)
        return None
    if value is not None and not re.fullmatch(r"[a-z_]+", str(value)):
        print(f"[delegate] unexpected windows.sandbox value {value!r} in codex config.toml, not passed on", file=sys.stderr)
        return None
    return value


def codex_models(include_hidden=False):
    """Models from Codex's own cache (efforts differ per model). [] when the cache is missing or unreadable."""
    try:
        data = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    models = [m for m in obj(data).get("models") or [] if isinstance(m, dict) and m.get("slug")]
    return [{"model": m["slug"], "default_effort": m.get("default_reasoning_level"),
             "efforts": [e["effort"] for e in m.get("supported_reasoning_levels") or [] if "effort" in obj(e)]}
            for m in models if include_hidden or m.get("visibility") == "list"]


def agy_models():
    """[{model, label}] from `agy models`. [] when agy is missing or the call fails."""
    exe = find_exe("agy")
    if not exe:
        return []
    try:
        p = _run([exe, "models"], None, 60)
    except subprocess.TimeoutExpired:
        return []
    rows = [line.split("\t", 1) for line in p.stdout.splitlines() if "\t" in line]
    return [{"model": r[0].strip(), "label": r[1].strip()} for r in rows]


def check_options(name, o):
    """Error text if the model/effort pair is known to be invalid for the provider, else None."""
    model, effort = o.get("model"), o.get("effort")
    if name == "agy" and effort and effort not in AGY_EFFORTS:
        return f"agy effort {effort!r} is not one of {', '.join(AGY_EFFORTS)}"
    if name == "codex" and (model or effort):
        known = {m["model"]: m for m in codex_models(include_hidden=True)}
        if model and known and model not in known:
            return f"codex has no model {model!r}; known: {', '.join(m['model'] for m in codex_models())}"
        m = known.get(model)  # without --model Codex's built-in default applies and its efforts are unknown
        if effort and m and effort not in m["efforts"]:
            return f"codex model {m['model']} does not support effort {effort!r}; it supports {', '.join(m['efforts'])}"
    return None


def per_provider(values, names, flag):
    """Turn repeated `[provider=]value` options into {provider: value}."""
    out = {}
    for v in values or []:
        head, sep, rest = v.partition("=")
        if sep and head in RUNNERS:
            name, val = head, rest
        elif len(names) == 1:
            name, val = names[0], v
        else:
            raise ValueError(f"{flag} {v!r}: with several providers write provider=value")
        if name not in names:
            raise ValueError(f"{flag} {v!r}: {name} is not in --providers")
        if name in out:
            raise ValueError(f"{flag} given twice for {name}")
        out[name] = val
    return out


def build_options(names, models, efforts):
    ms, es = per_provider(models, names, "--model"), per_provider(efforts, names, "--effort")
    opts = {n: {k: v for k, v in (("model", ms.get(n)), ("effort", es.get(n))) if v} for n in names}
    for n, o in opts.items():
        err = check_options(n, o)
        if err:
            raise ValueError(err)
    return opts


def load_state():
    """The saved state; empty when there is none. A damaged file is reported and replaced by an empty state on the next save."""
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        print(f"[delegate] {STATE} is not valid JSON, starting with empty state: {e}", file=sys.stderr)
        return {}
    if not isinstance(st, dict):
        print(f"[delegate] {STATE} does not hold a JSON object, starting with empty state", file=sys.stderr)
        return {}
    return st


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE)


def delegate(prompt, providers, cwd, write=False, opts=None, timeout=600, cooldown=DEFAULT_COOLDOWN, now=time.time):
    """Returns (exit_code, text). Tries providers in order, skipping and recording exhausted ones.

    opts maps provider -> {"model", "effort"}.
    """
    opts = opts or {}
    st = load_state()
    quota_blocked, failed = 0, 0
    for name in providers:
        p = st.setdefault(name, {})
        p.setdefault("history", [])
        if p.get("blocked_until", 0) > now():
            print(f"[delegate] {name}: skipped, out of quota until {time.strftime('%H:%M', time.localtime(p['blocked_until']))}",
                  file=sys.stderr)
            quota_blocked += 1
            continue
        try:
            res = RUNNERS[name](prompt, cwd, write, opts.get(name, {}), timeout)
        except subprocess.TimeoutExpired:
            res = Result(False, err=f"timeout after {timeout}s")
        except OSError as e:  # the provider could not even be started (bad --cwd, executable unusable): try the next one
            res = Result(False, err=f"could not start {name}: {e}")
        if res.ok:
            p["history"] = (p["history"] + [[now(), res.tokens]])[-HISTORY_CAP:]
            p.pop("blocked_until", None)
            p.pop("last_error", None)
            save_state(st)
            print(f"[delegate] {name}: ok, {res.tokens} tokens", file=sys.stderr)
            return 0, res.text
        p["last_error"] = res.err.strip()[:500]
        if QUOTA_RE.search(res.err):
            p["blocked_until"] = now() + (parse_cooldown(res.err) or cooldown)
            quota_blocked += 1
            print(f"[delegate] {name}: out of quota, blocked for {int(p['blocked_until'] - now())}s", file=sys.stderr)
        else:
            failed += 1
            print(f"[delegate] {name}: failed: {p['last_error'][:200]}", file=sys.stderr)
    save_state(st)
    return (4 if failed else 3), ""


def describe(st, name, now=None):
    now = now or time.time()
    p = st.get(name, {})
    h = p.get("history", [])

    def window(sec):
        w = [t for ts, t in h if now - ts <= sec]
        return len(w), sum(w)

    blocked = p.get("blocked_until", 0)
    return {"provider": name,
            "available": blocked <= now,
            "blocked_until": blocked if blocked > now else None,
            "calls_5h": window(5 * 3600)[0], "calls_24h": window(86400)[0], "calls_7d": window(7 * 86400)[0],
            "tokens_24h": window(86400)[1], "last_error": p.get("last_error")}


def cmd_status(args):
    st = load_state()
    rows = [describe(st, n) for n in RUNNERS]
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for r in rows:
        state = "available" if r["available"] else "OUT OF QUOTA until " + time.strftime("%H:%M", time.localtime(r["blocked_until"]))
        print(f"{r['provider']:6} {state} | calls 5h/24h/7d {r['calls_5h']}/{r['calls_24h']}/{r['calls_7d']}"
              f" | tokens 24h {r['tokens_24h']}" + (f" | last error: {r['last_error'][:80]}" if r["last_error"] else ""))
    return 0


def cmd_models(args):
    cx, ag = codex_models(), agy_models()
    if args.json:
        print(json.dumps({"codex": {"models": cx}, "agy": {"efforts": list(AGY_EFFORTS), "models": ag}}, indent=1))
        return 0
    print("codex  runs without your config.toml: pass --model; without --effort the model's own default applies")
    for m in cx:
        print(f"  {m['model']:16} model default effort {m['default_effort']}; efforts: {', '.join(m['efforts'])}")
    if not cx:
        print(f"  (no model list: {codex_home() / 'models_cache.json'} missing or unreadable; any -m value is passed through)")
    print(f"agy    --effort: {', '.join(AGY_EFFORTS)}; without --model the CLI picks its own default")
    for m in ag:
        print(f"  {m['model']:26} {m['label']}")
    if not ag:
        print("  (`agy models` returned nothing)")
    return 0


def cmd_reset(args):
    st = load_state()
    for n in ([args.provider] if args.provider else list(st)):
        st.get(n, {}).pop("blocked_until", None)
    save_state(st)
    return 0


def check_provider(name, opts, timeout=300):
    """Have the provider read a file whose content it can only learn by reading it. Returns (ok, detail)."""
    word = "check-" + os.urandom(4).hex()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        (Path(d) / "note.txt").write_text(f"The secret word is {word}.\n", encoding="utf-8")
        started = time.time()
        try:
            res = RUNNERS[name]("Read the file note.txt in the current directory and print the secret word it contains.",
                                Path(d), False, opts.get(name, {}), timeout)
        except subprocess.TimeoutExpired:
            return False, f"timeout after {timeout}s"
    if not res.ok:
        return False, res.err.strip()[:300]
    if word not in res.text:
        return False, "answered without the word: " + res.text.strip()[:200]
    return True, f"{res.tokens} tokens, {time.time() - started:.0f}s"


def cmd_check(args):
    names, opts = providers_and_options(args)
    if opts is None:
        return 2
    code = 0
    for n in names:
        ok, detail = check_provider(n, opts, args.timeout)
        print(f"{n:6} {'PASS' if ok else 'FAIL'}  {detail}")
        code = code or (0 if ok else 4)
    return code


def providers_and_options(args):
    """(names, opts) from --providers/--model/--effort; opts is None after printing an error."""
    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    unknown = [n for n in names if n not in RUNNERS]
    if unknown or not names:
        print(f"[delegate] unknown provider(s): {unknown or 'none given'}; known: {', '.join(RUNNERS)}", file=sys.stderr)
        return names, None
    try:
        return names, build_options(names, args.model, args.effort)
    except ValueError as e:
        print(f"[delegate] {e}", file=sys.stderr)
        return names, None


def cmd_run(args):
    names, opts = providers_and_options(args)
    if opts is None:
        return 2
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    code, text = delegate(prompt, names, Path(args.cwd).resolve(), args.write, opts, args.timeout, args.cooldown)
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return code


def seconds(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 (no limit) or a positive number of seconds")
    return n


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("prompt", help="prompt text, or - to read stdin")
    r.add_argument("--providers", default="agy,codex", help="try in this order (default: agy,codex)")
    r.add_argument("--cwd", default=".")
    r.add_argument("--write", action="store_true", help="allow edits in cwd (default is read-only)")
    r.add_argument("--model", action="append", metavar="[PROVIDER=]MODEL")
    r.add_argument("--effort", action="append", metavar="[PROVIDER=]LEVEL", help="reasoning effort, see `models`")
    r.add_argument("--timeout", type=seconds, default=600, metavar="SECONDS",
                   help="give up on a provider after this long and try the next; 0 means no limit (default 600)")
    r.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="seconds to block a provider when no reset time is given")
    r.set_defaults(fn=cmd_run)
    m = sub.add_parser("models", help="list the models and efforts each provider accepts")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_models)
    c = sub.add_parser("check", help="make each provider read a test file; spends tokens on every provider")
    c.add_argument("--providers", default="agy,codex")
    c.add_argument("--model", action="append", metavar="[PROVIDER=]MODEL")
    c.add_argument("--effort", action="append", metavar="[PROVIDER=]LEVEL")
    c.add_argument("--timeout", type=seconds, default=300, metavar="SECONDS", help="0 means no limit (default 300)")
    c.set_defaults(fn=cmd_check)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)
    z = sub.add_parser("reset", help="clear the out-of-quota mark")
    z.add_argument("provider", nargs="?")
    z.set_defaults(fn=cmd_reset)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
