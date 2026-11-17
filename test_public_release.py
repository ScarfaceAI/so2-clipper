"""Boundary tests for generated runtime sync and public archive validation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tools import build_skill_zip as builder
from tools import sync_skill_runtime
from tools import verify_release


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="so2_public_release_test_")
        self.root = Path(self.temp.name).resolve()
        skill = self.root / "skills/so2-clipper"
        (skill / "scripts").mkdir(parents=True)
        (skill / "SKILL.md").write_text("public skill instructions\n", encoding="utf-8")
        (skill / "scripts/clip.ps1").write_text("Write-Output 'clip'\n", encoding="utf-8")
        (skill / "scripts/install.ps1").write_text("Write-Output 'install'\n", encoding="utf-8")
        (skill / "scripts/setup.ps1").write_text("Write-Output 'setup'\n", encoding="utf-8")
        scripts = self.root / "scripts"
        (scripts / "assets").mkdir(parents=True)
        for name in ("config.py", "core.py", "kill_clipper.py", "media.py", "workspace.py"):
            (scripts / name).write_text(f"# public module {name}\n", encoding="utf-8")
        (scripts / "assets/spectator-eye.png").write_bytes(b"public fixture asset")
        (self.root / "VERSION").write_text("0.2.0-beta.1\n", encoding="utf-8")
        (self.root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        (self.root / "THIRD_PARTY_NOTICES.md").write_text("Third party notices\n", encoding="utf-8")
        (self.root / "settings.local.json").write_text('{"private": true}\n', encoding="utf-8")
        self.scope = patch.object(builder, "ROOT", self.root)
        self.scope.start()

    def tearDown(self):
        self.scope.stop()
        self.temp.cleanup()

    def build(self):
        sync_skill_runtime.write(self.root)
        result = builder.build()
        archive = Path(result["zip"])
        sidecar = archive.with_suffix(".zip.sha256")
        verified = verify_release.verify(archive, sidecar, "0.2.0-beta.1")
        return archive, sidecar, verified

    def test_runtime_check_fails_after_source_module_changes(self):
        sync_skill_runtime.write(self.root)
        (self.root / "scripts/core.py").write_text("# source changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            sync_skill_runtime.check(self.root)

    def test_runtime_check_rejects_extra_generated_file(self):
        sync_skill_runtime.write(self.root)
        (self.root / "skills/so2-clipper/runtime/scripts/orphan.py").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "file set differs"):
            sync_skill_runtime.check(self.root)

    def test_runtime_check_ignores_only_python_bytecode_cache(self):
        sync_skill_runtime.write(self.root)
        cache = self.root / "skills/so2-clipper/runtime/scripts/__pycache__/core.cpython-312.pyc"
        cache.parent.mkdir(parents=True)
        cache.write_bytes(b"ephemeral bytecode")
        self.assertEqual(sync_skill_runtime.check(self.root)["status"], "runtime_matches_source")
        with self.assertRaisesRegex(ValueError, "private or development"):
            verify_release._check_archive_path("so2-clipper/runtime/scripts/__pycache__/core.pyc")

    def test_sync_copies_license_notices_and_version_into_skill_root(self):
        result = sync_skill_runtime.write(self.root)
        self.assertEqual(result["status"], "runtime_matches_source")
        skill_root = self.root / "skills/so2-clipper"
        runtime_manifest = json.loads((skill_root / "runtime/manifest.json").read_text(encoding="utf-8"))
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "VERSION"):
            self.assertEqual((skill_root / name).read_bytes(), (self.root / name).read_bytes())
            self.assertIn(name, runtime_manifest["distribution_files"])

    def test_source_version_constant_drift_is_rejected(self):
        with patch.object(builder, "SOURCE_ROOT", self.root), patch.object(builder, "VERSION", "0.2.0"):
            with self.assertRaisesRegex(ValueError, "VERSION constant is stale"):
                builder.read_version(self.root)

    def test_missing_required_template_and_missing_sidecar_are_rejected(self):
        archive, sidecar, _ = self.build()
        rewritten = archive.with_name("without-installer.zip")
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(rewritten, "w", zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if info.filename != "so2-clipper/scripts/clip.ps1":
                    target.writestr(info, source.read(info.filename))
        digest = hashlib.sha256(rewritten.read_bytes()).hexdigest()
        rewritten_sidecar = rewritten.with_suffix(".zip.sha256")
        rewritten_sidecar.write_text(f"{digest}  {rewritten.name}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "members do not match manifest|missing required"):
            verify_release.verify(rewritten, rewritten_sidecar, "0.2.0-beta.1")

        sidecar.unlink()
        with self.assertRaisesRegex(ValueError, "sidecar is missing"):
            verify_release.verify(archive, sidecar, "0.2.0-beta.1")

    def test_changed_archive_sha_is_rejected(self):
        archive, sidecar, _ = self.build()
        with zipfile.ZipFile(archive, "a") as package:
            package.comment = b"changed archive bytes"
        with self.assertRaisesRegex(ValueError, "sidecar does not match"):
            verify_release.verify(archive, sidecar, "0.2.0-beta.1")

    def test_clean_archive_excludes_personal_settings_and_contains_notices(self):
        archive, _, result = self.build()
        self.assertEqual(result["status"], "public_release_valid")
        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())
            self.assertIn("so2-clipper/LICENSE", names)
            self.assertIn("so2-clipper/THIRD_PARTY_NOTICES.md", names)
            self.assertTrue(all("settings.local.json" not in name for name in names))
            self.assertTrue(all(not name.lower().endswith((".mp4", ".mkv", ".mov")) for name in names))
            self.assertNotIn("so2-clipper/evals/evals.json", names)
            payload = package.read("so2-clipper/manifest.json")
            manifest = json.loads(payload.decode("utf-8"))
            self.assertEqual(manifest["version"], "0.2.0-beta.1")
        pointer = json.loads((self.root / "dist/current_release.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["zip"], archive.name)
        self.assertFalse(Path(pointer["zip"]).is_absolute())

    def test_installed_skill_cannot_link_to_files_only_in_the_source_repository(self):
        (self.root / 'skills/so2-clipper/SKILL.md').write_text(
            '# Skill\n[Source-only manual](../../README.md)\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'local Markdown link'):
            self.build()

    def test_private_path_marker_is_rejected_without_rejecting_generic_recording_paths(self):
        with self.assertRaisesRegex(ValueError, "machine-specific absolute Windows path"):
            verify_release._verify_private_text("README.md", b"R:/My Projects/demo")
        with self.assertRaisesRegex(ValueError, "Windows user profile path"):
            verify_release._verify_private_text("README.md", b"C:\\Users\\sample")
        verify_release._verify_private_text("README.md", b"D:\\Recordings\\match.mp4")
        verify_release._verify_private_text("README.md", "D:\\录像\\比赛.mkv".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
