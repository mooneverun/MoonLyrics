# MoonLyrics · 月光歌词

以柔和、通透的玻璃材质呈现桌面悬浮歌词。

当前版本是 **Windows 玻璃界面原型**，保留已经实际试用确认的视觉效果。Apple Music 播放信息读取、歌词搜索与同步滚动尚未接入；窗口内目前显示示例文字。

## 已有功能

- 清透、柔光、凝润三种玻璃风格；柔光与凝润使用均匀高斯模糊。
- 背景透明度 1–10，沿用原型 90–100% 的范围，支持约 240 ms 平滑过渡。
- 拖动悬浮条、锁定与鼠标穿透，调节背景框宽度与高度。
- 真实桌面局部取样、边缘折射与高光；悬浮条自身排除在取样之外。
- 15 / 30 / 60 FPS 刷新上限、暂停刷新和 CPU / 内存 / 绘制耗时显示。
- 桌面图像在本机处理，不保存、不上传。

## 运行

已在 Windows、Python 3.12 和 OpenGL 3.3 环境验证。运行原型不需要 Apple Music 登录。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

设置和日志位于 `%LOCALAPPDATA%\MoonLyrics\`。首次启动可读取旧玻璃原型的设置，之后保存到 MoonLyrics 自己的目录。

## 检查与打包

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe main.py --self-test
.\build.ps1 -Python .\.venv\Scripts\python.exe
```

打包结果为 `dist\MoonLyrics.exe`。渲染自检会短暂显示独立的测试图案窗口，检查细条纹模糊、背景取样、透明度动画及实际绘制性能；需要交互式 Windows 桌面，不能用无界面测试替代。

## 开发基础

- `moonlyrics/styles.py`：与平台无关的玻璃风格、透明度映射与设置校验。
- `moonlyrics/windows.py`：当前 Windows 桌面取样、渲染、悬浮窗口和设置界面。
- `main.py`：启动入口。

接下来先接入 Apple Music 的当前曲目与播放进度，再实现歌词来源、中文及跨地区曲名匹配，最后接入背景框内居中滚动、上下渐隐的歌词展示。macOS 原生版本尚未实现，后续保留独立的平台实现，共用歌词与设置规则。

## 开源许可

采用 [GNU GPL v3.0](LICENSE)。当前界面依赖 PyQt6，其开源版本采用 GPL v3，详见 [Riverbank 官方说明](https://www.riverbankcomputing.com/software/pyqt/intro)。其他依赖遵循各自的许可证。
