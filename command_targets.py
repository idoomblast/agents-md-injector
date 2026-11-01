"""Classify shell command segments and extract existing file or directory targets.

This module deliberately avoids shell execution. It is a best-effort static
classifier for common read/search commands, not a general shell interpreter.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

READ_COMMANDS = {
    "cat", "tac", "nl", "less", "more", "bat", "strings", "xxd", "od",
    "hexdump", "head", "tail", "sed", "awk", "jq", "grep", "egrep",
    "fgrep", "rg", "ag", "ack", "diff", "cmp", "cut", "sort", "uniq",
    "comm", "join", "paste",
}
SEARCH_COMMANDS = {"find", "fd"}
SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}
WRAPPERS = {"sudo", "env", "time", "nice", "ionice", "timeout", "command", "builtin", "exec", "nohup"}

# Heredoc marker in a command line: <<EOF, <<-EOF, <<'EOF', <<"EOF".
_HEREDOC_RE = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")


def _strip_heredocs(command: str) -> str:
    """Remove heredoc bodies so feed content is never parsed as argv.

    Only the marker line survives (it is a redirection the tokenizer drops).
    """
    lines = command.splitlines()
    kept: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        match = _HEREDOC_RE.search(line)
        if match is None:
            index += 1
            continue
        delimiter = match.group("delimiter")
        index += 1
        while index < len(lines) and lines[index].strip() != delimiter:
            index += 1
        if index < len(lines):
            index += 1  # drop the delimiter line too
    return "\n".join(kept)


def _resolve(path: str, cwd: Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = cwd / value
    return value.resolve()


def _shell_tokens(command: str) -> List[str]:
    """Tokenize shell syntax without splitting quoted separators or operators."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    raw_tokens = list(lexer)
    tokens: List[str] = []
    for token in raw_tokens:
        if token and all(char in ";&|()<>" for char in token):
            tokens.extend(_split_operators(token))
        else:
            descriptor = token.rpartition(">")[0]
            if token.endswith(">") and descriptor.isdigit():
                tokens.extend([descriptor, ">"])
            else:
                tokens.append(token)
    return tokens


def _split_operators(value: str) -> List[str]:
    result: List[str] = []
    index = 0
    while index < len(value):
        if value.startswith(("&&", "||", "&>>", "<<<", ">>", "<<", "&>"), index):
            operator = next(
                candidate
                for candidate in ("&&", "||", "&>>", "<<<", ">>", "<<", "&>")
                if value.startswith(candidate, index)
            )
        else:
            operator = value[index]
        result.append(operator)
        index += len(operator)
    return result


def _segments(tokens: Sequence[str]) -> List[List[str]]:
    result: List[List[str]] = []
    current: List[str] = []
    for token in tokens:
        if token in SEPARATORS:
            if current:
                result.append(current)
                current = []
        else:
            current.append(token)
    if current:
        result.append(current)
    return result


def _strip_redirections(tokens: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Return command tokens and input-redirection targets."""
    command: List[str] = []
    inputs: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {">", ">>", "&>", "&>>", "<<<"}:
            index += 2
            continue
        if token in {"<", "<<", "<<-"}:
            if index + 1 < len(tokens):
                if token == "<":
                    inputs.append(tokens[index + 1])
                index += 2
            else:
                index += 1
            continue
        # shlex yields the descriptor in `2>file` as a standalone `2`.
        if token == "2" and index + 1 < len(tokens) and tokens[index + 1] in {">", ">>"}:
            index += 3
            continue
        command.append(token)
        index += 1
    return command, inputs


def _unwrap(tokens: Sequence[str]) -> List[str]:
    """Remove leading command wrappers and the arguments that belong to them."""
    args = list(tokens)
    while args and args[0] in WRAPPERS:
        wrapper = args.pop(0)
        if wrapper == "sudo":
            while args:
                option = args[0]
                if option == "--":
                    args.pop(0)
                    break
                if not option.startswith("-"):
                    break
                args.pop(0)
                if option in {"-u", "-g", "--user", "--group"} and args:
                    args.pop(0)
                elif option.startswith(("--user=", "--group=")):
                    continue
            continue
        if wrapper == "env":
            while args:
                if "=" in args[0] and not args[0].startswith("="):
                    args.pop(0)
                elif args[0] in {"-i", "--ignore-environment", "-0", "--null"}:
                    args.pop(0)
                elif args[0] in {"-u", "--unset", "-C", "--chdir"}:
                    args.pop(0)
                    if args:
                        args.pop(0)
                else:
                    break
            continue
        if wrapper == "timeout":
            if args and args[0] == "--":
                args.pop(0)
            else:
                while args and args[0].startswith("-"):
                    option = args.pop(0)
                    if option in {"-k", "--kill-after", "-s", "--signal"} and args:
                        args.pop(0)
                if args:
                    args.pop(0)  # duration
            continue
        if wrapper == "nice":
            if args and args[0] == "-n":
                args = args[2:] if len(args) > 1 else []
            elif args and args[0].startswith("-n") and args[0] != "-n":
                args.pop(0)
            continue
        if wrapper == "ionice":
            while args and args[0].startswith("-"):
                option = args.pop(0)
                if option in {"-c", "-n", "-t", "-p"} and args:
                    args.pop(0)
            continue
        if wrapper == "time":
            while args and args[0].startswith("-"):
                option = args.pop(0)
                if option in {"-f", "--format", "-o", "--output"} and args:
                    args.pop(0)
            continue
        if wrapper in {"command", "builtin"}:
            while args and args[0].startswith("-"):
                args.pop(0)
            continue
        # exec/nohup have no required operand options for this classifier.
    return args


def _is_option(token: str) -> bool:
    return token.startswith("-") and token != "-"


def _positionals(
    args: Sequence[str],
    valued: Iterable[str] = (),
    valued_prefixes: Iterable[str] = (),
) -> List[str]:
    """Collect positional operands while dropping option flags and values."""
    valued_set = set(valued)
    valued_prefixes_set = tuple(valued_prefixes)
    result: List[str] = []
    index = 0
    options = True
    while index < len(args):
        token = args[index]
        if options and token == "--":
            options = False
            index += 1
            continue
        if options and _is_option(token):
            name = token.split("=", 1)[0]
            if name in valued_set and "=" not in token and index + 1 < len(args):
                index += 2
            elif any(
                token.startswith(prefix) and len(token) > len(prefix)
                for prefix in valued_prefixes_set
            ):
                index += 1
            else:
                index += 1
            continue
        result.append(token)
        index += 1
    return result


def _read_operands(command: str, args: Sequence[str]) -> Optional[List[str]]:
    if command in {"head", "tail"}:
        files: List[str] = []
        index = 0
        options = True
        while index < len(args):
            token = args[index]
            if options and token == "--":
                options = False
                index += 1
            elif options and (token in {"-n", "-c"} or (command == "tail" and token == "-s")):
                index += 2
            elif options and token.startswith("-") and token != "-":
                option = token.split("=", 1)[0]
                if option in {"-n", "-c"} or (command == "tail" and option == "-s"):
                    index += 2 if "=" not in token else 1
                else:
                    index += 1
            else:
                files.append(token)
                index += 1
        if command == "tail" and any(
            a == "--follow"
            or a.startswith("--follow=")
            or (a.startswith("-") and "f" in a[1:])
            for a in args
        ):
            return None
        return files

    if command == "sed":
        if any(
            a in {"-i", "--in-place"}
            or a.startswith("--in-place=")
            or (a.startswith("-i") and a != "-i" and not a.startswith("--"))
            for a in args
        ):
            return None
        positional: List[str] = []
        has_script_option = False
        index = 0
        options = True
        while index < len(args):
            token = args[index]
            if options and token == "--":
                options = False
                index += 1
            elif options and token in {"-e", "--expression", "-f", "--file"}:
                has_script_option = True
                index += 2
            elif options and (token.startswith("-e") or token.startswith("-f")) and len(token) > 2:
                has_script_option = True
                index += 1
            elif options and _is_option(token):
                index += 1
            else:
                positional.append(token)
                index += 1
        if positional and not has_script_option:
            positional.pop(0)
        return positional

    if command == "awk":
        positional = _positionals(
            args,
            {"-F", "-v", "-f", "--field-separator", "--assign", "--file"},
            {"-F", "-v"},
        )
        if positional:
            positional.pop(0)
        return positional

    if command == "jq":
        positional = _positionals(args, {"-f", "--from-file", "-L", "--library-path"})
        if positional:
            positional.pop(0)
        return positional

    if command in {"grep", "egrep", "fgrep", "ag", "ack"}:
        valued = {
            "-e", "--regexp", "-f", "--file", "-A", "-B", "-C", "-m", "--max-count",
            "--include", "--exclude", "--exclude-dir",
        }
        positional = _positionals(args, valued, {"-e", "-f", "-A", "-B", "-C", "-m"})
        has_pattern_option = any(
            a in {"-e", "--regexp", "-f", "--file"}
            or (a.startswith(("-e", "-f")) and len(a) > 2)
            or a.startswith("--regexp=")
            or a.startswith("--file=")
            for a in args
        )
        if positional and not has_pattern_option:
            positional.pop(0)
        recursive = any(
            a in {"-r", "-R", "--recursive"}
            or a.startswith("--recursive=")
            or (
                a.startswith("-")
                and not a.startswith("--")
                and any(flag in a[1:] for flag in "rR")
            )
            for a in args
        )
        return positional if positional else (["."] if recursive else [])

    if command == "rg":
        valued = {
            "-e", "--regexp", "-f", "--file", "-g", "--glob", "-t", "--type", "-T",
            "-A", "-B", "-C", "-m", "--max-count",
        }
        positional = _positionals(args, valued, {"-e", "-f", "-g", "-t", "-A", "-B", "-C", "-m"})
        has_pattern_option = any(
            a in {"-e", "--regexp", "-f", "--file"}
            or (a.startswith(("-e", "-f")) and len(a) > 2)
            or a.startswith("--regexp=")
            or a.startswith("--file=")
            for a in args
        )
        if positional and not has_pattern_option:
            positional.pop(0)
        return positional or ["."]

    option_values = {
        "nl": {"-b", "-n", "-w", "-s"},
        "sort": {"-k", "-t", "-o", "--key", "--field-separator", "--output"},
        "cut": {"-d", "-f", "-c", "--delimiter", "--fields", "--characters"},
        "xxd": {"-l", "-c", "-g", "-o"},
        "od": {"-A", "-j", "-N", "-t", "-w"},
        "hexdump": {"-n", "-s", "-e", "-f"},
        "strings": {"-n", "-t"},
        "uniq": {"-f", "-s", "-w"},
        # Delimiter/field/format arguments are values, not input files.
        "paste": {"-d", "--delimiters"},
        "join": {
            "-1", "-2", "-j", "-o", "-t", "-e", "-a", "-v", "--field-separator",
        },
        "less": {
            "-p", "--pattern", "-P", "--prompt", "-x", "--tabs", "-y", "--shift",
            "-z", "--window", "-b", "--buffers", "-h", "--max-back-scroll",
            "-j", "--jump-target", "-k", "--lesskey-file", "-o", "--log-file",
            "-O", "--LOG-FILE",
        },
        "more": {"-n", "--lines"},
        "bat": {
            "-l", "--language", "-r", "--range", "--theme", "--style", "--tabs",
            "--wrap", "--terminal-width", "--pager", "-H", "--highlight-line",
        },
    }
    if command in {"diff", "cmp"}:
        valued = {"-U", "-C", "-L", "--label", "-S", "--starting-file", "--from-file", "--to-file"}
        if command == "cmp":
            valued.update({"-i", "--ignore-initial", "-n", "--bytes"})
        return _positionals(args, valued)[:2]
    return _positionals(args, option_values.get(command, set()))


def _search_operands(command: str, args: Sequence[str]) -> List[str]:
    positional: List[str] = []
    if command == "find":
        if args and args[0] in {"-D", "-O"}:
            index = 2
            while index < len(args):
                token = args[index]
                if token in {"!", "(", ")"} or token.startswith("-"):
                    break
                return [token]
            return ["."]
        for token in args:
            if token in {"!", "(", ")"} or token.startswith("-"):
                break
            positional.append(token)
            break
        return positional or ["."]

    # fd syntax: fd [options] [pattern] [root]. The first positional is pattern.
    positional = _positionals(
        args,
        {
            "-E", "--exclude", "-e", "--extension", "-t", "--type", "-x", "--exec",
            "-X", "--exec-batch", "-d", "--max-depth",
        },
    )
    if positional:
        positional.pop(0)
    return positional or ["."]


def _skip_path(value: str) -> bool:
    if not value or value == "-" or "://" in value or _is_option(value):
        return True
    return value.startswith(("/dev/", "/proc/", "/sys/"))


def _glob_paths(value: str, cwd: Path) -> List[Path]:
    resolved = _resolve(value, cwd)
    raw = str(resolved)
    if not any(char in raw for char in "*?["):
        return [resolved] if resolved.exists() else []
    parts = Path(raw).parts
    wildcard_index = next((i for i, part in enumerate(parts) if any(c in part for c in "*?[")), None)
    if wildcard_index is None:
        return [resolved] if resolved.exists() else []
    prefix_parts = parts[:wildcard_index]
    root = Path(*prefix_parts) if prefix_parts else Path("/")
    if raw.startswith("/") and not root.is_absolute():
        root = Path("/") / root
    pattern_parts = parts[wildcard_index:]
    pattern = str(Path(*pattern_parts))
    try:
        return sorted(path.resolve() for path in root.glob(pattern) if path.exists())
    except (OSError, ValueError):
        return []


def _collect_paths(
    values: Sequence[str],
    cwd: Path,
    limit: int,
    seen: Optional[set] = None,
) -> List[Path]:
    if limit <= 0:
        return []
    targets: List[Path] = []
    known = seen if seen is not None else set()
    for value in values:
        if _skip_path(value):
            continue
        for path in _glob_paths(value, cwd):
            key = str(path)
            if key not in known:
                known.add(key)
                targets.append(path)
                if len(targets) >= limit:
                    return targets
    return targets


def extract_terminal_targets(
    command: str,
    working_dir: Path,
    *,
    workdir: Optional[str] = None,
    result_cwd: Optional[str] = None,
    max_targets: int = 8,
) -> List[Path]:
    """Extract existing read/search targets from a shell command.

    ``working_dir`` is the session terminal cwd. ``workdir`` is the tool call's
    optional cwd override. A result cwd is used only if no cd/pushd occurs.
    """
    base = _resolve(os.fspath(workdir), working_dir) if workdir else working_dir.resolve()
    try:
        tokens = _shell_tokens(_strip_heredocs(command))
    except (ValueError, OSError):
        return []
    segments = _segments(tokens)
    has_directory_change = any(
        _unwrap(_strip_redirections(segment)[0])[:1] in (["cd"], ["pushd"])
        for segment in segments
    )
    current = base
    if not has_directory_change and result_cwd:
        try:
            current = _resolve(result_cwd, base)
        except (OSError, RuntimeError):
            current = base

    targets: List[Path] = []
    seen = set()
    for segment in segments:
        clean, redirected_inputs = _strip_redirections(segment)
        args = _unwrap(clean)
        if not args:
            name = ""
            command_args: List[str] = []
        else:
            name = Path(args[0]).name
            command_args = args[1:]
        if name in {"cd", "pushd"}:
            operand = next((arg for arg in command_args if not _is_option(arg)), None)
            if operand:
                try:
                    current = _resolve(operand, current)
                except (OSError, RuntimeError):
                    pass
            continue
        # Input redirection is a read only for commands that read files.
        segment_values: List[str] = []
        if name in READ_COMMANDS:
            segment_values.extend(redirected_inputs)
            operands = _read_operands(name, command_args)
            if operands:
                segment_values.extend(operands)
        elif name in SEARCH_COMMANDS:
            segment_values.extend(redirected_inputs)
            segment_values.extend(_search_operands(name, command_args))
        targets.extend(_collect_paths(segment_values, current, max_targets - len(targets), seen))
        if len(targets) >= max_targets:
            return targets
    return targets
