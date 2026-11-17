# Changelog

Notable user-facing changes are recorded here. This project is in beta; release packages and CI results are tied to their own published tag and commit.

## [0.2.0-beta.1] - 2026-11-17

First public beta release.

### Added

- A Windows Skill workflow for finding candidate kills in local Standoff 2 recordings and preparing reviewable MP4 clips.
- Automatic viewpoint attribution by default, including spectator selection and stable anonymous identities when a name cannot be read but visual attribution remains supported.
- Warmup filtering, viewpoint and round-boundary handling, incremental scan reuse, resumable review plans, frame evidence, and delivery validation.
- User-level setup for CPU or available NVIDIA acceleration, with release ZIP installation and Skills CLI installation guidance.

### Boundaries

- The scanner supplies candidates. Full-round boundaries, final kill counts, weapon categories, and duplicate decisions require AI visual review.
- This beta does not claim complete recall or a general accuracy rate across arbitrary footage.
- First-run setup may download dependencies and OCR models totaling several gigabytes. Source recordings are processed locally.
- Continuous playback, audio listening, fresh-computer installation without Python, and user acceptance are separate validation steps; see the [validation record](skills/so2-clipper/references/validation.md).
