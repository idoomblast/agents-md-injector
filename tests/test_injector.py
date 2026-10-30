"""Behavioral tests for the AGENTS.md injector plugin."""

import os

import pytest

HEADER = "Instructions from:"


def make_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read(injector, path, *, result="FILE CONTENT", session_id="test-session", **kwargs):
    return injector._on_transform_tool_result(
        tool_name="read_file",
        args={"path": str(path)},
        result=result,
        session_id=session_id,
        **kwargs,
    )


# ── Injection ─────────────────────────────────────────────────────────────────


def test_injects_root_agents_md(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "Root rules")
    target = write(repo / "src" / "app.py", "print('hi')")

    out = read(injector, target)

    assert out.startswith("FILE CONTENT")
    assert f"{HEADER} {repo / 'AGENTS.md'}" in out
    assert "Root rules" in out


def test_nested_files_injected_nearest_first(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "ROOT_MARKER")
    write(repo / "src" / "AGENTS.md", "SRC_MARKER")
    target = write(repo / "src" / "pkg" / "app.py", "code")

    out = read(injector, target)

    assert out.index("SRC_MARKER") < out.index("ROOT_MARKER")
    assert out.count(HEADER) == 2


def test_lowercase_agents_md_is_picked_up(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "agents.md", "lowercase rules")
    target = write(repo / "app.py", "code")

    out = read(injector, target)

    assert "lowercase rules" in out


def test_no_agents_md_returns_none(injector, tmp_path):
    repo = make_repo(tmp_path)
    target = write(repo / "app.py", "code")

    assert read(injector, target) is None


def test_injection_skips_the_file_being_read(injector, tmp_path):
    repo = make_repo(tmp_path)
    instructions = write(repo / "AGENTS.md", "rules")

    assert read(injector, instructions) is None
    # Already in context from the read itself — a sibling read must not repeat it.
    assert read(injector, write(repo / "app.py", "code")) is None


# ── Session / dedupe ──────────────────────────────────────────────────────────


def test_same_session_gets_one_injection(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")

    assert read(injector, target) is not None
    assert read(injector, target) is None


def test_each_session_gets_its_own_injection(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")

    assert read(injector, target, session_id="s1") is not None
    assert read(injector, target, session_id="s1") is None
    assert read(injector, target, session_id="s2") is not None


def test_cache_falls_back_to_task_id(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")

    assert read(injector, target, session_id="", task_id="sub-1") is not None
    assert read(injector, target, session_id="", task_id="sub-1") is None


def test_reset_cache_reenables_injection(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")

    assert read(injector, target) is not None
    injector.reset_cache()
    assert read(injector, target) is not None


def test_session_cache_is_bounded(injector, monkeypatch):
    monkeypatch.setattr(injector, "MAX_TRACKED_SESSIONS", 2)
    for session in ("a", "b", "c"):
        injector._session_cache(session)

    assert len(injector._injected) == 2
    assert "a" not in injector._injected


# ── Path resolution / boundaries ──────────────────────────────────────────────


def test_relative_paths_resolve_against_terminal_cwd(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    write(repo / "src" / "app.py", "code")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TERMINAL_CWD", str(repo))

    out = injector._on_transform_tool_result(
        tool_name="read_file",
        args={"path": "src/app.py"},
        result="FILE CONTENT",
        session_id="s1",
    )

    assert out is not None
    assert "rules" in out


def test_walk_up_stops_at_git_root(injector, tmp_path):
    write(tmp_path / "AGENTS.md", "ABOVE_GIT_ROOT")
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "INSIDE_REPO")
    target = write(repo / "app.py", "code")

    out = read(injector, target)

    assert "INSIDE_REPO" in out
    assert "ABOVE_GIT_ROOT" not in out


def test_walk_up_without_git_root_reaches_parent_dirs(injector, tmp_path):
    write(tmp_path / "AGENTS.md", "OUTSIDE_REPO")
    target = write(tmp_path / "standalone" / "pkg" / "app.py", "code")

    out = read(injector, target)

    assert out is not None
    assert "OUTSIDE_REPO" in out


def test_path_object_argument(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")

    out = injector._on_transform_tool_result(
        tool_name="read_file",
        args={"path": target},
        result="FILE CONTENT",
        session_id="s1",
    )

    assert out is not None and "rules" in out


def test_missing_parent_directory_returns_none(injector, tmp_path):
    out = injector._on_transform_tool_result(
        tool_name="read_file",
        args={"path": str(tmp_path / "nope" / "deep" / "app.py")},
        result="FILE CONTENT",
        session_id="s1",
    )

    assert out is None


# ── Caps and configuration ────────────────────────────────────────────────────


def test_file_cap_truncates_with_marker(injector, tmp_path, monkeypatch):
    monkeypatch.setenv(injector.MAX_FILE_ENV, "1000")
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "x" * 5000)
    target = write(repo / "app.py", "code")

    out = read(injector, target)

    assert "bytes truncated" in out
    assert len(out) < 1400


def test_total_cap_limits_multiple_files(injector, tmp_path, monkeypatch):
    monkeypatch.setenv(injector.MAX_TOTAL_ENV, "1000")
    monkeypatch.setenv(injector.MAX_FILE_ENV, "0")  # unlimited per file
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "r" * 800)
    write(repo / "src" / "AGENTS.md", "s" * 800)
    target = write(repo / "src" / "app.py", "code")

    out = read(injector, target)

    assert out.count(HEADER) == 2
    assert "bytes truncated" in out


def test_invalid_cap_falls_back_to_default(injector, tmp_path, monkeypatch):
    monkeypatch.setenv(injector.MAX_FILE_ENV, "not-a-number")
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")

    assert "rules" in read(injector, target)


def test_custom_filenames(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    write(repo / "CLAUDE.md", "claude rules")
    target = write(repo / "app.py", "code")

    assert read(injector, target, session_id="default") is None

    monkeypatch.setenv(injector.FILENAMES_ENV, "CLAUDE.md, AGENTS.md")
    out = read(injector, target, session_id="custom")
    assert out is not None and "claude rules" in out


# ── Robustness ────────────────────────────────────────────────────────────────


def test_disable_flag(injector, tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    target = write(repo / "app.py", "code")
    monkeypatch.setenv(injector.DISABLE_ENV, "1")

    assert read(injector, target) is None


def test_other_tools_are_ignored(injector, tmp_path):
    repo = make_repo(tmp_path)
    write(repo / "AGENTS.md", "rules")
    write(repo / "app.py", "code")

    out = injector._on_transform_tool_result(
        tool_name="terminal",
        args={"command": "ls"},
        result="output",
        session_id="s1",
    )

    assert out is None


def test_non_dict_args_are_ignored(injector):
    assert injector._on_transform_tool_result(tool_name="read_file", args=None, result="x") is None


def test_blank_path_is_ignored(injector):
    out = injector._on_transform_tool_result(
        tool_name="read_file", args={"path": "   "}, result="x", session_id="s1"
    )
    assert out is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_unreadable_agents_md_is_skipped(injector, tmp_path):
    repo = make_repo(tmp_path)
    instructions = write(repo / "AGENTS.md", "secret")
    target = write(repo / "app.py", "code")
    instructions.chmod(0)
    try:
        assert read(injector, target) is None
    finally:
        instructions.chmod(0o644)


def test_register_declares_transform_hook(injector):
    registered = []

    class Ctx:
        def register_hook(self, name, handler):
            registered.append((name, handler))

    injector.register(Ctx())

    assert registered == [("transform_tool_result", injector._on_transform_tool_result)]


def test_manifest_declares_registered_hooks():
    yaml = pytest.importorskip("yaml")
    with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugin.yaml")) as fh:
        manifest = yaml.safe_load(fh)

    assert manifest["name"] == "agents-md-injector"
    assert manifest["provides_hooks"] == ["transform_tool_result"]
