"""Tests for static shell read/search target classification."""

from pathlib import Path

import pytest


@pytest.fixture()
def classifier(injector):
    return injector.extract_terminal_targets


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "src").mkdir()
    (root / "lib").mkdir()
    (root / "services").mkdir()
    (root / "src" / "app.py").write_text("code", encoding="utf-8")
    (root / "file.py").write_text("code", encoding="utf-8")
    (root / "in.txt").write_text("input", encoding="utf-8")
    (root / "services" / "A.py").write_text("code", encoding="utf-8")
    (root / "lib" / "a.py").write_text("a", encoding="utf-8")
    (root / "lib" / "b.py").write_text("b", encoding="utf-8")
    return root


def paths(command, repo, classifier, **kwargs):
    return classifier(command, repo, **kwargs)


def relative(targets, root):
    return [path.relative_to(root) for path in targets]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("head -n 40 src/app.py", ["src/app.py"]),
        ("sed -n '1,130p' src/app.py", ["src/app.py"]),
        ("sed -i 's/a/b/' src/app.py", []),
        ("grep -rn 'foo' src/", ["src"]),
        ("grep -n 'foo' file.py | head -20", ["file.py"]),
        ("awk '{print $1}' file.py", ["file.py"]),
        ("jq '.x' file.py", ["file.py"]),
        ("cat > out <<EOF", []),
        ("cat < in.txt", ["in.txt"]),
        ("tail -f file.py", []),
        ("find . -name '*.py'", ["."]),
        ("fd pattern src", ["src"]),
        ("sudo -u www cat /repo/x.py", []),
        ("cd /repo && head services/A.py", []),
        ("cat lib/*.py", ["lib/a.py", "lib/b.py"]),
        ("ssh host 'cat /x'", []),
        ("wc -l", []),
        ("ls -la", []),
        ("git status", []),
    ],
)
def test_classifier_cases(command, expected, repo, classifier):
    actual = paths(command, repo, classifier)
    assert [str(path).replace(str(repo), "/repo") for path in actual] == [
        f"/repo/{path}" if path != "." else "/repo" for path in expected
    ]


def test_cd_operands_resolve_at_their_segment(repo, classifier):
    assert relative(paths("cd src && cat app.py", repo, classifier), repo) == [Path("src/app.py")]


def test_wrappers_and_options(repo, classifier):
    assert relative(
        paths("env FOO=bar nice -n 5 cat src/app.py", repo, classifier), repo
    ) == [Path("src/app.py")]


def test_input_redirection_is_a_target_and_output_is_not(repo, classifier):
    assert relative(paths("cat out > result.txt < in.txt", repo, classifier), repo) == [
        Path("in.txt")
    ]


def test_target_limit_and_deduplication(repo, classifier):
    targets = paths("cat src/app.py file.py src/app.py", repo, classifier, max_targets=2)
    assert relative(targets, repo) == [Path("src/app.py"), Path("file.py")]


def test_result_cwd_used_without_cd(repo, tmp_path, classifier):
    other = tmp_path / "other"
    other.mkdir()
    (other / "file.py").write_text("code", encoding="utf-8")
    assert classifier("cat file.py", repo, result_cwd=str(other)) == [
        (other / "file.py").resolve()
    ]


def test_workdir_is_resolved_from_session_cwd(repo, classifier):
    assert classifier("cat app.py", repo, workdir="src") == [(repo / "src" / "app.py").resolve()]


def test_combined_pattern_and_option_arguments(repo, classifier):
    assert relative(paths("grep -efoo -A3 file.py", repo, classifier), repo) == [Path("file.py")]
    assert relative(paths("rg -g*.py -C2 pattern src", repo, classifier), repo) == [Path("src")]
    assert relative(paths("head -n40 src/app.py", repo, classifier), repo) == [Path("src/app.py")]
