# AureusBot

**An independent digital life.**

[中文版](README.md)

AureusBot runs on your own computer, independently of any chat platform. Models interpret messages, propose plans and compose replies. The kernel decides whether to act: body signals, emotions and feedback from earlier actions affect attention and reply strategy, not just the dashboard.

It is first a companion. It talks with you in a human-like way and offers emotional support. When work needs doing, it only outsources specialist tasks to other agents. Its own part is limited to deciding, delegating, accepting results, and reporting. These abilities arrive step by step, following the roadmap.

## Status

The cognitive kernel has completed Phases 0 through 2. In Phase 3, a test bot retained its Aureus persona while importing MaiBot memories, expressions, slang and relationship history. Live delivery and recall of pre-migration memories were verified in the authorized QQ group and direct conversation. That acceptance applies to the migration, not to later features. [Roadmap](docs/08-roadmap.md) · [Migration acceptance record](docs/tasks/3.9.md) (in Chinese)

## Quick start

You need Python 3.12 or newer, and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                         # install dependencies
uv run aureus --home ./aureus_home init         # create the data root
# Copy config/identity.example.toml to aureus_home/config/identity.toml, then fill it in
uv run aureus --home ./aureus_home chat --fake  # trial run with a fake organ: no model calls, no cost
```

Source checkouts store user data in the project-root `aureus_home/` folder by default. Keep this folder when updating; moving the project also moves its data. Use `--home` or `AUREUS_HOME` to choose another location (see [docs/14-user-data.md](docs/14-user-data.md)). The web interface defaults to `http://127.0.0.1:28976`. To connect real models, see [docs/15-model-management.md](docs/15-model-management.md). The full check, `uv run python scripts/check.py`, covers formatting, types, architecture and tests; see the [development guide](docs/11-dev-guide.md) for the release workflow.

The design documents are in Chinese only.

---

## Four core traits

| Trait | Meaning | How it is achieved |
|---|---|---|
| **Independent** | Does not depend on any platform or model vendor | Its own process and data directory. Platforms are pluggable channels, and models are replaceable organs. |
| **Continuous** | Its experience is one unbroken line, and restarts do not erase it | One timeline that only grows. All state is persisted. It knows how long it slept. |
| **Unique** | At any moment there is only one of it, the same one on every platform | A single-instance lock. The timeline never forks. All channels share one attention, one memory, and one set of relationships. |
| **Autonomous** | Acts on its own state and plans even when nobody is talking to it | An always-on heartbeat loop and its own agenda. Driven by real body signals, and limited by budget and permissions. |

## Two principles

**The LLM is an organ, not the subject.**
Identity, body, emotion, memory, and relationships belong to the core program, and they change only by its code rules. The LLM understands, thinks, and speaks, but it can only offer suggestions. It cannot rewrite any state directly.

**No decoration.**
Every internal feeling must meet two requirements:

1. It comes from real, measurable signals, such as budget, load, faults, and time. It does not come from text the LLM invents.
2. Code changes its behavior for real, such as switching models, slowing down, or asking for approval. A line in the prompt that says "you are tired" does not count.

If a state cannot meet both requirements, it is not implemented.

---

## Two parts

| Part | Contents | Notes |
|---|---|---|
| **Part 1: Cognitive kernel** | Core skeleton; four persona layers; self-monitoring (body signals); feedback-driven behavior (confidence, hormones, mood, autonomous action); terminal and web channels; observation console and experiment bench | See [docs/12-kernel.md](docs/12-kernel.md) (in Chinese) |
| **Part 2: Extensions** | Long-term memory; QQ and other platforms; cross-platform identity; operating a computer; outsourcing and skills; internet life | Added on top of the kernel step by step |

## Subsystems

| Group | Subsystem | Code name | Summary |
|---|---|---|---|
| Who it is | Identity | `identity` | Identity facts and four persona layers: immutable character, mutable character, behavior style, expression style |
| | Body | `body` | Real body signals: energy, fatigue, stress, need for social contact, sleep |
| | Hormones | `hormones` | Short-term feelings on a scale of minutes: anger, pleasure, low mood, surprise |
| | Mood | `mood` | Overall mood on a scale of hours to days |
| What it has lived through | Timeline | `timeline` | One append-only record of its experiences, summarized into a diary each day |
| | Memory | `memory` | Turns experiences into things it can recall later. Backend: A_memorix |
| | People | `people` | One profile for each person it knows, whichever platform they use |
| | Skills | `skills` | Ways of doing things it has learned, which it can summarize and write itself |
| How it thinks and acts | Heartbeat | `heartbeat` | The always-running main loop. Each beat decides what to do next |
| | Attention | `attention` | What it focuses on right now, and what goes into the prompt |
| | Agenda | `agenda` | Goals, to-dos, promises, and its own small projects |
| | Organs | `organs` | The single entry point for all LLM calls: planner, replier, judge, summarizer, vision |
| | Actions | `actions` | Sending messages, calling tools, operating a computer, outsourcing work |
| | Feedback | `feedback` | Writes an expectation before acting, compares it with the result, and learns from the gap |
| Its world | Channels | `channels` | Terminal, web, QQ, WeChat, and others. Platforms are only channels |
| | Safety | `safety` | Permissions, approval, audit, budget ceiling, and emergency stop. Applies to every layer |

Two more tools: the **observation console** (`monitor`) shows its body signals, emotions, attention, timeline, and spending at any time. The **experiment bench** (`lab`) runs reproducible experiments that compare settings on and off.

## Existing projects it builds on

| Purpose | Project | How it is used |
|---|---|---|
| Long-term memory | [A_memorix](https://github.com/A-Dawn/A_memorix) | Memory backend, wrapped by our own interface |
| Human-like speech | [MaiBot](https://github.com/Mai-with-u/MaiBot) | Reference for the planner and replier split, speaking style, group chat behavior, and platform adapters |
| Operating a computer | [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) | Its "hands", used in an isolated environment |
| Local models | [Ollama](https://github.com/ollama/ollama) | Embeddings and light tasks on a local GPU |

For the reasons behind these choices, see [docs/07-tech-stack.md](docs/07-tech-stack.md) (in Chinese).

## Relationship with MaiBot

AureusBot is an independently developed derivative of MaiBot. It adapts GPL-3.0 code from MaiBot to split and merge replies, format memory references, match slang, normalize learned expressions, and map person identifiers. Its long-term memory also adapts validity filtering, full-text ranking, and time-based decay from A_Memorix. AureusBot as a whole is licensed under AGPL-3.0. [MaiBot upstream](https://github.com/Mai-with-u/MaiBot) · [Upstream license](https://github.com/Mai-with-u/MaiBot/blob/main/LICENSE) · [Third-party notice](THIRD_PARTY.md) · [AureusBot license](LICENSE)

## Documents

The documents are in `docs/`. They are in Chinese.

```text
docs/
├── 01-vision.md                    Goals and boundaries (read first)
├── 02-architecture.md              Overall architecture and main loop
├── 03-subsystems.md                Responsibilities and rules of each subsystem
├── 04-body-signals.md              Body signals: inner sensations tied to real signals
├── 05-people.md                    Relationships and cross-platform identity
├── 06-safety.md                    Safety and permissions
├── 07-tech-stack.md                Technology choices and budget
├── 08-roadmap.md                   Roadmap and acceptance criteria
├── 09-glossary.md                  Glossary
├── 10-decisions.md                 Decision log and open questions
├── 11-dev-guide.md                 Development guide (read before writing code)
├── 12-kernel.md                    Part 1: cognitive kernel
├── 13-references-and-migration.md  Reference projects, license findings, MaiBot data migration
├── 14-user-data.md                 User data kept apart from the program: data root, updates, export and import
├── 15-model-management.md          Model management: providers, models, and organs
├── 16-organ-architecture.md        Early organ architecture draft (partly superseded by later decisions)
├── 17-neuroscience-optimization.md Design basis of the biological model (hormones, energy, and parameters)
├── 18-neuroscience-completeness.md Neuroscience mapping (self-assessment, not experimentally validated)
├── 19-code-review.md               Phase 0 code review and follow-up fixes
└── archive/                        Abandoned early drafts, kept for reference only
```

Suggested reading order: 01, 02, 12, 08. To see how the code is organized, read 11.

## Contributing

AI writes most of the code. The maintainer shapes the project, makes design decisions, and reviews the results. Issues are welcome, including design flaws. The design documents are only in Chinese for now.

## License

[AGPL-3.0](LICENSE) (GNU Affero General Public License v3.0). See [D12](docs/10-decisions.md) (in Chinese).

---
