"""Integration tests for terminal and native search_files injection."""

import json
import threading

import pytest


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
    assert "s1" not in injector._last_seen


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
    assert "s1" not in injector._last_seen


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
    assert "s1" not in injector._last_seen


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
    assert "s1" not in injector._last_seen


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


def test_external_results_do_not_consume_refinement_cap(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "lib" / "AGENTS.md").write_text("lib rules", encoding="utf-8")
    (repo / "lib" / "app.py").write_text("app", encoding="utf-8")
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
                {"path": str(repo / "lib" / "app.py")},
            ]
        }
    )
    monkeypatch.setenv(injector.MAX_SEARCH_DIRS_ENV, "2")

    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None and "lib rules" in output


def test_dotdot_anchor_keeps_in_repo_results(injector, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "lib" / "AGENTS.md").write_text("lib rules", encoding="utf-8")
    (repo / "lib" / "app.py").write_text("app", encoding="utf-8")
    result = json.dumps({"matches": [{"path": str(repo / "lib" / "app.py")}]})

    output = hook(
        injector,
        "search_files",
        {"path": str((repo / "src") / "..")},
        result,
    )

    assert output is not None and "lib rules" in output


def test_symlink_dotdot_result_cannot_escape_search_anchor(injector, tmp_path, monkeypatch):
    work = tmp_path / "work"
    repo = work / "repo"
    repo.mkdir(parents=True)
    (repo / "sub").mkdir()
    (repo / "lib").mkdir()
    (repo / "lib" / "app.py").write_text("app", encoding="utf-8")
    (repo / "lib" / "AGENTS.md").write_text("lib rules", encoding="utf-8")
    (repo / "repo" / "lib").mkdir(parents=True)
    (repo / "repo" / "lib" / "app.py").write_text("app", encoding="utf-8")
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (work / "AGENTS.md").write_text("outside rules", encoding="utf-8")
    (work / "link").symlink_to(repo / "sub", target_is_directory=True)
    result_path = work / "link" / ".." / "repo" / "lib" / "app.py"
    assert result_path.resolve() == (repo / "repo" / "lib" / "app.py").resolve()
    result = json.dumps({"matches": [{"path": str(result_path)}]})

    reads = []
    original = injector._read_text_limited

    def spy(path, limit):
        reads.append(str(path))
        return original(path, limit)

    monkeypatch.setattr(injector, "_read_text_limited", spy)
    output = hook(injector, "search_files", {"path": str(repo)}, result)

    assert output is not None and "lib rules" in output
    assert "outside rules" not in output
    assert str((work / "AGENTS.md").resolve()) not in reads
    key = str((work / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())


def test_cancelled_directory_git_root_does_not_replace_origin(injector, tmp_path):
    repo = tmp_path / "repo"
    (repo / "area" / "nested" / ".git").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "area" / "nested" / "AGENTS.md").write_text(
        "nested rules", encoding="utf-8"
    )
    (repo / "area" / "lib").mkdir(parents=True)
    (repo / "area" / "lib" / "AGENTS.md").write_text("lib rules", encoding="utf-8")
    (repo / "area" / "lib" / "app.py").write_text("app", encoding="utf-8")
    result = json.dumps(
        {"matches": [{"path": str(repo / "area" / "lib" / "app.py")}]}
    )

    output = hook(
        injector,
        "search_files",
        {"path": str(repo / "area" / "nested" / "..")},
        result,
    )

    assert output is not None and "lib rules" in output
    assert "nested rules" not in output


def test_transform_failure_rolls_back_claims(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.py").write_text("x", encoding="utf-8")
    (bad / "AGENTS.md").write_text("bad rules", encoding="utf-8")
    original = injector._read_text_limited

    def flaky(path, limit):
        if "bad" in str(path):
            raise UnicodeEncodeError("utf-8", str(path), 0, 1, "bad path")
        return original(path, limit)

    monkeypatch.setattr(injector, "_read_text_limited", flaky)
    output = hook(
        injector,
        "terminal",
        {"command": f"cat {repo / 'src' / 'app.py'} {bad / 'x.py'}"},
        "raw",
    )
    assert output is None

    monkeypatch.setattr(injector, "_read_text_limited", original)
    retry = hook(
        injector,
        "read_file",
        {"path": str(repo / "src" / "app.py")},
        "raw",
    )
    assert retry is not None and "repo rules" in retry


def test_here_string_does_not_swallow_following_reader(injector, tmp_path):
    repo = make_repo(tmp_path)

    for index, command in enumerate(
        (
            f"cat <<< data\ncat {repo / 'src' / 'app.py'}",
            f"head <<< data\nhead {repo / 'src' / 'app.py'}",
        )
    ):
        output = hook(injector, "terminal", {"command": command}, "raw", session=f"h{index}")
        assert output is not None and "repo rules" in output


def test_newline_separates_commands(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))

    positive = hook(
        injector,
        "terminal",
        {"command": f"printf data\ncat {repo / 'src' / 'app.py'}"},
        "raw",
        session="n-positive",
    )
    assert positive is not None and "repo rules" in positive

    negative = hook(
        injector,
        "terminal",
        {"command": f"cat <<< data\nprintf {repo / 'src' / 'app.py'}"},
        "raw",
        session="n-negative",
    )
    assert negative is None or "repo rules" not in negative

    changed_cwd = hook(
        injector,
        "terminal",
        {"command": "cd repo\ncat src/app.py"},
        "raw",
        session="n-cwd",
    )
    assert changed_cwd is not None and "repo rules" in changed_cwd


def test_here_string_escaped_quote_does_not_hide_heredoc(injector, tmp_path):
    repo = make_repo(tmp_path)

    for index, escaped in enumerate(("\\'", '\\"')):
        command = (
            f"cat <<< x{escaped} <<EOF\n"
            f"cat {repo / 'src' / 'app.py'}\n"
            "EOF"
        )
        output = hook(
            injector,
            "terminal",
            {"command": command},
            "raw",
            session=f"escape-{index}",
        )
        assert output is None or "repo rules" not in output


@pytest.mark.parametrize("session", ["s1", ""])
def test_committed_delivery_survives_owner_rollback(
    injector, tmp_path, monkeypatch, session
):
    repo = make_repo(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.py").write_text("x", encoding="utf-8")
    (bad / "AGENTS.md").write_text("bad rules", encoding="utf-8")
    original = injector._read_text_limited
    paused = threading.Event()
    release = threading.Event()

    def gated(path, limit):
        if "bad" in str(path):
            raise UnicodeEncodeError("utf-8", str(path), 0, 1, "bad path")
        if str(path).endswith("repo/AGENTS.md") and not paused.is_set():
            paused.set()
            assert release.wait(5)
        return original(path, limit)

    monkeypatch.setattr(injector, "_read_text_limited", gated)
    result: dict[str, object] = {}

    def owner():
        result["value"] = hook(
            injector,
            "terminal",
            {"command": f"cat {repo / 'src' / 'app.py'} {bad / 'x.py'}"},
            "raw",
            session=session,
        )

    worker = threading.Thread(target=owner)
    worker.start()
    assert paused.wait(5)

    consumer = hook(
        injector,
        "search_files",
        {"path": str(repo)},
        json.dumps({"matches": [{"path": str(repo / "AGENTS.md")}]}),
        session=session,
    )
    assert consumer is None or "repo rules" not in consumer
    release.set()
    worker.join(5)
    assert result.get("value") is None

    monkeypatch.setattr(injector, "_read_text_limited", original)
    retry = hook(
        injector,
        "read_file",
        {"path": str(repo / "src" / "app.py")},
        "raw",
        session=session,
    )
    assert retry is None or "repo rules" not in retry


def test_core_coordination_uses_normalized_ancestry(injector, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "area" / "nested" / ".git").mkdir(parents=True)
    (repo / "area" / "nested" / "AGENTS.md").write_text(
        "nested rules", encoding="utf-8"
    )
    (repo / "area" / "app.py").write_text("app", encoding="utf-8")
    monkeypatch.setenv("TERMINAL_CWD", str(repo))

    output = hook(
        injector,
        "read_file",
        {"path": str(repo / "area" / "nested" / ".." / "app.py")},
        "raw",
    )

    assert output is None or "repo rules" not in output


def test_pending_empty_self_read_is_not_committed(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    (repo / "AGENTS.md").write_text("", encoding="utf-8")
    original = injector._read_text_limited
    paused = threading.Event()
    release = threading.Event()

    def gated(path, limit):
        if str(path).endswith("repo/AGENTS.md") and not paused.is_set():
            paused.set()
            assert release.wait(5)
        return original(path, limit)

    monkeypatch.setattr(injector, "_read_text_limited", gated)
    result: dict[str, object] = {}

    def owner():
        result["value"] = hook(
            injector,
            "read_file",
            {"path": str(repo / "src" / "app.py")},
            "raw",
            session="empty",
        )

    worker = threading.Thread(target=owner)
    worker.start()
    assert paused.wait(5)
    assert (
        hook(
            injector,
            "read_file",
            {"path": str(repo / "AGENTS.md")},
            "raw",
            session="empty",
        )
        is None
    )
    release.set()
    worker.join(5)
    assert result.get("value") is None

    (repo / "AGENTS.md").write_text("later rules", encoding="utf-8")
    retry = hook(
        injector,
        "read_file",
        {"path": str(repo / "src" / "app.py")},
        "raw",
        session="empty",
    )
    assert retry is not None and "later rules" in retry


def test_pending_sensitive_self_read_is_not_committed(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    sensitive = repo / ".ssh"
    sensitive.mkdir()
    (sensitive / "x.py").write_text("x", encoding="utf-8")
    (sensitive / "AGENTS.md").write_text("secret", encoding="utf-8")
    original_blocked = injector._read_blocked
    paused = threading.Event()
    release = threading.Event()

    def gated_blocked(path):
        if str(path).endswith(".ssh/AGENTS.md") and not paused.is_set():
            paused.set()
            assert release.wait(5)
        return original_blocked(path)

    monkeypatch.setattr(injector, "_read_blocked", gated_blocked)
    result: dict[str, object] = {}

    def owner():
        result["value"] = hook(
            injector,
            "read_file",
            {"path": str(sensitive / "x.py")},
            "raw",
            session="sensitive",
        )

    worker = threading.Thread(target=owner)
    worker.start()
    assert paused.wait(5)
    output = hook(
        injector,
        "read_file",
        {"path": str(sensitive / "AGENTS.md")},
        "raw",
        session="sensitive",
    )
    assert output is None or "secret" not in output
    release.set()
    worker.join(5)
    assert result.get("value") is None
    key = str((sensitive / "AGENTS.md").resolve())
    assert all(key not in paths for paths in injector._injected.values())
    assert all(key not in paths for paths in injector._committed.values())


def test_late_commit_restores_key_after_owner_rollback(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.py").write_text("x", encoding="utf-8")
    (bad / "AGENTS.md").write_text("bad rules", encoding="utf-8")
    original_read = injector._read_text_limited
    original_commit = injector._commit
    read_paused = threading.Event()
    read_release = threading.Event()
    commit_paused = threading.Event()
    commit_release = threading.Event()

    def gated_read(path, limit):
        if "bad" in str(path):
            raise UnicodeEncodeError("utf-8", str(path), 0, 1, "bad path")
        if str(path).endswith("repo/AGENTS.md") and not read_paused.is_set():
            read_paused.set()
            assert read_release.wait(5)
        return original_read(path, limit)

    def gated_commit(*args):
        commit_paused.set()
        assert commit_release.wait(5)
        return original_commit(*args)

    monkeypatch.setattr(injector, "_read_text_limited", gated_read)
    monkeypatch.setattr(injector, "_commit", gated_commit)
    owner_result: dict[str, object] = {}
    consumer_result: dict[str, object] = {}

    def owner():
        owner_result["value"] = hook(
            injector,
            "terminal",
            {"command": f"cat {repo / 'src' / 'app.py'} {bad / 'x.py'}"},
            "raw",
            session="late",
        )

    def consumer():
        consumer_result["value"] = hook(
            injector,
            "read_file",
            {"path": str(repo / "AGENTS.md")},
            "raw",
            session="late",
        )

    owner_thread = threading.Thread(target=owner)
    owner_thread.start()
    assert read_paused.wait(5)
    consumer_thread = threading.Thread(target=consumer)
    consumer_thread.start()
    assert commit_paused.wait(5)

    read_release.set()
    owner_thread.join(5)
    assert owner_result.get("value") is None
    commit_release.set()
    consumer_thread.join(5)

    retry = hook(
        injector,
        "read_file",
        {"path": str(repo / "src" / "app.py")},
        "raw",
        session="late",
    )
    assert retry is None or "repo rules" not in retry


def test_backslash_newline_continuations(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)

    for index, command in enumerate(
        (
            f"cat <<\\\nEOF\ncat {repo / 'src' / 'app.py'}\nEOF",
            f"cat <<EO\\\nF\ncat {repo / 'src' / 'app.py'}\nEOF",
        )
    ):
        output = hook(
            injector,
            "terminal",
            {"command": command},
            "raw",
            session=f"continued-heredoc-{index}",
        )
        assert output is None or "repo rules" not in output

    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    quoted = hook(
        injector,
        "terminal",
        {"command": f'cat "{repo / "src" / "ap"}\\\n{"p.py"}"'},
        "raw",
        session="continued-quoted",
    )
    assert quoted is not None and "repo rules" in quoted

    following = hook(
        injector,
        "terminal",
        {"command": f"cat <<EOF\nbody\nEOF\ncat {repo / 'src' / 'app.py'}"},
        "raw",
        session="continued-following",
    )
    assert following is not None and "repo rules" in following


def test_unquoted_heredoc_continuations_follow_bash(injector, tmp_path):
    repo = make_repo(tmp_path)
    target = repo / "src" / "app.py"

    for index, operator in enumerate(("<<EOF", "<< EOF", "<<-EOF")):
        false_positive = (
            f"cat {operator}\nbody\\\nEOF\ncat {target}\nEOF"
        )
        output = hook(
            injector,
            "terminal",
            {"command": false_positive},
            "raw",
            session=f"unquoted-heredoc-fp-{index}",
        )
        assert output is None or "repo rules" not in output

        false_negative = f"cat {operator}\nEO\\\nF\ncat {target}"
        output = hook(
            injector,
            "terminal",
            {"command": false_negative},
            "raw",
            session=f"unquoted-heredoc-fn-{index}",
        )
        assert output is not None and "repo rules" in output

    quoted_positive = f"cat <<'EOF'\nbody\\\nEOF\ncat {target}\nEOF"
    output = hook(
        injector,
        "terminal",
        {"command": quoted_positive},
        "raw",
        session="quoted-heredoc-control-positive",
    )
    assert output is not None and "repo rules" in output

    quoted_negative = f"cat <<'EOF'\nEO\\\nF\ncat {target}"
    output = hook(
        injector,
        "terminal",
        {"command": quoted_negative},
        "raw",
        session="quoted-heredoc-control-negative",
    )
    assert output is None or "repo rules" not in output


def test_quoted_newlines_preserve_quote_state_across_lines(injector, tmp_path):
    repo = make_repo(tmp_path)
    single_name = "a\nb\\\nc"
    single = repo / "src" / single_name
    single.write_text("single", encoding="utf-8")
    output = hook(
        injector,
        "terminal",
        {"command": f"cat '{repo / 'src' / 'a'}\nb\\\nc'"},
        "raw",
        session="quoted-newline-single",
    )
    assert output is not None and "repo rules" in output

    double_name = "a\nb'c"
    double = repo / "src" / double_name
    double.write_text("double", encoding="utf-8")
    output = hook(
        injector,
        "terminal",
        {"command": f'cat "{repo / "src" / "a"}\nb\'\\\nc"'},
        "raw",
        session="quoted-newline-double",
    )
    assert output is not None and "repo rules" in output


def test_core_coordination_accepts_symlinked_working_directory(
    injector, tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("repo rules", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("app", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    monkeypatch.setenv("TERMINAL_CWD", str(alias))

    output = hook(
        injector,
        "read_file",
        {"path": "src/app.py"},
        "raw",
    )

    assert output is None or "repo rules" not in output


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
