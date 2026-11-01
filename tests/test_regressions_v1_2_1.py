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
