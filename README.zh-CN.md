# MoonLyrics · 月光歌词

[English](README.md) · 简体中文

为 Windows 版 Apple Music 提供桌面悬浮歌词，支持流畅滚动与液态玻璃效果。

**v0.1.3** · **[下载最新版](https://github.com/mooneverun/MoonLyrics/releases/latest)** · [发布说明](https://github.com/mooneverun/MoonLyrics/releases) · [反馈问题](https://github.com/mooneverun/MoonLyrics/issues)

## 功能

- 自动跟随 Apple Music 的当前歌曲和播放进度。
- 从酷狗、LRCLIB、网易云、QQ 音乐中选择三个启用并排序，其余保留备用、不参与搜索；无需额外登录或 API Key。
- 中文歌词统一显示简体，支持跨地区曲名与中英韩歌手别名匹配。
- 歌词平滑滚动、上下渐隐，长句横向移动；支持字体切换和时间校准，内置 Noto Sans SC Black。
- 悬停显示上一曲、播放/暂停、下一曲，控制区与歌词条共用玻璃效果。
- 成功匹配的歌词自动缓存，重启后也可直接读取，支持一键清空。
- 歌词条和设置面板共用日间、夜间、跟随系统三种外观，跟随系统读取 Windows 的应用明暗设置；背景框大小可调，设置隐藏后停止桌面取样。
- 支持拖动、六种快捷位置，以及右键歌词条打开设置。
- 简体中文、繁體中文、English、日本語、한국어界面，刷新上限可设为 120 FPS；静止画面按需重绘，闲置时自动降频。

## 使用

1. 从 [Releases](https://github.com/mooneverun/MoonLyrics/releases/latest) 下载 Windows 版 `.exe`，直接运行，无需安装 Python。
2. 打开 Windows 版 Apple Music 并播放歌曲，等待歌词自动匹配。
3. 在设置中选择外观、歌词源顺序或同步时间；按住歌词条可拖动，悬停可使用播放控制。最小化后设置窗口从任务栏隐藏，也不创建托盘图标；右键歌词条即可重新打开设置。

运行要求：64 位 Windows 10（2004+）或 Windows 11，显卡支持 OpenGL 3.3。当前已在 Windows 11 验证，Windows 10 尚未实机测试，macOS 版暂未提供。FPS 设置为刷新上限，实际帧率取决于设备性能。

单文件 EXE 会自动选择可写的解压位置。用户数据目录或系统临时目录不可用时，会使用 EXE 旁的 `MoonLyrics-data\runtime\`，退出后清理展开的依赖。复制到其他电脑时只需要 EXE。

## 说明

- 首次歌词查询需要网络，缓存命中时无需重新查询；覆盖率和同步精度取决于第三方来源，不使用 Apple Music 的订阅歌词。
- 桌面图像只在本机处理，不保存、不上传；歌词查询会向曲目和歌词服务发送歌曲元数据。
- 设置和日志保存在 `%LOCALAPPDATA%\MoonLyrics\`；歌词缓存位于其 `cache\lyrics\` 子目录，自动清理过期和较早使用的内容。
- 数据目录无法写入时，会改用 EXE 旁的 `MoonLyrics-data` 文件夹；之后继续使用这份便携配置。

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
打包命令追加 `-Portable` 可生成无需启动时临时解压的便携文件夹，位于 `dist\MoonLyrics\`。

</details>

## 许可

[GNU GPL v3.0](LICENSE)。内置字体单独遵循 [SIL Open Font License](assets/NotoSansSC-OFL.txt)。

液态玻璃渲染参考 [Glasswork](https://github.com/kubai087/glasswork)（`4f0b6744`，[MIT 许可](assets/Glasswork-LICENSE.txt)），采用中性色彩处理和 [Atrium](https://github.com/wynter-yaxley/atrium-desktop) 的连续端头轮廓（`2e51d036`，[MIT 许可](assets/Atrium-LICENSE.txt)）。
