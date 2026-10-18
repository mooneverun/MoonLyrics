# MoonLyrics · 月光歌词

以柔和、通透的玻璃材质呈现桌面悬浮歌词。

当前版本是 **Windows 播放信息预览版**：保留已经实际试用确认的玻璃效果，接入 Windows 版 Apple Music 的当前歌曲与播放进度。歌词搜索与同步滚动尚未接入，悬浮条目前显示歌曲、歌手、播放状态和时间。

## 已有功能

- 自动跟随 Apple Music，显示歌曲、歌手、专辑、播放状态、当前进度与总时长。
- 暂停时冻结进度，恢复、切歌及拖动进度后重新校准；不会跟随浏览器等其他播放器。
- 媒体读取在后台线程运行，系统事件与半秒检查配合本地时钟平滑显示进度。
- 清透、柔光、凝润三种玻璃风格；柔光与凝润使用均匀高斯模糊。
- 背景透明度 1–10，沿用原型 90–100% 的范围，支持约 240 ms 平滑过渡。
- 拖动悬浮条、锁定与鼠标穿透，调节背景框宽度与高度。
- 真实桌面局部取样、边缘折射与高光；悬浮条自身排除在取样之外。
- 15 / 30 / 60 FPS 刷新上限、暂停刷新和 CPU / 内存 / 绘制耗时显示。
- 桌面图像在本机处理，不保存、不上传。

## 运行

使用 64 位 Windows 10（2004 或更新版本）/ Windows 11，显卡驱动需要支持 OpenGL 3.3。开发环境使用 Python 3.12。播放信息来自本机 Apple Music 的 Windows 媒体会话，MoonLyrics 不需要额外的 Apple ID 登录。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

打开 Windows 版 Apple Music 并播放歌曲。悬浮条与设置窗口会同时显示播放信息；未检测到会话时显示等待状态，Apple Music 启动后会自动连接。

设置和日志位于 `%LOCALAPPDATA%\MoonLyrics\`。首次启动可读取旧玻璃原型的设置，之后保存到 MoonLyrics 自己的目录。

## 检查与打包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe main.py --self-test
.\.venv\Scripts\python.exe main.py --media-probe
.\build.ps1 -Python .\.venv\Scripts\python.exe
```

打包结果为 `dist\MoonLyrics.exe`。渲染自检会短暂显示独立的测试图案窗口，检查细条纹模糊、背景取样、透明度动画及实际绘制性能；需要交互式 Windows 桌面，不能用无界面测试替代。

`--media-probe` 会只读检查约 5 秒的真实媒体会话，不控制 Apple Music 播放。可增加 `--report <路径>` 保存诊断结果，其中包含当前歌曲信息；分享前请自行检查。

## 开发基础

- `moonlyrics/styles.py`：与平台无关的玻璃风格、透明度映射与设置校验。
- `moonlyrics/playback.py`：与平台无关的播放快照、进度时钟与曲目信息处理。
- `moonlyrics/windows_media.py`：Windows 媒体会话读取、事件订阅与后台线程。
- `moonlyrics/windows.py`：当前 Windows 桌面取样、渲染、悬浮窗口和设置界面。
- `main.py`：启动入口。

接下来实现歌词来源、中文及跨地区曲名匹配，再接入背景框内居中滚动、上下渐隐的歌词展示。macOS 原生版本尚未实现，后续保留独立的平台实现，共用歌词、播放时钟与设置规则。

## 开源许可

采用 [GNU GPL v3.0](LICENSE)。当前界面依赖 PyQt6，其开源版本采用 GPL v3，详见 [Riverbank 官方说明](https://www.riverbankcomputing.com/software/pyqt/intro)。其他依赖遵循各自的许可证。
