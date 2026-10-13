# Roadmap

## Works today

- Automatic capture from Claude Code, Codex and Cursor hooks, plus CleanSlate and the
  Python and TypeScript SDKs.
- Automatic learning from sessions whose tests pass after the last code change.
  Hooks only: CleanSlate and SDK sessions are recorded but don't learn by themselves yet.
- Code changes made from the shell (scripts, heredocs, `sed -i`) are seen by comparing
  which project files differ from git around each command.
- Skills move between agents: one learned with Codex is handed to Claude Code and
  counted as checked by Claude Code's passing sessions.
- Two independent passing checks before a skill is trusted, a pause after a failed check,
  and a person's OK before any automatically learned skill reaches an agent.
- Failure contrasts that turn failed attempts into plain "watch out" and "if it goes
  wrong" notes.
- Recall once per session as a short pointer: files to change, the test to run,
  what to avoid.
- Local by default: SQLite and a small on-device model, no account and no outside calls.
  PostgreSQL with pgvector for teams.
- A dashboard with skills (in plain words: files to look at, the exact edits, the test,
  and which agents checked it), a playground, activity and API keys.
- Exporting proven skills to Codex or Claude Code as `SKILL.md`.

## Next

1. **Check before handing over.** Before injecting a skill, run cheap read-only probes
   (does this file or function exist here?) and stay quiet on a clear mismatch.
   This stops a look-alike task from getting the wrong skill.
2. **Recall from errors.** When a command fails, look up recovery notes for that exact
   error signature, but only from fixes that a later passing run confirmed.
3. **Learn which skills help.** Record every inject or skip decision, measure token
   and success differences per skill, and retire skills that don't help. This needs
   a few hundred real sessions first.
4. **Automatic learning for CleanSlate and the SDKs**, matching the hooks.
5. **Disconnect command** and per-project opt-out.
6. **A larger Claude Code benchmark**, with repeats and skills learned by Claude Code
   itself, to follow the early sonnet-01 run.
7. **Scale results** for 100k stored skills on SQLite and PostgreSQL.

## Later

- Hosted option for teams: sign-in with GitHub or Google, invitations, shared workspaces.
- Larger, independent benchmarks on real repositories and new kinds of tasks.

## Known limits

- Sessions with parallel tool calls, or a missing "tool finished" event, are kept
  but not learned from.
- A shell command counts as a code change when it changes files that differ from
  version control (or, outside git, any file in the project). Changes that leave the
  same state as before, or that are committed inside the same command, are not seen.
- A piped test run (`tests | tail`) counts only when the output shows the test
  runner's own pass or fail line.
- A skill is a suggestion. Agents still read the current files and ask for approval
  as usual. MuscleMemory never replays actions.
