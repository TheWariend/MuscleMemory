<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/logo-dark.png">
    <img src="docs/images/logo-light.png" alt="MuscleMemory" width="150">
  </picture>
</p>

<h1 align="center">MuscleMemory</h1>

<p align="center">
  <strong>Open-source procedural memory for coding agents.</strong><br>
  It learns from what worked and what failed.<br>
  Local by default · Apache-2.0 · by TheWariend
</p>

---

Coding agents relearn the same things every session: which files to open, how the tests
run, what broke last time. MuscleMemory watches your agent work. When a task really
succeeds (the tests pass after the last change), it learns how the task was done. The next
time a similar task comes up, it hands the agent a short note: the files to change, the
test that proves it worked, and what to avoid.

In a benchmark with real Codex sessions, it cut **input tokens by 25.5%, tool calls by 30.4%
and time by 28.2%**, with every task still passing. In an early run with Claude Code and
Sonnet 5.5, using skills that Codex had learned, it cut **input tokens by 43.5% and tool
calls by 30%**. On real Django issues from SWE-bench, with a skill learned from other
issues in the same area, Codex solved as many as without memory with about half the tokens
(a small run; [details](benchmarks/results/codex/django-forms-01/report.md)).
[Codex method and data](benchmarks/results/codex/luna-03/report.md) ·
[Claude Code run](benchmarks/results/claude/sonnet-01/report.md) ·
[Whitepaper](docs/whitepaper.md).

<p align="center">
  <img src="docs/images/dashboard-playground.png" alt="The MuscleMemory playground: a task, the skill it matched, and what gets added to the agent's prompt" width="860">
</p>

## Quick start

```sh
pipx install thewariend-musclememory   # or, from a clone: pip install -e .

musclememory init                      # sets up ~/.musclememory and the small search model
musclememory start                     # runs in the background on http://127.0.0.1:8080
cd your-project && musclememory connect codex   # or claude, cursor, cleanslate
musclememory dashboard                 # opens the dashboard, already signed in
```

Then use your agent as usual. There are no keys or config files to copy.
More in [Install](docs/install.md).

## How it works

```mermaid
flowchart LR
    A["Your agent works on a task"] --> B{"Tests pass after the last change?"}
    B -- no --> C["Nothing is learned"]
    B -- yes --> D["Learn a skill or count a check"]
    D --> E["Two separate passing checks"]
    E --> F["You tap Keep"]
    F --> G["Next similar task: a short note in the agent prompt"]
    G --> A
```

- **Verified, twice.** A skill is trusted only after two separate sessions pass with it.
  The session it was learned from doesn't count. One failed check pauses it.
- **Learns from failures.** Failed attempts become plain "watch out" and "if it goes wrong"
  notes, so the agent avoids the same dead ends.
- **You stay in charge.** Automatically learned skills wait for your OK before any agent
  sees them. Agents still read the current files and ask for approval as usual.
  MuscleMemory never replays actions.
- **Quiet when it should be.** If nothing fits, nothing is added, and the agent pays no
  extra tokens.
- **Shared between agents.** A skill learned with Codex is handed to Claude Code or
  Cursor too: changing a file counts the same whichever tool did it.
  Proven skills can also be exported to Codex or Claude Code as `SKILL.md`.
- **Local and open.** SQLite and a small on-device model by default: no account and no
  outside calls. Use PostgreSQL with pgvector for teams.

More detail in [How it works](docs/how-it-works.md).

## Works with

| Agent | How it connects |
|---|---|
| Claude Code | `musclememory connect claude` installs capture hooks |
| Codex | `musclememory connect codex` installs capture hooks |
| Cursor | `musclememory connect cursor` installs capture hooks |
| CleanSlate | `musclememory connect cleanslate` wires its host stream |
| Any MCP client | Five tools: recall, observe, learn, evaluate, inspect |
| Your own agent | Python and TypeScript SDKs |

See [Integrations](docs/integrations.md).

## Dashboard

| | |
|---|---|
| ![Home](docs/images/dashboard-home.png) | ![Skills](docs/images/dashboard-skills.png) |
| **Home:** what your agents can reuse, and anything waiting for your OK | **Skills:** each one with the task it came from, the files it changes and the test that checks it |
| ![A skill](docs/images/dashboard-skill.png) | ![Connect](docs/images/dashboard-connect.png) |
| **A skill:** its steps in plain words, its proof, and what to watch out for | **Connect:** one command per agent |

## Docs

- [Whitepaper](docs/whitepaper.md): the design, trust rules, retrieval and evaluation in one paper
- [Install and run](docs/install.md): local setup, Docker and PostgreSQL, configuration
- [How it works](docs/how-it-works.md): the learning loop, retrieval, storage and security
- [Integrations](docs/integrations.md): each agent, MCP and the SDKs
- [Benchmarks](docs/benchmarks.md): results, method and limits
- [API contracts](docs/contracts.md): MCP tools and REST
- [Roadmap](docs/roadmap.md): what's next and known limits

## Status

MuscleMemory works and is tested: 126 Python tests on SQLite and PostgreSQL, and 21
TypeScript tests. It's early. The benchmarks cover repeat tasks on three controlled codebases,
not every kind of work, and the Claude Code run is small. The hooks and CleanSlate learn on their own; with the SDKs your code
reports when a task succeeded.
See the [roadmap](docs/roadmap.md).

Issues and pull requests are welcome. Please report security issues privately, as
described in [SECURITY.md](SECURITY.md).

## License

Apache-2.0. Made by [TheWariend](https://thewariend.com).
