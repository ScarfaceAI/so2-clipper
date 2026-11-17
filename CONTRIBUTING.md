# Contributing

Thanks for helping improve SO2 Clipper. Please keep changes focused and include enough context for maintainers to reproduce behavior without sharing private recordings.

## Before opening a change

- Search existing issues and discussions for the same behavior.
- Describe the input conditions, expected behavior, actual behavior, and relevant command or log excerpt.
- Do not upload game recordings, user names, machine paths, runtime configuration, OCR models, caches, generated reports, or private task evidence. Use synthetic or explicitly permissioned media for reproductions.
- For a security issue, follow [SECURITY.md](SECURITY.md) instead of opening a public issue with exploit details.

## Source ownership

The engine source of truth is repository-root `scripts/`. Skill instructions and operator guidance live in `skills/so2-clipper/`. The checked-in `skills/so2-clipper/runtime/scripts/` directory is generated from `scripts/`; edit only the source, regenerate the runtime snapshot, and include the synchronized copy in your change. CI checks that both copies match. See [SOURCE_OF_TRUTH.md](SOURCE_OF_TRUTH.md) before changing packaging or runtime behavior.

## Development environment

Use Windows 10/11 x64 and Python 3.12 or 3.13. Install project test dependencies in an isolated environment. Do not point development commands at personal recordings, local user settings, or an active production output directory.

## Checks

From the repository root, run the project contract check and the tests relevant to your change:

```powershell
python tools/check_project_contracts.py
python -m unittest test_options test_spectator
```

Replace the example test modules with the tests for your change. For a complete code change, run the full regression suite:

```powershell
python test_so2_clipper.py
```

Regenerate and verify the packaged Skill runtime when engine files change:

```powershell
python tools/sync_skill_runtime.py --write
python tools/sync_skill_runtime.py --check
```

The runtime copy is generated output; do not make a separate manual implementation there. The release ZIP is built from the maintained sources by `tools/build_skill_zip.py` and includes the matching runtime snapshot. Do not rebuild or attach a release package unless your change is part of a release task.

## Pull requests

Keep the PR focused, explain the user-visible effect, list the checks you ran, and call out checks that require real footage or hardware and remain unverified. Avoid accuracy claims based only on unit tests or generated sample footage.
