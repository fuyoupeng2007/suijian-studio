# 随剪 Studio 0.5
随剪是一款本地视频剪辑工作室。导入照片和短视频后，可以按文字要求生成时间线，点击证据帧回看原素材，再调整镜头顺序、字幕、音量和转场，最后用 FFmpeg 导出 MP4。

## 从源码启动

需要 Python 3.10 或以上版本、可从命令行运行的 `ffmpeg` 和 `ffprobe`，以及 Chrome 或 Edge。Windows 下在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe desktop.py
```

打开的本地工作室使用 127.0.0.1。导入自己的素材后，可先用基础模式试剪。视觉理解和文字规划可使用本机已登录的 Codex CLI，或在模型设置中填写兼容 API。云模型会接收用于分析的抽帧和任务文字，并使用对应账号的额度。

## 工程目录

- `web/` 包含时间线、素材预览和剪辑界面。
- `server/` 负责媒体采样、模型交互、任务状态及本地渲染。剪辑规则位于 `server/knowledge/`。
- `tests/` 包含工作流与渲染测试，运行 `python -m unittest discover -s tests`。
- 工作数据默认保存在 `.local/`，可使用 `SUIJIAN_DATA` 指定目录。

界面使用 Lucide 图标，许可证随 [图标文件](web/assets/icons/LICENSE.txt) 提供。
