"""Integration tests for terminal and native search_files injection."""

import json


def make_repo(root, *, instructions=True):
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    if instructions:
        (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("app", encoding="utf-8")
    (repo / "lib").mkdir()
    (repo / "lib" / "lib.py").write_text("lib", encoding="utf-8")
    return repo


def hook(injector, name, args, result, session="s1"):
    return injector._on_transform_tool_result(
        tool_name=name, args=args, result=result, session_id=session
    )


def test_terminal_json_keeps_prefix_and_appends_context(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(session_dir))
    result = json.dumps({"stdout": "app", "cwd": str(repo)})

    output = hook(injector, "terminal", {"command": "head -n 3 src/app.py"}, result)

    assert output.startswith(result + "\n\n---\n\nInstructions from:")
    assert "repo rules" in output
    assert output[: len(result)] == result


def test_terminal_non_json_result_does_not_crash(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(session_dir))

    output = hook(
        injector, "terminal", {"command": f"cat {repo / 'src' / 'app.py'}"}, "raw output"
    )

    assert output is not None and output.startswith("raw output\n\n---\n\n")


def test_search_files_matches_paths_refine_directories(injector, tmp_path):
    repo = make_repo(tmp_path)
    result = json.dumps({"matches": [{"path": str(repo / "src" / "app.py")}]})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None and "repo rules" in output


def test_search_files_matches_text_refines_directories(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    result = json.dumps({"matches_text": f"{repo / 'src' / 'app.py'}:7:match\\n  ignored"})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None and "src rules" in output
    assert "Instructions from: " + str(repo / "src" / "AGENTS.md") in output


def test_search_result_matching_instruction_file_marks_it_seen(injector, tmp_path):
    repo = make_repo(tmp_path)
    result = json.dumps({"matches": [{"path": str(repo / "AGENTS.md")}]})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is None
    assert hook(injector, "read_file", {"path": str(repo / "src" / "app.py")}, "file") is None


def test_search_files_files_and_empty_result_fallback(injector, tmp_path):
    repo = make_repo(tmp_path)
    output = hook(
        injector,
        "search_files",
        {"path": str(repo)},
        json.dumps({"files": [str(repo / "lib" / "lib.py")]}),
    )
    assert output is not None and "repo rules" in output

    injector.reset_cache()
    output = hook(injector, "search_files", {"path": str(repo)}, json.dumps({}))
    assert output is not None and "repo rules" in output


def test_core_gate_skips_in_working_directory(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(repo))

    assert hook(injector, "terminal", {"command": "cat src/app.py"}, "output") is None


def test_core_gate_does_not_skip_when_working_dir_is_not_repo_root(
    injector, tmp_path, monkeypatch
):
    """A TERMINAL_CWD that is not the target's repo root must not suppress injection.

    In the TUI, TERMINAL_CWD can point at the Hermes home while core's session
    workspace is elsewhere (or home-like and therefore disabled). Gating on
    containment alone silently dropped instructions there.
    """
    make_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    output = hook(injector, "terminal", {"command": "cat repo/src/app.py"}, "output")

    assert output is not None and "repo rules" in output


def test_dedupe_is_shared_across_search_read_terminal(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(session_dir))
    result = json.dumps({"files": [str(repo / "src" / "app.py")]})

    assert hook(injector, "search_files", {"path": str(repo)}, result) is not None
    assert hook(injector, "read_file", {"path": str(repo / "src" / "app.py")}, "file") is None
    assert hook(injector, "terminal", {"command": f"cat {repo / 'lib' / 'lib.py'}"}, "raw") is None


def test_search_dir_env_limit(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (repo / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (repo / "lib" / "AGENTS.md").write_text("lib rules", encoding="utf-8")
    monkeypatch.setenv(injector.MAX_SEARCH_DIRS_ENV, "1")
    result = json.dumps(
        {"matches": [
            {"path": str(repo / "src" / "app.py")},
            {"path": str(repo / "lib" / "lib.py")},
        ]}
    )

    output = hook(injector, "search_files", {"path": str(repo.parent)}, result)

    assert output is not None and "src rules" in output and "lib rules" not in output


def test_total_cap_applies_across_multiple_terminal_targets(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "src" / "AGENTS.md").write_text("s" * 800, encoding="utf-8")
    (repo / "lib" / "AGENTS.md").write_text("l" * 800, encoding="utf-8")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", str(session_dir))
    monkeypatch.setenv(injector.MAX_TOTAL_ENV, "1500")
    monkeypatch.setenv(injector.MAX_FILE_ENV, "0")

    output = hook(
        injector,
        "terminal",
        {"command": f"cat {repo / 'src' / 'app.py'} {repo / 'lib' / 'lib.py'}"},
        "raw",
    )

    assert output is not None
    assert len(output) - len("raw") <= 1500
    assert output.count("Instructions from:") >= 2
    assert output.count("bytes truncated") >= 1


def test_search_matches_counts_keys(injector, tmp_path):
    repo = make_repo(tmp_path)
    result = json.dumps({"counts": {str(repo / "src" / "app.py"): 1}})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None and "repo rules" in output


def test_instruction_file_target_is_marked_seen(injector, tmp_path):
    repo = make_repo(tmp_path)
    instruction = repo / "AGENTS.md"
    output = hook(injector, "terminal", {"command": f"cat {instruction}"}, "rules")

    assert output is None
    assert hook(injector, "read_file", {"path": str(repo / "src" / "app.py")}, "file") is None


def test_empty_search_result_does_not_mark_seen(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("", encoding="utf-8")
    result = json.dumps({"matches": [{"path": str(repo / "AGENTS.md")}]})

    assert hook(injector, "search_files", {"path": str(repo)}, result) is None
    assert injector._injected == {}
    assert injector._last_seen == {}

    (repo / "AGENTS.md").write_text("later rules", encoding="utf-8")
    output = hook(
        injector, "read_file", {"path": str(repo / "src" / "app.py")}, "file"
    )
    assert output is not None and "later rules" in output


def test_sensitive_search_result_is_not_cached(injector, tmp_path):
    repo = make_repo(tmp_path)
    sensitive_dir = repo / ".ssh"
    sensitive_dir.mkdir()
    sensitive = sensitive_dir / "AGENTS.md"
    sensitive.write_text("SECRET KEY MATERIAL", encoding="utf-8")
    result = json.dumps({"matches": [{"path": str(sensitive)}]})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is None or "SECRET KEY MATERIAL" not in output
    key = str(sensitive.resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_out_of_tree_symlink_search_result_is_not_cached(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = outside_dir / "AGENTS.md"
    outside.write_text("outside rules", encoding="utf-8")
    (repo / "AGENTS.md").unlink()
    (repo / "AGENTS.md").symlink_to(outside)
    result = json.dumps({"matches": [{"path": str(repo / "AGENTS.md")}]})

    reads = []
    original = injector._read_text_limited

    def spy(path, limit):
        reads.append(str(path))
        return original(path, limit)

    monkeypatch.setattr(injector, "_read_text_limited", spy)
    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is None or "outside rules" not in output
    assert str(outside.resolve()) not in reads
    key = str(outside.resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_parent_symlink_search_result_is_not_read(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    (outside / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    result = json.dumps({"matches": [{"path": str(repo / "linked" / "AGENTS.md")}]})

    reads = []
    original = injector._read_text_limited

    def spy(path, limit):
        reads.append(str(path))
        return original(path, limit)

    monkeypatch.setattr(injector, "_read_text_limited", spy)
    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is None or "outside rules" not in output
    external = str((outside / "AGENTS.md").resolve())
    assert external not in reads
    assert all(external not in paths for paths in injector._injected.values())
    assert external not in injector._last_seen


def test_parent_symlink_external_repo_is_not_injected(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    (outside / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (outside / "app.py").write_text("app", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    for name, args in (
        ("read_file", {"path": str(repo / "linked" / "app.py")}),
        ("terminal", {"command": f"cat {repo / 'linked' / 'app.py'}"}),
    ):
        output = hook(injector, name, args, "raw")
        assert output is None or "outside rules" not in output
        key = str((outside / "AGENTS.md").resolve())
        assert all(key not in paths for paths in injector._injected.values())
        assert key not in injector._last_seen


def test_symlinked_working_dir_relative_targets_not_injected(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    (outside / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (outside / "subdir").mkdir()
    (outside / "subdir" / "app.py").write_text("app", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("TERMINAL_CWD", str(repo / "linked" / "subdir"))

    for name, args in (
        ("read_file", {"path": "app.py"}),
        ("terminal", {"command": "cat app.py"}),
    ):
        output = hook(injector, name, args, "raw")
        assert output is None or "outside rules" not in output
        key = str((outside / "AGENTS.md").resolve())
        assert all(key not in paths for paths in injector._injected.values())
        assert key not in injector._last_seen


def test_symlinked_search_anchor_is_not_injected(injector, tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    (outside / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)

    output = hook(
        injector,
        "search_files",
        {"path": str(repo / "linked")},
        json.dumps({"matches": []}),
    )

    assert output is None or "outside rules" not in output
    key = str((outside / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_nonempty_result_through_symlinked_anchor_is_not_injected(injector, tmp_path):
    repo = make_repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    (outside / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (outside / "app.py").write_text("app", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    result = json.dumps({"matches": [{"path": str(repo / "linked" / "app.py")}]})

    output = hook(
        injector,
        "search_files",
        {"path": str(repo / "linked")},
        result,
    )

    assert output is None or "outside rules" not in output
    key = str((outside / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_symlinked_search_anchor_without_git_is_not_injected(injector, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".git").mkdir()
    (outside / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (outside / "app.py").write_text("app", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    result = json.dumps({"matches": [{"path": str(repo / "linked" / "app.py")}]})

    output = hook(
        injector,
        "search_files",
        {"path": str(repo / "linked")},
        result,
    )

    assert output is None or "outside rules" not in output
    key = str((outside / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_search_result_in_other_repo_is_not_injected(injector, tmp_path):
    repo = make_repo(tmp_path)
    other = tmp_path / "other_repo"
    other.mkdir()
    (other / ".git").mkdir()
    (other / "AGENTS.md").write_text("other rules", encoding="utf-8")
    (other / "app.py").write_text("app", encoding="utf-8")
    result = json.dumps({"matches": [{"path": str(other / "app.py")}]})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is None or "other rules" not in output
    key = str((other / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_symlinked_external_result_into_anchor_is_not_injected(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "app.py").write_text("app", encoding="utf-8")
    other = tmp_path / "other_repo"
    other.mkdir()
    (other / ".git").mkdir()
    (other / "AGENTS.md").write_text("other rules", encoding="utf-8")
    (other / "linked").symlink_to(repo, target_is_directory=True)
    result = json.dumps({"matches": [{"path": str(other / "linked" / "app.py")}]})

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is None or "other rules" not in output
    key = str((other / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert key not in injector._last_seen


def test_external_results_do_not_consume_refinement_cap(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    external_one = tmp_path / "external_one"
    external_one.mkdir()
    (external_one / "app.py").write_text("one", encoding="utf-8")
    external_two = tmp_path / "external_two"
    external_two.mkdir()
    (external_two / "app.py").write_text("two", encoding="utf-8")
    result = json.dumps(
        {
            "matches": [
                {"path": str(external_one / "app.py")},
                {"path": str(external_two / "app.py")},
                {"path": str(repo / "src" / "app.py")},
            ]
        }
    )
    monkeypatch.setenv(injector.MAX_SEARCH_DIRS_ENV, "2")

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None and "repo rules" in output


def test_alias_search_dirs_do_not_consume_cap(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "src" / "AGENTS.md").write_text("src rules", encoding="utf-8")
    (repo / "other").mkdir()
    (repo / "other" / "AGENTS.md").write_text("other rules", encoding="utf-8")
    (repo / "other" / "app.py").write_text("other app", encoding="utf-8")
    result = json.dumps(
        {
            "matches": [
                {"path": str(repo / "src" / "app.py")},
                {"path": str(repo / "src" / ".." / "src" / "app.py")},
                {"path": str(repo / "other" / "app.py")},
            ]
        }
    )
    monkeypatch.setenv(injector.MAX_SEARCH_DIRS_ENV, "2")

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None
    assert "other rules" in output
