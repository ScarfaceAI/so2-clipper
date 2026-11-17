# 安装与环境

适用 Windows 10／11 x64，Python 3.12 或 3.13。ZIP 中的 `so2-clipper` 是完整 Skill 目录，包含运行时快照，不需要开发者的项目。把完整目录放入当前 AI 工具的 Skill 搜索目录，例如用户的 `.agents/skills/`。同名目录已存在时先比较版本和来源，不覆盖其他作品。

`scripts/clip.ps1` 是日常入口。默认运行配置在 `%LOCALAPPDATA%/SO2KillClips/runtime.json`。不存在时启动 `setup.ps1`。安装会下载 Python（没有合适版本时）、锁定的依赖、FFmpeg（没有可用 FFmpeg／ffprobe 时）及 EasyOCR 模型。录像不会上传。依赖和模型下载需要网络，可能占用数 GB；已有可用环境与模型直接复用。

```powershell
# 全新用户级环境，自动选择 CPU 或可用的 NVIDIA 加速
& '<Skill>/scripts/setup.ps1'
# 指定已确认的 Python 创建独立 CPU 环境；不改系统 Python
& '<Skill>/scripts/setup.ps1' -Python 'C:\Python312\python.exe' -Acceleration cpu
# 复用已经具备 OCR 依赖的明确环境
& '<Skill>/scripts/setup.ps1' -ExistingPython 'D:\venvs\ocr\Scripts\python.exe'
# 已有环境的严格离线自检；不会尝试下载
& '<Skill>/scripts/clip.ps1' -Offline doctor
```

可用 `-RuntimeRoot` 指定安装和状态目录。测试或多环境使用 `SO2_CLIPS_RUNTIME` 指向具体 runtime.json；它只影响当前进程。CPU 默认 1 个 OCR 工作进程；GPU 按实际可用性启用，最多 4 路且不嵌套并发。无 GPU 仍可完成扫描，耗时以实际回执为准。

安装器只在完整的导入、模型初始化与媒体工具自检后写 runtime.json；失败保留诊断日志和可重试的安装目录。安装器不调整执行策略、不修改系统 PATH、不要求管理员、不清理现有模型。Python 版本、实际依赖列表、GPU 结果和媒体工具路径记录在安装回执中。

依赖版本见 `scripts/requirements.txt` 和安装生成的 `installed.txt`。PyTorch 下载源采用 [官方旧版安装说明](https://pytorch.org/get-started/previous-versions/)；Python 安装器来自 [python.org](https://www.python.org/downloads/release/python-31315/)；FFmpeg 下载自 [官方列出的 Windows 构建提供者](https://ffmpeg.org/download.html) [Gyan](https://www.gyan.dev/ffmpeg/builds/)。下载的 Python／FFmpeg 安装包核验固定 SHA-256。FFmpeg 的许可与源码入口随该提供者下载保留；本 ZIP 不捆绑 FFmpeg 二进制。

严格离线首次安装不能补齐缺失依赖。使用已准备好依赖和模型的环境，再执行 `-Offline doctor`。在有 NVIDIA 显卡的电脑上强制 CPU 的测试，不等于另一台无显卡电脑的安装验收。
