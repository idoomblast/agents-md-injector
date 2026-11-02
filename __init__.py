"""AGENTS.md Injector — add relevant agent instructions to file-oriented results.

Hermes plugin that hooks ``transform_tool_result``. When the agent reads or searches
files, the plugin walks up from each target to the repository root (the first
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

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Set, Tuple

if __package__:
    from .command_targets import extract_terminal_targets

logger = logging.getLogger(__name__)

DEFAULT_FILENAMES: Tuple[str, ...] = ("AGENTS.md", "agents.md")
DEFAULT_MAX_FILE_BYTES = 32 * 1024
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024
SEPARATOR = "\n\n---\n\n"
SEPARATOR_BYTES = len(SEPARATOR.encode("utf-8"))
MAX_TRACKED_SESSIONS = 64
# A session idle for less than this keeps its dedupe state even when the map is
# over its bound; only genuinely stale sessions are evicted.
SESSION_IDLE_TTL = 900.0

DISABLE_ENV = "AGENTS_MD_INJECTOR_DISABLE"
FILENAMES_ENV = "AGENTS_MD_INJECTOR_FILENAMES"
MAX_FILE_ENV = "AGENTS_MD_INJECTOR_MAX_BYTES"
MAX_TOTAL_ENV = "AGENTS_MD_INJECTOR_MAX_TOTAL_BYTES"
MAX_TERMINAL_TARGETS_ENV = "AGENTS_MD_INJECTOR_MAX_TERMINAL_TARGETS"
MAX_SEARCH_DIRS_ENV = "AGENTS_MD_INJECTOR_MAX_SEARCH_DIRS"
DEFAULT_MAX_TERMINAL_TARGETS = 8
DEFAULT_MAX_SEARCH_DIRS = 5

# Session key -> canonical paths already injected in that session.
_injected: dict[str, Set[str]] = {}
_injected_lock = threading.Lock()
# Sessions with a transform in flight; their cache must not be evicted.
_active_sessions: dict[str, int] = {}
_last_seen: dict[str, float] = {}


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


def _env_count(name: str, default: int) -> int:
    """Read a positive count, using *default* for blank or invalid values."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
        return value if value >= 0 else default
    except ValueError:
        logger.debug("Ignoring invalid %s=%r (expected integer)", name, raw)
        return default


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


def _trim_locked(now: Optional[float] = None) -> None:
    """Evict stale inactive sessions until the map is within its bound."""
    current = time.monotonic() if now is None else now
    while len(_injected) > MAX_TRACKED_SESSIONS:
        victim = next(
            (
                key
                for key in _injected
                if _active_sessions.get(key, 0) == 0
                and current - _last_seen.get(key, 0.0) >= SESSION_IDLE_TTL
            ),
            None,
        )
        if victim is None:
            return
        del _injected[victim]
        _last_seen.pop(victim, None)


def _session_cache(session_key: str) -> Set[str]:
    """Return the injected-path cache for *session_key*, evicting the oldest
    inactive session once ``MAX_TRACKED_SESSIONS`` is reached."""
    with _injected_lock:
        cache = _injected.get(session_key)
        if cache is None:
            if len(_injected) >= MAX_TRACKED_SESSIONS:
                now = time.monotonic()
                for victim in list(_injected):
                    if (
                        _active_sessions.get(victim, 0) == 0
                        and now - _last_seen.get(victim, 0.0) >= SESSION_IDLE_TTL
                    ):
                        del _injected[victim]
                        _last_seen.pop(victim, None)
                        break
            cache = _injected[session_key] = set()
        _last_seen[session_key] = time.monotonic()
        return cache


def _activate(session_key: str) -> None:
    with _injected_lock:
        _active_sessions[session_key] = _active_sessions.get(session_key, 0) + 1
        if session_key in _injected:
            _last_seen[session_key] = time.monotonic()


def _deactivate(session_key: str) -> None:
    with _injected_lock:
        remaining = _active_sessions.get(session_key, 0) - 1
        if remaining > 0:
            _active_sessions[session_key] = remaining
            if session_key in _injected:
                _last_seen[session_key] = time.monotonic()
            return
        _active_sessions.pop(session_key, None)
        cache = _injected.get(session_key)
        if cache is None:
            _last_seen.pop(session_key, None)
        elif not cache:
            # A call that found no instruction file must not leave metadata.
            del _injected[session_key]
            _last_seen.pop(session_key, None)
        else:
            _last_seen[session_key] = time.monotonic()
        _trim_locked()


def _reserve(cache: Set[str], key: str, claim_log: Optional[List[str]] = None) -> bool:
    """Atomically claim *key*; False when it was already claimed."""
    with _injected_lock:
        if key in cache:
            return False
        cache.add(key)
        if claim_log is not None:
            claim_log.append(key)
        return True


def _release(cache: Set[str], key: str, claim_log: Optional[List[str]] = None) -> None:
    """Give a reservation back so a later call may retry the read."""
    with _injected_lock:
        cache.discard(key)
    if claim_log is not None and key in claim_log:
        claim_log.remove(key)


def _mark_seen(cache: Set[str], key: str, claim_log: Optional[List[str]] = None) -> None:
    """Record a path as handled without claiming it for a read."""
    with _injected_lock:
        added = key not in cache
        cache.add(key)
    if claim_log is not None and added:
        claim_log.append(key)


def reset_cache(session_key: Optional[str] = None) -> None:
    """Forget injected files (one session when given, otherwise all sessions)."""
    with _injected_lock:
        if session_key is None:
            _injected.clear()
            _active_sessions.clear()
            _last_seen.clear()
        else:
            _injected.pop(session_key, None)
            _last_seen.pop(session_key, None)


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


def _working_dir() -> Path:
    """Return the session terminal cwd without resolving symlinks."""
    base = Path(os.environ.get("TERMINAL_CWD") or os.getcwd()).expanduser()
    return Path(os.path.abspath(str(base)))


def core_hints_active(working_dir: Path) -> bool:
    """Mirror Hermes core's home-like cwd check for subdirectory hints."""
    home = Path.home().resolve()
    resolved = working_dir.resolve()
    if resolved == home or resolved in {Path("/home"), Path("/root"), Path("/workspace")}:
        return False
    if resolved.parent in {Path("/home"), Path("/root"), Path("/workspace")}:
        return False
    return not any(part in {".home", "host-home", "host_home"} for part in resolved.parts)


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _core_should_skip(target_dir: Path, working_dir: Path) -> bool:
    """True when Hermes core's own hints already cover *target_dir*.

    Core anchors on the session workspace and only covers targets inside it.
    Requiring the working dir to be the target's repository root keeps the gate
    from misfiring when ``TERMINAL_CWD`` points at a non-project directory
    (e.g. the Hermes home) while core's session cwd is the real workspace.
    """
    if not core_hints_active(working_dir):
        return False
    if not _is_inside(target_dir, working_dir):
        return False
    repo_root = find_git_root(target_dir)
    return repo_root is not None and repo_root == working_dir.resolve()


def _lexical_from(value: Any, base: Path) -> Optional[Path]:
    """Join *value* onto *base* without resolving symlinks."""
    try:
        path = Path(os.fspath(value)).expanduser()
        if not path.is_absolute():
            path = base / path
        return path
    except (TypeError, ValueError, OSError, RuntimeError):
        return None


def _has_symlink_component(path: Path, stop: Path) -> bool:
    """True when *path* or one of its parents up to *stop* is a symlink."""
    try:
        stop = stop.resolve()
    except OSError:
        return True
    current = path
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def _first_symlink_component(path: Path) -> Optional[Path]:
    """Return the first symlink encountered while walking *path* top-down."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return current
        except OSError:
            return current
    return None


def _is_within_lexical(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _find_origin_git_root(start: Path) -> Optional[Path]:
    """Find the git root of the lexical path, ignoring .git reached via symlinks.

    A symlinked directory inside a repository must not establish a new trusted
    root: only ancestors above the first symlink component are eligible.
    """
    start = _lexical_normalize(start)
    first = _first_symlink_component(start)
    current = start
    while True:
        if not (first is not None and _is_within_lexical(current, first)):
            if (current / ".git").exists():
                return current
        if current.parent == current:
            return None
        current = current.parent


def _lexical_normalize(value: Path) -> Path:
    """Collapse ``..`` without resolving symlinks."""
    return Path(os.path.normpath(str(value)))


def _within_trust(path: Path, trust: Path) -> bool:
    """True only when both the lexical path and its resolution stay in trust."""
    return _is_within_lexical(
        _lexical_normalize(path), _lexical_normalize(trust)
    ) and _is_inside(path, trust)


def _anchor_trust_root(anchor: Path) -> Path:
    """Return the normalized lexical trust boundary for a search anchor."""
    anchor = _lexical_normalize(anchor)
    origin = _find_origin_git_root(anchor)
    if origin is not None:
        return _lexical_normalize(origin)
    first = _first_symlink_component(anchor)
    if first is not None:
        return _lexical_normalize(first.parent)
    return _lexical_normalize(anchor)


def _resolve_from(value: Any, base: Path) -> Optional[Path]:
    try:
        text = os.fspath(value)
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = base / path
        return path.resolve()
    except (TypeError, ValueError, OSError, RuntimeError):
        return None


def _json_metadata(result: Any) -> Any:
    """Parse a JSON tool result, including the escaped representation from text wrappers."""
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError):
        try:
            return json.loads(result.replace("\\n", "\n"))
        except (json.JSONDecodeError, TypeError):
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


_SENSITIVE_NAMES = {
    ".netrc",
    ".pgpass",
    ".htpasswd",
    "credentials",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
_SENSITIVE_SUFFIXES = {
    ".asc",
    ".cer",
    ".crt",
    ".der",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
}
_SENSITIVE_DIRS = {".aws", ".gnupg", ".kube", ".ssh"}


def _looks_sensitive(path: Path) -> bool:
    """Name-based deny list that works even when the core guard is unavailable."""
    name = path.name.lower()
    if name.startswith(".env"):
        return True
    if name.startswith("id_") or name.startswith("secrets."):
        return True
    if name in _SENSITIVE_NAMES or path.suffix.lower() in _SENSITIVE_SUFFIXES:
        return True
    return any(part.lower() in _SENSITIVE_DIRS for part in path.parts)


def _read_blocked(path: Path) -> bool:
    """True when *path* must not be injected as instructions.

    The known-sensitive names are always refused, so the guard cannot fail
    open when Hermes core is not importable (plugin tests, standalone use).
    """
    if _looks_sensitive(path):
        return True
    try:
        from agent.file_safety import get_read_block_error
    except Exception:
        return False
    try:
        return get_read_block_error(str(path)) is not None
    except Exception:
        return False


def collect_instruction_chunks(
    path: Path,
    *,
    session_key: str = "",
    filenames: Optional[Sequence[str]] = None,
    max_file_bytes: Optional[int] = None,
    max_total_bytes: Optional[int] = None,
    start_at_directory: bool = False,
    trust_root: Optional[Path] = None,
    claim_log: Optional[List[str]] = None,
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

    start = _lexical_normalize(path if start_at_directory else path.parent)
    if not start.exists():
        return chunks

    if trust_root is not None:
        trust_root = _lexical_normalize(trust_root)
        if not _within_trust(start, trust_root):
            return chunks
        git_root = trust_root
    else:
        git_root = _find_origin_git_root(start)
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
            if total_budget and used >= total_budget:
                return chunks
            if not _reserve(cache, key, claim_log):
                continue

            # A symlinked instruction file (or a symlinked parent directory)
            # may point outside the tree it sits in (e.g. at ~/.aws/credentials).
            # Inside a repository, every resolved target must stay in that
            # repository. Without a repository, only a path with a symlink
            # component must stay inside the starting directory; the normal
            # walk-up to ancestor instruction files is preserved.
            if git_root is not None:
                if not _is_inside(resolved, git_root):
                    logger.debug("Ignoring out-of-tree instruction target %s -> %s", candidate, resolved)
                    _release(cache, key, claim_log)
                    continue
            elif _has_symlink_component(candidate, start):
                first = _first_symlink_component(start)
                boundary = first.parent if first is not None else start
                if not _is_inside(resolved, boundary):
                    logger.debug("Ignoring out-of-tree instruction symlink %s -> %s", candidate, resolved)
                    _release(cache, key, claim_log)
                    continue
            if _read_blocked(resolved):
                logger.debug("Ignoring sensitive instruction target %s", resolved)
                _release(cache, key, claim_log)
                continue

            limit = per_file
            if total_budget:
                limit = min(limit, total_budget - used) if limit else total_budget - used

            try:
                content, omitted = _read_text_limited(resolved, limit)
            except OSError as exc:
                logger.debug("Could not read %s: %s", resolved, exc)
                _release(cache, key, claim_log)
                continue

            if not content:
                # Nothing to inject: release the reservation so a later call
                # can inject once the file actually has instructions.
                _release(cache, key, claim_log)
                continue

            # Reading the instruction file itself: its content is already the
            # tool result, so keep it reserved and move on. Only do this when
            # the file actually has content; an empty self-read is released
            # above so a later call can inject it once populated.
            if resolved == path.resolve():
                continue

            header = f"Instructions from: {resolved}\n"
            header_bytes = len(header.encode("utf-8"))

            def _marker(count: int) -> str:
                return (
                    f"\n\n[agents-md-injector: {count} bytes truncated "
                    f"from {resolved.name}]"
                )

            marker = _marker(omitted) if omitted else ""
            if total_budget:
                remaining = total_budget - used
                if remaining <= SEPARATOR_BYTES + header_bytes:
                    _release(cache, key, claim_log)
                    return chunks
                max_chunk_bytes = remaining - SEPARATOR_BYTES
                content_bytes = content.encode("utf-8")
                content_budget = 0
                for _ in range(4):
                    marker_bytes = len(marker.encode("utf-8"))
                    content_budget = max(
                        max_chunk_bytes - header_bytes - marker_bytes, 0
                    )
                    if len(content_bytes) <= content_budget:
                        break
                    trimmed = content_bytes[:content_budget].decode("utf-8", "ignore")
                    trimmed_bytes = trimmed.encode("utf-8")
                    cut = len(content_bytes) - len(trimmed_bytes)
                    if cut <= 0:
                        break
                    omitted += cut
                    marker = _marker(omitted)
                    content_bytes = trimmed_bytes
                if content_budget <= 0 or not content_bytes:
                    _release(cache, key, claim_log)
                    continue
                content = content_bytes.decode("utf-8", "ignore")
                chunk = header + content + marker
                if len(chunk.encode("utf-8")) > max_chunk_bytes:
                    # The marker grew after the final trim; shrinking content
                    # again would make the marker's count stale. Omit the
                    # chunk rather than emit silently incomplete instructions.
                    _release(cache, key, claim_log)
                    continue
            else:
                chunk = header + content + marker
            chunks.append(chunk)
            used += len(chunk.encode("utf-8")) + SEPARATOR_BYTES

        if git_root is not None and current == git_root:
            break
        if trust_root is not None and current == trust_root:
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
    """Append not-yet-injected AGENTS.md context to file-oriented tool results.

    Returns ``None`` (result unchanged) whenever there is nothing to inject or
    anything goes wrong.
    """
    if tool_name not in {"read_file", "search_files", "terminal"} or not isinstance(args, dict):
        return None
    if _env_flag(DISABLE_ENV):
        return None

    session_key = _session_key(kwargs)
    _activate(session_key)
    claim_log: List[str] = []
    try:
        working_dir = _working_dir()
        targets: List[Tuple[Path, bool]] = []

        if tool_name == "read_file":
            filepath = args.get("path")
            if not isinstance(filepath, str):
                try:
                    filepath = os.fspath(filepath)  # type: ignore[arg-type]
                except TypeError:
                    return None
            if not filepath.strip():
                return None
            path = resolve_read_path(filepath)
            if path is None or not path.parent.exists():
                return None
            lexical = _lexical_from(filepath, working_dir)
            if lexical is not None and lexical.exists():
                path = lexical
            targets.append((path, False))

        elif tool_name == "terminal":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return None
            result_data = _json_metadata(result)
            result_cwd = result_data.get("cwd") if isinstance(result_data, dict) else None
            found = extract_terminal_targets(
                command,
                working_dir,
                workdir=os.fspath(args["workdir"]) if args.get("workdir") else None,
                result_cwd=result_cwd,
                max_targets=_env_count(MAX_TERMINAL_TARGETS_ENV, DEFAULT_MAX_TERMINAL_TARGETS),
            )
            for path in found:
                targets.append((path, path.is_dir()))

        else:
            anchor_value = args.get("path", ".")
            anchor = _lexical_from(anchor_value, working_dir)
            if anchor is None or not anchor.exists():
                anchor = _resolve_from(anchor_value, working_dir)
            if anchor is None:
                anchor = working_dir
            if anchor.is_file():
                anchor = anchor.parent
            if not anchor.is_dir():
                return None
            anchor_trust = _anchor_trust_root(anchor)
            refined = _search_result_directories(
                result,
                anchor,
                working_dir,
                _env_count(MAX_SEARCH_DIRS_ENV, DEFAULT_MAX_SEARCH_DIRS),
                trust_root=anchor_trust,
            )
            targets.extend((directory, True) for directory in refined)
        chunks: List[str] = []
        if tool_name == "search_files":
            for path in _search_result_paths(result, anchor, working_dir):
                if path.name not in _configured_filenames():
                    continue
                if _core_should_skip(path.parent, working_dir):
                    continue
                trust_root = _anchor_trust_root(anchor)
                if not _within_trust(path, trust_root):
                    continue
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if _read_blocked(resolved):
                    continue
                try:
                    content, _ = _read_text_limited(resolved, 4096)
                except OSError:
                    continue
                if content:
                    _mark_seen(
                        _session_cache(session_key),
                        str(resolved),
                        claim_log=claim_log,
                    )
        total_budget = _env_bytes(MAX_TOTAL_ENV, DEFAULT_MAX_TOTAL_BYTES)
        total_used = 0
        for path, is_directory in targets:
            target_dir = path if is_directory else path.parent
            if _core_should_skip(target_dir, working_dir):
                continue
            if not target_dir.exists():
                continue
            if total_budget and total_used >= total_budget:
                break
            new_chunks = collect_instruction_chunks(
                path,
                session_key=session_key,
                start_at_directory=is_directory,
                max_total_bytes=(total_budget - total_used) if total_budget else None,
                trust_root=anchor_trust if tool_name == "search_files" else None,
                claim_log=claim_log,
            )
            chunks.extend(new_chunks)
            total_used += sum(
                len(chunk.encode("utf-8")) + SEPARATOR_BYTES for chunk in new_chunks
            )
    except Exception as exc:  # never break a tool call because of this plugin
        logger.debug("agents-md-injector failed for %s: %s", tool_name, exc)
        try:
            cache = _session_cache(session_key)
        except Exception:
            cache = None
        if cache is not None:
            for key in list(claim_log):
                _release(cache, key, claim_log)
        return None
    finally:
        _deactivate(session_key)

    if not chunks:
        return None

    return f"{result}{SEPARATOR}{SEPARATOR.join(chunks)}"


def _search_result_directories(
    result: Any,
    anchor: Path,
    working_dir: Path,
    limit: int,
    trust_root: Optional[Path] = None,
) -> List[Path]:
    """Best-effort extraction of match parents, falling back to the search anchor."""
    if limit == 0:
        return [anchor]
    data = _json_metadata(result)
    if not isinstance(data, dict):
        return [anchor]

    paths = _search_result_paths(result, anchor, working_dir)
    directories: List[Path] = []
    seen: Set[str] = set()
    for path in paths:
        if trust_root is not None and not _within_trust(path, trust_root):
            continue
        directory = path if path.is_dir() else path.parent
        try:
            identity = str(directory.resolve())
        except OSError:
            identity = str(directory)
        if identity not in seen:
            seen.add(identity)
            directories.append(directory)
            if len(directories) >= limit:
                break
    return directories or [anchor]


def _search_result_paths(result: Any, anchor: Path, working_dir: Path) -> List[Path]:
    """Resolve existing paths exposed by search_files result metadata."""
    data = _json_metadata(result)
    if not isinstance(data, dict):
        return []

    candidates: List[str] = []
    matches = data.get("matches")
    if isinstance(matches, list):
        for match in matches:
            if isinstance(match, dict) and isinstance(match.get("path"), str):
                candidates.append(match["path"])

    matches_text = data.get("matches_text")
    if isinstance(matches_text, str):
        for line in matches_text.splitlines():
            if line and not line[0].isspace():
                candidates.append(line)
                parts = line.split(":")
                candidates.extend(":".join(parts[:index]) for index in range(1, len(parts)))

    files = data.get("files")
    if isinstance(files, list):
        candidates.extend(value for value in files if isinstance(value, str))

    counts = data.get("counts")
    if isinstance(counts, dict):
        candidates.extend(key for key in counts if isinstance(key, str))

    paths: List[Path] = []
    seen: Set[str] = set()
    for value in candidates:
        path = _lexical_from(value, anchor)
        if path is None or not path.exists():
            path = _lexical_from(value, working_dir)
        if path is None or not path.exists():
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


# ── Registration ──────────────────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — called once by the Hermes plugin loader."""
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    logger.info("AGENTS.md injector plugin registered")
