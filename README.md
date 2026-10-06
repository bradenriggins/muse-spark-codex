# Muse Spark for Codex

Use **Muse Spark 1.3** (regular and Contributor) inside Codex: the Codex CLI
with `--profile`, and the ChatGPT Desktop app's Codex model picker, plus a
108KB native menu bar switcher.

Unofficial project. Not affiliated with Meta or OpenAI. Contributor-tier
content may be used for product improvement (see Meta's terms).

## What you get

- `codex --profile muse` — Muse Spark 1.3 in the terminal, listed in `/model`
- ChatGPT Desktop app — both Muse entries in the Codex model picker
- `MuseBar` — menu bar app to swap modes (restarts the ChatGPT app)
- Lean modes — ~14 tools / ~26K tokens / ~4s instead of 53 / 262K / ~16s,
  while keeping agents, multi-agent, web search, and skills
- Full modes — every native Codex capability (computer-use, subagents, MCP,
  app connectors) translated to Meta's Responses API

## Requirements

- macOS for the Desktop toggle, menu bar app, and launch agents
  (CLI profiles + shim also work on Linux with a manual shim start)
- Codex CLI (`codex`) and optionally the ChatGPT Desktop app
- A Meta API key with Muse access
- Python 3.9+, Xcode command line tools (for the menu bar build)

## Install

```sh
git clone https://github.com/bradenriggins/muse-spark-codex.git
cd muse-spark-codex
mkdir -p ~/.config/muse
echo -n '<meta-api-key>' > ~/.config/muse/meta_api_key
chmod 600 ~/.config/muse/meta_api_key
./install.sh
```

Verify:

```sh
curl -s http://127.0.0.1:18199/healthz            # {"ok": true}
codex --profile muse exec "Reply with exactly: muse-ok"   # muse-ok
```

## Use

CLI profiles (model + effort; tools follow the Desktop mode):

| Profile | Model | Reasoning |
|---|---|---|
| `muse` | Muse Spark 1.3 | high |
| `muse-contributor` | Muse Spark 1.3 Contributor | high |
| `muse-fast` | Muse Spark 1.3 | minimal |
| `muse-lean` | Muse Spark 1.3, connectors stripped | minimal |

Desktop modes (`~/.codex/muse-shim/muse-desktop list`, or the menu bar —
switching restarts the ChatGPT app):

| Mode | Meaning |
|---|---|
| `native` | personal GPT setup (exact restore) |
| `muse` / `muse-contributor` | full tools |
| `muse-fast` | full tools, minimal reasoning |
| `muse-lean` / `muse-lean-contributor` | MCP/apps/plugins stripped; keeps agents, search, effort |

In Muse mode, pick a Muse Spark entry in the app's picker. Picking a GPT
entry while in Muse mode errors until you toggle back. The mode also sets
the tool environment for plain terminal `codex` and `--profile` sessions;
override per run with `-c`, e.g.
`-c agents.enabled=true -c features.multi_agent_v2.enabled=true`.

After a Codex upgrade, rebuild the catalog snapshot:

```sh
codex debug models > /tmp/bundled_models.json
python3 ~/.codex/muse-shim/build_muse_catalog.py
```

## How it works

Codex talks to a custom `muse` provider that points at a localhost
translation shim (`127.0.0.1:18199`), which forwards to `api.meta.ai`.
Codex-only wire shapes are converted on the way out and translated back:

- `custom` apply_patch ⇄ function (streaming-aware SSE synthesis)
- `web_search` → `web_search_preview`
- flat `ns.tool` calls → split namespace form; bare calls with exactly one
  owner get their namespace re-attached
- tools with recursive `$ref` schemas are dropped (logged by name)
- empty/invalid `function_call` arguments → `{}` (Meta emits these, then
  rejects its own history without the repair)
- multi-agent `agent_message` → plain message, including the
  `encrypted_content` part that carries task assignments

The shim logs tool names and byte counts only — never prompts or keys —
to `~/Library/Logs/muse-codex-shim.log`.

## Troubleshooting

- `connection refused` from Codex: the shim is down.
  `curl -s http://127.0.0.1:18199/healthz`, restart with
  `launchctl kickstart -k gui/$(id -u)/ai.muse.codex-shim`.
- Meta `400` errors: read the shim log tail; it logs upstream errors.
- `Reconnecting` in the Desktop app with Meta `500`s in the log: Meta-side
  outage — wait and retry.
- Model picker shows no Muse entries: rebuild the catalog (above).
- `muse-desktop mode <name> --no-restart` switches without restarting the
  app (useful for testing; the app reads config at startup).

## Layout

| Path | Purpose |
|---|---|
| `shim/shim.py` | localhost translation shim |
| `desktop-toggle/muse-desktop` | six-mode config toggle |
| `catalog/build_muse_catalog.py` | model catalog builder |
| `profiles/` | CLI profile templates |
| `menubar/` | MuseBar Swift source + build |
| `launchd/` | macOS agent templates |
| `install.sh` | one-shot installer |

## License

MIT — see [LICENSE](LICENSE).
