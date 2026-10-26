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
- `status` shows calls, tokens and out-of-quota marks. `reset [provider]` clears a mark.
- Codex runs without the user's `config.toml` and personal rules, so always pass `--model` for it.
- Default is read-only (Codex `-s read-only`, agy `--mode plan`). `--write` allows edits in `--cwd`.

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

Each call costs roughly 12 000 to 30 000 tokens of the provider's quota before any real work, so small tasks are a
net loss.

## Choosing model and effort

A starting point taken from [delegate-router](https://github.com/pvjbuilds/delegate-router)'s routing table, which
rests on its author's benchmark reading. It has not been verified here. Change it when real results disappoint.

| Task | Provider | Model | Effort |
|------|----------|-------|--------|
| Large implementation from a written spec | codex | `gpt-6.1-sol` | `high` |
| Small mechanical edits | codex | `gpt-6-luna` | `medium` |
| Long reading, documents, reading code without edits | agy | `gemini-3.8-flash-low` or `-medium` | model's own |
| Diff review, second opinion | codex | `gpt-6-astra` | `low`, read-only |

Do not go above `high`: the gain is small and the cost two to three times higher. `--write` only in a separate
branch or worktree, never in a working copy someone is using.

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
