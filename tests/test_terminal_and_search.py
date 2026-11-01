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
    monkeypatch.setenv(injector.MAX_TOTAL_ENV, "1000")
    monkeypatch.setenv(injector.MAX_FILE_ENV, "0")

    output = hook(
        injector,
        "terminal",
        {"command": f"cat {repo / 'src' / 'app.py'} {repo / 'lib' / 'lib.py'}"},
        "raw",
    )

    assert output is not None
    assert output.count("Instructions from:") == 3
    assert output.count("bytes truncated") == 1


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
