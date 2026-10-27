# llm-delegate

A small command-line wrapper that hands a prompt to **Codex CLI** or **Antigravity CLI (`agy`)**, picks the model and
reasoning effort, and moves on to the next provider when a subscription runs out of quota.

It exists so a host coding agent (or a person) can offload bulky reading and mechanical work to the subscriptions
already paid for, instead of spending the host's own tokens. One file, no dependencies, standard library only.

```
$ python delegate.py run --providers agy,codex "Read src/ and summarise the module layout in 15 lines"
[delegate] agy: ok, 29019 tokens
<the answer, and nothing else, on stdout>
```

## What it does

- **Runs one prompt** through `agy` or `codex` in non-interactive mode and prints only the answer to stdout.
  Diagnostics go to stderr, so the output can be piped or captured as is.
- **Falls back.** Providers are tried in the order given. If one reports a usage limit, it is marked as exhausted
  until the reset time (taken from the error text when present, otherwise one hour) and is skipped on later calls.
- **Chooses model and effort** per provider (`--model`, `--effort`) and rejects combinations Codex is known not to
  support before spending a call.
- **Keeps a usage ledger** (calls in the last 5 hours, 24 hours and 7 days, tokens in the last 24 hours) so a
  caller can look before sending a large batch.
- **Is read-only by default.** Codex runs with `-s read-only`, `agy` with `--mode plan`. `--write` allows edits in
  `--cwd`.

## Requirements

- Python 3 (developed and tested on 3.14 on Windows 11; other versions and other systems are untested).
- `codex` and/or `agy` installed, on `PATH` and already signed in. This tool does not log in for you.

## Usage

```
delegate.py run [--providers agy,codex] [--model [P=]M] [--effort [P=]E] [--cwd DIR] [--write]
                [--timeout SECONDS] [--cooldown SECONDS] PROMPT|-
delegate.py models [--json]
delegate.py check [--providers agy,codex] [--model [P=]M] [--effort [P=]E]
delegate.py status [--json]
delegate.py reset [PROVIDER]
```

`PROMPT` may be `-` to read it from stdin.

### Model and effort

With one provider the value is plain; with several, prefix it with the provider:

```
python delegate.py run --providers codex --model gpt-6-luna --effort low "Rename foo to bar in utils.py"
python delegate.py run --providers agy,codex --model agy=gemini-3.8-flash-low --model codex=gpt-6-luna "..."
```

`python delegate.py models` lists what each provider accepts on the current machine: Codex's list comes from its own
`models_cache.json`, `agy`'s from `agy models`. Without `--model`, Codex uses the model from your `config.toml`.
Codex keeps that file on purpose: on Windows its `[windows] sandbox` setting is what lets a read-only run execute any
command. Starting Codex with `--ignore-user-config` made it unable to read files, so the tool does not use that flag.

### Check

```
python delegate.py check [--providers agy,codex] [--model [P=]M] [--effort [P=]E]
```

Asks each provider to read a file whose contents it can only learn by reading it, and prints `PASS` or `FAIL`.
Run it after changing models, flags or either CLI, and before a large delegation. It costs tokens on every provider
(roughly 20 000 to 45 000 each). A provider can answer "I can't read files" and still exit successfully, and `run`
has no way to tell that from a real answer, so this is the way to catch it.

### Status and reset

```
$ python delegate.py status
codex  available | calls 5h/24h/7d 2/5/5 | tokens 24h 61207
agy    OUT OF QUOTA until 19:48 | calls 5h/24h/7d 1/3/3 | tokens 24h 38120 | last error: ...
$ python delegate.py reset agy      # clear the mark if you know the quota is back
```

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
- `agy` receives the prompt as a command-line argument, so prompts over 30 000 characters are refused. Put large
  context in files and refer to their paths. Codex receives the prompt on stdin and has no such limit.
- Calls are expensive. Measured on one machine, answering a one-line question about a one-line file used about 23 000
  tokens with `agy` and 35 000 to 80 000 with Codex, and a one-line edit with `--write` 60 000 to 120 000. Tiny
  prompts are a poor trade.

## Known limitations

- The usage-limit wording it matches (`usage limit`, `rate limit`, `quota`, `429`, ...) comes from public reports,
  not from a real exhausted account. If a real quota error is not recognised, it will be reported as exit 4. Add its
  wording to `QUOTA_RE` in `delegate.py`.
- A provider that cannot do the work can still answer with an apology or a plan and exit successfully (it looks like
  an answer to `run`). `agy` in plan mode may say "I prepared a plan, please approve it"; the tool appends an
  instruction to answer in plain text, which fixed it in testing (3 of 3), but it is not a guarantee. Read answers
  critically and run `check` when results look wrong.
- On one Windows machine `gpt-6-luna` answered "I can't read files" in read-only mode on every try (3 of 3), while it
  edited files fine with `--write`, and the other Codex models tested read normally. Model behaviour differs per
  machine and over time, which is what `check` is for.
- `agy` often reports success with an empty answer (about one run in three in testing). The tool retries once, then
  treats it as a failure.
- A long `agy` run once hit the 600 second timeout in real use; this was not reproduced with small prompts (10 to 40
  seconds). Keep delegated prompts scoped and lower `--timeout` if you would rather fail early.
- Remaining quota is not known in advance, only after a failure.
- `--write` is available but has had very little real use; keep it to a separate branch or worktree.
- Only Codex and `agy` are supported. `gemini` CLI is not, because it needs its own sign-in.

## Tests

```
python -m unittest
```

The tests replace the providers with fakes and use a temporary state file; they do not call any service.

## Related projects

Not affiliated with any of these. They solve nearby problems and are worth a look:

- [pvjbuilds/delegate-router](https://github.com/pvjbuilds/delegate-router): routing table and delegation rules for
  Codex, `agy` and a subagent; no quota tracking.
- [janekbaraniewski/openusage](https://github.com/janekbaraniewski/openusage): reads usage from local tool data,
  including Codex and `agy`.
