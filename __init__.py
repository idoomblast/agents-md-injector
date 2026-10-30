"""AGENTS.md Injector — inject the nearest AGENTS.md into ``read_file`` results.

Hermes plugin that hooks ``transform_tool_result``. When the agent reads a file,
the plugin walks up from that file's directory to the repository root (the first
directory containing ``.git``) and appends every not-yet-injected ``AGENTS.md``
it finds to the tool result.

Design notes:

* **Per-session dedupe** — a file is injected at most once per Hermes session, so
  repeated reads in one conversation do not re-send the same instructions.
* **Token safety** — per-file and total byte caps (configurable) keep a runaway
  ``AGENTS.md`` from flooding the context.
* **Fail open** — any error is swallowed; the tool result passes through
  unchanged instead of breaking the agent.
* **Gateway safe** — relative paths resolve against ``TERMINAL_CWD`` first
  (gateway workers do not run with the project as their cwd).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

DEFAULT_FILENAMES: Tuple[str, ...] = ("AGENTS.md", "agents.md")
DEFAULT_MAX_FILE_BYTES = 32 * 1024
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024
MAX_TRACKED_SESSIONS = 64

DISABLE_ENV = "AGENTS_MD_INJECTOR_DISABLE"
FILENAMES_ENV = "AGENTS_MD_INJECTOR_FILENAMES"
MAX_FILE_ENV = "AGENTS_MD_INJECTOR_MAX_BYTES"
MAX_TOTAL_ENV = "AGENTS_MD_INJECTOR_MAX_TOTAL_BYTES"

# Session key -> canonical paths already injected in that session.
_injected: dict[str, Set[str]] = {}


# ── Configuration helpers ─────────────────────────────────────────────────────


def _env_flag(name: str) -> bool:
    """Return True when *name* is set to a truthy value (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_bytes(name: str, default: int) -> int:
    """Read a non-negative byte budget from the environment.

    Missing/blank/invalid values fall back to *default*. A value of ``0`` means
    "no limit" so users can disable a cap without disabling the plugin.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.debug("Ignoring invalid %s=%r (expected integer)", name, raw)
        return default
    return value if value > 0 else 0


def _configured_filenames() -> Tuple[str, ...]:
    """Instruction filenames to look for, nearest-first per directory.

    ``AGENTS_MD_INJECTOR_FILENAMES`` accepts a comma-separated list, e.g.
    ``AGENTS.md,CLAUDE.md``.
    """
    raw = os.environ.get(FILENAMES_ENV, "")
    names = tuple(name.strip() for name in raw.split(",") if name.strip())
    return names or DEFAULT_FILENAMES


# ── Session bookkeeping ───────────────────────────────────────────────────────


def _session_key(kwargs: dict) -> str:
    """Identify the session a tool call belongs to.

    Falls back to ``task_id`` (subagents) and then to the empty string (CLI
    single-process) when the hook fires without identity kwargs.
    """
    return str(kwargs.get("session_id") or kwargs.get("task_id") or "")


def _session_cache(session_key: str) -> Set[str]:
    """Return the injected-path cache for *session_key*, evicting the oldest
    session once ``MAX_TRACKED_SESSIONS`` is reached."""
    cache = _injected.get(session_key)
    if cache is None:
        if len(_injected) >= MAX_TRACKED_SESSIONS:
            _injected.pop(next(iter(_injected)))
        cache = _injected[session_key] = set()
    return cache


def reset_cache(session_key: Optional[str] = None) -> None:
    """Forget injected files (one session when given, otherwise all sessions)."""
    if session_key is None:
        _injected.clear()
    else:
        _injected.pop(session_key, None)


# ── Path helpers ──────────────────────────────────────────────────────────────


def resolve_read_path(filepath: str) -> Optional[Path]:
    """Resolve a ``read_file`` path the same way the tool does, but gateway-safe.

    Relative paths resolve against ``TERMINAL_CWD`` first because gateway
    workers run with the daemon home as their cwd, not the project directory.
    """
    if not filepath:
        return None
    path = Path(filepath).expanduser()
    if not path.is_absolute():
        base = os.environ.get("TERMINAL_CWD") or os.getcwd()
        path = Path(base).expanduser() / path
    try:
        return path.resolve()
    except OSError:
        return None


def find_git_root(start: Path) -> Optional[Path]:
    """Walk up from *start* and return the first directory containing ``.git``.

    ``.git`` may be a directory (normal clone) or a file (worktree/submodule).
    """
    for parent in (start, *start.parents):
        if (parent / ".git").exists():
            return parent
        if parent == parent.parent:
            break
    return None


# ── Instruction collection ────────────────────────────────────────────────────


def _read_text_limited(path: Path, max_bytes: int) -> Tuple[str, int]:
    """Read *path* as UTF-8, truncating at *max_bytes*.

    Returns ``(text, omitted_bytes)``. ``max_bytes == 0`` means no limit.
    """
    data = path.read_bytes()
    omitted = 0
    if max_bytes and len(data) > max_bytes:
        omitted = len(data) - max_bytes
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace").strip(), omitted


def collect_instruction_chunks(
    path: Path,
    *,
    session_key: str = "",
    filenames: Optional[Sequence[str]] = None,
    max_file_bytes: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
) -> List[str]:
    """Collect "Instructions from: <path>" chunks for *path*.

    Walks from the file's directory up to the git root (or the filesystem root
    when the file is outside any repository), nearest directory first. Files
    already injected in this session are skipped, as is the file being read
    when it is itself an instruction file.
    """
    names = tuple(filenames) if filenames is not None else _configured_filenames()
    per_file = (
        max_file_bytes if max_file_bytes is not None else _env_bytes(MAX_FILE_ENV, DEFAULT_MAX_FILE_BYTES)
    )
    total_budget = (
        max_total_bytes
        if max_total_bytes is not None
        else _env_bytes(MAX_TOTAL_ENV, DEFAULT_MAX_TOTAL_BYTES)
    )

    cache = _session_cache(session_key)
    chunks: List[str] = []
    used = 0

    start = path.parent
    if not start.exists():
        return chunks

    git_root = find_git_root(start)
    current = start
    while True:
        for name in names:
            candidate = current / name
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue

            key = str(resolved)
            if key in cache:
                continue

            # Reading the instruction file itself: its content is already the
            # tool result, so mark it seen and move on.
            if resolved == path:
                cache.add(key)
                continue

            if total_budget and used >= total_budget:
                return chunks

            limit = per_file
            if total_budget:
                limit = min(limit, total_budget - used) if limit else total_budget - used

            try:
                content, omitted = _read_text_limited(resolved, limit)
            except OSError as exc:
                logger.debug("Could not read %s: %s", resolved, exc)
                continue

            if not content:
                cache.add(key)
                continue

            cache.add(key)
            if omitted:
                content += (
                    f"\n\n[agents-md-injector: {omitted} bytes truncated "
                    f"from {resolved.name}]"
                )
            chunks.append(f"Instructions from: {resolved}\n{content}")
            used += len(content.encode("utf-8"))

        if git_root is not None and current == git_root:
            break
        if current.parent == current:  # filesystem root
            break
        current = current.parent

    return chunks


# ── Hook ──────────────────────────────────────────────────────────────────────


def _on_transform_tool_result(
    tool_name: str = "",
    args: Any = None,
    result: Any = None,
    **kwargs: Any,
) -> Optional[str]:
    """Append not-yet-injected AGENTS.md context to a ``read_file`` result.

    Returns ``None`` (result unchanged) whenever there is nothing to inject or
    anything goes wrong.
    """
    if tool_name != "read_file" or not isinstance(args, dict):
        return None
    if _env_flag(DISABLE_ENV):
        return None

    filepath = args.get("path")
    if not isinstance(filepath, str):
        try:
            filepath = os.fspath(filepath)  # type: ignore[arg-type]
        except TypeError:
            return None
    if not filepath.strip():
        return None

    try:
        path = resolve_read_path(filepath)
        if path is None or not path.parent.exists():
            return None
        chunks = collect_instruction_chunks(path, session_key=_session_key(kwargs))
    except Exception as exc:  # never break a tool call because of this plugin
        logger.debug("agents-md-injector failed for %r: %s", filepath, exc)
        return None

    if not chunks:
        return None

    separator = "\n\n---\n\n"
    return f"{result}{separator}{separator.join(chunks)}"


# ── Registration ──────────────────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — called once by the Hermes plugin loader."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    logger.info("AGENTS.md injector plugin registered")
