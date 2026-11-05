"""Bundled skills must be registered through ctx, not just left in the tree."""

from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]


class _FakeContext:
    def __init__(self):
        self.hooks = {}
        self.skills = []

    def register_hook(self, name, handler):
        self.hooks[name] = handler

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, Path(path), description, frontmatter))


def _frontmatter_name(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    head, sep, _ = text.partition("\n---\n")
    assert text.startswith("---\n") and sep, f"missing frontmatter: {skill_md}"
    for line in head.splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no name key in frontmatter: {skill_md}")


def test_register_bundles_init_deep_skill(injector):
    ctx = _FakeContext()
    injector.register(ctx)

    assert set(ctx.hooks) == {"transform_tool_result"}
    assert [name for name, *_ in ctx.skills] == ["init-deep"]

    name, path, *_ = ctx.skills[0]
    assert path == PLUGIN_DIR / "skills" / name / "SKILL.md"
    assert path.is_file()
    assert _frontmatter_name(path) == name


def test_register_skips_dirs_without_skill_md(injector, tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    (skills_dir / "not-a-skill").mkdir(parents=True)
    (skills_dir / "init-deep").mkdir()
    (skills_dir / "init-deep" / "SKILL.md").write_text(
        "---\nname: init-deep\ndescription: test\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(injector, "__file__", str(tmp_path / "__init__.py"))

    ctx = _FakeContext()
    injector.register(ctx)

    assert [name for name, *_ in ctx.skills] == ["init-deep"]
