#!/usr/bin/env python3
"""Hand a prompt to Codex or Antigravity (agy) and fall back when a subscription runs out.

    delegate.py run [--providers agy,codex] [--cwd DIR] [--write] [--model [P=]M] [--effort [P=]E] PROMPT|-
    delegate.py models [--json]
    delegate.py status [--json]
    delegate.py reset [PROVIDER]

--model and --effort are repeatable. With several providers prefix the value with the provider
(`--model codex=gpt-6.1-sol --model agy=gemini-3.8-flash-low`); with one provider the prefix is optional.
`models` lists what each provider accepts. Codex runs with --ignore-user-config and --ignore-rules (no MCP servers,
no personal rules, fewer tokens), so its config.toml model and effort do not apply: pass --model for codex.

stdout of `run` is only the model's answer; diagnostics go to stderr.
Exit codes: 0 answered, 2 bad arguments, 3 every provider is out of quota, 4 a provider failed for another reason.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

STATE = Path(os.environ.get("LLM_DELEGATE_STATE")
             or Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "llm-delegate" / "state.json")
DEFAULT_COOLDOWN = 3600  # shortcut: used when the CLI gives no reset time, one retry per hour costs one cheap failed call
HISTORY_CAP = 1000
MAX_ARG_PROMPT = 30000  # agy takes the prompt as an argument; Windows caps a command line near 32k
# shortcut: patterns are guesses from public reports, not captured from a real exhausted account; extend on first real miss
QUOTA_RE = re.compile(r"usage limit|rate.?limit|quota|resource.?exhausted|\b429\b|too many requests"
                      r"|exceeded your|out of credits", re.I)
RETRY_RE = re.compile(r"(?:try again|retry|resets?)\s+(?:in|after)\s+((?:\d+\s*[dhms]\w*[\s,]*(?:and\s+)?)+)", re.I)
UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}
AGY_EFFORTS = ("low", "medium", "high", "xhigh", "max")  # from `agy --help`; per-model support is not published


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


def _run(cmd, cwd, timeout, stdin_text=None):
    kw = {"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **kw)


def parse_codex(out):
    text, tokens, done = "", 0, False
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "item.completed" and ev.get("item", {}).get("type") == "agent_message":
            text = ev["item"].get("text", "")
        elif ev.get("type") == "turn.completed":
            done = True
            u = ev.get("usage", {})
            tokens = u.get("input_tokens", 0) + u.get("output_tokens", 0)
    return done and bool(text), text, tokens


def run_codex(prompt, cwd, write, o, timeout):
    exe = find_exe("codex")
    if not exe:
        return Result(False, err="codex not installed")
    cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--json", "--ignore-user-config", "--ignore-rules",
           "-s", "workspace-write" if write else "read-only", "-C", str(cwd)]
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
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or ev.get("error", {}).get("message") or (
            ev["item"].get("message") if ev.get("item", {}).get("type") == "error" else None)
        if msg:
            msgs.append(msg)
    return "\n".join(msgs) or out


def parse_agy(out):
    for line in reversed(out.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return None


def run_agy(prompt, cwd, write, o, timeout):
    exe = find_exe("agy")
    if not exe:
        return Result(False, err="agy not installed")
    if len(prompt) > MAX_ARG_PROMPT:
        return Result(False, err=f"prompt is {len(prompt)} chars; put the context in files and refer to their paths")
    cmd = [exe, f"-p={prompt}", "--output-format", "json", "--print-timeout", f"{timeout}s",
           "--mode", "accept-edits" if write else "plan"]
    if o.get("model"):
        cmd += ["--model", o["model"]]
    if o.get("effort"):
        cmd += ["--effort", o["effort"]]
    p = _run(cmd, cwd, timeout)
    data = parse_agy(p.stdout)
    # agy sometimes reports SUCCESS with an empty response; that is no answer, so let the next provider try
    if data and data.get("status") == "SUCCESS" and p.returncode == 0 and data.get("response", "").strip():
        return Result(True, data["response"], data.get("usage", {}).get("total_tokens", 0))
    return Result(False, err=f"exit {p.returncode}\n{p.stdout}\n{p.stderr}")


RUNNERS = {"codex": run_codex, "agy": run_agy}


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_models(include_hidden=False):
    """Models from Codex's own cache (efforts differ per model). [] when the cache is missing or unreadable."""
    try:
        data = json.loads((codex_home() / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [{"model": m["slug"], "default_effort": m.get("default_reasoning_level"),
             "efforts": [e["effort"] for e in m.get("supported_reasoning_levels", [])]}
            for m in data.get("models", []) if include_hidden or m.get("visibility") == "list"]


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
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


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
        p = st.setdefault(name, {"history": []})
        if p.get("blocked_until", 0) > now():
            print(f"[delegate] {name}: skipped, out of quota until {time.strftime('%H:%M', time.localtime(p['blocked_until']))}",
                  file=sys.stderr)
            quota_blocked += 1
            continue
        try:
            res = RUNNERS[name](prompt, cwd, write, opts.get(name, {}), timeout)
        except subprocess.TimeoutExpired:
            res = Result(False, err=f"timeout after {timeout}s")
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
    print("codex  runs without your config.toml: always pass --model; without --effort the model's own default applies")
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


def cmd_run(args):
    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    unknown = [n for n in names if n not in RUNNERS]
    if unknown or not names:
        sys.exit(f"unknown provider(s): {unknown or 'none given'}; known: {', '.join(RUNNERS)}")
    try:
        opts = build_options(names, args.model, args.effort)
    except ValueError as e:
        print(f"[delegate] {e}", file=sys.stderr)
        return 2
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    code, text = delegate(prompt, names, Path(args.cwd).resolve(), args.write, opts, args.timeout, args.cooldown)
    if text:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return code


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
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="seconds to block a provider when no reset time is given")
    r.set_defaults(fn=cmd_run)
    m = sub.add_parser("models", help="list the models and efforts each provider accepts")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_models)
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
