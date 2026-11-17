"""Generate or verify the installable Skill's copy of the shared runtime.

The project-level ``scripts/`` tree remains the only maintained engine source.
This checked-in snapshot lets tools that install only ``skills/so2-clipper``
run without a second copy of the engine being maintained by hand.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path("skills") / "so2-clipper"
RUNTIME = SKILL / "runtime"
SCRIPTS = Path("scripts")
MANIFEST_NAME = "manifest.json"
DISTRIBUTION_NAMES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "VERSION")


def _builder():
    # Keep the test-fixture fallback and version parsing in one place.
    try:
        from tools.build_skill_zip import read_version
    except ModuleNotFoundError:
        from build_skill_zip import read_version

    return read_version


def _source_files(root: Path) -> dict[str, bytes]:
    scripts = root / SCRIPTS
    if not scripts.is_dir():
        raise ValueError(f"runtime source directory is missing: {SCRIPTS.as_posix()}")
    files: dict[str, bytes] = {}
    for path in sorted(scripts.glob("*.py")):
        if path.is_file() and not path.is_symlink():
            files[path.name] = path.read_bytes()
    assets = scripts / "assets"
    if not assets.is_dir():
        raise ValueError("runtime source directory is missing: scripts/assets")
    for path in sorted(assets.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is not an allowed runtime asset: {path.relative_to(root)}")
        if path.is_file():
            files[path.relative_to(scripts).as_posix()] = path.read_bytes()
    if not any(name.endswith(".py") for name in files):
        raise ValueError("runtime source has no Python modules")
    if not any(name.startswith("assets/") for name in files):
        raise ValueError("runtime source has no assets")
    return files


def _distribution_files(root: Path) -> dict[str, bytes]:
    files = {}
    for name in DISTRIBUTION_NAMES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required distribution source is missing or not a regular file: {name}")
        files[name] = path.read_bytes()
    return files


def _expected_manifest(files: dict[str, bytes], distribution_files: dict[str, bytes], version: str) -> dict:
    return {
        "schema_version": 1,
        "name": "so2-clipper-runtime",
        "version": version,
        "source": "scripts/*.py and scripts/assets/**",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())},
        "distribution_files": {
            name: hashlib.sha256(data).hexdigest() for name, data in sorted(distribution_files.items())
        },
    }


def _actual_files(directory: Path) -> dict[str, bytes]:
    if not directory.exists():
        return {}
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"runtime scripts path must be a regular directory: {directory}")
    files: dict[str, bytes] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in generated runtime: {path}")
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        if path.is_file():
            files[path.relative_to(directory).as_posix()] = path.read_bytes()
    return files


def check(root: Path = ROOT) -> dict:
    root = root.resolve()
    source = _source_files(root)
    distribution = _distribution_files(root)
    version = _builder()(root)
    expected_manifest = _expected_manifest(source, distribution, version)
    skill_root = root / SKILL
    for name, data in distribution.items():
        target = skill_root / name
        if target.is_symlink() or not target.is_file() or target.read_bytes() != data:
            raise ValueError(f"generated Skill distribution file differs from project root: {name}")
    runtime = root / RUNTIME
    runtime_scripts = runtime / "scripts"
    actual = _actual_files(runtime_scripts)
    if actual.keys() != source.keys():
        missing = sorted(source.keys() - actual.keys())
        extra = sorted(actual.keys() - source.keys())
        raise ValueError(f"runtime file set differs from source (missing={missing}, extra={extra})")
    changed = [name for name in sorted(source) if actual[name] != source[name]]
    if changed:
        raise ValueError(f"runtime bytes differ from source: {changed}")
    manifest_path = runtime / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("runtime manifest is missing or is not a regular file")
    try:
        actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime manifest is invalid: {exc}") from exc
    if actual_manifest != expected_manifest:
        raise ValueError("runtime manifest differs from the source-derived manifest")
    runtime_files = {
        path.relative_to(runtime).as_posix()
        for path in runtime.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() not in {".pyc", ".pyo"}
    }
    expected_runtime = {f"scripts/{name}" for name in source} | {MANIFEST_NAME}
    if runtime_files != expected_runtime:
        raise ValueError(
            "runtime contains extra or missing files "
            f"(missing={sorted(expected_runtime - runtime_files)}, "
            f"extra={sorted(runtime_files - expected_runtime)})"
        )
    return {"version": version, "files": len(source), "status": "runtime_matches_source"}


def write(root: Path = ROOT) -> dict:
    root = root.resolve()
    source = _source_files(root)
    version = _builder()(root)
    runtime = root / RUNTIME
    scripts_target = runtime / "scripts"
    if runtime.is_symlink() or (runtime.exists() and not runtime.is_dir()):
        raise ValueError(f"runtime path must be a regular directory: {runtime}")
    if scripts_target.is_symlink() or (scripts_target.exists() and not scripts_target.is_dir()):
        raise ValueError(f"runtime scripts path must be a regular directory: {scripts_target}")
    scripts_target.mkdir(parents=True, exist_ok=True)

    expected = set(source)
    for path in sorted(scripts_target.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed in generated runtime: {path}")
        if "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        relative = path.relative_to(scripts_target).as_posix()
        if path.is_file() and relative not in expected:
            path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    for name, data in source.items():
        target = scripts_target / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f"{target.name}.{os.getpid()}.part")
        try:
            part.write_bytes(data)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)

    for name, data in _distribution_files(root).items():
        target = root / SKILL / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"Skill distribution file path must be a regular file: {name}")
        part = target.with_name(f"{target.name}.{os.getpid()}.part")
        try:
            part.write_bytes(data)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)

    manifest_path = runtime / MANIFEST_NAME
    part = manifest_path.with_name(f"{manifest_path.name}.{os.getpid()}.part")
    try:
        part.write_bytes(
            (
                json.dumps(
                    _expected_manifest(source, _distribution_files(root), version),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8")
        )
        os.replace(part, manifest_path)
    finally:
        part.unlink(missing_ok=True)
    return check(root)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="compare generated files with root scripts")
    group.add_argument("--write", action="store_true", help="refresh the checked-in runtime snapshot")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        result = write(args.root) if args.write else check(args.root)
    except (OSError, ValueError) as exc:
        print(f"runtime sync failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
