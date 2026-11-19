"""GitHub Actions 渲染端共用的回调/拉清单工具（v273.25）。

目标：
- PAGES_BASE_URL 统一成 https，避免 http→https 的 301 把 POST 变成 GET
- 回调不跟随跨站重定向，避免密钥被带到第三方
- 密钥只放在 X-Render-Secret 请求头，不放进 URL
- 日志里的 URL/密钥一律脱敏
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

import requests


def normalize_pages_base_url(raw: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    elif not value.startswith("https://"):
        value = "https://" + value
    return value


def _redact_query(query: str) -> str:
    pairs = []
    for key, val in parse_qsl(query, keep_blank_values=True):
        if key.lower() in {"secret", "token", "authorization", "key", "sig"}:
            pairs.append((key, "[已隐藏]"))
        else:
            pairs.append((key, val))
    return urlencode(pairs)


def redact_urls(text) -> str:
    """隐藏完整 URL，并把 URL 里的 secret 等查询参数打码。"""
    if not text:
        return text

    def repl(match: re.Match) -> str:
        raw = match.group(0)
        # 去掉结尾常见标点，避免把日志里的逗号/括号算进 URL
        trail = ""
        while raw and raw[-1] in ".,;)]}>\"'":
            trail = raw[-1] + trail
            raw = raw[:-1]
        try:
            parsed = urlparse(raw)
            redacted = parsed._replace(query=_redact_query(parsed.query), netloc="[链接已隐藏]")
            return urlunparse(redacted) + trail
        except Exception:
            return "[链接已隐藏]" + trail

    text = str(text)
    text = re.sub(r"(?i)([?&](?:secret|token|authorization)=)[^&\s]+", r"\1[已隐藏]", text)
    text = re.sub(r"https?://[^\s]+", repl, text)
    return text


def _same_origin(url_a: str, url_b: str) -> bool:
    a, b = urlparse(url_a), urlparse(url_b)
    return a.scheme in {"https"} and b.scheme in {"https"} and a.netloc == b.netloc


def fetch_url_with_retry(url, timeout=60, max_retries=3, backoff_base=3, headers=None, method="GET", allow_redirects=True, **kwargs):
    last_error = None
    headers = headers or {}
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(method, url, timeout=timeout, headers=headers, allow_redirects=allow_redirects, **kwargs)
            if not allow_redirects and (resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308)):
                loc = resp.headers.get("Location") or ""
                raise RuntimeError(
                    f"请求发生 {resp.status_code} 重定向（Location={redact_urls(loc)}）。"
                    "请把 PAGES_BASE_URL 配成最终 https 地址，不要依赖自动跳转。"
                )
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, RuntimeError) as e:
            last_error = e
            if attempt < max_retries and not str(e).startswith("请求发生"):
                wait_sec = backoff_base * attempt
                print(f"请求失败（第{attempt}次，{type(e).__name__}），{wait_sec}秒后重试：{redact_urls(url)}")
                time.sleep(wait_sec)
            elif attempt < max_retries:
                # 重定向是配置错误，重试同一错误地址没有意义
                print(redact_urls(str(e)))
                break
            else:
                print(f"请求失败，已重试{max_retries}次仍未成功：{redact_urls(url)}")
    raise last_error


def post_json_callback(url, payload, secret, timeout=30, max_retries=3):
    """向 Cloudflare 回调接口 POST JSON。密钥只放请求头，不跟随跨域重定向。"""
    headers = {
        "Content-Type": "application/json",
        "X-Render-Secret": secret or "",
    }
    # 兼容期也不再把 secret 放进 JSON 正文
    body = {k: v for k, v in dict(payload or {}).items() if k != "secret"}
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=timeout, allow_redirects=False)
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location") or ""
                raise RuntimeError(
                    f"回调地址发生 {resp.status_code} 重定向（Location={redact_urls(loc)}）。"
                    "请把 GitHub Secret PAGES_BASE_URL 改成最终的 https 地址；"
                    "自动跟随跳转会把 POST 变成 GET，导致状态同步失败。"
                )
            print("回调地址：", redact_urls(url))
            print("回调 HTTP 状态：", resp.status_code)
            print("回调响应内容：", (resp.text or "")[:500])
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_error = e
            print(f"回调失败（第{attempt}次）：", redact_urls(str(e)))
            if attempt < max_retries:
                time.sleep(5 * attempt)
    print("回调重试后仍未成功，任务状态可能无法在网页上正确显示：", redact_urls(str(last_error)))
    return None
