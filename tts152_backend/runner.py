"""Audio-only runner for the uploaded reference's edge-tts scheme.
Never log payloads, text, HTTP errors, authorization, or audio responses.
"""
import asyncio
import array
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave

VOICES = {"zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural", "zh-CN-YunjianNeural", "zh-CN-YunyangNeural", "zh-CN-YunxiaNeural"}
RATE = 24000
MAX_FRAMES = RATE * 300

class JobStopped(Exception):
    pass

class API:
    def __init__(self, base, secret, job):
        u = urllib.parse.urlsplit(base)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError("Invalid API base")
        if not re.fullmatch(r"[0-9a-f-]{36}", job) or not 32 <= len(secret) <= 512:
            raise ValueError("Invalid configuration")
        self.base, self.secret, self.job, self.lease = base.rstrip("/"), secret, job, None

    def call(self, action, data=None, audio=None):
        body = audio if audio is not None else json.dumps(data or {}).encode()
        headers = {"Authorization": "Bearer " + self.secret, "Content-Type": "audio/wav" if audio is not None else "application/json"}
        if self.lease:
            headers["X-TTS-Lease"] = self.lease
        request = urllib.request.Request(self.base + "/api/tts152-render/" + self.job + "/" + action, body, headers, method="POST")
        # Do not follow redirects with the service credential.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=35) as response:
                raw = response.read(30000)
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (409, 410):
                raise JobStopped() from None
            raise RuntimeError("Backend rejected request") from None
        except Exception:
            raise RuntimeError("Backend connection failed") from None

    def claim(self):
        data = self.call("claim")
        self.lease = data["lease"]
        if not re.fullmatch(r"[0-9a-f-]{36}", self.lease):
            raise ValueError("Invalid lease")
        return data


def validate(p):
    if p.get("voice") not in VOICES or not isinstance(p.get("parts"), list) or not 1 <= len(p["parts"]) <= 80:
        raise ValueError("Invalid payload")
    chars = 0
    for part in p["parts"]:
        text = part.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 200:
            raise ValueError("Invalid text")
        chars += len(text)
    if chars > 3000:
        raise ValueError("Too long")
    for k, low, high in (("speed", .6, 1.6), ("gap", 0, 3), ("paraGap", 0, 5)):
        x = p.get(k)
        if isinstance(x, bool) or not isinstance(x, (float, int)) or not low <= x <= high:
            raise ValueError("Invalid settings")


def decode_mp3(path):
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-t", "301", "-ar", str(RATE), "-ac", "1", "-f", "s16le", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=90)
    pcm = array.array("h")
    pcm.frombytes(result.stdout)
    if sys.byteorder != "little":
        pcm.byteswap()
    if not pcm or len(pcm) > MAX_FRAMES:
        raise ValueError("Invalid duration")
    a, b, threshold, pad = 0, len(pcm), 131, round(RATE * .04)
    while a < b and abs(pcm[a]) < threshold:
        a += 1
    while b > a and abs(pcm[b-1]) < threshold:
        b -= 1
    if a == b:
        raise ValueError("Silent audio")
    pcm = pcm[max(0, a-pad):min(len(pcm), b+pad)]
    fade = min(round(RATE * .006), len(pcm)//2)
    for k in range(fade):
        pcm[k] = round(pcm[k] * k / fade)
        pcm[len(pcm)-1-k] = round(pcm[len(pcm)-1-k] * k / fade)
    return pcm


def encode_wav(pcm, normalize):
    if not pcm or len(pcm) > MAX_FRAMES:
        raise ValueError("Duration exceeded")
    if normalize:
        peak = max(abs(x) for x in pcm)
        if peak > 327:
            scale = .89 * 32767 / peak
            pcm = array.array("h", (max(-32768, min(32767, round(x*scale))) for x in pcm))
    if sys.byteorder != "little":
        pcm.byteswap()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(pcm.tobytes())
    return buffer.getvalue()


async def render(payload, api, synthesizer=None):
    validate(payload)
    if synthesizer is None:
        import edge_tts
        async def synthesizer(text, voice, rate, path):
            client = edge_tts.Communicate(text, voice, rate=rate, connect_timeout=10, receive_timeout=35)
            async def receive():
                size = 0
                with open(path, "wb") as output:
                    async for chunk in client.stream():
                        if chunk["type"] == "audio":
                            size += len(chunk["data"])
                            if size > 4000000:
                                raise ValueError("Audio too large")
                            output.write(chunk["data"])
            await asyncio.wait_for(receive(), timeout=65)
    pcm = array.array("h")
    rate = f"{round((payload['speed']-1)*100):+d}%"
    with tempfile.TemporaryDirectory(prefix="tts152-") as directory:
        path = Path(directory) / "speech.mp3"
        for index, part in enumerate(payload["parts"]):
            # Callback also checks cancellation/expiry before every external synthesis.
            await asyncio.to_thread(api.call, "progress", {"progress": index})
            await synthesizer(part["text"], payload["voice"], rate, path)
            segment = await asyncio.to_thread(decode_mp3, path)
            frames = len(segment)
            if index + 1 < len(payload["parts"]):
                frames += round(RATE * (payload["paraGap"] if part.get("para") else payload["gap"]))
            if len(pcm) + frames > MAX_FRAMES:
                raise ValueError("Duration exceeded")
            pcm.extend(segment)
            pcm.extend(array.array("h", [0]) * (frames-len(segment)))
            path.unlink(missing_ok=True)
        await asyncio.to_thread(api.call, "progress", {"progress": len(payload["parts"])})
        audio = await asyncio.to_thread(encode_wav, pcm, payload.get("normalize", True))
        await asyncio.to_thread(api.call, "complete", audio=audio)
        return len(audio)


async def main():
    api = None
    try:
        api = API(os.environ["TTS_API_BASE"], os.environ["TTS_RENDER_SECRET"], os.environ["TTS_JOB_ID"])
        payload = await asyncio.to_thread(api.claim)
        await render(payload, api)
        print("TTS152 completed. No text or audio logged.")
        return 0
    except JobStopped:
        print("TTS152 stopped: cancelled, expired, or already claimed.")
        return 0
    except Exception:
        if api and api.lease:
            try:
                await asyncio.to_thread(api.call, "error")
            except Exception:
                pass
        print("TTS152 failed. Check configuration, service availability or limits; details suppressed for privacy.")
        return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
