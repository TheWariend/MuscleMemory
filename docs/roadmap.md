# Roadmap

## Works today

- Automatic capture from Claude Code, Codex and Cursor hooks, plus CleanSlate and the
  Python and TypeScript SDKs.
- Automatic learning from sessions whose tests pass after the last code change.
  Hooks and CleanSlate (CLI and desktop); SDK sessions are learned when your code reports success.
- Code changes made from the shell (scripts, heredocs, `sed -i`) are seen by comparing
  which project files differ from git around each command.
- Skills move between agents: one learned with Codex is handed to Claude Code and
  counted as checked by Claude Code's passing sessions.
- Two independent passing checks before a skill is trusted, a pause after a failed check,
  and a person's OK before any skill reaches an agent, however it was learned. Only
  admin keys (the dashboard) can approve.
- Failed checks reported by the hooks: a session given a skill whose own test fails
  last after a code change flags that skill.
- Real test detection: known test runners and test subcommands, not any command that
  mentions "test" or "check".
- Removed skills stay removed: deleting or retiring a skill keeps a fingerprint so the
  same skill is not learned again.
- Checks must pass the skill's own test: exact comparison after normalizing pipes,
  redirects and shell wrappers, using completion evidence after the last code change.
  Automatic checks use the final passing test. Parameterized commands keep shape matching.
- Check before handing over, in native hooks: files the skill found in place must
  still exist (files it created may not yet), and at least one stored edit must match
  its before or after text. No matches means no
  injection; partial matches carry an untrimmed "files changed since this skill was
  learned" warning. The check reads at most 32 files of at most 1 MiB each, rejects
  symlinks and unsafe paths, and records skip reasons in the private local queue.
  It runs no commands or model calls and sends no file contents.
- Failure contrasts that turn failed attempts into plain "watch out" and "if it goes
  wrong" notes.
- Recall once per session as a short pointer: files to change, the test to run,
  what to avoid.
- Local by default: SQLite and a small on-device model, no account and no outside calls.
  PostgreSQL with pgvector for teams.
- A dashboard with skills (in plain words: files to look at, the exact edits, the test,
  and which agents checked it), a playground, activity and API keys.
- Exporting proven skills to Codex or Claude Code as `SKILL.md`.
- A browser MCP server for Claude Code, Codex and Cursor: it learns skills from the
  agent's browsing, checks them twice, and lets the agent run a proven skill, which
  does every step with checks except the final submit. See [browser agents](integrations.md#browser-agents).
- Automatic approval as an option (`MM_AUTO_APPROVE`); the two checks stay required.

## Next

1. **Recall from errors.** When a command fails, look up warning notes for an exact
   error signature, only from fail → change → tests-pass evidence and approved sources.
   Not shipped: existing prose notes lack per-note confirmation and error signatures.
   See the [design note](error-recall-design.md).
2. **Learn which skills help.** Use recorded injections and file-check skips to measure
   token and success differences per skill, and retire skills that don't help. Complete
   other skip-decision coverage and collect a few hundred real sessions first.
3. **Automatic learning for custom SDK agents**, matching the hooks and CleanSlate.
4. **Disconnect command** and per-project opt-out.
5. **A larger Claude Code benchmark**, with repeats and skills learned by Claude Code
   itself, to follow the early sonnet-01 run.
6. **Scale results** for 100k stored skills on SQLite and PostgreSQL.
7. **A browser evaluation.** Memory off against running proven skills, on longer
   forms and multi-step flows, with several runs per arm.

## Later

- Hosted option for teams: sign-in with GitHub or Google, invitations, shared workspaces.
- Larger, independent benchmarks on real repositories and new kinds of tasks.

## Known limits

- Sessions with conflicting parallel actions, or a missing "tool finished" event
  for a potentially mutating action, are kept but not learned from. Parallel reads
  are supported. Concurrent edits require distinct explicit `file_path` values;
  concurrent Codex `apply_patch` calls still block learning.
- A shell command counts as a code change when it changes files that differ from
  version control (or, outside git, any file in the project). Changes that leave the
  same state as before, or that are committed inside the same command, are not seen.
- A piped test run (`tests | tail`) counts only when the output shows the test
  runner's own pass or fail line.
- The hook file check is literal text matching, not a behavioral test. It skips
  skills whose existing files are missing, oversized, unsafe or unreadable; skills
  without stored edit snippets are delivered without a text comparison; read-only skills keep their delivery behavior.
  On platforms without safe directory-relative, no-follow file APIs, these file
  checks skip delivery. Server, MCP, SDK and CleanSlate recall do not run this probe.
- Projects in the same namespace share skills and checks. The default namespace
  is `"default"`; cross-repository recall is untested. Use a separate namespace
  when a project's skills should be isolated.
- Error-triggered notes before trust are design-only; existing notes remain within
  eligible, approved skills.
- Coding skills are guidance: the agent makes every change. Browser skills run only
  when the agent asks, step by step with checks, and the agent always submits.
- The browser reads visible form controls and `aria-label` sections. Iframes, shadow
  DOM and screenshot-only pages aren't supported, and exact label matching leaves
  renamed fields to the agent.
