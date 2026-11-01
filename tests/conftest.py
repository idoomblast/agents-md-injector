"""Load the plugin the same way the Hermes loader does.

The plugin directory is hyphenated, so it cannot be imported by name directly.
Hermes imports it as the package ``hermes_plugins.<slug>`` with
``submodule_search_locations`` set, which is mirrored here so the tests exercise
the real import shape (see hermes-plugin-development pitfall #9).
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "hermes_plugins.agents_md_injector"


def _install_namespace_package() -> None:
    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []  # type: ignore[attr-defined]
        sys.modules["hermes_plugins"] = namespace


@pytest.fixture()
def injector(monkeypatch):
    """Fresh plugin module with all injector env vars cleared."""
    _install_namespace_package()
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)

    for name in (
        module.DISABLE_ENV,
        module.FILENAMES_ENV,
        module.MAX_FILE_ENV,
        module.MAX_TOTAL_ENV,
        module.MAX_TERMINAL_TARGETS_ENV,
        module.MAX_SEARCH_DIRS_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    module.reset_cache()

    try:
        yield module
    finally:
        module.reset_cache()
        for name in list(sys.modules):
            if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
                sys.modules.pop(name, None)
