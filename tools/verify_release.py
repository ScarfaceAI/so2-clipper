"""Validate a public so2-clipper ZIP and its SHA-256 sidecar."""
from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
import tempfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen
import zipfile

try:
    from tools.build_skill_zip import INSTALL_TEXT, ROOT, SKILL_NAME, read_version
except ModuleNotFoundError:
    from build_skill_zip import INSTALL_TEXT, ROOT, SKILL_NAME, read_version

REPO_RE = re.compile(r"\A[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
REQUIRED_FILES = {
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "VERSION",
    "SKILL.md",
    "scripts/clip.ps1",
    "scripts/install.ps1",
    "scripts/setup.ps1",
    "runtime/manifest.json",
    "runtime/scripts/config.py",
    "runtime/scripts/core.py",
    "runtime/scripts/kill_clipper.py",
    "runtime/scripts/media.py",
    "runtime/scripts/assets/spectator-eye.png",
}
PRIVATE_PATH_PATTERNS = (
    # Generic Windows profile pattern; no developer drive, account name, or
    # project folder is embedded in the public source.
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/](?:users|documents and settings)[\\/][^\\/\s]+"),
)
GENERIC_WINDOWS_ROOTS = {
    "recordings", "videos", "video", "media", "录像", "视频", "成片",
    "skills", "venvs", "venv", "output", "program files", "program files (x86)",
}
FORBIDDEN_COMPONENTS = {
    ".git",
    ".scratch",
    "evals",
    "dist",
    "output",
    "runs",
    "__pycache__",
    "screenshots",
    "evidence",
    "captures",
}
FORBIDDEN_NAMES = {"settings.local.json", "scan_results.json", "last_run.json"}
RECORDING_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".mpg", ".mpeg", ".wmv", ".mts", ".m2ts", ".3gp"}
BINARY_RUNTIME_SUFFIXES = {".exe", ".dll", ".pyd", ".pyc", ".pyo", ".pth", ".pt", ".onnx", ".bin", ".safetensors", ".msi", ".whl", ".zip", ".7z", ".tar", ".gz", ".xz"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg", ".pdf", ".psd", ".heic"}
WINDOWS_DRIVE_RE = re.compile(r"(?i)(?<![A-Za-z0-9])([A-Z]):[\\/]")


def _fail(message: str):
    raise ValueError(message)


def _check_archive_path(name: str) -> None:
    if name == "安装说明.txt":
        return
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or ".." in path.parts or "\\" in name or "\x00" in name or ":" in name:
        _fail(f"unsafe ZIP path: {name!r}")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        _fail(f"absolute ZIP path is forbidden: {name!r}")
    if any(part.lower() in FORBIDDEN_COMPONENTS for part in path.parts):
        _fail(f"private or development content is forbidden: {name}")
    if path.name.lower() in FORBIDDEN_NAMES:
        _fail(f"private runtime state is forbidden: {name}")
    if path.suffix.lower() in RECORDING_SUFFIXES:
        _fail(f"recording media is forbidden in the public package: {name}")
    if path.suffix.lower() in BINARY_RUNTIME_SUFFIXES:
        _fail(f"runtime binary or model weights are forbidden in the public package: {name}")
    if path.suffix.lower() in IMAGE_SUFFIXES or (
        path.suffix.lower() == ".png" and f"{SKILL_NAME}/runtime/scripts/assets/" not in name
    ):
        _fail(f"documentation screenshots or unreviewed images are forbidden: {name}")
    if path.suffix.lower() in {".log", ".jsonl"}:
        _fail(f"runtime logs are forbidden in the public package: {name}")
    if path.parts and path.parts[0] != SKILL_NAME:
        _fail(f"unexpected ZIP root entry: {name}")


def _verify_private_text(name: str, data: bytes) -> None:
    if len(data) > 8 * 1024 * 1024 or b"\x00" in data:
        return
    text = data.decode("utf-8", errors="ignore").casefold().replace("%3a", ":")
    for pattern in PRIVATE_PATH_PATTERNS:
        if pattern.search(text):
            _fail(f"Windows user profile path appears in {name}")
    # Deep absolute paths and custom one-folder roots often expose a machine's
    # private workspace. A generic example such as D:\\Recordings is allowed.
    for match in WINDOWS_DRIVE_RE.finditer(text):
        tail = text[match.end():].splitlines()[0]
        tail = re.split(r"[`\"'<>|;,.!?)}\]]", tail, maxsplit=1)[0].strip()
        parts = [part for part in re.split(r"[\\/]+", tail) if part]
        if not parts:
            continue
        root_name = parts[0].casefold()
        generic_root = root_name in GENERIC_WINDOWS_ROOTS or bool(re.fullmatch(r"python\d+", root_name))
        custom_single_root = len(parts) == 1 and not generic_root and any(char.isspace() for char in parts[0])
        if (len(parts) >= 2 and not generic_root) or custom_single_root:
            _fail(f"machine-specific absolute Windows path appears in {name}")


def _verify_local_markdown_links(payload: dict[str, bytes]) -> None:
    """A source-tree link must still work after installing only the Skill."""
    for name, data in payload.items():
        if not name.endswith('.md'):
            continue
        text = data.decode('utf-8')
        for target in re.findall(r'\]\((<[^>]+>|[^\s)]+)\)', text):
            target = target.strip('<>')
            parsed = urlsplit(target)
            if parsed.scheme or not parsed.path:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parsed.path)))
            if resolved.startswith('../') or resolved not in payload:
                _fail(f'local Markdown link leaves or is missing from the installed Skill: {name} -> {target}')


def _verify_zip(archive_path: Path, expected_version: str | None = None) -> dict:
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        _fail(f"cannot open release ZIP: {exc}")
    with archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            _fail(f"ZIP CRC check failed: {bad_member}")
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            _fail("ZIP contains duplicate member paths")
        for info in infos:
            _check_archive_path(info.filename.rstrip("/"))
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) == 0o120000:
                _fail(f"ZIP symlink is forbidden: {info.filename}")

        manifest_name = f"{SKILL_NAME}/manifest.json"
        if manifest_name not in names:
            _fail("ZIP manifest is missing")
        try:
            manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail(f"ZIP manifest is invalid: {exc}")
        if manifest.get("name") != SKILL_NAME:
            _fail("manifest name does not match the Skill")
        version = manifest.get("version")
        if not isinstance(version, str) or not version:
            _fail("manifest release version is missing")
        if expected_version and version != expected_version:
            _fail(f"release version mismatch: expected {expected_version}, found {version}")
        if not SHA_RE.fullmatch(str(manifest.get("content_id", ""))):
            _fail("manifest content_id is not a SHA-256 digest")
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            _fail("manifest file map is missing")
        expected_content_id = hashlib.sha256(
            json.dumps({"files": files, "version": version}, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if manifest["content_id"] != expected_content_id:
            _fail("manifest content_id does not match its version and file map")

        expected_names = {f"{SKILL_NAME}/{name}" for name in files}
        expected_names.update({manifest_name, "安装说明.txt"})
        if set(names) != expected_names or len(names) != len(expected_names):
            _fail(
                "ZIP members do not match manifest "
                f"(missing={sorted(expected_names - set(names))}, "
                f"extra={sorted(set(names) - expected_names)})"
            )

        payload: dict[str, bytes] = {}
        for relative_name, digest in files.items():
            if not isinstance(relative_name, str) or not isinstance(digest, str) or not SHA_RE.fullmatch(digest):
                _fail(f"invalid file/hash record in manifest: {relative_name!r}")
            member = f"{SKILL_NAME}/{relative_name}"
            _check_archive_path(member)
            data = archive.read(member)
            actual = hashlib.sha256(data).hexdigest()
            if actual != digest:
                _fail(f"file hash mismatch: {relative_name}")
            _verify_private_text(relative_name, data)
            payload[relative_name] = data

        _verify_local_markdown_links(payload)

        if archive.read("安装说明.txt") != INSTALL_TEXT.encode("utf-8"):
            _fail("bilingual installation instructions are missing or outdated")
        required = REQUIRED_FILES - payload.keys()
        if required:
            _fail(f"public package is missing required files: {sorted(required)}")
        try:
            packaged_version = payload["VERSION"].decode("utf-8").strip()
        except UnicodeDecodeError:
            _fail("packaged VERSION is not UTF-8")
        if packaged_version != version:
            _fail("packaged VERSION differs from manifest version")
        try:
            runtime_manifest = json.loads(payload["runtime/manifest.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail(f"runtime manifest is invalid: {exc}")
        if runtime_manifest.get("version") != version:
            _fail("runtime manifest version differs from release version")
        runtime_files = runtime_manifest.get("files")
        if not isinstance(runtime_files, dict) or not runtime_files:
            _fail("runtime manifest has no generated source files")
        distribution_files = runtime_manifest.get("distribution_files")
        if not isinstance(distribution_files, dict) or set(distribution_files) != {
            "LICENSE", "THIRD_PARTY_NOTICES.md", "VERSION"
        }:
            _fail("runtime manifest is missing required Skill distribution file hashes")
        for relative_name, digest in distribution_files.items():
            data = payload.get(relative_name)
            if data is None or not SHA_RE.fullmatch(str(digest)) or hashlib.sha256(data).hexdigest() != digest:
                _fail(f"Skill distribution file differs from runtime manifest: {relative_name}")
        for relative_name, digest in runtime_files.items():
            package_name = f"runtime/scripts/{relative_name}"
            data = payload.get(package_name)
            if data is None or hashlib.sha256(data).hexdigest() != digest:
                _fail(f"runtime snapshot differs from its manifest: {relative_name}")
        packaged_runtime_files = {
            name.removeprefix("runtime/scripts/")
            for name in payload
            if name.startswith("runtime/scripts/")
        }
        if packaged_runtime_files != set(runtime_files):
            _fail("runtime snapshot file set differs from its manifest")
        for required_runtime in ("config.py", "core.py", "kill_clipper.py", "media.py"):
            if required_runtime not in runtime_files:
                _fail(f"runtime module is missing: {required_runtime}")

    return {
        "archive": archive_path.name,
        "version": version,
        "content_id": manifest["content_id"],
        "files": len(files),
        "status": "public_release_valid",
    }


def _verify_sidecar(archive_path: Path, checksum_path: Path) -> str:
    if not checksum_path.is_file():
        _fail(f"SHA-256 sidecar is missing: {checksum_path.name}")
    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    expected_line = f"{actual}  {archive_path.name}\n"
    try:
        provided = checksum_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"SHA-256 sidecar is not UTF-8: {exc}")
    if provided != expected_line:
        _fail("SHA-256 sidecar does not match the release ZIP")
    return actual


def verify(archive_path: Path, checksum_path: Path | None = None, expected_version: str | None = None) -> dict:
    archive_path = Path(archive_path)
    if not archive_path.is_file():
        _fail(f"release ZIP is missing: {archive_path}")
    checksum_path = checksum_path or archive_path.with_suffix(".zip.sha256")
    result = _verify_zip(archive_path, expected_version=expected_version)
    result["sha256"] = _verify_sidecar(archive_path, checksum_path)
    result["bytes"] = archive_path.stat().st_size
    return result


def _download_release(repo: str, tag: str, directory: Path) -> tuple[Path, Path]:
    if not REPO_RE.fullmatch(repo):
        _fail("--repo must use owner/name format")
    if not tag or any(ord(char) < 32 for char in tag):
        _fail("--tag is invalid")
    api = f"https://api.github.com/repos/{repo}/releases/tags/{quote(tag, safe='')}"
    request = Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "so2-clipper-release-verifier"})
    try:
        with urlopen(request, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"GitHub release lookup failed: {exc}")
    assets = release.get("assets")
    if not isinstance(assets, list):
        _fail("GitHub release has no asset list")
    zip_assets = [asset for asset in assets if str(asset.get("name", "")).startswith(f"{SKILL_NAME}-") and str(asset.get("name", "")).endswith(".zip")]
    if len(zip_assets) != 1:
        _fail(f"expected one {SKILL_NAME} ZIP attachment, found {len(zip_assets)}")
    zip_asset = zip_assets[0]
    zip_name = zip_asset["name"]
    sha_asset_name = zip_name + ".sha256"
    checksum_assets = [asset for asset in assets if asset.get("name") == sha_asset_name]
    if len(checksum_assets) != 1:
        _fail(f"GitHub release is missing {sha_asset_name}")
    paths = []
    for asset in (zip_asset, checksum_assets[0]):
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if not isinstance(name, str) or Path(name).name != name or not isinstance(url, str):
            _fail("GitHub release asset metadata is invalid")
        target = directory / name
        download = Request(url, headers={"User-Agent": "so2-clipper-release-verifier"})
        try:
            with urlopen(download, timeout=90) as response, target.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            _fail(f"GitHub release asset download failed ({name}): {exc}")
        paths.append(target)
    return paths[0], paths[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="ZIP to verify; defaults to the local current_release.json")
    parser.add_argument("--sha256-file", type=Path, help="sidecar file (default: <archive>.zip.sha256)")
    parser.add_argument("--version", help="expected release version (defaults to local VERSION)")
    parser.add_argument("--download", action="store_true", help="download ZIP and SHA attachment from GitHub")
    parser.add_argument("--repo", help="GitHub owner/name, used with --download")
    parser.add_argument("--tag", help="GitHub release tag, used with --download")
    args = parser.parse_args(argv)
    try:
        if args.download:
            if args.archive or args.sha256_file or not args.repo or not args.tag:
                _fail("--download requires --repo and --tag, and cannot be combined with --archive/--sha256-file")
            expected_version = args.version or args.tag.removeprefix("v")
            with tempfile.TemporaryDirectory(prefix="so2-release-verify-") as temp:
                archive_path, checksum_path = _download_release(args.repo, args.tag, Path(temp))
                result = verify(archive_path, checksum_path, expected_version)
        else:
            if args.repo or args.tag:
                _fail("--repo and --tag require --download")
            if args.archive:
                archive_path = args.archive
            else:
                pointer = ROOT / "dist" / "current_release.json"
                if not pointer.is_file():
                    _fail("--archive is required when dist/current_release.json is absent")
                data = json.loads(pointer.read_text(encoding="utf-8"))
                zip_name = data["zip"]
                if not isinstance(zip_name, str) or Path(zip_name).name != zip_name:
                    _fail("current release pointer must contain a ZIP filename, not a local path")
                archive_path = ROOT / "dist" / zip_name
                if archive_path.resolve().parent != (ROOT / "dist").resolve():
                    _fail("current release pointer ZIP is outside project dist")
            result = verify(archive_path, args.sha256_file, args.version or read_version(ROOT))
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        print(f"release verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
