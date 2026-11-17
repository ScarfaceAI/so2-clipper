# Installation and usage

SO2 Clipper is a Windows Skill for AI tools. It combines a local scanner with an AI visual review step. The scanner produces evidence and candidates; the AI reviews the source frames and makes content decisions before clips are delivered.

## Requirements

- Windows 10 or 11, x64.
- Python 3.12 or 3.13. The first-run installer can prepare a suitable user-level environment.
- FFmpeg and ffprobe 7 or newer; setup downloads a pinned build when they are absent.
- CPU support; NVIDIA acceleration is used when available and configured.
- Internet access for initial dependency and model downloads. An existing environment can be checked in offline mode.

The initial setup can download Python, pinned dependencies, FFmpeg/ffprobe when needed, and EasyOCR model files. The total download may be several gigabytes. Setup does not require administrator privileges, change the system PATH, or change PowerShell execution policy. Source videos are processed locally and are not uploaded by this project.

## Installation

### Skills CLI

```powershell
npx -y skills add ScarfaceAI/so2-clipper --skill so2-clipper -g
```

Reload skills in the AI application after installation. Node.js is only needed for this installation route.

### Complete Release ZIP

1. Download the ZIP from the [v0.2.0-beta.1 Release](https://github.com/ScarfaceAI/so2-clipper/releases/tag/v0.2.0-beta.1).
2. Extract the entire archive.
3. In PowerShell, run the included installer:

   ```powershell
   & '.\so2-clipper\scripts\install.ps1'
   ```

4. Reload skills in your AI application.

The installer places the Skill at `%USERPROFILE%\.agents\skills\so2-clipper` by default. To choose a different destination, pass `-Destination 'D:\Skills\so2-clipper'`. The installer stops if the destination already exists; compare the current and new versions before replacing an existing installation.

The ZIP is self-contained and does not require the project checkout or Node.js. Keep the complete Skill directory, including `runtime/`, when installing; both the npx Skills CLI and Release ZIP include the runnable runtime snapshot. Developers maintain only the repository-root `scripts/` directory, and CI checks that the committed `runtime/scripts/` copy matches it.

## First run and diagnostics

On first use, the Skill prepares the user-level runtime and model files. Let the setup finish before processing a long recording. The default runtime configuration is `%LOCALAPPDATA%\SO2KillClips\runtime.json`; task state is kept under `%LOCALAPPDATA%\SO2KillClips\tasks\`. You can override the runtime root with the setup script's `-RuntimeRoot` option.

For the default ZIP installation path, run a dependency check and inspect a short representative recording:

```powershell
$clip = "$env:USERPROFILE\.agents\skills\so2-clipper\scripts\clip.ps1"
& $clip doctor
& $clip inspect --input 'D:\Recordings\match.mp4'
```

`doctor` checks the configured Python environment, OCR imports, acceleration, and media tools. `inspect` reports recognized layouts and returns representative evidence images; it does not confirm every kill. For non-default installation paths, set `$clip` to that installation's `scripts\clip.ps1`.

## AI review and delivery

Give the AI tool a local video or folder and specify any target person, output location, or grouping preference. If no person is specified, the workflow reviews all identifiable current-viewpoint players. A named-player filter requires a confirmed name-to-viewpoint match; an anonymous visual identity is not treated as a named target.

For a batch, the Skill resumes any existing task, scans incrementally, prepares a review plan, creates source-frame evidence, and has the AI inspect each candidate and the surrounding footage. The AI confirms the player, actual kills, same-round boundaries, weapon category, and duplicates before freezing the delivery list. The tool then exports and validates the clips; the AI inspects rendered clip frames and records the review before finalizing delivery. In shorthand:

`status → scan → prepare → evidence → AI visual review → deliver → clip-frame review → deliver`

Default batch output aims for complete multi-kills within the same viewpoint and round, organized by weapon category. A task can request short multi-kill windows, flat output, or another grouping. New users' output defaults to `output/<local date>/` under the input directory. The standard CLI can extract its candidate groups without AI, but full-round completion, final kill count, and weapon classification require visual review.

## Local data

Source recordings, temporary evidence, task state, and rendered clips stay on the local machine. This project has no source-video upload step. An AI host may receive evidence images or other content shown to it; review that host's own privacy and retention settings.

Runtime data is kept separately from the output folder. Do not move or delete task state while a task is running if you expect to resume it. The default output folder is inside the input location; use the task's output option when you want a different destination.

## Limits

Automatic attribution depends on visible HUD evidence and a supported layout. A clipped recording, missing avatar, obscured feed, or abrupt edit can leave the player or boundary uncertain. Those events are reported for review rather than forced into a clip. The scanner does not promise complete kill recall for arbitrary footage. A successful file check or process exit does not by itself confirm content accuracy, playback, audio, or user acceptance.

For the current evidence summary, see [validation status](../skills/so2-clipper/references/validation.md). For implementation ownership, see [source of truth](../SOURCE_OF_TRUTH.md).
