# llm-delegate

A small command-line wrapper that hands a prompt to **Hermes Agent**, **Antigravity CLI (`agy`)** or **Codex CLI**,
picks the model and reasoning effort, and moves on to the next provider when a subscription runs out of quota.

It exists so a host coding agent (or a person) can offload bulky reading and mechanical work to the subscriptions
already paid for, instead of spending the host's own tokens. One file, no dependencies, standard library only.

```
$ python delegate.py run "Read src/ and summarise the module layout in 15 lines"
[delegate] agy: ok, 23187 tokens
<the answer, and nothing else, on stdout>
```

## What it does

- **Runs one prompt** through `hermes`, `agy` or `codex` in non-interactive mode and prints only the answer to
  stdout. Diagnostics go to stderr, so the output can be piped or captured as is.
- **Falls back.** Providers are tried in the order given (default `agy,codex`). If one reports a usage limit,
  it is marked as exhausted until the reset time (taken from the error text when present, otherwise one hour) and is
  skipped on later calls. Any other failure moves on to the next provider without a mark.
- **Chooses model and effort** per provider (`--model`, `--effort`) and rejects combinations Codex is known not to
  support before spending a call.
- **Keeps a usage ledger** (calls in the last 5 hours, 24 hours and 7 days, tokens in the last 24 hours) so a
  caller can look before sending a large batch.
- **Reads the ChatGPT subscription's usage windows** (`hermes usage`) and skips `hermes` and `codex`, without
  spending a call, while a window is at or above 95 percent. `status` shows them next to the ledger.
- **Can use the Nous Portal free tier** through Hermes (`nous`), opt-in because of its data-use terms.
- **Is read-only by default** for `codex` (`-s read-only`) and `agy` (`--mode plan`); `--write` allows edits in
  `--cwd`. **`hermes` and `nous` are not sandboxed** and refuse to run unless you pass `--allow-unsandboxed` (see
  Hermes below).

## Requirements

- Python 3.11 or newer (`tomllib`). Developed and tested on 3.14 on Windows 11; other versions and other systems are
  untested.
- `hermes`, `codex` and/or `agy` installed, on `PATH` and already signed in. This tool does not log in for you.
  Hermes is used through its ChatGPT login: `hermes auth add openai-codex --type oauth`. The free tier needs the Nous
  Portal login (`hermes auth upgrade` or `hermes portal`).

## Usage

```
delegate.py run [--providers agy,codex] [--model [P=]M] [--effort [P=]E] [--cwd DIR] [--write]
                [--allow-unsandboxed] [--timeout SECONDS] [--cooldown SECONDS] [--stop-at PERCENT] PROMPT|-
delegate.py models [--json]
delegate.py check [--providers agy,codex] [--model [P=]M] [--effort [P=]E] [--allow-unsandboxed]
delegate.py status [--json] [--no-limits]
delegate.py reset [PROVIDER]
```

`PROMPT` may be `-` to read it from stdin. `hermes` and `agy` receive it as a command-line argument, so very long
prompts are refused (see Safety notes).

### Model and effort

With one provider the value is plain; with several, prefix it with the provider:

```
python delegate.py run --providers codex --model gpt-6-luna --effort low "Rename foo to bar in utils.py"
python delegate.py run --providers agy,codex --model agy=gemini-3.8-flash-low --model codex=gpt-6-luna "..."
```

`python delegate.py models` lists what each provider accepts on the current machine: Codex's list comes from its own
`models_cache.json`, `agy`'s from `agy models`.

Codex is started with `--ignore-user-config --ignore-rules`, so the MCP servers, `notify` command and rules from your
`config.toml` do not run inside a delegated session, and without `--model` Codex uses its own default model, not the
one from that file. Pass `--model`. Those flags also drop `[windows] sandbox`, without which Codex on Windows cannot
run any command and answers that it can't read files. The tool therefore reads that one value from your `config.toml`
and passes it back with `-c windows.sandbox=...`.

### Hermes (not sandboxed)

[Hermes Agent](https://github.com/nousresearch/hermes-agent) is a full agent, not a plain CLI, and it cannot be made
read-only. Its one-shot mode (`hermes -z`) bypasses approvals, and the smallest toolset that reads files, `file`, also
writes them: single tools cannot be picked (`-t read_file,search_files` is rejected). **Tested: with `-t file` it wrote
a file in the working directory, at `../name` and at an absolute path outside it.** Hermes documents a guard,
`HERMES_WRITE_SAFE_ROOT`, for exactly this; setting it for the call changed nothing, because the file tools run in a
worker started by Hermes's own long-running daemon, which does not receive this process's environment. Setting it
for the daemon would limit every use of Hermes on the machine, so the tool does not.

What follows from that:

- **`hermes` and `nous` refuse to run unless `--allow-unsandboxed` is given**, and they are not in the default
  order. Pass the flag only for input you wrote or trust. A prompt injected through the input (a file, a log, a web
  page) could make the agent write or overwrite files anywhere the user can, which can mean code that runs later.
  Use `agy` or `codex` for anything untrusted.
- The tool still runs it with `-t file` (no terminal), `--safe-mode` (no config, memory, plugins or MCP servers) and
  in a throwaway copy of `--cwd` that is deleted afterwards. That keeps an ordinary run from changing the original
  through a relative path, nothing more. Symbolic links and junctions are not copied, `.git`, `node_modules`,
  `__pycache__`, `.venv` and `venv` are skipped, and a directory over 200 MB is refused.
- `--write` is refused for Hermes. File names in its answer refer to the copy.
- Its file tools can also read any file the user can, and what they read goes to the model provider.
- `--safe-mode` ignores Hermes's own config, so a model is always passed: `gpt-6.1-sol` unless you give `--model`.
  The provider is fixed to `openai-codex` (the ChatGPT login), the same subscription Codex CLI uses.
- Hermes does not read the prompt from stdin (`-z -` is taken as the text `-`), so the prompt is an argument.

Measured on one machine with `gpt-6.1-sol`: reading a one-line file took 7 400 tokens and 15 seconds (Codex CLI:
30 000 to 38 000), and a review of a 437-line file 14 500 tokens and 30 seconds (Codex CLI: 154 000 tokens, 40
seconds), with findings at least as good, all confirmed by hand. One task, one run per setting; Hermes was given only
file tools while Codex CLI could run commands to reproduce what it found, which explains part of the gap. That
saving is real, and it is why the flag exists instead of the provider being removed.

### Check

```
python delegate.py check [--providers agy,codex] [--model [P=]M] [--effort [P=]E] [--allow-unsandboxed]
```

Asks each provider to read a file whose contents it can only learn by reading it, and prints `PASS` or `FAIL`.
Run it after changing models, flags or either CLI, and before a large delegation. It costs tokens on every provider
(roughly 20 000 to 45 000 each). A provider can answer "I can't read files" and still exit successfully, and `run`
has no way to tell that from a real answer, so this is the way to catch it.

### Status and reset

```
$ python delegate.py status
hermes  available | calls 5h/24h/7d 1/1/1 | tokens 24h 11271
codex   available | calls 5h/24h/7d 0/0/0 | tokens 24h 0
agy     OUT OF QUOTA until 19:48 | calls 5h/24h/7d 1/3/3 | tokens 24h 38120 | last error: ...
nous    available | calls 5h/24h/7d 0/0/0 | tokens 24h 0
subscription limits (hermes usage; hermes and codex share the chatgpt one):
  chatgpt (Plus): Session 7% used, resets 28.10 19:02; Weekly 1% used, resets 04.11 14:02
  anthropic: Current session 0% used, resets 28.10 21:00; Current week 60% used, resets 01.11 04:00
$ python delegate.py reset agy      # clear the mark if you know the quota is back
```

### Subscription limits

`hermes usage --provider openai-codex --json` returns the plan and the usage windows ("Session", about five hours, and
"Weekly") of the ChatGPT subscription, which `hermes` and `codex` both spend. Before the first of them is tried, `run`
reads it once (about 3 seconds) and skips both while any window is at or above `--stop-at` percent (default 95, `0`
turns the check off); the mark lasts until that window resets and says why. If the windows cannot be read, a line on
stderr says so and the call goes ahead. `status` also shows them, plus the `anthropic` windows Hermes can read with
its own login (informational, it is not a provider here), and skips the lookup with `--no-limits`.
`hermes usage` has no figures for the Google subscription behind `agy` or for the Nous free tier, so those are still
learned only from a failed call.

### Free tier (`nous`)

Hermes can use the free models of the [Nous Portal](https://portal.nousresearch.com/) (ids ending in `:free`). The
provider `nous` runs them the same way as `hermes`, with the same lack of a sandbox (see above): it needs
`--allow-unsandboxed`, is never in the default order and refuses a model that does not end in `:free`, so it cannot
spend paid credits.

**Data use: according to a third-party summary of the vendor's terms, inputs to the free tier may be used for training
or product improvement unless Privacy Mode is on.** Do not send private, proprietary or sensitive code or documents
through `nous`; check the current terms and the Privacy Mode setting on the portal first. The vendor publishes no rate
limit or quota figures for the free tier.

Tried on one account on 2026-10-28 (the model list changes; `longcat-2.0:free` was not available on it):

| Model | Read a one-line file | Review of a 437-line file (five findings asked for) |
|-------|----------------------|------------------------------------------------------|
| `reflection/beam-1.0-501b-a23b:free` (default) | 11 s, 5 600 tokens | 46 s, 19 700 tokens: 3 real findings, 2 doubtful |
| `stepfun/step-3.7-flash:free` | 29 s, 6 700 tokens | 279 s, 30 500 tokens: 5 plausible findings |
| `poolside/laguna-xs-2.1:free` | 13 s, 6 300 tokens | 14 s, 13 600 tokens: 2 findings, neither a real bug |
| `poolside/laguna-s-2.1:free`, `upstage/solar-mini4:free`, `ornith/ornith-1.5-122b:free` | 17 to 22 s | no answer within 280 s |

One run per cell. The paid `gpt-6.1-sol` through Hermes found more, in 30 seconds. The free tier suits reading and
low-stakes first passes, not a review you intend to trust.

### Long tasks

`--timeout SECONDS` is per provider and defaults to 600. When it expires the provider is killed together with
everything it started, and the next provider is tried. `--timeout 0` means no limit. A caller whose shell tool caps
foreground commands (often around 10 minutes) should start the whole `delegate.py` command in the background and read
the result when it finishes, with `--timeout 0` or a large value.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | A provider answered. |
| 2 | Bad arguments (unknown provider, unsupported model/effort pair, ...). |
| 3 | Every provider is out of quota. Nothing was answered. |
| 4 | A provider failed for another reason (timeout, error, empty answer). |

## How quota is detected

Reactively. Neither CLI has a documented non-interactive way to read the remaining quota, so the tool finds out when
a call fails: an error that matches usage-limit wording marks the provider as exhausted. An empty answer reported as
success (seen from `agy`) counts as a failure, so the next provider gets the prompt.

State is kept in `%LOCALAPPDATA%\llm-delegate\state.json` (or `~/llm-delegate/state.json` without `LOCALAPPDATA`).
Set `LLM_DELEGATE_STATE` to use another file.

## Safety notes

- **The prompt and any files the provider reads are sent to a third-party service.** Do not delegate secrets.
- Executables are looked up only in absolute `PATH` entries other than the current directory, and on Windows only as
  `<name>.exe`, never `.cmd` or `.bat`.
- `agy` and `hermes` receive the prompt as a command-line argument, so prompts over 30 000 characters are refused, and
  on Windows so are prompts whose quoted command line passes the 32 767 UTF-16 unit limit (every `"` doubles and an
  emoji counts twice). Put large context in files and refer to their paths. Codex receives the prompt on stdin and
  has no such limit.
- Hermes's file tools are not confined to `--cwd`: they can read and write any file the logged-in user can. The copy
  protects `--cwd` from ordinary relative-path writes, not other paths, and `HERMES_WRITE_SAFE_ROOT` did not help
  (see Hermes). That is why `hermes` and `nous` need `--allow-unsandboxed`.
- Calls are expensive. Measured on one machine, answering a one-line question about a one-line file used about 23 000
  tokens with `agy` and 30 000 to 40 000 with Codex, and a one-line edit with `--write` about 36 000 (`gpt-6.1-sol`).
  Reading two files with Codex cost 85 000 before the isolation flags were added. Tiny prompts are a poor trade.

## Known limitations

- The usage-limit wording it matches (`usage limit`, `rate limit`, `quota`, `429`, ...) comes from public reports,
  not from a real exhausted account. If a real quota error is not recognised, it will be reported as exit 4. Add its
  wording to `QUOTA_RE` in `delegate.py`.
- A provider that cannot do the work can still answer with an apology or a plan and exit successfully (it looks like
  an answer to `run`). `agy` in plan mode may say "I prepared a plan, please approve it"; the tool appends an
  instruction to answer in plain text, which fixed it in testing (3 of 3), but it is not a guarantee. Read answers
  critically and run `check` when results look wrong.
- On one Windows machine `gpt-6-luna` answered "I can't read files" in read-only mode on 4 of 5 tries, while it
  edited files fine with `--write`, and the other Codex models tested read normally. Model behaviour differs per
  machine and over time, which is what `check` is for.
- `agy` sometimes reports success with an empty answer. In the case inspected in agy's conversation database the model
  had tried to run a shell command; headless plan mode has nobody to approve it, the permission is denied
  ("user denied permission to run command") and the turn ends without text. The tool therefore tells `agy` up front
  that commands are denied and to read files with its file viewer. Reviews of one 480-line file, empty before the
  change, gave a real answer in all three runs after it (4 to 5 minutes and 90 000 to 105 000 tokens each); an empty
  answer that came back within 60 seconds is retried once, a slower one is not, so a long run is not paid for twice.
- On that same review Codex took 40 seconds and 154 000 tokens and found four real defects in the tool, all confirmed
  by hand; `agy` found different, smaller ones (damaged state file, unexpected JSON shapes) and missed those four.
  The two were complementary, and neither replaces running the tests.
- The default timeout (600 seconds per provider) is an arbitrary choice, not a limit of either CLI: `agy` itself waits
  for the end of the turn without one. A real `agy` run once hit it; small prompts take 10 to 40 seconds. Use
  `--timeout 0` for tasks that may run for hours (see Long tasks).
- Killing a timed-out provider takes its whole process tree (`taskkill /T` on Windows, a process group elsewhere).
  Verified on Windows for Codex with a long command running as its child. For `agy` no long child process could be
  provoked, so that case is untested, and the tree kill cannot reach a grandchild whose parent already exited.
- Remaining quota is known in advance only for the ChatGPT subscription (through `hermes usage`). For `agy` (Google)
  and `nous` it is learned from a failed call. Whether a full ChatGPT window comes back as an error the tool
  recognises was not tested, since no exhausted account was available; the percent-based pre-check is what avoids it.
- Killing a timed-out Hermes returned on time and left no copy behind. Hermes starts a helper process
  (`agent.managed_worker`) from its own long-running daemon, not from the process the tool kills; it exited on its
  own about 20 seconds later. Whether the model turn keeps running (and spending quota) in that time was not checked.
- Hermes runs on the same ChatGPT subscription as Codex CLI, so when that quota is exhausted both are blocked, each
  after one failed call. Using subscriptions through another program's login may be restricted by the provider's
  terms; they were not checked.
- `--write` is available for `codex` and `agy` but has had very little real use; keep it to a separate branch or
  worktree.
- Only Hermes, Codex and `agy` are supported. `gemini` CLI is not, because it needs its own sign-in.

## Tests

```
python -m unittest
```

The tests replace the providers with fakes and use a temporary state file; they do not call any service.

## Related projects

Not affiliated with any of these. They solve nearby problems and are worth a look:

- [NousResearch/hermes-agent](https://github.com/nousresearch/hermes-agent): the agent used here as a provider; it
  has its own fallback providers and credential pools.
- [pvjbuilds/delegate-router](https://github.com/pvjbuilds/delegate-router): routing table and delegation rules for
  Codex, `agy` and a subagent; no quota tracking.
- [janekbaraniewski/openusage](https://github.com/janekbaraniewski/openusage): reads usage from local tool data,
  including Codex and `agy`.
