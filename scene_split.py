import os
import re
import json
import time
import base64
import subprocess
import requests
import boto3
from botocore.config import Config

# ==================== 智能拆条：上传一段长视频，自动识别里面不同的场景/地点，
# 每个场景各自截出一段小视频+一张代表图，自动打好标签，作为独立素材导入素材库 ====================
#
# 整体思路：
# 1. 用ffmpeg的"场景切换检测"功能，找出视频里发生"硬切"（镜头突然切换）的时间点——这一步
#    对"剪辑过、有明显分镜切换"的素材效果好（比如一条视频里依次剪了天安门、长城、颐和园），
#    对"一镜到底、镜头缓慢平移带过好几个地方"的连续长镜头效果有限（没有明显的"切"可以检测），
#    这是ffmpeg场景检测本身的技术局限，不是这份脚本的bug
# 2. 每个检测到的片段，取中间那一帧截成图片，交给AI看一眼，判断这一段拍的是哪里/是什么内容，
#    同时让AI判断这一段是不是"有实际内容"（排除掉太短的转场、纯黑场、模糊过渡帧这类没意义的片段）
# 3. 对"有实际内容"的片段，同时截出这一段对应的小视频文件+一张代表图片，各自上传到R2，
#    作为两条独立的素材记录（一条视频、一条图片）导入素材库，标签用AI识别出来的地点/内容

JOB_ID = os.environ["JOB_ID"]
SOURCE_KEY = os.environ["SOURCE_KEY"]
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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")  # 跟触发这个workflow用的是同一个token，不需要额外配置任何东西
TASK_DOMAIN = os.environ.get("TASK_DOMAIN") or "travel"
TASK_REGION = os.environ.get("TASK_REGION") or ""

GITHUB_MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
GITHUB_MODELS_MODEL = "openai/gpt-4.1"  # 跟这套系统其它地方（自动打标签、脚本生成）用的是同一个免费模型，本身支持图片输入

WORKDIR = "work"
# 场景切换检测的敏感度：数值越低越容易被判定成"切换了一个新场景"（片段会切得更碎），
# 数值越高只有变化很剧烈的硬切才会被抓到。0.3是ffmpeg社区比较常用的经验值，兼顾了
# "不要切得太碎"和"不要漏掉明显的镜头切换"
SCENE_THRESHOLD = 0.3
MIN_SEGMENT_SEC = 1.5  # 短于这个时长的片段大概率是转场/噪声，直接跳过不处理
MAX_SEGMENTS = 20  # 单次最多处理这么多个片段，避免一条超长视频把整个任务拖得很久


def run(cmd, check=True):
    print("+ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


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
    if not text:
        return text
    return re.sub(r"https?://\S+", "[链接已隐藏]", str(text))


def callback(status, results=None, error=None):
    payload = {"job_id": JOB_ID, "secret": RENDER_SECRET, "status": status}
    if results is not None:
        payload["results"] = results
    if error:
        payload["error"] = redact_urls(error)[:2000]
    callback_url = f"{PAGES_BASE_URL}/api/scene-split-callback"
    try:
        resp = requests.post(callback_url, json=payload, timeout=30)
        print("回调地址：", callback_url, "状态：", resp.status_code, resp.text[:300])
        resp.raise_for_status()
    except Exception as e:
        print("回调失败:", e)


def get_video_duration(path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(result.stdout.strip())


def detect_scene_cuts(path, duration):
    """用ffmpeg的scene检测滤镜找出镜头切换的时间点，返回一串时间戳（不含0和结尾）。
    只对"有硬切"的素材有效，一镜到底的连续长镜头检测不到明显切换点，这是技术上的
    正常表现，不是bug——这种情况下这条视频会被当成"只有一个场景"整体处理。"""
    result = run([
        "ffmpeg", "-i", path, "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
        "-f", "null", "-",
    ], check=False)
    # showinfo滤镜把每一帧的信息打印到stderr里，用pts_time字段抓出被判定为"场景切换"的时间点
    timestamps = [float(t) for t in re.findall(r"pts_time:([\d.]+)", result.stderr)]
    cuts = sorted(set(round(t, 2) for t in timestamps if 0 < t < duration))
    return cuts


def build_segments(cuts, duration):
    """把切换时间点前后拼成一个个片段的[start, end]区间，过滤掉太短的片段"""
    bounds = [0.0] + cuts + [duration]
    segments = []
    for i in range(len(bounds) - 1):
        start, end = bounds[i], bounds[i + 1]
        if end - start >= MIN_SEGMENT_SEC:
            segments.append((start, end))
    return segments[:MAX_SEGMENTS]


def extract_frame(video_path, timestamp, out_path):
    run(["ffmpeg", "-y", "-ss", str(timestamp), "-i", video_path, "-vframes", "1",
         "-q:v", "3", out_path])


def extract_clip(video_path, start, end, out_path):
    """截取[start,end]这一段视频。优先尝试-c copy（不重新编码，速度快），如果因为关键帧
    没对齐导致copy失败/出来的片段有问题，退回重新编码的方式，牺牲一点速度换稳定性"""
    try:
        run(["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video_path,
             "-c", "copy", "-avoid_negative_ts", "make_zero", out_path])
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            return
    except Exception as e:
        print("直接拷贝截取失败，改用重新编码方式：", e)
    run(["ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video_path,
         "-c:v", "libx264", "-c:a", "aac", out_path])


def recognize_scene(frame_path):
    """把这一帧截图交给AI看一眼，判断拍的是哪里/是什么内容，以及这一段是不是有实际拆分价值。
    走的是GitHub Models这个免费接口（跟这套系统其它地方——自动打标签、脚本生成——用的是
    同一个token、同一个模型），不需要额外注册/配置任何付费API，触发这个workflow本身
    就已经必须配置GITHUB_TOKEN了，这里直接复用，没有增加新的前提条件。免费接口有请求频率
    限制，遇到限流（429）会等几秒重试一次，还是不行就跳过这一帧，不影响其它片段继续处理。"""
    if not GITHUB_TOKEN:
        return None
    with open(frame_path, "rb") as f:
        image_base64 = base64.b64encode(f.read()).decode("utf-8")

    system_prompt = (
        "你是短视频素材管理专家，帮忙识别视频截图里的场景内容，用于素材库自动打标签、自动分类。"
        "请判断这张图片是否值得作为一条独立素材保留（排除纯黑屏、纯色过渡、完全模糊看不清内容、"
        "字幕卡/黑场转场这类没有实际画面价值的截图），如果值得保留，识别出图中的地点/景点名称"
        "（不确定具体名字就写场景类型，比如'城市街景''海边''山景''室内'）、主要内容类型"
        "（landmark地标建筑/person人物/scenery风光/food美食/other其它）、能看出的季节（春夏秋冬，"
        "看不出就填null）。严格只返回JSON，不要有任何JSON以外的文字：\n"
        '{"meaningful": true或false, "location": "地点名称", "subject_type": "landmark/person/scenery/food/other", '
        '"season": "春/夏/秋/冬或null", "tags": "3-5个逗号分隔的中文标签"}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": "识别这张截图"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
        ]},
    ]

    def do_request():
        return requests.post(
            GITHUB_MODELS_ENDPOINT,
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"},
            json={"model": GITHUB_MODELS_MODEL, "max_tokens": 300, "messages": messages},
            timeout=60,
        )

    try:
        resp = do_request()
        if resp.status_code == 429:
            time.sleep(3)
            resp = do_request()
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(json)?|```$", "", cleaned.strip(), flags=re.M).strip()
        return json.loads(cleaned)
    except Exception as e:
        print("AI识别这一帧失败，跳过这个片段：", e)
        return None


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    source_path = f"{WORKDIR}/source.mp4"

    print(f"下载源视频：{SOURCE_KEY}")
    s3 = s3_client()
    s3.download_file(R2_BUCKET_NAME, SOURCE_KEY, source_path)

    duration = get_video_duration(source_path)
    print(f"视频总时长约 {duration:.1f} 秒")

    cuts = detect_scene_cuts(source_path, duration)
    print(f"检测到 {len(cuts)} 个疑似镜头切换点")
    segments = build_segments(cuts, duration)
    if not segments:
        segments = [(0.0, duration)]  # 一个切换点都没检测到（很可能是一镜到底的连续长镜头），就把整条当一个场景处理
    print(f"最终切分成 {len(segments)} 个候选片段")

    results = []
    for idx, (start, end) in enumerate(segments):
        try:
            if idx > 0:
                time.sleep(2)  # GitHub Models免费额度有请求频率限制，片段之间留个间隔，减少触发限流的概率
            mid = (start + end) / 2
            frame_path = f"{WORKDIR}/frame_{idx}.jpg"
            extract_frame(source_path, mid, frame_path)

            info = recognize_scene(frame_path)
            if not info or not info.get("meaningful"):
                print(f"片段{idx}（{start:.1f}s-{end:.1f}s）AI判断没有独立保留价值，跳过")
                continue

            location = (info.get("location") or "未知场景").strip()
            subject_type = info.get("subject_type") or "other"
            season = info.get("season")
            tags = info.get("tags") or location
            print(f"片段{idx}（{start:.1f}s-{end:.1f}s）识别为：{location}（{subject_type}）")

            # 代表图片：直接复用刚才截的那一帧
            image_key = f"assets/scenesplit_{JOB_ID}_{idx}_img.jpg"
            s3.upload_file(frame_path, R2_BUCKET_NAME, image_key, ExtraArgs={"ContentType": "image/jpeg"})

            # 对应的小视频片段
            clip_path = f"{WORKDIR}/clip_{idx}.mp4"
            extract_clip(source_path, start, end, clip_path)
            video_key = f"assets/scenesplit_{JOB_ID}_{idx}_clip.mp4"
            s3.upload_file(clip_path, R2_BUCKET_NAME, video_key, ExtraArgs={"ContentType": "video/mp4"})

            results.append({
                "location": location, "subject_type": subject_type, "season": season, "tags": tags,
                "image_url": f"{R2_PUBLIC_BASE_URL}/{image_key}",
                "video_url": f"{R2_PUBLIC_BASE_URL}/{video_key}",
                "duration_sec": round(end - start, 1),
            })
        except Exception as e:
            print(f"处理片段{idx}失败，跳过，继续处理下一个：", e)
            continue

    print(f"最终成功提取 {len(results)} 个有效场景素材")
    callback("succeeded", results=results)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        callback("failed", error=str(e))
        raise
