"""Regression tests for the v1.2.1 acceptance-gate findings."""

import os
import sys
import threading
import types
from pathlib import Path

import pytest


def make_repo(root, *, instructions=True):
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    if instructions:
        (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("app", encoding="utf-8")
    (repo / "README.md").write_text("readme", encoding="utf-8")
    (repo / "plugin.yaml").write_text("name: x", encoding="utf-8")
    return repo


def hook(injector, name, args, result, session="s1"):
    return injector._on_transform_tool_result(
        tool_name=name, args=args, result=result, session_id=session
    )


def test_symlinked_instruction_outside_repo_is_not_injected(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("outside rules", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    os.symlink(outside, repo / "AGENTS.md")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    output = hook(injector, "terminal", {"command": "cat repo/src/app.py"}, "raw")

    assert output is None


def test_symlinked_instruction_inside_repo_is_injected(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    shared = repo / "shared.md"
    shared.write_text("shared rules", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    os.symlink(shared, repo / "AGENTS.md")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    output = hook(injector, "terminal", {"command": "cat repo/src/app.py"}, "raw")

    assert output is not None and "shared rules" in output


def test_core_gate_treats_workspace_child_as_home_like(injector):
    assert injector.core_hints_active(Path("/workspace/project")) is False


def test_input_redirection_on_non_reader_is_ignored(injector, tmp_path):
    repo = make_repo(tmp_path)

    assert injector.extract_terminal_targets("git status < README.md", repo) == []


def test_heredoc_body_is_not_an_operand(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = "cat <<EOF\nREADME.md\nEOF"

    assert injector.extract_terminal_targets(command, repo) == []


def test_paste_delimiter_is_not_a_target(injector, tmp_path):
    repo = make_repo(tmp_path)

    targets = injector.extract_terminal_targets("paste -d README.md plugin.yaml", repo)

    assert [path.name for path in targets] == ["plugin.yaml"]


def test_find_without_root_anchors_at_cwd(injector, tmp_path):
    repo = make_repo(tmp_path)

    targets = injector.extract_terminal_targets("find -name '*.py'", repo)

    assert targets == [repo.resolve()]


def test_reserve_claims_a_key_once_under_concurrency(injector):
    cache = set()
    barrier = threading.Barrier(16)
    results = []

    def claim():
        barrier.wait()
        results.append(injector._reserve(cache, "k"))

    threads = [threading.Thread(target=claim) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count(True) == 1
    assert "k" in cache


def test_symlinked_sensitive_file_inside_repo_is_blocked_without_core_guard(
    injector, tmp_path, monkeypatch
):
    repo = make_repo(tmp_path)
    secret = repo / ".env"
    secret.write_text("API_KEY=super-secret", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    os.symlink(secret, repo / "AGENTS.md")
    # Simulate a host where the core read guard cannot be imported.
    monkeypatch.setitem(sys.modules, "agent.file_safety", None)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    output = hook(injector, "terminal", {"command": "cat repo/src/app.py"}, "raw")

    assert output is None


@pytest.mark.parametrize(
    "name",
    ["id_custom", "secrets.toml", ".envrc", "server.crt", "server.cer", "server.der"],
)
def test_sensitive_prefixes_blocked_without_core_guard(injector, tmp_path, monkeypatch, name):
    repo = make_repo(tmp_path)
    secret = repo / name
    secret.write_text("top secret", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    os.symlink(secret, repo / "AGENTS.md")
    monkeypatch.setitem(sys.modules, "agent.file_safety", None)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    output = hook(injector, "terminal", {"command": "cat repo/src/app.py"}, "raw")

    assert output is None


@pytest.mark.parametrize("name", [".envrc", "server.crt"])
def test_sensitive_prefix_blocked_when_core_guard_raises(
    injector, tmp_path, monkeypatch, name
):
    repo = make_repo(tmp_path)
    secret = repo / name
    secret.write_text("export TOKEN=top-secret", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    os.symlink(secret, repo / "AGENTS.md")

    agent = types.ModuleType("agent")
    agent.__path__ = []  # type: ignore[attr-defined]
    safety = types.ModuleType("agent.file_safety")

    def boom(_path):
        raise RuntimeError("guard down")

    safety.get_read_block_error = boom
    agent.file_safety = safety  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.file_safety", safety)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    output = hook(injector, "terminal", {"command": "cat repo/src/app.py"}, "raw")

    assert output is None


def test_quoted_heredoc_marker_does_not_swallow_later_commands(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = "grep '<<EOF' README.md\ncat plugin.yaml"

    targets = injector.extract_terminal_targets(command, repo)

    assert {path.name for path in targets} == {"README.md", "plugin.yaml"}


def test_multiple_heredocs_skip_every_body(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = "cat <<A <<B\nREADME.md\nA\nplugin.yaml\nB"

    assert injector.extract_terminal_targets(command, repo) == []


def test_active_session_cache_is_not_evicted(injector):
    injector.reset_cache()
    injector._activate("s1")
    try:
        first = injector._session_cache("s1")
        for index in range(injector.MAX_TRACKED_SESSIONS):
            injector._session_cache(f"other-{index}")
        assert injector._session_cache("s1") is first
        assert "s1" in injector._injected
    finally:
        injector._deactivate("s1")


def test_quoted_heredoc_filename_still_a_target(injector, tmp_path):
    repo = make_repo(tmp_path)
    odd = repo / "<<EOF"
    odd.write_text("literal", encoding="utf-8")

    targets = injector.extract_terminal_targets("cat '<<EOF'", repo)

    assert targets == [odd.resolve()]


def test_dash_heredoc_terminator_strips_tabs_only(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = "cat <<-EOF\n EOF\ncat plugin.yaml\n\tEOF"

    assert injector.extract_terminal_targets(command, repo) == []


def test_cache_returns_to_bound_after_deactivation(injector, monkeypatch):
    monkeypatch.setattr(injector, "MAX_TRACKED_SESSIONS", 2)
    monkeypatch.setattr(injector, "SESSION_IDLE_TTL", 0)
    injector.reset_cache()
    for key in ("s1", "s2", "s3"):
        injector._activate(key)
        injector._session_cache(key)
    assert len(injector._injected) == 3

    for key in ("s1", "s2", "s3"):
        injector._deactivate(key)

    assert len(injector._injected) <= 2


def test_recent_session_keeps_dedupe_under_pressure(injector, monkeypatch):
    monkeypatch.setattr(injector, "MAX_TRACKED_SESSIONS", 2)
    injector.reset_cache()
    cache = injector._session_cache("s1")
    assert injector._reserve(cache, "k")

    injector._activate("s1")
    injector._deactivate("s1")
    injector._activate("s2")
    injector._session_cache("s2")
    injector._activate("s3")
    injector._session_cache("s3")

    assert injector._session_cache("s1") is cache
    assert injector._reserve(cache, "k") is False

    injector._deactivate("s2")
    injector._deactivate("s3")


def test_mixed_quoted_heredoc_delimiter(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = 'cat <<E"OF"\nEOF\ncat plugin.yaml'

    targets = injector.extract_terminal_targets(command, repo)

    assert [path.name for path in targets] == ["plugin.yaml"]


def test_spaced_dash_heredoc_does_not_target_the_delimiter(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "EOF").write_text("delimiter", encoding="utf-8")
    command = "cat <<- EOF\n\tEOF\ncat plugin.yaml"

    targets = injector.extract_terminal_targets(command, repo)

    assert [path.name for path in targets] == ["plugin.yaml"]


def test_escaped_quote_in_heredoc_delimiter(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = 'cat <<E"F\\"O"\nEF"O\ncat plugin.yaml'

    targets = injector.extract_terminal_targets(command, repo)

    assert [path.name for path in targets] == ["plugin.yaml"]


def test_spaced_heredoc_dash_keeps_file_operand(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "EOF").write_text("data", encoding="utf-8")
    command = "cat << - EOF\n-\ncat plugin.yaml"

    targets = injector.extract_terminal_targets(command, repo)

    assert {path.name for path in targets} == {"EOF", "plugin.yaml"}


def test_no_target_calls_do_not_leak_session_metadata(injector):
    injector.reset_cache()
    for index in range(100):
        hook(injector, "read_file", {"path": ""}, "content", session=f"s{index}")

    assert injector._injected == {}
    assert injector._last_seen == {}


def test_valid_read_without_instructions_leaves_no_metadata(injector, tmp_path):
    repo = make_repo(tmp_path, instructions=False)

    output = hook(
        injector, "read_file", {"path": str(repo / "src" / "app.py")}, "content"
    )

    assert output is None
    assert injector._injected == {}
    assert injector._last_seen == {}


def test_backslash_before_ordinary_char_kept_in_delimiter(injector, tmp_path):
    repo = make_repo(tmp_path)
    command = 'cat <<E"F\\qO"\nEF\\qO\ncat plugin.yaml'

    targets = injector.extract_terminal_targets(command, repo)

    assert [path.name for path in targets] == ["plugin.yaml"]


def test_heredoc_sentinel_filename_is_still_a_target(injector, tmp_path):
    repo = make_repo(tmp_path)
    odd = repo / "__HEREDOC_DASH__"
    odd.write_text("literal", encoding="utf-8")

    targets = injector.extract_terminal_targets("cat __HEREDOC_DASH__", repo)

    assert targets == [odd.resolve()]


def test_empty_instruction_file_is_released(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("", encoding="utf-8")

    first = hook(injector, "read_file", {"path": str(repo / "src" / "app.py")}, "content")
    assert first is None
    assert injector._injected == {}
    assert injector._last_seen == {}

    (repo / "AGENTS.md").write_text("late rules", encoding="utf-8")
    second = hook(injector, "read_file", {"path": str(repo / "src" / "app.py")}, "content")
    assert second is not None and "late rules" in second


def test_total_budget_includes_framing(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("x" * 500, encoding="utf-8")
    monkeypatch.setenv(injector.MAX_TOTAL_ENV, "300")
    result = "raw"

    output = hook(
        injector, "terminal", {"command": f"cat {repo / 'src' / 'app.py'}"}, result
    )

    assert output is not None
    assert len(output) - len(result) <= 300
