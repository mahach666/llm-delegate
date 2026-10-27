---
name: delegate
description: Offload work to Codex CLI or Antigravity CLI (agy) through delegate.py to save the host agent's own tokens. Use when deciding whether to hand off bulk reading or summarising of many files, logs or documents, a second-opinion review of a diff, or a large mechanical edit that is already fully specified; when choosing a provider, model or reasoning effort for such a hand-off; or when a provider's quota may be exhausted. Skip for small tasks, debugging, architecture, or anything involving secrets.
---

# Delegate to Codex / Antigravity

`delegate.py` (in the llm-delegate checkout) runs one prompt through `agy` or `codex`, falls back to the next
provider when a quota is exhausted, and prints only the answer on stdout. Call it as `python <path>/delegate.py`.
If the path is not already known from the project or user instructions, find the checkout first.

## Commands

- `run [--providers agy,codex] [--model [P=]M] [--effort [P=]E] [--cwd DIR] [--write] PROMPT|-`
  Exit code: 0 answered, 2 bad arguments, 3 every provider is out of quota, 4 a provider failed for another reason.
  With several providers prefix each value: `--model codex=gpt-6-luna --model agy=gemini-3.8-flash-low`.
- `models` lists the models and efforts each provider accepts on this machine. Trust it over the table below.
- `check [--providers ...] [--model ...] [--effort ...]` makes each provider read a test file and prints PASS or FAIL.
  It spends 20 000 to 45 000 tokens per provider. Run it once before the first real delegation in a session and
  whenever an answer looks wrong. A provider can say "I can't read files" and still exit 0, and `run` cannot tell.
- `status` shows calls, tokens and out-of-quota marks. `reset [provider]` clears a mark.
- Codex runs without the user's `config.toml` (their MCP servers, `notify` command and rules stay out), so it uses its
  own default model unless you pass `--model`. The one setting kept from that file is `[windows] sandbox`.
- Default is read-only (Codex `-s read-only`, agy `--mode plan`). `--write` allows edits in `--cwd`.

## Long tasks

`--timeout SECONDS` is per provider (default 600; `0` = no limit). It is an arbitrary default, not a limit of either
CLI. On expiry the provider's whole process tree is killed and the next provider is tried.

If a task may run longer than the host's foreground command limit (often about 10 minutes), do not run it in the
foreground: start the whole `delegate.py run ...` command in the background (for example with the shell tool's
background option) with `--timeout 0` or a large value, keep working or wait, and read the output when it finishes.
Do not poll in a loop and do not start a second identical call. A silent run is not necessarily stuck, so decide
from the elapsed time and the task size, and stop it deliberately if you give up.

## When to delegate

All four must hold, except that pure reading or surveying of a large body of material only needs to be large:

1. **Bulk**: about 40 or more lines of output, the same edit across 3+ files, or a large read.
2. **Decided**: every design decision is already made; the delegate chooses nothing.
3. **Verifiable**: a command can confirm the result.
4. **Contained**: one file, or files that need not be reasoned about together.

## Never delegate

Debugging; architecture and naming; refactors across files that must stay consistent; authentication, money or
trust-boundary code; work under about 40 lines (the brief would be longer than the code); work the user is
following live; anything that depends on the current conversation; and **any secret** (`.env`, keys, tokens):
the prompt and the files the provider reads go to a third-party service.

Calls are expensive: measured, a one-line question about a one-line file cost about 23 000 tokens with agy and
30 000 to 40 000 with Codex; a one-line `--write` edit about 36 000 (`gpt-6.1-sol`). Small tasks are a net loss.

## Choosing model and effort

A starting point taken from [delegate-router](https://github.com/pvjbuilds/delegate-router)'s routing table, which
rests on its author's benchmark reading. It has not been verified here. Change it when real results disappoint.

| Task | Provider | Model | Effort |
|------|----------|-------|--------|
| Large implementation from a written spec | codex | `gpt-6.1-sol` | `high` |
| Small mechanical edits | codex | `gpt-6-luna` with `--write` only | `medium` |
| Long reading, summarising documents or logs | agy | `gemini-3.8-flash-low` or `-medium` | model's own |
| Code review, diff review, second opinion | codex | `gpt-6-astra` or `gpt-6.1-sol` | `low`, read-only |

Do not go above `high`: the gain is small and the cost two to three times higher. `--write` only in a separate
branch or worktree, never in a working copy someone is using.

Observed on this machine (read-only file read, `check`): `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-luna`,
`gpt-5.6-terra` passed; `gpt-6-luna` failed 4 of 5 ("can't read files") yet edited correctly with `--write`. For
agy, `gemini-3.8-flash-low`, `-medium` and `-high` passed. agy can return an empty answer: in the case inspected
the model had tried to run a shell command, which nobody can approve in headless plan mode, so the turn ended
without text. The tool tells agy not to run commands, and a quick empty answer is retried once. In plan mode agy may
also answer with a plan awaiting approval instead of the answer.

For a code review of one 480-line file, agy (flash-medium) took 4 to 5 minutes and about 90 000 tokens, Codex 40
seconds and 154 000 tokens. Codex found four real defects (all confirmed by hand); agy found different, smaller ones
and missed those four. Another session reported agy flash models inventing findings on a one-file review. So: send
code review to Codex, use agy for reading and summarising, and check every finding against the code. Treat the table
as a prior and trust what `check` shows.

## When an answer looks wrong

If the answer is an apology ("can't read files"), a plan awaiting approval, or says access was blocked: that is a
failed delegation even though the exit code is 0. Do not re-run the same call in a loop (each costs 20 000+ tokens).
Run `check` for that provider and model, switch to a model that passes, or do the work directly.

## Writing the brief

Include every decision, the files and functions involved, and what must not change, and end with an instruction to
change nothing else. For Codex also say not to delete or weaken existing tests. For `agy` keep the prompt under
30 000 characters; put large context in files and name their paths.

## Verifying the result

A delegate's "done" is a claim, not evidence. Run the check, read the whole diff, and confirm it did what was asked
and nothing more. After a second failed attempt the task is not suited to delegation: do it directly.

## Quota

Remaining quota is not known in advance; exhaustion shows up as an error. On exit code 3 do not retry in a loop:
do the work directly or tell the user. Check `status` before a large batch of calls.
