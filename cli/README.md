# openagents

Installer and launcher for **[OpenAgents](https://github.com/kabatin/OpenAgents)** —
self-hosted AI agents that live in your team chat.

They sit in your Discord, remember what was said, answer when asked — and
occasionally speak up on their own when they notice something. Everything runs
on your machine; the conversation archive is a SQLite file on your own disk.

> **Not published to npm yet.** Install it from the repository:
>
> ```bash
> git clone https://github.com/kabatin/OpenAgents.git
> cd OpenAgents/cli && npm install -g .
> ```

```bash
openagents setup
```

A browser opens and walks you through creating the Discord bot, picking your
server and channel from a list, choosing your AI, and naming your first agent.
No IDs to look up by hand.

## Requirements

- **Python 3.10+**
- **Node.js 20+**
- **git**
- [Claude Code](https://claude.com/claude-code) or [Codex CLI](https://github.com/openai/codex)

## Commands

| | |
|---|---|
| `openagents setup` | Install and open the setup wizard (the default with no arguments) |
| `openagents start` | Run the supervisor in the foreground |
| `openagents stop` | Stop a running supervisor |
| `openagents status` | Where it lives, what version, which bots are up |
| `openagents update [--restart]` | Update to the latest; `--restart` swaps the running supervisor too |
| `openagents where` | Print the install directory and why it was chosen |

## Where it installs

OpenAgents keeps `config.json`, the conversation archive, and your persona
files **inside its own directory**, so the install directory *is* the checkout.
It is resolved in this order:

1. `--dir <path>`
2. `$OPENAGENTS_HOME`
3. the checkout you are currently inside, if any
4. `~/.openagents`

Step 3 means that if you already installed from source with `git clone`, this
CLI operates on *that* checkout rather than cloning a second copy.

This package is a thin launcher with **zero dependencies** — the application
itself is Python and is fetched with git on first run.

## Docs

Full documentation lives in the repository and is written in Japanese.

- [README (English)](https://github.com/kabatin/OpenAgents#readme)
- [README（日本語）](https://github.com/kabatin/OpenAgents/blob/main/README.ja.md)
- [CLI reference](https://github.com/kabatin/OpenAgents/blob/main/docs/11-cli.md)

MIT
