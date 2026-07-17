import os
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


def callback(status, video_url=None, cover_url=None, error=None):
    payload = {"task_id": TASK_ID, "secret": RENDER_SECRET, "status": status}
    if video_url:
        payload["video_url"] = video_url
    if cover_url:
        payload["cover_url"] = cover_url
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

    full_text = "。".join(s["narration"] for s in shots if s.get("narration"))

    # 2. edge-tts 生成配音 + 真实语音时间轴字幕（免费）
    audio_path = f"{WORKDIR}/audio.mp3"
    srt_path = f"{WORKDIR}/audio.srt"
    run([
        "edge-tts", "--voice", TTS_VOICE,
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

    # 4. 生成封面图（截一帧+叠加标题文字），失败不阻断主流程
    cover_path = f"{WORKDIR}/cover.jpg"
    cover_url = None
    try:
        make_cover(concat_path, topic, cover_path)
    except Exception as e:
        print("封面生成失败，跳过：", e)
        cover_path = None

    # 5. 烧录字幕
    subtitled_path = f"{WORKDIR}/subtitled.mp4"
    style = "FontName=Noto Sans CJK SC,FontSize=16,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,BorderStyle=1,Outline=2,Alignment=2,MarginV=80"
    run([
        "ffmpeg", "-y", "-i", concat_path,
        "-vf", f"subtitles={srt_path}:force_style='{style}'",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", subtitled_path,
    ])

    # 6. 准备背景音乐（找到就裁剪到配音时长并压低音量，找不到就跳过，不阻断流程）
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
                "-t", str(audio_duration), "-af", "volume=0.18",
                bgm_ready_path,
            ])
        except Exception as e:
            print("背景音乐处理失败，跳过：", e)
            bgm_ready_path = None

    # 7. 合入配音（+背景音乐）输出最终视频
    final_path = f"{WORKDIR}/final.mp4"
    if bgm_ready_path:
        run([
            "ffmpeg", "-y", "-i", subtitled_path, "-i", audio_path, "-i", bgm_ready_path,
            "-filter_complex", "[1:a][2:a]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", final_path,
        ])
    else:
        run([
            "ffmpeg", "-y", "-i", subtitled_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac", "-shortest", final_path,
        ])

    # 8. 上传视频（和封面，如果生成成功）到 R2
    s3 = s3_client()
    video_key = f"videos/{TASK_ID}_final.mp4"
    s3.upload_file(final_path, R2_BUCKET_NAME, video_key, ExtraArgs={"ContentType": "video/mp4"})
    video_url = f"{R2_PUBLIC_BASE_URL}/{video_key}"

    cover_url = None
    if cover_path and os.path.exists(cover_path):
        cover_key = f"videos/{TASK_ID}_cover.jpg"
        s3.upload_file(cover_path, R2_BUCKET_NAME, cover_key, ExtraArgs={"ContentType": "image/jpeg"})
        cover_url = f"{R2_PUBLIC_BASE_URL}/{cover_key}"

    # 9. 通知 Cloudflare 渲染完成
    callback("succeeded", video_url=video_url, cover_url=cover_url)
    print("完成，视频地址:", video_url, "封面地址:", cover_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("渲染失败:", e, file=sys.stderr)
        callback("failed", error=str(e))
        sys.exit(1)
