"""Version mit Git-Stand (multigpt/version.py): Datei BUILD, sonst git, sonst nichts."""

import pytest

from multigpt import version


@pytest.fixture(autouse=True)
def _fresh_cache():
    version.app_build.cache_clear()
    yield
    version.app_build.cache_clear()


def test_build_file_wins(tmp_path, monkeypatch):
    build = tmp_path / "BUILD"
    build.write_text("d9face2-dirty\n", encoding="utf-8")
    monkeypatch.setattr(version, "BUILD_FILE", build)
    assert version.app_build() == "d9face2-dirty"
    assert version.version_label() == f"{version.app_version()} (d9face2-dirty)"


@pytest.mark.parametrize("content", ["", "unbekannt", "<script>", "d9face2 (mit Änderungen)"])
def test_unusable_build_file_shows_only_the_version(tmp_path, monkeypatch, content):
    build = tmp_path / "BUILD"
    build.write_text(content, encoding="utf-8")
    monkeypatch.setattr(version, "BUILD_FILE", build)
    assert version.app_build() == ""
    assert version.version_label() == version.app_version()


def test_without_file_and_without_git_nothing(tmp_path, monkeypatch):
    package = tmp_path / "multigpt"
    package.mkdir()
    monkeypatch.setattr(version, "BUILD_FILE", package / "BUILD")
    assert version.app_build() == ""


def test_source_tree_uses_git():
    # Im Quellbaum (Entwicklung, CI) gibt es .git, aber keine BUILD-Datei.
    if version.BUILD_FILE.exists() or not (version.BUILD_FILE.parent.parent / ".git").exists():
        pytest.skip("kein Quellbaum mit .git")
    build = version.app_build()
    assert build and build.split("-")[0].isalnum()
