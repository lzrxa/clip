import os
import re
import subprocess
import sys
import requests
import boto3
from botocore.config import Config

TASK_ID = os.environ["TASK_ID"]
_raw_base = os.environ["PAGES_BASE_URL"].strip().rstrip("/")
if _raw_base.startswith("http://"):
    _raw_base = "https://" + _raw_base[len("http://"):]
elif not _raw_base.startswith("https://"):
    _raw_base = "https://" + _raw_base
PAGES_BASE_URL = _raw_base
RENDER_SECRET = os.environ["RENDER_SECRET"].strip()
R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_PUBLIC_BASE_URL = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")
TTS_VOICE = os.environ.get("TTS_VOICE") or "zh-CN-XiaoxiaoNeural"

WORKDIR = "work"


def run(cmd):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def redact_urls(text):
    """把错误信息里任何完整URL都替换成占位符，避免真实的存储地址/服务器信息被存进数据库、
    展示在网页上给用户看。GitHub Actions日志里print出来的原始信息不受影响（只有管理员能看到
    Actions日志），这里只处理会被callback回传、最终显示在网页任务卡片上的error内容。"""
    if not text:
        return text
    return re.sub(r"https?://\S+", "[链接已隐藏]", str(text))


def callback(status, video_url=None, cover_url=None, cover_options=None, error=None):
    payload = {"task_id": TASK_ID, "secret": RENDER_SECRET, "status": status}
    if video_url:
        payload["video_url"] = video_url
    if cover_url:
        payload["cover_url"] = cover_url
    if cover_options:
        payload["cover_options"] = cover_options
    if error:
        payload["error"] = redact_urls(error)[:2000]

    callback_url = f"{PAGES_BASE_URL}/api/render-callback"
    try:
        resp = requests.post(callback_url, json=payload, timeout=30)
        if resp.history:
            print("警告：这次请求发生了跳转，PAGES_BASE_URL可能配置有误：",
                  " -> ".join(str(r.url) for r in resp.history) + " -> " + resp.url)
        print("回调地址：", callback_url)
        print("回调 HTTP 状态：", resp.status_code)
        print("回调响应内容：", resp.text[:500])
        resp.raise_for_status()
    except Exception as e:
        print("回调失败:", e)


# ==================== 字幕生成：简洁样式，颜色/字号/位置/粗细都可由用户在网页里选择 ====================
# 之前版本做过"emoji自动插入+逐句换色+关键词高亮"，实际使用时emoji经常显示成乱码方块（字体没匹配上），
# 逐句换色也显得杂乱，已经按反馈去掉，改成朴素干净、参数可控的样式。

SUBTITLE_COLOR_MAP = {
    "white": (255, 255, 255),
    "yellow": (255, 214, 92),
    "cyan": (120, 220, 255),
    "red": (255, 90, 60),
}


def rgb_to_ass_bgr(rgb):
    """ASS字幕颜色是 BGR 顺序（不是常见的RGB），这里做一次转换"""
    r, g, b = rgb
    return f"&H{b:02X}{g:02X}{r:02X}&"


def srt_time_to_sec(t):
    h, m, s_ms = t.split(":")
    s, ms = s_ms.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def sec_to_ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def parse_srt(content):
    """把 edge-tts 生成的 .srt 字幕解析成 (开始秒, 结束秒, 文本) 列表"""
    blocks = re.split(r"\n\s*\n", content.strip())
    cues = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue
        time_line_idx = None
        for i, l in enumerate(lines):
            if "-->" in l:
                time_line_idx = i
                break
        if time_line_idx is None:
            continue
        start_str, end_str = [s.strip() for s in lines[time_line_idx].split("-->")]
        text = " ".join(lines[time_line_idx + 1:]).strip()
        if text:
            cues.append((srt_time_to_sec(start_str), srt_time_to_sec(end_str), text))
    return cues


def wrap_subtitle_text(text, font_size, canvas_width=1080, margin=60):
    """中文字幕手动换行：ASS/libass的自动换行是按空格断词的，中文没有空格，
    一整句会被当成一个不可拆分的词，超出画面宽度不会自动折行，而是直接溢出被裁掉。
    这里按字号估算每行大概能放多少个字，优先在标点处断行，找不到合适标点就硬断。"""
    usable_width = canvas_width - margin * 2
    max_chars = max(4, int(usable_width / font_size))
    if len(text) <= max_chars:
        return text

    break_chars = "，。！？、,."
    lines = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars + 1]
        split_pos = -1
        for i in range(len(window) - 1, -1, -1):
            if window[i] in break_chars:
                split_pos = i + 1
                break
        if split_pos <= 0:
            split_pos = max_chars
        lines.append(remaining[:split_pos])
        remaining = remaining[split_pos:]
    if remaining:
        lines.append(remaining)
    return "\\N".join(lines)


def build_title_caption_ass(lines, out_path, duration_sec=4.5, font_size=58):
    """开头顶部大字标题字幕（悬念式大标题样式）：仿照新闻/热门短视频账号常见的开头字幕——
    多行文字叠在画面顶部，白色/金色逐行交替，加粗+黑色描边，只在视频最开头这几秒出现一次，
    之后自动消失，不会跟下面逐句解说的字幕（build_subtitle_ass）冲突——这是完全独立的
    第二层ASS字幕，渲染的时候在ffmpeg里跟主字幕链式叠加(-vf "subtitles=A,subtitles=B")，
    互不干扰。逐句解说字幕本身还是保持"统一一种颜色"不变，多色交替是这层"标题字幕"独有的
    风格，不会影响到解说字幕那边。
    """
    if not lines:
        return False
    font_name = "Noto Sans CJK SC Black"
    white_tag = rgb_to_ass_bgr((255, 255, 255))
    gold_tag = rgb_to_ass_bgr((255, 204, 0))

    def escape_ass_text(t):
        # 花括号在ASS里是"样式覆盖标签"的语法符号，AI生成的文字里万一恰好带了这几个字符，
        # 会把后面的颜色交替标签forcibly打断、甚至让整行文字不显示，这里保险起见过滤掉
        return str(t).replace("{", "").replace("}", "").replace("\\", "").strip()

    styled_parts = []
    for idx, line in enumerate(lines):
        clean_line = escape_ass_text(line)
        if not clean_line:
            continue
        color = white_tag if idx % 2 == 0 else gold_tag
        styled_parts.append(f"{{\\c{color}}}{clean_line}")
    if not styled_parts:
        return False
    text = "\\N".join(styled_parts)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{white_tag},{white_tag},&H000000&,&H000000&,0,0,0,0,100,100,0,0,"
        f"1,3,1,8,60,60,120,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    end_time = sec_to_ass_time(duration_sec)
    dialogue = f"Dialogue: 0,0:00:00.00,{end_time},Default,,0,0,0,,{text}\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + dialogue)
    return True


def build_subtitle_ass(srt_content, out_path, font_size=76, position="bottom", color_key="white", bold=True):
    """生成朴素字幕的ASS文件：统一颜色、统一字号、统一字体粗细，不做逐句变色/emoji/关键词高亮

    font_size: 字号（数字越大字越大）
    position: 'top' / 'middle' / 'bottom'
    color_key: SUBTITLE_COLOR_MAP 里的一个key，如 'white'/'yellow'/'cyan'/'red'
    bold: 是否加粗
    """
    # 'middle' 之前用的是ASS的"5"（正中心）对齐方式，这种对齐是按文字块的几何中心来定位的——
    # 一句话如果换行成1行还是2行，文字块整体高度不一样，"中心"对齐的结果就是每一句字幕的
    # 上下位置都会跟着换行行数跳来跳去，看起来就是"一下子上、一下子下"。
    # 改成跟 bottom 一样"从画面底部往上锚定"的方式（对齐方式"2"），只是把锚点位置挪到画面纵向
    # 中部附近，这样换行再多行，也是从同一个固定基准线往上长，基准位置不会变，视觉上才是稳定的。
    # 具体每一条字幕的锚点还会按这条字幕实际换行行数做微调（见下面循环里的 this_margin_v），
    # 尽量让1行、2行、3行的字幕视觉中心都还是落在画面中部附近，而不是行数越多就整体往下坠。
    position_map = {
        "top": (8, 90),
        "middle": (2, 960),
        "bottom": (2, 110),
    }
    alignment, margin_v = position_map.get(position, position_map["bottom"])
    color_rgb = SUBTITLE_COLOR_MAP.get(color_key, SUBTITLE_COLOR_MAP["white"])
    color_tag = rgb_to_ass_bgr(color_rgb)
    # 加粗时直接换用 Noto Sans CJK 的 Black（最粗）字重，比原来单纯给Bold字重再加ASS粗体标记
    # 视觉效果更扎实清晰；这两个字重都是 SIL Open Font License 完全免费商用，没有版权顾虑。
    # 不加粗则维持原来的常规字重不变。
    font_name = "Noto Sans CJK SC Black" if bold else "Noto Sans CJK SC"
    line_height = int(font_size * 1.2)

    cues = parse_srt(srt_content)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{color_tag},{color_tag},&H000000&,&H000000&,0,0,0,0,100,100,0,0,"
        f"1,3,0,{alignment},60,60,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for start, end, text in cues:
        wrapped_text = wrap_subtitle_text(text, font_size)
        if position == "middle":
            num_lines = wrapped_text.count("\\N") + 1
            this_margin_v = max(50, margin_v - int((num_lines - 1) * line_height / 2))
        else:
            this_margin_v = 0  # 0表示沿用Style里的默认MarginV，不做逐条覆盖
        lines.append(f"Dialogue: 0,{sec_to_ass_time(start)},{sec_to_ass_time(end)},Default,,0,0,{this_margin_v},,{wrapped_text}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return len(cues)
# ==================== 字幕生成函数结束 ====================


def make_shot_clip(i, shot, duration):
    """生成单个镜头的标准化竖屏片段。图片素材加 Ken Burns 缓慢缩放效果，避免死画面。"""
    asset_url = shot["asset_url"]
    asset_type = shot.get("asset_type") or "video"
    ext = "mp4" if asset_type == "video" else "jpg"
    src_path = f"{WORKDIR}/src_{i}.{ext}"
    clip_path = f"{WORKDIR}/clip_{i}.mp4"

    r = requests.get(asset_url, timeout=60)
    r.raise_for_status()
    with open(src_path, "wb") as f:
        f.write(r.content)

    fps = 30
    if asset_type == "video":
        vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"
        run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", src_path,
            "-t", str(duration), "-vf", vf, "-r", str(fps),
            "-an", "-pix_fmt", "yuv420p", clip_path,
        ])
    else:
        frames = max(1, round(duration * fps))
        # Ken Burns：缓慢放大，避免图片素材是一张死画面
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:s=1080x1920:fps={fps},setsar=1"
        )
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", src_path,
            "-vf", vf, "-frames:v", str(frames), "-r", str(fps),
            "-pix_fmt", "yuv420p", clip_path,
        ])
    return clip_path


def main():
    os.makedirs(WORKDIR, exist_ok=True)

    # 1. 拉取该任务的分镜清单（含匹配好的素材、选题、bgm_mood）
    resp = requests.get(
        f"{PAGES_BASE_URL}/api/render-manifest",
        params={"task_id": TASK_ID, "secret": RENDER_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    manifest = resp.json()
    if not manifest.get("ok"):
        raise RuntimeError(manifest.get("message", "获取任务清单失败"))

    shots = manifest["shots"]
    if not shots:
        raise RuntimeError("该任务没有分镜数据")
    bgm_url = manifest.get("bgm_url")
    task_voice = manifest.get("tts_voice") or TTS_VOICE
    bgm_volume = manifest.get("bgm_volume")
    bgm_volume = float(bgm_volume) if bgm_volume is not None else 0.35
    voice_volume = manifest.get("voice_volume")
    voice_volume = float(voice_volume) if voice_volume is not None else 1.0
    subtitle_size = manifest.get("subtitle_size")
    subtitle_size = int(subtitle_size) if subtitle_size else 76
    subtitle_position = manifest.get("subtitle_position") or "bottom"
    subtitle_color = manifest.get("subtitle_color") or "white"
    subtitle_bold = manifest.get("subtitle_bold")
    subtitle_bold = True if subtitle_bold is None else bool(subtitle_bold)
    title_caption_lines = manifest.get("title_caption_lines") or []
    enable_title_caption = manifest.get("enable_title_caption")
    enable_title_caption = True if enable_title_caption is None else bool(enable_title_caption)

    full_text = "。".join(s["narration"] for s in shots if s.get("narration"))

    # 2. edge-tts 生成配音 + 真实语音时间轴字幕（免费），语音优先用任务里选的那个
    audio_path = f"{WORKDIR}/audio.mp3"
    srt_path = f"{WORKDIR}/audio.srt"
    run([
        "edge-tts", "--voice", task_voice,
        "--text", full_text,
        "--write-media", audio_path,
        "--write-subtitles", srt_path,
    ])

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, check=True,
    )
    audio_duration = float(probe.stdout.strip())
    planned_total = sum(float(s["duration_sec"]) for s in shots)
    scale = audio_duration / planned_total if planned_total > 0 else 1.0
    print(f"配音时长 {audio_duration:.2f}s，分镜计划总时长 {planned_total:.2f}s，缩放系数 {scale:.3f}")

    # 3. 逐镜头生成竖屏分段（图片自动加 Ken Burns 缓慢缩放）
    clip_paths = []
    for i, shot in enumerate(shots):
        duration = max(1.0, float(shot["duration_sec"]) * scale)
        clip_paths.append(make_shot_clip(i, shot, duration))

    clip_list_path = f"{WORKDIR}/concat_list.txt"
    with open(clip_list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    concat_path = f"{WORKDIR}/concat.mp4"
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", clip_list_path,
        "-c", "copy", concat_path,
    ])

    # 5. 生成字幕（简洁样式：统一颜色/字号/粗细/位置，都取自用户在网页里的设置）
    subtitled_path = f"{WORKDIR}/subtitled.mp4"
    ass_path = f"{WORKDIR}/audio.ass"
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            srt_content = f.read()
        cue_count = build_subtitle_ass(
            srt_content, ass_path, font_size=subtitle_size, position=subtitle_position,
            color_key=subtitle_color, bold=subtitle_bold,
        )
        print(f"字幕生成完成，共 {cue_count} 条")
        subtitle_filter = f"subtitles={ass_path}"
    except Exception as e:
        # 生成失败就退回最基础的样式，不能因为字幕这一步把整条视频搞挂
        print("字幕生成失败，退回基础字幕样式：", e)
        # "middle"这里也用跟主路径一样的"从底部往上锚定"方式，理由跟build_subtitle_ass里注释的一样：
        # 真正的正中心对齐在换行行数不一致时会显得位置来回跳。这个兜底路径没法像主路径那样逐条
        # 微调，只能给一个折中的固定锚点，但至少比之前的"5"（正中心）稳定
        _pos_map = {"top": (8, 90), "middle": (2, 850), "bottom": (2, 110)}
        _align, _mv = _pos_map.get(subtitle_position, _pos_map["bottom"])
        _color_rgb = SUBTITLE_COLOR_MAP.get(subtitle_color, SUBTITLE_COLOR_MAP["white"])
        _color_tag = rgb_to_ass_bgr(_color_rgb)
        _font_name = "Noto Sans CJK SC Black" if subtitle_bold else "Noto Sans CJK SC"
        style = f"FontName={_font_name},FontSize={subtitle_size},PrimaryColour={_color_tag},OutlineColour=&H000000&,BorderStyle=1,Outline=2,Alignment={_align},MarginV={_mv}"
        subtitle_filter = f"subtitles={srt_path}:force_style='{style}'"

    # 开头顶部大字标题字幕（悬念式大标题样式），是独立于上面逐句解说字幕的第二层，
    # 生成成功就在ffmpeg的滤镜链里跟主字幕串联叠加；不开启/没有内容/生成失败都不影响
    # 主字幕正常工作，这层从设计上就是"锦上添花，出问题就跳过"，不会拖累主流程
    if enable_title_caption and title_caption_lines:
        title_caption_path = f"{WORKDIR}/title_caption.ass"
        try:
            if build_title_caption_ass(title_caption_lines, title_caption_path):
                subtitle_filter = f"{subtitle_filter},subtitles={title_caption_path}"
                print(f"开头标题字幕生成完成，共 {len(title_caption_lines)} 行")
        except Exception as e:
            print("开头标题字幕生成失败，跳过（不影响主字幕和视频合成）：", e)

    run([
        "ffmpeg", "-y", "-i", concat_path,
        "-vf", subtitle_filter,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", subtitled_path,
    ])

    # 6. 准备背景音乐：找到就裁剪到配音时长，按设定音量混入（不做淡入淡出/自动闪避，
    # 保持简单直接的听感，这两个效果之前实际使用体验不好，已经去掉）
    bgm_ready_path = None
    if bgm_url:
        try:
            bgm_src_path = f"{WORKDIR}/bgm_src.mp3"
            r = requests.get(bgm_url, timeout=60)
            r.raise_for_status()
            with open(bgm_src_path, "wb") as f:
                f.write(r.content)
            bgm_ready_path = f"{WORKDIR}/bgm.mp3"
            run([
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", bgm_src_path,
                "-t", str(audio_duration), "-af", f"volume={bgm_volume}",
                bgm_ready_path,
            ])
        except Exception as e:
            print("背景音乐处理失败，跳过：", e)
            bgm_ready_path = None

    # 7. 合入配音（+背景音乐）输出最终视频。人声/BGM各自按设定音量直接混音。
    final_path = f"{WORKDIR}/final.mp4"
    if bgm_ready_path:
        run([
            "ffmpeg", "-y", "-i", subtitled_path, "-i", audio_path, "-i", bgm_ready_path,
            "-filter_complex",
            f"[1:a]volume={voice_volume}[voice_adj];"
            "[voice_adj][2:a]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", final_path,
        ])
    else:
        run([
            "ffmpeg", "-y", "-i", subtitled_path, "-i", audio_path,
            "-filter_complex", f"[1:a]volume={voice_volume}[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", final_path,
        ])

    # 8. 上传视频到 R2
    s3 = s3_client()
    video_key = f"videos/{TASK_ID}_final.mp4"
    s3.upload_file(final_path, R2_BUCKET_NAME, video_key, ExtraArgs={"ContentType": "video/mp4"})
    video_url = f"{R2_PUBLIC_BASE_URL}/{video_key}"

    # 8.5 顺手截一帧做封面图（不叠加标题文字，就是单纯的画面截图，缩小到480宽）。
    # 这张小图会被网页用作<video>标签的poster属性：任务流水线一打开，先加载的是这张几十KB的
    # 小图，不用把每个视频开头那段数据都提前拉下来才能看到画面；真正的视频数据要等用户
    # 点了播放才开始下载，首页打开的速度和省流量效果都会好很多。截图失败不影响视频本身。
    cover_url = None
    try:
        cover_path = f"{WORKDIR}/cover.jpg"
        run(["ffmpeg", "-y", "-ss", "0.8", "-i", final_path, "-frames:v", "1", "-vf", "scale=480:-1", cover_path])
        cover_key = f"videos/{TASK_ID}_cover.jpg"
        s3.upload_file(cover_path, R2_BUCKET_NAME, cover_key, ExtraArgs={"ContentType": "image/jpeg"})
        cover_url = f"{R2_PUBLIC_BASE_URL}/{cover_key}"
    except Exception as e:
        print("封面截图失败，跳过（不影响视频本身）：", e)

    # 9. 通知 Cloudflare 渲染完成
    callback("succeeded", video_url=video_url, cover_url=cover_url)
    print("完成，视频地址:", video_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("渲染失败:", e, file=sys.stderr)
        callback("failed", error=str(e))
        sys.exit(1)
