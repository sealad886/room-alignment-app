from __future__ import annotations

import ast
import os
import re
import sys
import tomllib
from pathlib import Path


VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[a-z]+\d+)?$")


def _runtime_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise ValueError("room_alignment.__version__ is missing or not a string literal")


def validate_release_version(root: Path, tag: str | None = None) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    package_version = str(project["version"])
    runtime_version = _runtime_version(root / "room_alignment" / "__init__.py")
    if not VERSION_PATTERN.fullmatch(package_version):
        raise ValueError(f"Project version is not supported release syntax: {package_version}")
    if runtime_version != package_version:
        raise ValueError(
            f"Version mismatch: pyproject.toml={package_version}, runtime={runtime_version}"
        )
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(package_version)}(?:\s|—|-)", changelog, re.MULTILINE):
        raise ValueError(f"CHANGELOG.md has no release section for {package_version}")
    if tag is not None and tag != f"v{package_version}":
        raise ValueError(f"Release tag {tag} does not match package version v{package_version}")
    return package_version


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tag = os.environ.get("GITHUB_REF_NAME") if os.environ.get("GITHUB_REF_TYPE") == "tag" else None
    try:
        version = validate_release_version(root, tag)
    except (KeyError, OSError, SyntaxError, ValueError) as error:
        print(f"release-version check failed: {error}", file=sys.stderr)
        return 1
    print(f"release-version check passed: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
