# agents-md-injector

A small [Hermes Agent](https://hermes-agent.nousresearch.com/docs) plugin that injects the
nearest `AGENTS.md` into `read_file` tool results — dynamically, per file path, and only once
per session.

Hermes already accepts repo-level agent instructions, but dumping every `AGENTS.md` a
monorepo contains into the system prompt burns tokens on rules for directories the agent may
never touch. This plugin loads instructions **when and where they become relevant**: the moment
the agent reads a file, the instruction files above that file are appended to the result.

```
read_file("app/Models/Downloaded.php")
        │
        ▼
  tool result (unchanged)
        │
        ▼
  transform_tool_result hook
        │
        ├── walk up: app/Models → app → repo root (stop at .git)
        ├── collect AGENTS.md not yet injected in this session
        └── append "Instructions from: …" blocks
        │
        ▼
  model sees the file + the rules for that part of the tree
```

**Version:** 1.1.0 · **Tests:** 26 · **License:** MIT

---

## Requirements

- Hermes Agent with plugin support (`hermes plugins list` works)
- Python 3.9+ (uses `pathlib` / type hints only, no third-party runtime dependencies)

## Install

```bash
# from the plugin registry / GitHub
hermes plugins install idoomblast/agents-md-injector --enable

# or, keeping the clone as the working copy
git clone https://github.com/idoomblast/agents-md-injector.git \
  ~/.hermes/plugins/agents-md-injector
```

Enable it if the install command did not:

```bash
hermes plugins enable agents-md-injector
```

`plugins.enabled` in `~/.hermes/config.yaml` is the source of truth; the entry should look
like this:

```yaml
plugins:
  enabled:
    - agents-md-injector
```

Restart the gateway (or start a new session / `/reset` in the CLI/TUI) so the hook is loaded:

```bash
hermes gateway restart
hermes plugins show agents-md-injector   # should list: provides_hooks: transform_tool_result
```

## Configuration

All knobs are environment variables read at hook time, so they can be exported per-shell,
per-service, or in `~/.hermes/.env`. No config schema, no restart needed to change them.

| Variable | Default | Meaning |
|---|---|---|
| `AGENTS_MD_INJECTOR_DISABLE` | *(unset)* | `1`/`true`/`yes`/`on` disables injection entirely |
| `AGENTS_MD_INJECTOR_FILENAMES` | `AGENTS.md,agents.md` | Comma-separated instruction filenames, checked in order per directory |
| `AGENTS_MD_INJECTOR_MAX_BYTES` | `32768` | Per-file byte cap before truncation; `0` = unlimited |
| `AGENTS_MD_INJECTOR_MAX_TOTAL_BYTES` | `65536` | Total bytes appended per read; `0` = unlimited |

Examples:

```bash
# also pick up CLAUDE.md
export AGENTS_MD_INJECTOR_FILENAMES="AGENTS.md,CLAUDE.md"

# hard cap total injected context per read
export AGENTS_MD_INJECTOR_MAX_TOTAL_BYTES=16384
```

When a file is truncated, the injected block ends with a visible marker so the model knows
content was cut:

```
[agents-md-injector: 8123 bytes truncated from AGENTS.md]
```

## Behavior

1. **Only `read_file`.** Every other tool result passes through untouched.
2. **Gateway-safe path resolution.** Relative paths resolve against `TERMINAL_CWD`, then
   `os.getcwd()`. Gateway workers run with the daemon home as cwd, so resolving against
   `os.getcwd()` alone would look in the wrong tree.
3. **Walk up to the repository root.** Starting at the read file's directory and moving up,
   the walk stops *after* processing the first directory that contains `.git` (directory or
   worktree file). Files outside any repository walk to the filesystem root — put a global
   `AGENTS.md` in `$HOME` and it applies everywhere.
4. **Nearest first.** A nested `AGENTS.md` is appended before the repo-root one, so the model
   reads local rules before global rules.
5. **Once per session.** Each instruction file is injected at most once per Hermes session
   (`session_id`, falling back to `task_id`). Parallel gateway sessions each get their own
   copy; the tracked-session map is bounded and evicts the oldest session.
6. **Reading `AGENTS.md` itself injects nothing.** The content is already the tool result.
7. **Fail open.** A missing directory, unreadable file, decode error, or unexpected bug
   returns `None` — the tool result is never broken by this plugin.
8. **Size-capped.** Per-file and total caps apply *before* anything is appended, so one
   runaway `AGENTS.md` cannot flood the context window.

### Known interaction: core session-start injection

Hermes core already loads `AGENTS.md` from the session cwd upward into the system prompt.
This plugin is intentionally path-driven on top of that: the repo-root file may therefore
appear twice (once at session start, once on the first read inside that repo) unless the
core loader has already been disabled. That duplication is bounded by rule 5 — it happens at
most once per file per session.

### One hook wins

`transform_tool_result` is a string-replacing hook and Hermes uses the **first non-`None`
string returned by any plugin**. If another enabled plugin also transforms `read_file`
results, only one of them takes effect. Disable the other plugin (or extend this one) rather
than expecting the outputs to merge.

## Development

```bash
git clone https://github.com/idoomblast/agents-md-injector.git
cd agents-md-injector

# run the suite (no project install needed)
uv run --with pytest --with pyyaml pytest -q

# or with a plain environment
python3 -m venv .venv && .venv/bin/pip install pytest pyyaml
.venv/bin/pytest -q
```

The tests load the plugin exactly the way the Hermes loader does — as the package
`hermes_plugins.agents_md_injector` with `submodule_search_locations` — because the
hyphenated directory name cannot be imported directly and an absolute import would pass
locally while failing in the gateway.

Manual smoke test:

```bash
DEMO_DIR="$(mktemp -d)"
mkdir -p "$DEMO_DIR/.git" "$DEMO_DIR/src"
printf 'Use tabs.\n' > "$DEMO_DIR/AGENTS.md"
printf 'print("hi")\n' > "$DEMO_DIR/src/app.py"

DEMO_DIR="$DEMO_DIR" python3 - <<'PY'
import importlib.util, os, sys, types
from pathlib import Path

plugin_dir = Path.home() / ".hermes/plugins/agents-md-injector"
ns = types.ModuleType("hermes_plugins"); ns.__path__ = []; sys.modules["hermes_plugins"] = ns
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.agents_md_injector", plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)])
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)

out = mod._on_transform_tool_result(
    tool_name="read_file", args={"path": os.path.join(os.environ["DEMO_DIR"], "src/app.py")},
    result="FILE CONTENT", session_id="smoke")
print(out)
PY
```

Expected output ends with an `Instructions from: /tmp/inject-demo/AGENTS.md` block containing
`Use tabs.`

## Repository layout

```
agents-md-injector/
├── plugin.yaml          # manifest: name, version, provided hooks
├── __init__.py          # hook + walk-up logic (no third-party imports)
├── pyproject.toml       # project metadata + pytest/ruff config
├── .github/workflows/   # CI: ruff + pytest on Python 3.9 / 3.12
├── tests/
│   ├── conftest.py      # loads the plugin like the Hermes loader
│   └── test_injector.py # behavior, dedupe, caps, path boundaries
├── README.md
└── LICENSE
```

## Limitations

- Only `read_file` results are inspected. Content pulled in through `terminal`, `grep`, or a
  subagent prompt does not trigger injection.
- Only the filenames in `AGENTS_MD_INJECTOR_FILENAMES` are recognized; `CLAUDE.md` and other
  conventions require opting in through that variable.
- Injection state is in-process memory. A gateway restart or plugin reload clears it, and the
  next read re-injects (once) per session.
- The plugin does not parse or validate instruction content; it appends files verbatim
  (UTF-8, invalid bytes replaced, size-capped).

## License

MIT — see [LICENSE](LICENSE).
