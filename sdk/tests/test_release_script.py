import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_release.py"
spec = importlib.util.spec_from_file_location("check_release", SCRIPT)
check_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_release)


def tree(tmp_path, version="1.2.3", changelog=None):
    (tmp_path / "src" / "gateway_client").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "gateway_client" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / "CHANGELOG.md").write_text(
        changelog
        if changelog is not None
        else "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-11-09\n\n### Added\n- Thing\n\n## [1.2.2] - 2026-10-01\n- Old\n"
    )
    return tmp_path


def test_consistent_release_passes(tmp_path):
    assert check_release.problems("sdk-v1.2.3", tree(tmp_path)) == []


def test_notes_are_only_this_versions_section(tmp_path):
    root = tree(tmp_path)
    _, body = check_release.changelog_section("1.2.3", root)
    assert "Thing" in body and "Old" not in body


@pytest.mark.parametrize("tag", ["sdk-v1.2.4", "v1.2.3", "1.2.3"])
def test_wrong_tag_is_rejected(tmp_path, tag):
    assert any("does not match" in p for p in check_release.problems(tag, tree(tmp_path)))


def test_missing_undated_or_empty_changelog_entries_are_rejected(tmp_path):
    root = tree(tmp_path, changelog="# Changelog\n\n## [Unreleased]\n")
    assert any("no '## [1.2.3]" in p for p in check_release.problems(None, root))
    root = tree(tmp_path, changelog="## [1.2.3] - unreleased\n- x\n")
    assert any("release date" in p for p in check_release.problems(None, root))
    root = tree(tmp_path, changelog="## [1.2.3] - 2026-11-09\n\n## [1.2.2] - 2026-10-01\n- x\n")
    assert any("empty" in p for p in check_release.problems(None, root))


def test_bad_version_is_rejected(tmp_path):
    assert any("not X.Y.Z" in p for p in check_release.problems(None, tree(tmp_path, version="1.2")))


def test_the_real_package_version_matches_the_module():
    import gateway_client

    assert check_release.read_version() == gateway_client.__version__
