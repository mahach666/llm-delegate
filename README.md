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
`models_cache.json`, `agy`'s from `agy models`. Codex is started with `--ignore-user-config --ignore-rules`, so your
`config.toml` model and effort do **not** apply to it. Always pass `--model` for Codex.

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
- Each call costs roughly 12 000 to 30 000 tokens of the provider's quota before any real work, which makes tiny
  prompts a poor trade.

## Known limitations

- The usage-limit wording it matches (`usage limit`, `rate limit`, `quota`, `429`, ...) comes from public reports,
  not from a real exhausted account. If a real quota error is not recognised, it will be reported as exit 4. Add its
  wording to `QUOTA_RE` in `delegate.py`.
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
