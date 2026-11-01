"""Regression tests for the v1.2.1 acceptance-gate findings."""

import os
import threading
from pathlib import Path


def make_repo(root):
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
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
    import sys

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
