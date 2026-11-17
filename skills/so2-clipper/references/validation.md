# Validation status and evidence boundaries

This page summarizes public-safe validation evidence available for the v0.2.0-beta.1 source and workflow. These results cover specific regression checks and selected footage windows; they are not a benchmark of arbitrary recordings and do not establish complete kill recall.

## Automated regression

The complete local pre-publication regression run completed with 91 pre-existing assertions and 165 unit tests passing. These are local project results, not a result from the GitHub Actions workflow for the public repository. Check the [release commit's CI run](https://github.com/ScarfaceAI/so2-clipper/actions/workflows/test.yml) for the status of its own code revision.

## Visual attribution samples

One static-frame review covered 9 source recordings at 17 time points: 10 kill examples, 2 deaths, 2 assists, 1 bomb suicide, and 2 frames without a kill-feed banner. The inspected feed roles matched the visual review in those samples.

A separate scan covered 5 source recordings across 7 continuous windows, totaling 61 seconds and 183 sampled frames, with player-name OCR disabled. Five reliable kills were retained as candidates. Seven items remained unresolved, involving brief avatar loss, an old feed item at the window start, or a counter increase near a viewpoint change. This supports conservative handling in these windows; it is not a zero-issue result.

## Standalone Skill workflow

An independent copy of the Skill was exercised without personal settings. It used automatic attribution without a pre-filled ID on a live-layout recording, exported a single-kill result, and then reused the existing output with matching bytes and modification time. The output passed full decode, and representative rendered frames were inspected.

Publication preparation also created a separate CPU virtual environment, installed
the pinned runtime dependencies, initialized EasyOCR, and scanned an eight-second
replay sample through the real PowerShell entry point. It exported one visually
attributed kill with exit code 0 and no name supplied. Existing model files and
FFmpeg were reused; this is not a first installation on a computer without Python.

## Remaining validation limits

- The visual samples do not establish full-library recall, a general accuracy rate, or coverage of every HUD layout.
- Seven scan items in the sampled continuous windows remained unresolved; a short recording can begin with an old notification, briefly lose the viewpoint avatar, or cross a viewpoint change before a counter increase becomes clear.
- Continuous playback and audio listening were not performed as part of the evidence above.
- A complete first-time installation on a clean computer without Python was not verified in this validation set.
- User acceptance and the GitHub Actions result for a release commit are separate from these local checks and visual samples.

For the operator process, see [setup](setup.md) and [the complete workflow](workflow.md).
For candidate decisions and evidence requirements, see [review](review.md) and
[autonomous delivery](delivery.md). These references remain inside the installed Skill.
