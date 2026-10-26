#!/usr/bin/env python3
"""Hand a prompt to Codex or Antigravity (agy) and fall back when a subscription runs out.

    delegate.py run [--providers agy,codex] [--cwd DIR] [--write] PROMPT|-
    delegate.py status [--json]
    delegate.py reset [PROVIDER]

stdout of `run` is only the model's answer; diagnostics go to stderr.
Exit codes: 0 answered, 3 every provider is out of quota, 4 a provider failed for another reason.
"""
import argparse
import json
import os
import re
import shutil
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


def run_codex(prompt, cwd, write, model, timeout):
    exe = shutil.which("codex")
    if not exe:
        return Result(False, err="codex not installed")
    cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--json",
           "-s", "workspace-write" if write else "read-only", "-C", str(cwd)]
    if model:
        cmd += ["-m", model]
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


def run_agy(prompt, cwd, write, model, timeout):
    exe = shutil.which("agy")
    if not exe:
        return Result(False, err="agy not installed")
    if len(prompt) > MAX_ARG_PROMPT:
        return Result(False, err=f"prompt is {len(prompt)} chars; put the context in files and refer to their paths")
    cmd = [exe, f"-p={prompt}", "--output-format", "json", "--print-timeout", f"{timeout}s",
           "--mode", "accept-edits" if write else "plan"]
    if model:
        cmd += ["--model", model]
    p = _run(cmd, cwd, timeout)
    data = parse_agy(p.stdout)
    if data and data.get("status") == "SUCCESS" and p.returncode == 0:
        return Result(True, data.get("response", ""), data.get("usage", {}).get("total_tokens", 0))
    return Result(False, err=f"exit {p.returncode}\n{p.stdout}\n{p.stderr}")


RUNNERS = {"codex": run_codex, "agy": run_agy}


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


def delegate(prompt, providers, cwd, write=False, model=None, timeout=600, cooldown=DEFAULT_COOLDOWN, now=time.time):
    """Returns (exit_code, text). Tries providers in order, skipping and recording exhausted ones."""
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
            res = RUNNERS[name](prompt, cwd, write, model, timeout)
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
    if args.model and len(names) != 1:
        sys.exit("--model needs exactly one provider")
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    code, text = delegate(prompt, names, Path(args.cwd).resolve(), args.write, args.model, args.timeout, args.cooldown)
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
    r.add_argument("--model")
    r.add_argument("--timeout", type=int, default=600)
    r.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="seconds to block a provider when no reset time is given")
    r.set_defaults(fn=cmd_run)
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
