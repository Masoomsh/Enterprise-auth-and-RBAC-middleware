"""Release gate for gateway-client. Used by CI; also run it by hand before tagging.

    python scripts/check_release.py                  # version + changelog are consistent
    python scripts/check_release.py --tag sdk-v0.1.0 # ...and the git tag matches the version
    python scripts/check_release.py --notes          # print this version's changelog section

The version lives in exactly one place: __version__ in src/gateway_client/__init__.py.
Tags are `sdk-vX.Y.Z` because the repository also holds the gateway itself.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")
TAG_PREFIX = "sdk-v"


def read_version(root: Path = ROOT) -> str:
    text = (root / "src" / "gateway_client" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("error: __version__ not found")
    return match.group(1)


def changelog_section(version: str, root: Path = ROOT) -> tuple[str, str] | None:
    """Return (heading, body) of the `## [version] - date` section, or None."""
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S)
    match = pattern.search(text)
    if not match:
        return None
    heading = text[match.start():text.index("\n", match.start())]
    return heading, match.group(1).strip()


def problems(tag: str | None, root: Path = ROOT) -> list[str]:
    version = read_version(root)
    found: list[str] = []
    if not SEMVER.match(version):
        found.append(f"version {version!r} is not X.Y.Z (optionally with a/b/rc suffix)")
    if tag is not None and tag != f"{TAG_PREFIX}{version}":
        found.append(f"tag {tag!r} does not match version {version!r} (expected {TAG_PREFIX}{version})")
    section = changelog_section(version, root)
    if section is None:
        found.append(f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' section")
    else:
        heading, body = section
        if not re.search(r"- \d{4}-\d{2}-\d{2}\s*$", heading):
            found.append(f"changelog heading must carry a release date, got: {heading!r}")
        if not body:
            found.append(f"changelog section for {version} is empty")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", help="git tag being released, e.g. sdk-v0.1.0")
    parser.add_argument("--notes", action="store_true", help="print the changelog section and exit")
    args = parser.parse_args(argv)
    if args.notes:
        section = changelog_section(read_version())
        if section is None:
            print("error: no changelog section for this version", file=sys.stderr)
            return 1
        print(section[1])
        return 0
    errors = problems(args.tag)
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    if not errors:
        print(f"release {read_version()} looks consistent")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
