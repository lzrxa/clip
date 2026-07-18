import os
import time
import subprocess
import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]

# 和 _worker.js 里 VOICE_OPTIONS 保持一致，两边各自独立维护但内容要对得上
# 和 _worker.js 里 VOICE_OPTIONS 保持一致，两边各自独立维护但内容要对得上。
# 晓梦(XiaomengNeural)、晓睿(XiaoruiNeural) 这两个音色跟微软官方语音列表核对过名字没写错，
# 但edge-tts对这两个音色持续"没有收到音频数据"，重试也没用，是这两个音色本身在edge-tts/
# Azure那边的服务端问题，暂时从列表里去掉，等确认修复了再加回来
VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunxiaNeural",
]

SAMPLE_TEXT = "夏天在喀纳斯可以看到雪山和碧绿的湖水，独库公路沿途的草原随手一拍就是大片。"

# edge-tts 是个没有官方保证的免费接口，偶尔会出现"命令执行成功但实际没收到音频数据"的情况
# （生成一个几乎空的文件，而不是报错），这种文件上传上去点了试听也没声音。这里用文件大小做
# 一道保险：正常这句话生成出来的mp3不会小于这个数，明显偏小就当作这次生成失败，重试
MIN_VALID_SIZE_BYTES = 5000
MAX_ATTEMPTS_PER_VOICE = 3


def run(cmd):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True, timeout=60)


def generate_one(voice, out_path):
    """生成单个音色的试听样本，失败或者产出文件明显不对就重试，重试之间留个间隔避免被限流。"""
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS_PER_VOICE + 1):
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
            run(["edge-tts", "--voice", voice, "--text", SAMPLE_TEXT, "--write-media", out_path])
            size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
            if size < MIN_VALID_SIZE_BYTES:
                raise RuntimeError(f"生成的文件只有{size}字节，明显不正常（可能是edge-tts那次没真正返回音频）")
            return True
        except Exception as e:
            last_error = e
            print(f"{voice} 第{attempt}次尝试失败：{e}")
            if attempt < MAX_ATTEMPTS_PER_VOICE:
                time.sleep(3)  # 留点时间再试，减少连续请求触发限流的概率
    print(f"{voice} 重试{MAX_ATTEMPTS_PER_VOICE}次仍然失败，跳过：{last_error}")
    return False


def main():
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

    os.makedirs("work", exist_ok=True)
    succeeded, failed = [], []

    for voice in VOICES:
        out_path = f"work/{voice}.mp3"
        ok = generate_one(voice, out_path)
        if not ok:
            failed.append(voice)
            continue
        try:
            key = f"voice-samples/{voice}.mp3"
            s3.upload_file(out_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "audio/mpeg"})
            print(f"{voice} 生成并上传完成")
            succeeded.append(voice)
        except Exception as e:
            # 某一个音色失败不影响其他音色继续生成
            print(f"{voice} 上传失败，跳过：", e)
            failed.append(voice)
        time.sleep(1)  # 每个音色之间留点间隔，减少被限流的概率

    print(f"\n全部处理完成：成功 {len(succeeded)} 个，失败 {len(failed)} 个")
    if failed:
        print("失败的音色：", "、".join(failed))
        print("可以重新点一次「生成语音试听样本」再试一次，通常是edge-tts接口偶发抽风，重试能解决")


if __name__ == "__main__":
    main()
