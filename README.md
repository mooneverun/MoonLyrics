# MoonLyrics · 月光歌词

为 Windows 版 Apple Music 提供桌面悬浮歌词，以凝润磨砂玻璃呈现流畅的歌词滚动。

**[下载最新版](https://github.com/mooneverun/MoonLyrics/releases/latest)** · [v0.1.2 发布说明](https://github.com/mooneverun/MoonLyrics/releases/tag/v0.1.2) · [反馈问题](https://github.com/mooneverun/MoonLyrics/issues)

## 功能

- 自动跟随 Apple Music 的当前歌曲和播放进度。
- 酷狗、LRCLIB、网易云、QQ 音乐四源自动匹配，可调整优先顺序，无需额外登录。
- 中文歌词统一显示简体，支持跨地区曲名与中英韩歌手别名匹配。
- 歌词居中平滑滚动、上下渐隐，长句横向移动，支持字体切换和时间校准。
- 解锁后悬停显示上一曲、播放/暂停、下一曲，控制区与歌词条共用玻璃效果。
- 成功匹配的歌词自动保存到本地，重启后也可直接读取，支持一键清空。
- 歌词条与设置面板透明度联动，背景框大小可调。
- 支持拖动、锁定、鼠标穿透，以及收起到系统托盘。
- 简体中文、繁體中文、English、日本語、한국어界面，六种快捷位置，刷新上限可设为 120 FPS。

## 使用

1. 从 [Releases](https://github.com/mooneverun/MoonLyrics/releases/latest) 下载 Windows 版 `.exe`，直接运行，无需安装 Python。
2. 打开 Windows 版 Apple Music 并播放歌曲，等待歌词自动匹配。
3. 在设置中调整透明度、歌词源顺序或同步时间；点击“−”收起到托盘，点击托盘月光图标恢复。

运行要求：64 位 Windows 10（2004+）或 Windows 11，显卡支持 OpenGL 3.3。当前已在 Windows 11 验证，Windows 10 尚未实机测试，macOS 版暂未提供。

## 说明

- 首次歌词查询需要网络，缓存命中时无需重新查询；覆盖率和同步精度取决于第三方来源，不使用 Apple Music 的订阅歌词。
- 桌面图像只在本机处理，不保存、不上传；歌词查询会发送歌曲元数据。
- 设置和日志保存在 `%LOCALAPPDATA%\MoonLyrics\`。
- 歌词缓存位于其 `cache\lyrics\` 子目录，自动清理过期和较早使用的内容。

<details>
<summary>从源码运行与打包</summary>

需要 Python 3.12，在项目目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe main.py
.\build.ps1 -Python .\.venv\Scripts\python.exe
```

打包结果：`dist\MoonLyrics.exe`。版本号见 [VERSION](VERSION)。

</details>

## 许可

[GNU GPL v3.0](LICENSE)。
