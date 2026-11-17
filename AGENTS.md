# Repository guidance for agents

This repository contains the public SO2 Clipper project. Keep code, documentation, test data, and generated packages free of private recordings, user-specific paths, credentials, local runtime files, and personal task records.

## Source ownership

- `scripts/` is the only maintained engine implementation.
- `skills/so2-clipper/` contains the Skill instructions and references.
- `skills/so2-clipper/runtime/scripts/` is a committed generated copy of root `scripts/`, used by npx and the Release ZIP. Edit the engine in root `scripts/`, regenerate and check the snapshot, and include the synchronized copy; CI checks consistency.
- `tools/build_skill_zip.py` builds the standalone Skill release package from maintained sources. Do not hand-edit packaged copies.
- `docs/images/` contains approved, sanitized public documentation assets. Do not add source-game footage or identifiable player information without permission.

## Working rules

- Read [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md) before changing engine/runtime/package ownership.
- Keep changes within the requested files and preserve unrelated work in the shared checkout.
- Use synthetic fixtures or media with explicit permission for tests. Do not run automated tools against a user's real media or local configuration as part of repository checks.
- Do not infer accuracy, full recall, playback, audio, installation, or user acceptance from unit-test success. State the specific evidence and remaining gaps.
- Do not publish, tag, or release a build unless the task explicitly includes release authorization.

## Development checks

Use Python 3.12 or 3.13 on Windows x64. Before code changes, run `python tools/check_project_contracts.py`. For a code change, run the affected tests; after a completed batch of code changes, run `python test_so2_clipper.py`. If the engine changes, update and verify the generated Skill runtime:

```powershell
python tools/sync_skill_runtime.py --write
python tools/sync_skill_runtime.py --check
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contributor workflow.
