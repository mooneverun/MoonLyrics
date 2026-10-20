"""Settings text only; song metadata and lyric contents keep their own language."""
import re

from opencc import OpenCC

LANGUAGES = {"zh_CN": "简体中文", "zh_TW": "繁體中文", "en": "English", "ja": "日本語", "ko": "한국어"}
# English, Japanese, Korean. Simplified Chinese is the key; OpenCC supplies Traditional Chinese.
TEXTS = {
    "已找到歌词，但没有时间轴，无法同步滚动": ("Lyrics found, but no timing is available for synchronized scrolling", "歌詞は見つかりましたが、同期用の時間情報がありません", "가사는 찾았지만 동기화에 필요한 시간 정보가 없습니다"),
    "找到候选歌曲，但资料不足以可靠确认录音": ("Candidates found, but there is not enough information to confirm the recording", "候補曲は見つかりましたが、同じ録音と確認できる情報が不足しています", "후보 곡을 찾았지만 같은 녹음인지 확인할 정보가 부족합니다"),
    "候选歌手署名或发行版本尚未核实，未自动采用": ("Candidate credits or release could not be verified; not selected", "候補の歌手表記またはリリースを確認できないため、採用していません", "후보의 아티스트 표기나 발매 정보를 확인하지 못해 선택하지 않았습니다"),
    "月光歌词": ("MoonLyrics", "月光歌詞", "달빛 가사"),
    "MoonLyrics · 设置": ("MoonLyrics · Settings", "MoonLyrics · 設定", "MoonLyrics · 설정"),
    "MoonLyrics · 月光歌词": ("MoonLyrics · Desktop lyrics", "MoonLyrics · デスクトップ歌詞", "MoonLyrics · 데스크톱 가사"),
    "MoonLyrics · 月光歌词设置": ("MoonLyrics · Settings", "MoonLyrics · 設定", "MoonLyrics · 설정"),
    "APPLE MUSIC  /  桌面歌词": ("APPLE MUSIC  /  DESKTOP LYRICS", "APPLE MUSIC  /  デスクトップ歌詞", "APPLE MUSIC  /  데스크톱 가사"),
    "通用设置": ("General", "一般", "일반"),
    "界面语言": ("Language", "表示言語", "표시 언어"),
    "收起到托盘": ("Minimize to tray", "トレイに最小化", "트레이로 최소화"),
    "最大化或还原": ("Maximize or restore", "最大化・元に戻す", "최대화 또는 복원"),
    "关闭": ("Close", "閉じる", "닫기"),
    "打开设置": ("Open settings", "設定を開く", "설정 열기"),
    "退出 MoonLyrics": ("Quit MoonLyrics", "MoonLyrics を終了", "MoonLyrics 종료"),
    "正在播放": ("Now playing", "再生中", "재생 중"),
    "等待 Apple Music": ("Waiting for Apple Music", "Apple Music を待機中", "Apple Music 대기 중"),
    "打开 Apple Music 并播放一首歌曲": ("Open Apple Music and play a song", "Apple Music で曲を再生してください", "Apple Music에서 노래를 재생하세요"),
    "Apple Music 播放进度": ("Apple Music playback progress", "Apple Music の再生位置", "Apple Music 재생 진행률"),
    "已暂停": ("Paused", "一時停止中", "일시 정지됨"),
    "已停止": ("Stopped", "停止中", "정지됨"),
    "正在切换歌曲": ("Changing song", "曲を切り替え中", "곡 전환 중"),
    "等待播放": ("Waiting for playback", "再生待機中", "재생 대기 중"),
    "Apple Music 已关闭": ("Apple Music is closed", "Apple Music は終了しています", "Apple Music이 종료됨"),
    "暂时无法读取，正在重试": ("Temporarily unavailable; retrying", "一時的に読み取れません。再試行中", "일시적으로 읽을 수 없어 재시도 중"),
    "播放音乐后显示歌曲与进度": ("Play music to see the song and progress", "再生すると曲名と再生位置を表示します", "음악을 재생하면 곡과 진행 상황이 표시됩니다"),
    "歌词匹配": ("Lyric matching", "歌詞の検索", "가사 검색"),
    "曲目地区": ("Catalog region", "カタログの地域", "카탈로그 지역"),
    "土耳其": ("Türkiye", "トルコ", "튀르키예"),
    "中国大陆": ("China mainland", "中国本土", "중국 본토"),
    "香港": ("Hong Kong", "香港", "홍콩"),
    "台湾": ("Taiwan", "台湾", "대만"),
    "美国": ("United States", "米国", "미국"),
    "仅用于跨地区曲名匹配，不会修改 Apple Music 账号地区。": ("Used to match song titles across regions. Your Apple Music account region stays unchanged.", "地域ごとの曲名照合に使用します。Apple Music アカウントの地域は変更しません。", "지역별 곡명 검색에 사용합니다. Apple Music 계정 지역은 변경하지 않습니다."),
    "重新匹配": ("Match again", "再検索", "다시 검색"),
    "成功匹配的歌词会自动缓存，下次播放直接读取。": ("Matched lyrics are cached locally for the next playback.", "取得した歌詞は保存され、次回の再生時に読み込まれます。", "찾은 가사는 로컬에 저장되어 다음 재생 시 바로 불러옵니다."),
    "清空歌词缓存": ("Clear lyric cache", "歌詞キャッシュを消去", "가사 캐시 비우기"),
    "清空本地歌词缓存，不影响正在显示的歌词。": ("Clears saved lyrics without interrupting the lyrics on screen.", "表示中の歌詞に影響せず、保存した歌詞を消去します。", "화면에 표시 중인 가사에 영향을 주지 않고 저장된 가사를 지웁니다."),
    "正在清空歌词缓存…": ("Clearing lyric cache…", "歌詞キャッシュを消去中…", "가사 캐시를 비우는 중…"),
    "歌词缓存已清空": ("Lyric cache cleared", "歌詞キャッシュを消去しました", "가사 캐시를 비웠습니다"),
    "缓存暂时无法清空，请稍后重试": ("Could not clear the cache; try again later", "キャッシュを消去できません。後でもう一度お試しください", "캐시를 비울 수 없습니다. 잠시 후 다시 시도해 주세요"),
    "可点击重新匹配获取当前歌曲的歌词": ("Use Match again to find lyrics for the current song", "再検索で現在の曲の歌詞を取得できます", "다시 검색을 눌러 현재 곡의 가사를 찾을 수 있습니다"),
    "歌词源顺序 · 从左到右优先": ("Source priority · left to right", "歌詞ソースの優先順 · 左から右", "가사 소스 우선순위 · 왼쪽부터"),
    "第 {position} 优先歌词源": ("Lyric source priority {position}", "優先順位 {position} の歌詞ソース", "가사 소스 우선순위 {position}"),
    "按从左到右的顺序自动匹配，未找到时继续尝试下一家。\n选择已有来源会交换位置，并重新匹配当前歌曲。": ("Searches from left to right, moving to the next source if needed.\nSelecting an existing source swaps positions and matches the current song again.", "左から順に検索し、見つからない場合は次のソースを試します。\n選択済みのソースを選ぶと順序を入れ替え、現在の曲を再検索します。", "왼쪽부터 검색하고 찾지 못하면 다음 소스로 넘어갑니다.\n이미 선택된 소스를 고르면 순서를 바꾸고 현재 곡을 다시 검색합니다."),
    "酷狗": ("Kugou", "Kugou", "Kugou"),
    "网易云": ("NetEase", "NetEase", "NetEase"),
    "网易云音乐": ("NetEase Music", "NetEase Music", "NetEase Music"),
    "QQ音乐": ("QQ Music", "QQ Music", "QQ Music"),
    "播放歌曲后自动匹配同步歌词": ("Timed lyrics will be matched when a song plays", "曲を再生すると同期歌詞を自動検索します", "곡을 재생하면 동기화 가사를 자동 검색합니다"),
    "等待歌曲信息稳定…": ("Waiting for song details…", "曲情報の確定を待機中…", "곡 정보 확인 중…"),
    "正在匹配歌词…": ("Matching lyrics…", "歌詞を検索中…", "가사 검색 중…"),
    "等待完整的歌曲信息": ("Waiting for complete song details", "曲情報の取得を待機中", "전체 곡 정보 대기 중"),
    "歌词匹配失败，可点击重新匹配": ("Lyric matching failed; try Match again", "歌詞の検索に失敗しました。再検索できます", "가사 검색에 실패했습니다. 다시 검색해 주세요"),
    "部分曲目或歌词查询暂未完成，可重新匹配": ("Some song or lyric requests are incomplete; try again", "曲または歌詞の取得が一部未完了です。再検索できます", "일부 곡 또는 가사 조회가 완료되지 않았습니다. 다시 검색해 주세요"),
    "未找到可靠的同步歌词": ("No reliable timed lyrics found", "信頼できる同期歌詞が見つかりません", "신뢰할 수 있는 동기화 가사를 찾지 못했습니다"),
    "歌词服务暂时不可用，可点击重新匹配": ("Lyric services are temporarily unavailable; try again", "歌詞サービスを一時的に利用できません。再検索できます", "가사 서비스를 일시적으로 사용할 수 없습니다. 다시 검색해 주세요"),
    "已匹配 · {source} · {count} 行{cached}": ("Matched · {source} · {count} lines{cached}", "取得済み · {source} · {count} 行{cached}", "검색 완료 · {source} · {count}줄{cached}"),
    "（缓存）": (" (cached)", "（キャッシュ）", " (캐시)"),
    "{message}；{seconds} 秒后自动重试（{attempt}/{total}）": ("{message}; retrying in {seconds}s ({attempt}/{total})", "{message}。{seconds} 秒後に再試行（{attempt}/{total}）", "{message}; {seconds}초 후 재시도 ({attempt}/{total})"),
    "{message}（已重试 {count} 次）": ("{message} (retried {count} times)", "{message}（{count} 回再試行済み）", "{message} ({count}회 재시도함)"),
    "歌词校准": ("Lyric timing", "歌詞のタイミング", "가사 타이밍"),
    "提前 0.1 秒": ("Earlier by 0.1s", "0.1 秒早める", "0.1초 빠르게"),
    "延后 0.1 秒": ("Later by 0.1s", "0.1 秒遅らせる", "0.1초 늦게"),
    "提前 {seconds:.1f} 秒": ("{seconds:.1f}s earlier", "{seconds:.1f} 秒早く", "{seconds:.1f}초 빠르게"),
    "延后 {seconds:.1f} 秒": ("{seconds:.1f}s later", "{seconds:.1f} 秒遅く", "{seconds:.1f}초 늦게"),
    "标准": ("Standard", "標準", "표준"),
    "重置": ("Reset", "リセット", "초기화"),
    "以标准时序为基准微调，重置恢复标准。\n只校准歌词，不改变歌曲播放进度。": ("Fine-tune relative to standard timing. Reset restores the standard.\nOnly lyrics are adjusted; playback stays unchanged.", "標準タイミングを基準に微調整します。リセットで標準に戻ります。\n歌詞のみを調整し、曲の再生位置は変更しません。", "표준 타이밍을 기준으로 조절합니다. 초기화하면 표준으로 돌아갑니다.\n가사만 조절하며 음악 재생 위치는 바꾸지 않습니다."),
    "玻璃外观": ("Glass appearance", "ガラスの外観", "유리 효과"),
    "细腻的浓磨砂，让繁杂背景更安静。": ("Soft frosted glass for a calmer background.", "きめ細かなすりガラスで、背景を落ち着いた印象に。", "섬세한 반투명 유리로 배경을 차분하게 만듭니다."),
    "背景透明度": ("Transparency", "背景の透明度", "배경 투명도"),
    "玻璃条宽度": ("Lyric bar width", "歌詞バーの幅", "가사 바 너비"),
    "玻璃条高度": ("Lyric bar height", "歌詞バーの高さ", "가사 바 높이"),
    "同步调节歌词条和设置面板：1 更浓，10 更通透，切换时柔和过渡。\n设置面板保留系统磨砂，文字与按钮不会一起变淡。": ("Adjusts the lyric bar and settings together: 1 is denser, 10 is clearer.\nText and buttons stay readable while transparency transitions smoothly.", "歌詞バーと設定画面を同時に調整します。1 は濃く、10 は透明感が高くなります。\n滑らかに切り替わり、文字やボタンは薄くなりません。", "가사 바와 설정 창을 함께 조절합니다. 1은 진하게, 10은 투명하게 표시합니다.\n부드럽게 전환되며 글자와 버튼은 흐려지지 않습니다."),
    "快捷位置": ("Quick position", "クイック配置", "빠른 위치 설정"),
    "左上": ("Top left", "左上", "왼쪽 위"),
    "顶部居中": ("Top center", "上中央", "위쪽 가운데"),
    "右上": ("Top right", "右上", "오른쪽 위"),
    "左下": ("Bottom left", "左下", "왼쪽 아래"),
    "底部居中": ("Bottom center", "下中央", "아래쪽 가운데"),
    "右下": ("Bottom right", "右下", "오른쪽 아래"),
    "刷新上限": ("Frame rate limit", "フレームレート上限", "프레임 속도 제한"),
    "刷新上限不代表实际帧率，实际表现取决于桌面取样速度与设备性能。": ("This sets the target limit. Actual frame rate depends on desktop capture speed and your device.", "設定値は上限です。実際のフレームレートは画面取得速度と端末の性能に依存します。", "설정값은 상한입니다. 실제 프레임 속도는 화면 캡처 속도와 기기 성능에 따라 달라집니다."),
    "暂停刷新": ("Pause refresh", "更新を一時停止", "새로 고침 일시 정지"),
    "继续刷新": ("Resume refresh", "更新を再開", "새로 고침 재개"),
    "重试刷新": ("Retry refresh", "更新を再試行", "새로 고침 재시도"),
    "锁定悬浮条，允许鼠标穿透（在这里解锁）": ("Lock bar and let clicks pass through (unlock here)", "歌詞バーを固定し、クリックを背面に通す（ここで解除）", "가사 바 잠금 및 클릭 통과 허용 (여기서 잠금 해제)"),
    "按住歌词条可拖动；锁定后鼠标可穿透。透明度同时作用于歌词条和设置面板。": ("Drag the lyric bar to move it. Lock it to let clicks pass through. Transparency applies to both windows.", "歌詞バーをドラッグして移動できます。固定するとクリックが背面に通ります。透明度は歌詞バーと設定画面に適用されます。", "가사 바를 드래그하여 이동합니다. 잠그면 클릭이 뒤로 통과합니다. 투명도는 가사 바와 설정 창에 함께 적용됩니다."),
    "运行状态": ("Runtime status", "動作状況", "실행 상태"),
    "版本：{version}": ("Version: {version}", "バージョン：{version}", "버전: {version}"),
    "正在测量 CPU、内存与实际帧率…": ("Measuring CPU, memory and frame rate…", "CPU、メモリ、フレームレートを測定中…", "CPU, 메모리 및 프레임 속도 측정 중…"),
    "正在识别显卡…": ("Detecting graphics adapter…", "グラフィックスを確認中…", "그래픽 장치 확인 중…"),
    "正在初始化显卡…": ("Initializing graphics…", "グラフィックスを初期化中…", "그래픽 초기화 중…"),
    "CPU  {cpu:.2f}%     内存  {memory_mb:.1f} MB     实际帧率  {fps:.1f} FPS": ("CPU  {cpu:.2f}%     Memory  {memory_mb:.1f} MB     Actual  {fps:.1f} FPS", "CPU  {cpu:.2f}%     メモリ  {memory_mb:.1f} MB     実測  {fps:.1f} FPS", "CPU  {cpu:.2f}%     메모리  {memory_mb:.1f} MB     실제  {fps:.1f} FPS"),
    "桌面取样  {capture_ms:.2f} ms / 帧     GPU 材质绘制  {gpu}": ("Desktop capture  {capture_ms:.2f} ms/frame     GPU rendering  {gpu}", "画面取得  {capture_ms:.2f} ms/フレーム     GPU 描画  {gpu}", "화면 캡처  {capture_ms:.2f} ms/프레임     GPU 렌더링  {gpu}"),
    "显卡：{renderer}": ("Graphics: {renderer}", "グラフィックス：{renderer}", "그래픽: {renderer}"),
    "桌面画面仅在本机处理，不保存、不上传。\n歌词匹配会向曲目和歌词服务查询歌名、歌手与时长。\nCPU 为整机归一化占比；GPU 显示材质绘制耗时。": ("Desktop images are processed locally, never saved or uploaded.\nLyric searches send the song title, artist and duration to catalog and lyric services.\nCPU is normalized across all cores; GPU shows rendering time.", "画面は端末内でのみ処理し、保存・送信しません。\n歌詞検索では曲名、アーティスト、曲の長さを外部サービスに問い合わせます。\nCPU は全コア基準の使用率、GPU は描画時間です。", "화면은 기기 내에서만 처리하며 저장하거나 업로드하지 않습니다.\n가사 검색 시 곡명, 아티스트, 재생 시간을 외부 서비스에 전송합니다.\nCPU는 전체 코어 기준 사용률이며 GPU는 렌더링 시간입니다."),
    "拖动后方的网页或播放视频，可检验真实背景取样。": ("Move a window behind the bar to see the live background.", "背面のウィンドウを動かすと背景の更新を確認できます。", "가사 바 뒤의 창을 움직이면 실시간 배경을 확인할 수 있습니다."),
    "桌面取样暂时不可用，正在自动恢复…": ("Desktop capture is unavailable; recovering automatically…", "画面を一時的に取得できません。自動復旧中…", "화면 캡처를 일시적으로 사용할 수 없어 자동 복구 중…"),
    "桌面取样已恢复。": ("Desktop capture recovered.", "画面取得が復旧しました。", "화면 캡처가 복구되었습니다."),
    "已停止刷新：{message}": ("Refresh stopped: {message}", "更新を停止しました：{message}", "새로 고침 중지: {message}"),
    "正在结束后台请求…": ("Finishing background requests…", "バックグラウンド処理を終了中…", "백그라운드 요청 종료 중…"),
    "当前系统无法将玻璃条排除在桌面取样之外，已停止刷新以避免画面递归。": ("This system cannot exclude the bar from capture. Refresh stopped to prevent visual feedback.", "歌詞バーを画面取得から除外できないため、映像の再帰を防ぐ目的で更新を停止しました。", "가사 바를 화면 캡처에서 제외할 수 없어 화면 반복을 방지하기 위해 새로 고침을 중지했습니다."),
    "无效的桌面取样尺寸": ("Invalid desktop capture size", "画面取得サイズが無効です", "화면 캡처 크기가 올바르지 않습니다"),
    "玻璃条位于显示器范围外": ("Lyric bar is outside the display", "歌詞バーが画面外にあります", "가사 바가 화면 밖에 있습니다"),
    "无法创建显卡绘制缓冲区": ("Could not create graphics buffers", "描画バッファを作成できません", "그래픽 버퍼를 만들 수 없습니다"),
    "无法创建模糊缓冲区": ("Could not create blur buffers", "ぼかしバッファを作成できません", "흐림 효과 버퍼를 만들 수 없습니다"),
    "MoonLyrics 无法启动": ("MoonLyrics could not start", "MoonLyrics を起動できません", "MoonLyrics를 시작할 수 없습니다"),
}
_traditional = OpenCC("s2t")


def translate(language, text, **values):
    if language == "zh_TW":
        result = _traditional.convert(text)
    elif language in ("en", "ja", "ko") and text in TEXTS:
        result = TEXTS[text][("en", "ja", "ko").index(language)]
    else:
        result = text
    return result.format(**values) if values else result


def translate_message(language, message):
    """Render existing worker status messages without changing search or cache behavior."""
    retry = re.fullmatch(r"(.+)；([\d.]+) 秒后自动重试（(\d+)/(\d+)）", message)
    if retry:
        text, seconds, attempt, total = retry.groups()
        return translate(language, "{message}；{seconds} 秒后自动重试（{attempt}/{total}）",
                         message=translate_message(language, text), seconds=seconds, attempt=attempt, total=total)
    retried = re.fullmatch(r"(.+)（已重试 (\d+) 次）", message)
    if retried:
        text, count = retried.groups()
        return translate(language, "{message}（已重试 {count} 次）", message=translate_message(language, text), count=count)
    matched = re.fullmatch(r"已匹配 · (.+) · (\d+) 行(（缓存）)?", message)
    if matched:
        source, count, cached = matched.groups()
        return translate(language, "已匹配 · {source} · {count} 行{cached}", source=translate(language, source),
                         count=count, cached=translate(language, "（缓存）") if cached else "")
    return translate(language, message)
