import os
import re
import subprocess
import sys
import textwrap
import requests
import boto3
from botocore.config import Config
from PIL import Image, ImageDraw, ImageFont

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
NOTO_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def run(cmd):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def find_font():
    for p in NOTO_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def callback(status, video_url=None, cover_url=None, cover_options=None, error=None):
    payload = {"task_id": TASK_ID, "secret": RENDER_SECRET, "status": status}
    if video_url:
        payload["video_url"] = video_url
    if cover_url:
        payload["cover_url"] = cover_url
    if cover_options:
        payload["cover_options"] = cover_options
    if error:
        payload["error"] = error[:2000]

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


# ==================== 字幕美化：emoji自动插入 + 关键词高亮 + 逐句换色 ====================
EMOJI_KEYWORDS = [
    ("雪山", "🏔"), ("湖", "🌊"), ("草原", "🌾"), ("沙漠", "🏜"), ("星空", "✨"),
    ("日出", "🌅"), ("日落", "🌇"), ("森林", "🌲"), ("花田", "🌸"), ("手抓饭", "🍚"),
    ("美食", "🍖"), ("拍照", "📸"), ("自驾", "🚗"), ("公路", "🛣"), ("秋", "🍂"),
    ("夏", "☀"), ("冬", "❄"), ("春", "🌱"), ("便宜", "💰"), ("划算", "💰"),
    ("推荐", "👍"), ("打卡", "📍"), ("路线", "🗺"), ("小众", "💎"), ("避坑", "⚠"),
]

HIGHLIGHT_PATTERN = re.compile(r"(\d+%?|最[^，。！？,.!?]{0,4}|第一|唯一)")

# 逐句轮换的强调色（RGB），第一个是默认白色
SUBTITLE_PALETTE_RGB = [(255, 255, 255), (255, 214, 92), (120, 220, 255)]


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


def add_emoji(text):
    """按关键词表给字幕行前面加一个相关emoji，匹配到第一个就够了，不堆砌"""
    for kw, emoji in EMOJI_KEYWORDS:
        if kw in text:
            return f"{emoji} {text}"
    return text


def highlight_line(text, restore_color_tag):
    """把行内第一个数字/最字句/第一/唯一 用高亮色+放大做局部强调，其余文字保持逐句轮换色"""
    m = HIGHLIGHT_PATTERN.search(text)
    if not m:
        return text
    start, end = m.span()
    before, mid, after = text[:start], text[start:end], text[end:]
    highlight_color = rgb_to_ass_bgr((255, 90, 60))  # 强调色：橙红，抓眼球
    return f"{before}{{\\c{highlight_color}\\fscx135\\fscy135}}{mid}{{\\r}}{restore_color_tag}{after}"


def build_beautified_ass(srt_content, out_path):
    """把普通srt字幕升级成带emoji+关键词高亮+逐句换色的ASS字幕文件"""
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
        "Style: Default,Noto Sans CJK SC,52,&HFFFFFF&,&HFFFFFF&,&H000000&,&H000000&,-1,0,0,0,100,100,0,0,"
        "1,3,0,2,60,60,110,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    for i, (start, end, text) in enumerate(cues):
        color_rgb = SUBTITLE_PALETTE_RGB[i % len(SUBTITLE_PALETTE_RGB)]
        color_tag = f"{{\\c{rgb_to_ass_bgr(color_rgb)}}}"
        text_with_emoji = add_emoji(text)
        text_final = color_tag + highlight_line(text_with_emoji, color_tag)
        lines.append(f"Dialogue: 0,{sec_to_ass_time(start)},{sec_to_ass_time(end)},Default,,0,0,0,,{text_final}\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return len(cues)
# ==================== 字幕美化函数结束 ====================


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


def make_cover(concat_path, title_text, out_path):
    """从合成好的画面里截一帧，叠加标题文字做封面图，标题优先用hook，没有就用选题本身。"""
    raw_path = f"{WORKDIR}/cover_raw.jpg"
    run(["ffmpeg", "-y", "-i", concat_path, "-ss", "0.8", "-frames:v", "1", raw_path])

    img = Image.open(raw_path).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size

    font_path = find_font()
    font_size = 64
    font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()

    wrapped = textwrap.fill(title_text, width=12)
    lines = wrapped.split("\n")
    line_height = int(font_size * 1.35)
    block_height = line_height * len(lines) + 80

    # 底部渐深遮罩，保证白字在任何画面背景上都清晰
    overlay = Image.new("RGBA", (w, block_height), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for y in range(block_height):
        alpha = int(160 * (y / block_height))
        odraw.line([(0, y), (w, y)], fill=(0, 0, 0, alpha))
    img.paste(overlay, (0, h - block_height), overlay)

    y_text = h - block_height + 40
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        x = (w - text_w) / 2
        draw.text((x, y_text), line, font=font, fill=(255, 255, 255, 255),
                   stroke_width=3, stroke_fill=(0, 0, 0, 255))
        y_text += line_height

    img.save(out_path, quality=90)


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
    topic = manifest.get("topic") or "新疆旅行"
    task_voice = manifest.get("tts_voice") or TTS_VOICE
    bgm_volume = manifest.get("bgm_volume")
    bgm_volume = float(bgm_volume) if bgm_volume is not None else 0.35
    voice_volume = manifest.get("voice_volume")
    voice_volume = float(voice_volume) if voice_volume is not None else 1.0

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

    # 4. 生成3张不同角度标题的封面图（信息型/情绪型/悬念型），供人工挑选；失败不阻断主流程
    cover_titles = manifest.get("cover_titles") or []
    if not cover_titles:
        cover_titles = [topic]  # AI没给标题候选就退化成只用选题本身生成1张
    cover_paths = []
    for idx, title_text in enumerate(cover_titles[:3]):
        cp = f"{WORKDIR}/cover_{idx}.jpg"
        try:
            make_cover(concat_path, title_text, cp)
            cover_paths.append((title_text, cp))
        except Exception as e:
            print(f"第{idx+1}张封面生成失败，跳过：", e)

    # 5. 字幕美化：把 edge-tts 生成的普通srt升级成带emoji+关键词高亮+逐句换色的ASS字幕，再烧录
    subtitled_path = f"{WORKDIR}/subtitled.mp4"
    ass_path = f"{WORKDIR}/audio.ass"
    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            srt_content = f.read()
        cue_count = build_beautified_ass(srt_content, ass_path)
        print(f"字幕美化完成，共 {cue_count} 条字幕（emoji+关键词高亮+逐句换色）")
        subtitle_filter = f"subtitles={ass_path}"
    except Exception as e:
        # 美化失败就退回最基础的样式，不能因为字幕美化把整条视频搞挂
        print("字幕美化失败，退回基础字幕样式：", e)
        style = "FontName=Noto Sans CJK SC,FontSize=16,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,Alignment=2,MarginV=80"
        subtitle_filter = f"subtitles={srt_path}:force_style='{style}'"

    run([
        "ffmpeg", "-y", "-i", concat_path,
        "-vf", subtitle_filter,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", subtitled_path,
    ])

    # 6. 准备背景音乐：找到就裁剪到配音时长，加淡入淡出（开头1.5秒淡入，结尾2秒淡出）
    bgm_ready_path = None
    if bgm_url:
        try:
            bgm_src_path = f"{WORKDIR}/bgm_src.mp3"
            r = requests.get(bgm_url, timeout=60)
            r.raise_for_status()
            with open(bgm_src_path, "wb") as f:
                f.write(r.content)
            bgm_ready_path = f"{WORKDIR}/bgm.mp3"
            fade_out_start = max(0.5, audio_duration - 2.0)
            fade_out_dur = min(2.0, audio_duration - fade_out_start)
            run([
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", bgm_src_path,
                "-t", str(audio_duration),
                "-af", f"volume={bgm_volume},afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out_start}:d={fade_out_dur}",
                bgm_ready_path,
            ])
        except Exception as e:
            print("背景音乐处理失败，跳过：", e)
            bgm_ready_path = None

    # 7. 合入配音（+背景音乐）输出最终视频。
    # 有背景音乐时用 sidechaincompress 做"人声闪避"：人声一出现就自动压低BGM音量，
    # 人声停顿时BGM自动回升，不需要手动逐句调音量。
    final_path = f"{WORKDIR}/final.mp4"
    if bgm_ready_path:
        run([
            "ffmpeg", "-y", "-i", subtitled_path, "-i", audio_path, "-i", bgm_ready_path,
            "-filter_complex",
            f"[1:a]volume={voice_volume}[voice_boosted];"
            "[voice_boosted]asplit=2[voice_a][voice_b];"
            "[2:a][voice_a]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=300[bgm_ducked];"
            "[voice_b][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]",
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

    # 8. 上传视频（和最多3张封面）到 R2
    s3 = s3_client()
    video_key = f"videos/{TASK_ID}_final.mp4"
    s3.upload_file(final_path, R2_BUCKET_NAME, video_key, ExtraArgs={"ContentType": "video/mp4"})
    video_url = f"{R2_PUBLIC_BASE_URL}/{video_key}"

    cover_options = []
    for idx, (title_text, cp) in enumerate(cover_paths):
        if not os.path.exists(cp):
            continue
        cover_key = f"videos/{TASK_ID}_cover_{idx}.jpg"
        s3.upload_file(cp, R2_BUCKET_NAME, cover_key, ExtraArgs={"ContentType": "image/jpeg"})
        cover_options.append({"title": title_text, "url": f"{R2_PUBLIC_BASE_URL}/{cover_key}"})

    default_cover_url = cover_options[0]["url"] if cover_options else None

    # 9. 通知 Cloudflare 渲染完成
    callback("succeeded", video_url=video_url, cover_url=default_cover_url, cover_options=cover_options)
    print("完成，视频地址:", video_url, "封面数量:", len(cover_options))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("渲染失败:", e, file=sys.stderr)
        callback("failed", error=str(e))
        sys.exit(1)
