"""Native layout measured against the approved 624 px settings mockup."""
from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QComboBox, QFontComboBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QMenu, QProgressBar, QPushButton, QSizePolicy, QSlider,
    QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from .styles import APPEARANCE_MODES, DEFAULTS, FPS_OPTIONS, LIMITS, LYRIC_SOURCES, PRESETS
from .i18n import LANGUAGES

SHORT_MODES = {'day':'日间', 'night':'夜间', 'system':'跟随系统'}
POSITION_LABELS = ('左上', '上方', '右上', '左下', '下方', '右下')


def label(text='', name='', height=21):
    item = QLabel(text)
    if name: item.setObjectName(name)
    item.setFixedHeight(height)
    return item


def row(spacing=8):
    layout = QHBoxLayout()
    layout.setContentsMargins(0,0,0,0); layout.setSpacing(spacing)
    return layout


def button(text='', name='settingsButton', height=38):
    item = QPushButton(text)
    item.setObjectName(name); item.setFixedHeight(height)
    return item


def combo(name='settingsSelect', width=None):
    item = QComboBox(); item.setObjectName(name); item.setFixedHeight(38)
    if width is not None: item.setFixedWidth(width)
    return item


def build_settings(c, settings, media):
    c.ui_icons = list(c.chrome_icons)
    general = QFrame(); general.setObjectName('settingsGeneral')
    top = QVBoxLayout(general); top.setContentsMargins(0,0,0,0); top.setSpacing(0)
    appearance = row(12); appearance.addWidget(label('外观', 'settingsHeading'))
    appearance.addStretch()
    group = QFrame(); group.setObjectName('appearanceSegment'); group.setFixedSize(252,42)
    choices = QHBoxLayout(group); choices.setContentsMargins(4,4,4,4); choices.setSpacing(3)
    c.appearance = QComboBox(c); c.appearance.setAccessibleName('外观模式')
    for mode, text in APPEARANCE_MODES.items(): c.appearance.addItem(text, mode)
    c.appearance.setCurrentIndex(c.appearance.findData(settings['appearance_mode'])); c.appearance.hide()
    c.appearance.currentIndexChanged.connect(c.appearance_changed)
    c.appearance_buttons = {}
    for mode, icon in (('day','sun'),('night','moon'),('system','monitor')):
        item = button(SHORT_MODES[mode], 'appearanceChoice', 32)
        item.setCheckable(True); item.setAutoExclusive(True)
        item.setChecked(mode==settings['appearance_mode']); item.setIconSize(QSize(16,16))
        item.clicked.connect(lambda checked=False, value=mode: c.appearance.setCurrentIndex(c.appearance.findData(value)))
        c.appearance_buttons[mode] = item; c.ui_icons.append((item,icon,16,'choice'))
        choices.addWidget(item, 96 if mode=='system' else 70)
    appearance.addWidget(group); top.addLayout(appearance)
    top.addSpacing(6)
    c.appearance_caption = label('', 'secondary',18)
    c.appearance_caption.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
    top.addWidget(c.appearance_caption); top.addSpacing(16)
    c.content_layout.addWidget(general)

    playing = QFrame(); playing.setObjectName('nowPlayingCard'); playing.setFixedHeight(155)
    play = QVBoxLayout(playing); play.setContentsMargins(18,17,18,18); play.setSpacing(0)
    head = row(); head.addWidget(label('正在播放','playingEyebrow',17)); head.addStretch()
    head.addWidget(label('● Apple Music','playingConnection',17)); play.addLayout(head); play.addSpacing(8)
    track = row(10); track_text = QVBoxLayout(); track_text.setSpacing(4); track_text.setContentsMargins(0,0,0,0)
    c.track_title = label('等待 Apple Music','trackTitle',29)
    c.track_title.setTextFormat(Qt.TextFormat.PlainText)
    c.track_title.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
    c.track_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    c.track_details = label('打开 Apple Music 并播放一首歌曲','trackDetails',20)
    c.track_details.setTextFormat(Qt.TextFormat.PlainText)
    c.track_details.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
    track_text.addWidget(c.track_title); track_text.addWidget(c.track_details); track.addLayout(track_text,1)
    transport=row(4)
    c.settings_media_buttons = {}
    for action in ('previous','toggle','next'):
        item = QToolButton(); item.setObjectName('settingsPlay' if action=='toggle' else 'settingsTransport')
        item.setFixedSize(36 if action=='toggle' else 30,36 if action=='toggle' else 34)
        item.setIconSize(QSize(16,16)); item.clicked.connect(lambda checked=False, name=action:c.send_media_command(name))
        c.settings_media_buttons[action] = item; transport.addWidget(item)
    track.addLayout(transport);play.addLayout(track);play.addSpacing(12)
    times = row(); c.media_status=label('等待 Apple Music','playingTime',17); c.media_time=label('0:00 / --:--','playingTime',17)
    times.addWidget(c.media_status); times.addStretch(); times.addWidget(c.media_time); play.addLayout(times)
    play.addSpacing(7)
    c.media_progress=QProgressBar(); c.media_progress.setRange(0,1000); c.media_progress.setTextVisible(False)
    c.media_progress.setFixedHeight(4); c.media_progress.setValue(0); c.media_progress.setAccessibleName('Apple Music 播放进度')
    play.addWidget(c.media_progress); c.content_layout.addWidget(playing)

    def section(text, icon):
        frame=QFrame(); frame.setObjectName('settingsSection')
        box=QVBoxLayout(frame);box.setContentsMargins(0,21,0,20);box.setSpacing(0)
        title=row(9); glyph=QLabel();glyph.setFixedSize(17,21)
        c.ui_icons.append((glyph,icon,17,'secondary'));title.addWidget(glyph)
        title.addWidget(label(text,'sectionTitle',21));title.addStretch()
        box.addLayout(title);box.addSpacing(13);c.content_layout.addWidget(frame)
        return box

    match=section('歌词匹配','audio-lines')
    region=row(8);region.addWidget(label('曲目地区'));region.addStretch()
    c.storefront=combo(width=120)
    for key,text in (('tr','土耳其'),('cn','中国大陆'),('hk','香港'),('tw','台湾'),('us','美国')):c.storefront.addItem(text,key)
    c.storefront.setCurrentIndex(c.storefront.findData(settings['storefront']))
    c.storefront.setToolTip('仅用于跨地区曲名匹配，不会修改 Apple Music 账号地区。')
    c.storefront.currentIndexChanged.connect(c.storefront_changed);region.addWidget(c.storefront)
    c.retry_lyrics=button('重新匹配',height=39);c.retry_lyrics.setEnabled(False);c.retry_lyrics.setIconSize(QSize(16,16))
    c.ui_icons.append((c.retry_lyrics,'refresh-cw',16,'ink'))
    c.retry_lyrics.clicked.connect(lambda:c.request_lyrics(force=True));region.addWidget(c.retry_lyrics);match.addLayout(region)
    match.addSpacing(14);caption=row();caption.addWidget(label('歌词源顺序'));caption.addStretch()
    caption.addWidget(label('从左到右优先','secondary',21));match.addLayout(caption);match.addSpacing(8)
    sources=row(8);c.source_choices=[]
    for position,source in enumerate(settings['lyric_source_order']):
        if position:
            arrow=QLabel();arrow.setFixedSize(14,38);c.ui_icons.append((arrow,'arrow-right',14,'secondary'));sources.addWidget(arrow)
        item=combo('sourceChoice')
        item.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        item.setMinimumContentsLength(4);item.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        for key,text in LYRIC_SOURCES.items():item.addItem(text,key)
        item.setCurrentIndex(item.findData(source));item.setAccessibleName(f'第 {position+1} 优先歌词源')
        item.setToolTip('按从左到右的顺序自动匹配，未找到时继续尝试下一家。\n选择已有来源会交换位置，并重新匹配当前歌曲。')
        item.currentIndexChanged.connect(lambda index,slot=position:c.source_order_changed(slot))
        c.source_choices.append(item);sources.addWidget(item,1)
    match.addLayout(sources);match.addSpacing(10)
    result=row(7);result.addWidget(label('●','statusDot',17))
    c.lyrics_status=label('播放歌曲后自动匹配同步歌词','matchStatus',17)
    c.lyrics_status.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Fixed)
    result.addWidget(c.lyrics_status,1);c.reserve_source=label('','reserveSource',17);result.addWidget(c.reserve_source)
    match.addLayout(result);match.addSpacing(13)
    timing=row();timing.addWidget(label('歌词校准'));timing.addStretch()
    c.lyric_timing=label(c.offset_label(),'secondary',21)
    c.lyric_timing.setToolTip('以标准时序为基准微调，重置恢复标准。\n只校准歌词，不改变歌曲播放进度。')
    timing.addWidget(c.lyric_timing);match.addLayout(timing);match.addSpacing(8)
    calibration=row(8)
    for text,delta,weight in (('提前 0.1 秒',100,100),('延后 0.1 秒',-100,100),('重置',None,65)):
        item=button(text,'calibrationButton');item.clicked.connect(lambda checked=False,value=delta:
            c.change_lyric_offset(value if value is not None else DEFAULTS['lyric_offset_ms']-c.settings['lyric_offset_ms']))
        calibration.addWidget(item,weight)
    match.addLayout(calibration)

    display=section('歌词显示','type')
    fonts=row();fonts.addWidget(label('歌词字体'));fonts.addStretch()
    c.font_choice=QFontComboBox();c.font_choice.setObjectName('settingsFont');c.font_choice.setFixedSize(250,38)
    c.font_choice.setFontFilters(QFontComboBox.FontFilter.ScalableFonts)
    c.font_choice.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    c.font_choice.setMinimumContentsLength(12);c.font_choice.setAccessibleName('歌词字体')
    c.font_choice.setToolTip('使用本机已安装的字体，选择后立即应用；缺字时自动使用系统备用字体。')
    c.font_choice.setCurrentFont(QFont(settings['lyric_font']));c.font_choice.currentFontChanged.connect(c.font_changed)
    fonts.addWidget(c.font_choice);display.addLayout(fonts)
    for key,text in (('width','玻璃条宽度'),('height','玻璃条高度')):
        display.addSpacing(14);sizing=row(13);caption=label(text);caption.setFixedWidth(91);sizing.addWidget(caption)
        slider=QSlider(Qt.Orientation.Horizontal);slider.setFixedHeight(21)
        step=10 if key=='width' else 1;slider.setRange(*(v//step for v in LIMITS[key]));slider.setPageStep(1)
        slider.setValue(settings[key]//step);slider.setAccessibleName(text);caption.setBuddy(slider)
        value=label('','sliderValue');value.setFixedWidth(61);value.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        def change(number,field=key,readout=value,unit=step):
            c.settings[field]=number*unit;readout.setText(f'{number*unit} px')
            c.glass.resize(c.settings['width'],c.settings['height']);c.glass.refresh()
        slider.valueChanged.connect(change);change(slider.value());c.sliders[key]=slider
        sizing.addWidget(slider,1);sizing.addWidget(value);display.addLayout(sizing)
    display.addSpacing(14);position_label=row();position_label.addWidget(label('快捷位置'));position_label.addStretch()
    c.position_label=label('屏幕下方','secondary');position_label.addWidget(c.position_label);display.addLayout(position_label);display.addSpacing(8)
    positions=row(7);c.position_buttons=[]
    for index,(text,icon) in enumerate(zip(POSITION_LABELS,('arrow-up-left','arrow-up','arrow-up-right','arrow-down-left','arrow-down','arrow-down-right'))):
        item=button(text,'positionChoice',36);item.setCheckable(True);item.setAutoExclusive(True);item.setChecked(index==4)
        item.setIconSize(QSize(13,13));c.ui_icons.append((item,icon,13,'position'))
        item.clicked.connect(lambda checked=False,slot=index:c.place_lyrics(slot));positions.addWidget(item,1);c.position_buttons.append(item)
    display.addLayout(positions);display.addSpacing(14)
    refresh=row();refresh.addWidget(label('刷新上限'));refresh.addStretch();c.fps=combo(width=123)
    for fps in FPS_OPTIONS:c.fps.addItem(f'{fps} FPS',fps)
    c.fps.setCurrentIndex(c.fps.findData(settings['fps']));c.fps.currentIndexChanged.connect(c.fps_changed)
    c.fps.setToolTip('刷新上限不代表实际帧率；静止画面按需重绘，背景变化或拖动时恢复刷新。')
    refresh.addWidget(c.fps);display.addLayout(refresh)

    footer=QWidget();footer.setObjectName('runtimeFooter')
    foot=QHBoxLayout(footer);foot.setContentsMargins(0,14,0,17);foot.setSpacing(8)
    c.runtime_button=QPushButton('● 运行状态');c.runtime_button.setObjectName('runtimeButton');c.runtime_button.setFixedHeight(17)
    c.runtime_button.setToolTip('语言、缓存与运行详情');foot.addWidget(c.runtime_button)
    c.runtime_summary=label('CPU —   内存 —   帧率 —','runtimeSummary',17);foot.addWidget(c.runtime_summary,1)
    c.version_label=label('','versionLabel',17);foot.addWidget(c.version_label);c.content_layout.addWidget(footer)

    # Functions absent from the compact mockup stay available in its status menu.
    c.runtime_menu=QMenu(c);c.runtime_menu.setObjectName('runtimeMenu')
    advanced=QFrame();advanced.setObjectName('advancedSettings');advanced.setMinimumWidth(420)
    more=QVBoxLayout(advanced);more.setContentsMargins(18,16,18,16);more.setSpacing(12)
    languages=row();languages.addWidget(QLabel('界面语言'));languages.addStretch();c.language=combo(width=160)
    c.language.setAccessibleName('界面语言')
    for code,text in LANGUAGES.items():c.language.addItem(text,code)
    c.language.setCurrentIndex(c.language.findData(settings['language']));c.language.currentIndexChanged.connect(c.language_changed)
    languages.addWidget(c.language);more.addLayout(languages)
    c.cache_status=QLabel('成功匹配的歌词会自动缓存，下次播放直接读取。');c.cache_status.setObjectName('secondary');c.cache_status.setWordWrap(True);more.addWidget(c.cache_status)
    c.clear_lyrics_cache=button('清空歌词缓存');c.clear_lyrics_cache.setEnabled(media)
    c.clear_lyrics_cache.setToolTip('清空本地歌词缓存，不影响正在显示的歌词。');c.clear_lyrics_cache.clicked.connect(c.clear_cache);more.addWidget(c.clear_lyrics_cache)
    c.pause=button('暂停刷新');c.pause.clicked.connect(c.toggle_pause);more.addWidget(c.pause)
    c.stats=QLabel('正在测量 CPU、内存与实际帧率…');c.timing=QLabel();c.hardware=QLabel('正在识别显卡…')
    for item in (c.stats,c.timing,c.hardware):item.setWordWrap(True);more.addWidget(item)
    c.interaction_hint=QLabel('按住歌词条可拖动；右键歌词条打开设置。最小化后设置窗口隐藏。')
    c.interaction_hint.setObjectName('secondary');c.interaction_hint.setWordWrap(True);more.addWidget(c.interaction_hint)
    c.material_description=QLabel(PRESETS['rich']['description']);c.material_description.setObjectName('secondary');more.addWidget(c.material_description)
    privacy=QLabel('桌面画面仅在本机处理，不保存、不上传。\n歌词匹配会向曲目和歌词服务查询歌名、歌手与时长。\nCPU 为整机归一化占比；GPU 显示材质绘制耗时。')
    privacy.setWordWrap(True);privacy.setObjectName('secondary');more.addWidget(privacy)
    c.status=QLabel(c.runtime_message);c.status.setWordWrap(True);more.addWidget(c.status)
    action=QWidgetAction(c.runtime_menu);action.setDefaultWidget(advanced);c.runtime_menu.addAction(action)
    c.runtime_button.clicked.connect(lambda:c.runtime_menu.popup(c.runtime_button.mapToGlobal(QPoint(0,c.runtime_button.height()))))
