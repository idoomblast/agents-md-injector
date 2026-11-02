# agents-md-injector

A small [Hermes Agent](https://hermes-agent.nousresearch.com/docs) plugin that injects the
nearest `AGENTS.md` into `read_file`, `search_files`, and file-reading `terminal` results —
dynamically, per target path, and only once per session.

Hermes already accepts repo-level agent instructions, but dumping every `AGENTS.md` a
monorepo contains into the system prompt burns tokens on rules for directories the agent may
never touch. This plugin loads instructions **when and where they become relevant**: the moment
when the agent reads or searches files, the instruction files above those targets are appended to
the result.

```
terminal("head -n 40 app/Models/Downloaded.php")
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
  model sees the command result + the rules for that part of the tree
```

**Version:** 1.2.20 · **Tests:** `pytest` suite · **License:** MIT

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
| `AGENTS_MD_INJECTOR_MAX_TOTAL_BYTES` | `65536` | Total bytes appended per tool result, including headers, separators, and truncation markers; `0` = unlimited |
| `AGENTS_MD_INJECTOR_MAX_TERMINAL_TARGETS` | `8` | Maximum distinct existing paths classified from one terminal command |
| `AGENTS_MD_INJECTOR_MAX_SEARCH_DIRS` | `5` | Maximum distinct result directories refined from `search_files`; `0` disables refinement |

Examples:

```bash
# also pick up CLAUDE.md
export AGENTS_MD_INJECTOR_FILENAMES="AGENTS.md,CLAUDE.md"

# hard cap total injected context per read
export AGENTS_MD_INJECTOR_MAX_TOTAL_BYTES=16384
```

When a file is truncated — by the per-file cap or by the total injected budget — the block
ends with a visible marker so the model knows content was cut:

```
[agents-md-injector: 8123 bytes truncated from AGENTS.md]
```

## Behavior

1. **File-oriented tools.** `read_file` uses its target file; `terminal` statically classifies
   common read/search commands; `search_files` uses result paths when available and falls back to
   the requested search path. Other tool results pass through untouched.
2. **Gateway-safe path resolution.** Relative paths resolve against `TERMINAL_CWD`, then
   `os.getcwd()`. Gateway workers run with the daemon home as cwd, so resolving against
   `os.getcwd()` alone would look in the wrong tree.
3. **Walk up to the repository root.** Starting at each file's parent or directory target and
   moving up,
   the walk stops *after* processing the first directory that contains `.git` (directory or
   worktree file). Files outside any repository walk to the filesystem root — put a global
   `AGENTS.md` in `$HOME` and it applies everywhere.
4. **Nearest first.** A nested `AGENTS.md` is appended before the repo-root one, so the model
   reads local rules before global rules.
5. **Once per session.** Each instruction file is injected at most once per Hermes session
   (`session_id`, falling back to `task_id`). Parallel gateway sessions each get their own
   copy; the tracked-session map is bounded and evicts only sessions idle beyond its TTL, so
   a recent session keeps its dedupe state under cache pressure. The check-and-claim is
   atomic, so concurrent tool calls cannot inject the same file twice.
6. **Reading `AGENTS.md` itself injects nothing.** The content is already the tool result.
7. **Fail open.** A missing directory, unreadable file, decode error, or unexpected bug
   returns `None` — the tool result is never broken by this plugin.
8. **Size-capped.** Per-file and total caps apply *before* anything is appended, so one
   runaway `AGENTS.md` cannot flood the context window.
9. **Instruction-file safety.** A symlinked `AGENTS.md` is followed only when its resolved
   target stays inside the repository; links that escape the tree are ignored. A built-in
   deny list (`.env*`, private keys and certificates, cloud credentials, `secrets.*`,
   `.ssh`/`.aws`/`.kube`)
   refuses sensitive targets even when Hermes' read guard is unavailable, and the guard is
   consulted for everything else.

### Coordination with Hermes core hints

When Hermes core subdirectory hints are active, targets inside the terminal working directory
are skipped **when that working directory is the target's repository root** (the specialist
dispatch shape: `cwd=<repo>`), because core already appends their instructions. Targets outside
that directory, and targets under a working directory that is not their repo root (for example a
`TERMINAL_CWD` pointing at the Hermes home), stay eligible. Core hints are disabled by Hermes in
home-like working directories, so this plugin continues to provide the context there.

### One hook wins

`transform_tool_result` is a string-replacing hook and Hermes uses the **first non-`None`
string returned by any plugin**. If another enabled plugin also transforms these tool results,
only one of them takes effect. Disable the other plugin (or extend this one) rather
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
│   ├── test_injector.py # read_file behavior, dedupe, caps, path boundaries
│   ├── test_command_targets.py # terminal shell classifier
│   ├── test_terminal_and_search.py # terminal/search_files integration
│   └── test_regressions_v1_2_1.py # acceptance-gate regressions (symlinks, heredoc, dedupe race)
├── README.md
└── LICENSE
```

## Limitations

- Terminal classification is static and best-effort: unsupported commands, shell loops,
  `xargs`, and paths constructed through variables are not resolved.
- `search_files` result refinement depends on recognizable paths in its JSON metadata/text;
  otherwise the requested search path is used as the anchor.
- Only the filenames in `AGENTS_MD_INJECTOR_FILENAMES` are recognized; `CLAUDE.md` and other
  conventions require opting in through that variable.
- Injection state is in-process memory. A gateway restart or plugin reload clears it, and the
  next read re-injects (once) per session.
- The plugin does not parse or validate instruction content; it appends files verbatim
  (UTF-8, invalid bytes replaced, size-capped).

## License

MIT — see [LICENSE](LICENSE).
