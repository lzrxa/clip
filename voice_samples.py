import os
import subprocess
import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]

# 和 _worker.js 里 VOICE_OPTIONS 保持一致，两边各自独立维护但内容要对得上
VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-XiaomengNeural",
    "zh-CN-XiaoruiNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-YunxiaNeural",
]

SAMPLE_TEXT = "夏天在喀纳斯可以看到雪山和碧绿的湖水，独库公路沿途的草原随手一拍就是大片。"


def run(cmd):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


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
    for voice in VOICES:
        out_path = f"work/{voice}.mp3"
        try:
            run(["edge-tts", "--voice", voice, "--text", SAMPLE_TEXT, "--write-media", out_path])
            key = f"voice-samples/{voice}.mp3"
            s3.upload_file(out_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "audio/mpeg"})
            print(f"{voice} 生成并上传完成")
        except Exception as e:
            # 某一个音色失败不影响其他音色继续生成
            print(f"{voice} 失败，跳过：", e)

    print("全部语音试听样本处理完成")


if __name__ == "__main__":
    main()
