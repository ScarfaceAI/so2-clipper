# Source of truth

This file describes maintained implementation sources and generated distribution files for the public repository.

## Maintained sources

- `scripts/`: unique maintained source for video discovery, layout analysis, visual attribution, OCR orchestration, stage detection, caching, candidate grouping, extraction, evidence, review, and delivery execution.
- `skills/so2-clipper/SKILL.md`: the AI Skill entry point and its task workflow.
- `skills/so2-clipper/references/`: maintained setup, calibration, review, recovery, workflow, delivery, and validation guidance.
- `tools/`: project checks, runtime synchronization, and release package builder.
- `test_*.py`: regression and behavior checks for the maintained implementation.

## Generated files and packages

`skills/so2-clipper/runtime/scripts/` is the committed, generated runtime copy of the Python modules in the repository-root `scripts/`. It is included in both the npx Skills CLI installation and the standalone Release ZIP. Do not edit the generated copy by hand. After engine changes, regenerate and check the copy:

```powershell
python tools/sync_skill_runtime.py --write
python tools/sync_skill_runtime.py --check
```

The same command mirrors root `LICENSE`, `THIRD_PARTY_NOTICES.md`, and `VERSION`
into the Skill directory. Both engine hashes and distribution-file hashes are
checked, so a Skill-only installation includes its license notices and version.
The repository-root `VERSION` is the maintained release-version source.

CI checks that the committed `runtime/scripts/` copy matches the maintained engine source. The complete Skill directory, including `runtime/`, is used by the npx Skills CLI; the standalone Release ZIP contains the same Skill instructions, references, and runnable runtime snapshot. `tools/build_skill_zip.py` is the package builder; the ZIP is a snapshot of the maintained files at build time. Publish it only through the project release workflow, which also supplies a SHA-256 checksum. Preserve the full Skill directory when installing; omitting `runtime/` leaves no engine for the Skill entry point to call.

## Runtime data

At runtime, the Skill stores user-level configuration and task state under the current Windows user's local application data directory by default. These files are not source files and must not be committed. Video input and output paths are supplied by each task; new users' default clip output is under the input directory at `output/<local date>/`.

For package and legal component details, see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). For validation evidence and limits, see [`skills/so2-clipper/references/validation.md`](skills/so2-clipper/references/validation.md).
