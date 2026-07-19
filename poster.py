import os
import re
import sys
import textwrap
from io import BytesIO
import requests
import boto3
from botocore.config import Config
from PIL import Image, ImageDraw, ImageFont

try:
    import qrcode
except ImportError:
    qrcode = None

POSTER_ID = os.environ["POSTER_ID"]
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
SILICONFLOW_API_KEY = os.environ.get("SILICONFLOW_API_KEY")

W, H = 1080, 1920
WORKDIR = "poster_work"

NOTO_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
NOTO_REGULAR = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
NOTO_BLACK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc"
ARTISTIC_FONT = "fonts/ZCOOLKuaiLe-Regular.ttf"  # 站酷快乐体，海报标题专用的活泼艺术字体

TITLE_FONT_MAP = {
    "regular": NOTO_REGULAR, "bold": NOTO_BOLD, "black": NOTO_BLACK,
    "artistic": ARTISTIC_FONT,
}

TITLE_POSITION_RATIO = {"top": 0.20, "middle": 0.42, "bottom": 0.62}


def font(path_candidates, size):
    for p in path_candidates:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


# 之前海报上出现的那些黄色/白色小方块，是因为标题装饰符号（⊙、✦）不在 Noto Sans CJK 这套字体的
# 字符集里，Pillow画不出对应字形，就画成了"缺字方块"。这里做一道统一的过滤：只保留中文汉字、
# 常见中英文标点、ASCII字符、中间点这些确认能正常显示的字符，其余一律替换成空格——不管是
# 我自己写死的装饰符号，还是用户在标题/亮点/备注这些自由文本框里不小心打进去的emoji、
# 特殊符号，都会被过滤掉，从根上避免"缺字方块"再出现，而不是只修补已知的这一两处。
_SAFE_TEXT_PATTERN = re.compile(
    '[^'
    '\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff'  # CJK统一表意文字（含扩展A、兼容区）
    '\u3040-\u30ff\uac00-\ud7af'                # 日文假名、韩文（防止极少数场景混入也能显示）
    '\u3000-\u303f\uff00-\uffef'                # 中文标点、全角符号
    '\x20-\x7e'                                  # 基本ASCII可打印字符
    '\u00b7'                                     # 中间点·（已经在这套字体里验证能正常显示）
    ']'
)


def safe_text(s):
    """把字符串里这套CJK字体渲染不出来的字符（emoji、生僻符号等）统一替换成空格。"""
    if not s:
        return s
    return _SAFE_TEXT_PATTERN.sub(' ', str(s)).strip()


def redact_urls(text):
    """把错误信息里任何完整URL都替换成占位符，避免真实的存储地址/服务器信息被存进数据库、
    展示在网页上给用户看。GitHub Actions日志里print出来的原始信息不受影响。"""
    if not text:
        return text
    return re.sub(r"https?://\S+", "[链接已隐藏]", str(text))


def callback(status, poster_url=None, error=None):
    payload = {"poster_id": POSTER_ID, "secret": RENDER_SECRET, "status": status}
    if poster_url:
        payload["poster_url"] = poster_url
    if error:
        payload["error"] = redact_urls(error)[:2000]

    callback_url = f"{PAGES_BASE_URL}/api/poster-callback"
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


def fetch_manifest():
    resp = requests.get(
        f"{PAGES_BASE_URL}/api/poster-manifest",
        params={"poster_id": POSTER_ID, "secret": RENDER_SECRET},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("message", "获取海报任务数据失败"))
    return data


def get_background(manifest):
    """真实素材直接下载裁切；AI模式调用硅基流动 FLUX.1-schnell 生成装饰性背景。"""
    if manifest["background_source"] == "ai":
        if not SILICONFLOW_API_KEY:
            raise RuntimeError("选择了AI背景但未配置 SILICONFLOW_API_KEY")
        # 之前这里默认提示词写死是"山脉草原剪影"，不管什么领域的海报都会往这个方向生成，
        # 音乐培训这类海报用AI背景兜底时画面会很不搭。这里按manifest传过来的domain换一版默认词，
        # 用户自己填了ai_bg_prompt的话还是优先用用户填的
        if manifest.get("domain") == "music":
            default_prompt = (
                f"abstract elegant background for a music school poster, {manifest.get('title', '')}, "
                "soft warm gradient, subtle music notes and instrument silhouettes, minimal illustration, "
                "no text, no letters, no words, no people, vertical composition"
            )
        else:
            default_prompt = (
                f"abstract travel poster background, {manifest.get('title', '')}, "
                "soft gradient, mountains and grassland silhouette, minimal illustration, "
                "no text, no letters, no words, vertical composition"
            )
        prompt = manifest.get("ai_bg_prompt") or default_prompt
        resp = requests.post(
            "https://api.siliconflow.cn/v1/images/generations",
            headers={"Authorization": f"Bearer {SILICONFLOW_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "black-forest-labs/FLUX.1-schnell",
                "prompt": prompt,
                "image_size": "1024x1792",
                "num_inference_steps": 4,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        img_url = None
        if data.get("images"):
            img_url = data["images"][0].get("url")
        elif data.get("data"):
            img_url = data["data"][0].get("url")
        if not img_url:
            raise RuntimeError(f"AI背景生成返回格式异常: {data}")
        img_resp = requests.get(img_url, timeout=60)
        img_resp.raise_for_status()
        bg_path = f"{WORKDIR}/bg_ai.jpg"
        with open(bg_path, "wb") as f:
            f.write(img_resp.content)
        return Image.open(bg_path).convert("RGB")
    else:
        if not manifest.get("background_url"):
            raise RuntimeError("未提供背景素材")
        asset_type = manifest.get("background_asset_type") or "image"
        if asset_type == "video":
            video_path = f"{WORKDIR}/bg_video.mp4"
            img_resp = requests.get(manifest["background_url"], timeout=60)
            img_resp.raise_for_status()
            with open(video_path, "wb") as f:
                f.write(img_resp.content)
            bg_path = f"{WORKDIR}/bg_photo.jpg"
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", video_path, "-frames:v", "1", bg_path],
                check=True,
            )
        else:
            img_resp = requests.get(manifest["background_url"], timeout=60)
            img_resp.raise_for_status()
            bg_path = f"{WORKDIR}/bg_photo.jpg"
            with open(bg_path, "wb") as f:
                f.write(img_resp.content)
        return Image.open(bg_path).convert("RGB")


def cover_resize(img, target_w, target_h):
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def add_vertical_gradient(img, box, top_alpha, bottom_alpha, color=(0, 0, 0)):
    x0, y0, x1, y1 = box
    h = y1 - y0
    overlay = Image.new("RGBA", (x1 - x0, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for y in range(h):
        alpha = int(top_alpha + (bottom_alpha - top_alpha) * (y / max(1, h)))
        draw.line([(0, y), (x1 - x0, y)], fill=(*color, alpha))
    img.paste(overlay, (x0, y0), overlay)


def add_horizontal_gradient(img, box, left_alpha, right_alpha, color=(0, 0, 0)):
    x0, y0, x1, y1 = box
    w = x1 - x0
    overlay = Image.new("RGBA", (w, y1 - y0), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for x in range(w):
        alpha = int(left_alpha + (right_alpha - left_alpha) * (x / max(1, w)))
        draw.line([(x, 0), (x, y1 - y0)], fill=(*color, alpha))
    img.paste(overlay, (x0, y0), overlay)


def draw_pill(draw, xy, text, fnt, fill, text_fill, padding=(16, 8)):
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    rect = [x, y, x + tw + padding[0] * 2, y + th + padding[1] * 2]
    draw.rounded_rectangle(rect, radius=(rect[3] - rect[1]) // 2, fill=fill)
    draw.text((x + padding[0] - bbox[0], y + padding[1] - bbox[1]), text, font=fnt, fill=text_fill)
    return rect


def get_contact_image(manifest, size):
    """右下角联系方式图片：有自定义图片（个人二维码截图/logo）优先用它，
    没有就退回用wechat_link自动生成二维码，都没有就返回None"""
    logo_url = manifest.get("logo_url")
    if logo_url:
        try:
            resp = requests.get(logo_url, timeout=30)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content)).convert("RGBA")
            # 按比例缩放后居中裁剪成正方形，避免用户传的图片变形
            w, h = img.size
            side = min(w, h)
            img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
            return img.resize((size, size))
        except Exception as e:
            print("自定义图片下载失败，退回二维码：", e)
    wechat_link = manifest.get("wechat_link")
    if wechat_link and qrcode:
        return qrcode.make(wechat_link).resize((size, size)).convert("RGBA")
    return None


def draw_phone_number(draw, manifest, font, y_from_bottom):
    """在底部画电话号码，位置在footer行下方，不与二维码重叠"""
    phone = manifest.get("phone_number")
    if not phone:
        return
    text = f"电话：{safe_text(phone)}"
    draw.text((48, y_from_bottom), text, font=font, fill=(255, 255, 255, 230),
               stroke_width=1, stroke_fill=(0, 0, 0, 180))


def build_poster_standard(manifest, bg_img, out_path):
    highlights = manifest.get("highlights") or []          # 亮点列表，标题文字由 highlights_label 决定
    accommodations = manifest.get("accommodations") or []   # 第二组列表，标题文字由 accommodations_label 决定
    # 这两组列表原本是给旅游海报设计的（"体验亮点"/"尊享下榻"），音乐培训这类其他领域的海报
    # 需要不一样的标题（比如"教学亮点"/"学员成果"），由Worker那边按海报的domain传过来，
    # 这里给个旅游语境的默认值，防止manifest没传这两个字段时崩掉
    highlights_label = manifest.get("highlights_label") or "体验亮点"
    accommodations_label = manifest.get("accommodations_label") or "尊享下榻"
    departure_info = manifest.get("departure_info") or ""   # 如 "长沙直飞乌鲁木齐"
    tiers = manifest.get("price_tiers") or []

    # 画布高度按内容动态撑高：填得越多，海报就越长，不会互相挤压
    extra_h = 0
    if highlights:
        extra_h += 90 + len(highlights) * 46
    if accommodations:
        extra_h += 90 + len(accommodations) * 46
    if departure_info:
        extra_h += 90
    canvas_h = 1920 + extra_h
    has_extra_content = extra_h > 0

    # 关键修复：照片不能被强行拉伸去撑满整个加长后的画布（那样画面会变形拉花）。
    # 照片固定按1920高度做cover裁切，画布里多出来的部分用纯色深色背景承载文字内容，
    # 效果类似"上半截真实照片 + 下半截深色内容卡片"，而不是把同一张图硬拉长。
    canvas = Image.new("RGBA", (W, canvas_h), (18, 20, 24, 255))
    photo = cover_resize(bg_img, W, 1920).convert("RGBA")
    canvas.paste(photo, (0, 0))

    draw = ImageDraw.Draw(canvas, "RGBA")

    # 顶部渐深遮罩（衬托景点清单和右上角角标）
    add_vertical_gradient(canvas, (0, 0, W, 520), 140, 40)

    if has_extra_content:
        # 照片底部到下方纯色内容区之间做一个柔和过渡，不要生硬的分界线
        add_vertical_gradient(canvas, (0, 1720, W, 1920), 0, 235)
    else:
        # 没有额外内容板块时，价格和footer还是直接叠在照片底部，需要原来的深色遮罩衬托
        add_vertical_gradient(canvas, (0, canvas_h - 620, W, canvas_h), 30, 190)

    title_font_path = TITLE_FONT_MAP.get(manifest.get("title_font_weight"), ARTISTIC_FONT)
    f_loc = font([NOTO_BOLD], 30)
    f_badge = font([NOTO_BOLD], 24)
    f_tag = font([NOTO_BOLD], 26)
    f_title = font([title_font_path, NOTO_BOLD], 108)
    f_subtitle = font([NOTO_REGULAR], 28)
    f_section_head = font([NOTO_BLACK, NOTO_BOLD, NOTO_REGULAR], 40)
    f_section_item = font([NOTO_REGULAR], 30)
    f_price_label = font([NOTO_BOLD], 26)
    f_price = font([NOTO_BOLD], 70)
    f_price_unit = font([NOTO_BOLD], 26)
    f_footer = font([NOTO_REGULAR], 24)

    # 左上角：景点清单
    y = 56
    for loc in manifest.get("locations") or []:
        draw.text((48, y), f"· {safe_text(loc)}", font=f_loc, fill=(255, 255, 255, 255),
                   stroke_width=2, stroke_fill=(0, 0, 0, 200))
        y += 46

    # 右上角：两个角标
    badge_y = 56
    for raw_text in filter(None, [manifest.get("team_size_text"), manifest.get("badge_text")]):
        text = safe_text(raw_text)
        bbox = draw.textbbox((0, 0), text, font=f_badge)
        tw = bbox[2] - bbox[0]
        x = W - 48 - tw - 32
        rect = draw_pill(draw, (x, badge_y), text, f_badge,
                          fill=(255, 255, 255, 235), text_fill=(30, 30, 30, 255))
        badge_y = rect[3] + 16

    # 中部：高亮小标签 + 大标题 + 英文副标题
    title_ratio = TITLE_POSITION_RATIO.get(manifest.get("title_position"), 0.42)
    mid_y = int(1920 * title_ratio)
    if manifest.get("highlight_word"):
        tag_text = safe_text(manifest["highlight_word"])
        bbox = draw.textbbox((0, 0), tag_text, font=f_tag)
        tw = bbox[2] - bbox[0]
        draw_pill(draw, ((W - tw - 32) // 2, mid_y), tag_text, f_tag,
                  fill=(240, 210, 30, 255), text_fill=(30, 30, 20, 255))
        mid_y += 70

    title = safe_text(manifest.get("title") or "")
    wrapped = textwrap.fill(title, width=6)
    for line in wrapped.split("\n"):
        bbox = draw.textbbox((0, 0), line, font=f_title)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, mid_y), line, font=f_title, fill=(255, 255, 255, 255),
                   stroke_width=4, stroke_fill=(0, 0, 0, 255))
        mid_y += 130

    if manifest.get("subtitle_en"):
        spaced = " ".join(list(safe_text(manifest["subtitle_en"]).replace(" ", "")))
        bbox = draw.textbbox((0, 0), spaced, font=f_subtitle)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, mid_y + 20), spaced, font=f_subtitle, fill=(255, 255, 255, 230),
                   stroke_width=1, stroke_fill=(0, 0, 0, 200))

    # 内容区从这里开始往下堆叠：体验亮点 -> 住宿亮点 -> 出发地 -> 价格 -> footer
    content_y = canvas_h - extra_h - 500

    def draw_section_list(heading, items, y_start):
        draw.text((48, y_start), heading, font=f_section_head, fill=(240, 210, 30, 255),
                   stroke_width=2, stroke_fill=(0, 0, 0, 220))
        yy = y_start + 62
        for idx, item in enumerate(items, 1):
            draw.text((48, yy), f"{idx}. {safe_text(item)}", font=f_section_item, fill=(255, 255, 255, 240),
                       stroke_width=1, stroke_fill=(0, 0, 0, 200))
            yy += 46
        return yy + 20

    if highlights:
        content_y = draw_section_list(f"【{safe_text(highlights_label)}】", highlights, content_y)
    if accommodations:
        content_y = draw_section_list(f"【{safe_text(accommodations_label)}】", accommodations, content_y)
    if departure_info:
        draw.text((48, content_y), safe_text(departure_info), font=f_section_head, fill=(255, 255, 255, 245),
                   stroke_width=2, stroke_fill=(0, 0, 0, 220))
        content_y += 90

    # 价格档位（最多3档，等分排列）
    if tiers:
        n = len(tiers)
        col_w = W / n
        py = content_y if (highlights or accommodations or departure_info) else canvas_h - 500
        for i, tier in enumerate(tiers):
            cx = col_w * i + col_w / 2
            label = safe_text(tier.get("label", ""))
            price = safe_text(str(tier.get("price", "")))
            bbox = draw.textbbox((0, 0), label, font=f_price_label)
            draw.text((cx - (bbox[2] - bbox[0]) / 2, py), label, font=f_price_label, fill=(255, 255, 255, 235))
            price_text = price
            bbox2 = draw.textbbox((0, 0), price_text, font=f_price)
            price_w = bbox2[2] - bbox2[0]
            unit_text = "元/人"
            bbox3 = draw.textbbox((0, 0), unit_text, font=f_price_unit)
            unit_w = bbox3[2] - bbox3[0]
            total_w = price_w + 8 + unit_w
            start_x = cx - total_w / 2
            draw.text((start_x, py + 44), price_text, font=f_price, fill=(240, 210, 30, 255))
            draw.text((start_x + price_w + 8, py + 44 + (bbox2[3] - bbox3[3])), unit_text,
                       font=f_price_unit, fill=(255, 255, 255, 230))
            if i > 0:
                draw.line([(col_w * i, py - 10), (col_w * i, py + 120)], fill=(255, 255, 255, 90), width=2)

    # 底部：footer标签 + 说明文字
    footer_y = canvas_h - 320
    fx = 48
    if manifest.get("footer_tag"):
        footer_tag_text = safe_text(manifest["footer_tag"])
        rect = draw_pill(draw, (fx, footer_y), footer_tag_text, f_tag,
                          fill=(240, 210, 30, 255), text_fill=(30, 30, 20, 255))
        fx = rect[2] + 16
    if manifest.get("footer_text"):
        draw.text((fx, footer_y + 6), safe_text(manifest["footer_text"]), font=f_subtitle, fill=(255, 255, 255, 230))

    draw_phone_number(draw, manifest, f_footer, footer_y + 48)

    # 联系方式图片（自定义logo/二维码截图优先，否则自动生成微信二维码）
    contact_img = get_contact_image(manifest, 160)
    if contact_img:
        qr_bg = Image.new("RGBA", (176, 176), (255, 255, 255, 255))
        qr_bg.paste(contact_img, (8, 8))
        canvas.paste(qr_bg, (W - 220, canvas_h - 260), qr_bg)

    # 最底部装饰线（虚线+文字，呼应参考海报的点线装饰）
    dash_y = canvas_h - 100
    dash_x = 60
    while dash_x < W - 60:
        draw.line([(dash_x, dash_y), (dash_x + 14, dash_y)], fill=(255, 255, 255, 140), width=2)
        dash_x += 24
    bottom_text = safe_text(manifest.get("bottom_tagline") or "独家定制   100% 原创线路")
    f_bottom_tag = font([NOTO_BOLD, NOTO_REGULAR], 30)
    bbox = draw.textbbox((0, 0), bottom_text, font=f_bottom_tag)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) / 2, canvas_h - 74), bottom_text, font=f_bottom_tag, fill=(255, 255, 255, 210))

    canvas.convert("RGB").save(out_path, quality=92)


def build_poster_brand(manifest, bg_img, out_path):
    """纯大字风光版：不放价格，突出情绪和品牌感，适合形象宣传而非促销。"""
    canvas = cover_resize(bg_img, W, H).convert("RGBA")
    add_vertical_gradient(canvas, (0, 0, W, 460), 120, 20)
    add_vertical_gradient(canvas, (0, H - 380, W, H), 10, 170)
    draw = ImageDraw.Draw(canvas, "RGBA")

    title_font_path = TITLE_FONT_MAP.get(manifest.get("title_font_weight"), ARTISTIC_FONT)
    f_loc = font([NOTO_BOLD], 28)
    f_tag = font([NOTO_BOLD], 26)
    f_title = font([title_font_path, NOTO_BOLD], 128)
    f_subtitle = font([NOTO_REGULAR], 30)
    f_footer = font([NOTO_REGULAR], 26)

    y = 56
    for loc in (manifest.get("locations") or [])[:6]:
        draw.text((48, y), f"· {safe_text(loc)}", font=f_loc, fill=(255, 255, 255, 255),
                   stroke_width=2, stroke_fill=(0, 0, 0, 200))
        y += 42

    title_ratio = TITLE_POSITION_RATIO.get(manifest.get("title_position"), 0.46)
    mid_y = int(H * title_ratio)
    if manifest.get("highlight_word"):
        f_tagfont = f_tag
        tag_text = safe_text(manifest["highlight_word"])
        bbox = draw.textbbox((0, 0), tag_text, font=f_tagfont)
        tw = bbox[2] - bbox[0]
        draw_pill(draw, ((W - tw - 32) // 2, mid_y), tag_text, f_tagfont,
                  fill=(240, 210, 30, 255), text_fill=(30, 30, 20, 255))
        mid_y += 74

    title = safe_text(manifest.get("title") or "")
    wrapped = textwrap.fill(title, width=6)
    for line in wrapped.split("\n"):
        bbox = draw.textbbox((0, 0), line, font=f_title)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, mid_y), line, font=f_title, fill=(255, 255, 255, 255),
                   stroke_width=5, stroke_fill=(0, 0, 0, 255))
        mid_y += 150

    if manifest.get("subtitle_en"):
        spaced = " ".join(list(safe_text(manifest["subtitle_en"]).replace(" ", "")))
        bbox = draw.textbbox((0, 0), spaced, font=f_subtitle)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, mid_y + 26), spaced, font=f_subtitle, fill=(255, 255, 255, 230),
                   stroke_width=1, stroke_fill=(0, 0, 0, 200))

    if manifest.get("footer_text"):
        footer_text = safe_text(manifest["footer_text"])
        bbox = draw.textbbox((0, 0), footer_text, font=f_footer)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, H - 140), footer_text, font=f_footer, fill=(255, 255, 255, 220))

    draw_phone_number(draw, manifest, f_footer, H - 105)

    contact_img = get_contact_image(manifest, 150)
    if contact_img:
        qr_bg = Image.new("RGBA", (166, 166), (255, 255, 255, 255))
        qr_bg.paste(contact_img, (8, 8))
        canvas.paste(qr_bg, (W - 210, H - 250), qr_bg)

    canvas.convert("RGB").save(out_path, quality=92)


def build_poster_promo(manifest, bg_img, out_path):
    """促销价格版：价格是绝对视觉焦点，标题和景点清单缩小让位。"""
    canvas = cover_resize(bg_img, W, H).convert("RGBA")
    add_vertical_gradient(canvas, (0, 0, W, 380), 130, 30)
    add_vertical_gradient(canvas, (0, H - 760, W, H), 20, 210)
    draw = ImageDraw.Draw(canvas, "RGBA")

    f_loc = font([NOTO_BOLD], 26)
    f_tag = font([NOTO_BOLD], 24)
    title_font_path = TITLE_FONT_MAP.get(manifest.get("title_font_weight"), ARTISTIC_FONT)
    f_title = font([title_font_path, NOTO_BOLD], 74)
    f_hero_label = font([NOTO_BOLD], 32)
    f_hero_price = font([NOTO_BOLD], 168)
    f_hero_unit = font([NOTO_BOLD], 36)
    f_price_label = font([NOTO_BOLD], 24)
    f_price = font([NOTO_BOLD], 50)
    f_footer = font([NOTO_REGULAR], 24)

    # 顶部：一行小景点清单（横排，节省空间给价格）
    if manifest.get("locations"):
        line = "  ·  ".join(safe_text(loc) for loc in manifest["locations"][:5])
        draw.text((48, 56), line, font=f_loc, fill=(255, 255, 255, 240),
                   stroke_width=2, stroke_fill=(0, 0, 0, 200))

    # 标题（比标准版小，放在价格上方）
    title_y = 150
    title = safe_text(manifest.get("title") or "")
    bbox = draw.textbbox((0, 0), title, font=f_title)
    tw = bbox[2] - bbox[0]
    draw.text(((W - tw) / 2, title_y), title, font=f_title, fill=(255, 255, 255, 255),
               stroke_width=3, stroke_fill=(0, 0, 0, 255))

    # 价格焦点区：第一档价格做成超大"起"价展示，其余档位小字排在下面
    tiers = manifest.get("price_tiers") or []
    hero_y = H - 700
    if tiers:
        hero = tiers[0]
        label_text = f"{safe_text(hero.get('label', ''))} 起"
        bbox = draw.textbbox((0, 0), label_text, font=f_hero_label)
        draw.text(((W - (bbox[2] - bbox[0])) / 2, hero_y), label_text, font=f_hero_label, fill=(255, 255, 255, 230))
        hero_y += 50

        price_text = safe_text(str(hero.get("price", "")))
        bbox2 = draw.textbbox((0, 0), price_text, font=f_hero_price)
        price_w = bbox2[2] - bbox2[0]
        unit_text = "元/人"
        bbox3 = draw.textbbox((0, 0), unit_text, font=f_hero_unit)
        unit_w = bbox3[2] - bbox3[0]
        total_w = price_w + 12 + unit_w
        start_x = (W - total_w) / 2
        draw.text((start_x, hero_y), price_text, font=f_hero_price, fill=(240, 210, 30, 255),
                   stroke_width=2, stroke_fill=(0, 0, 0, 180))
        draw.text((start_x + price_w + 12, hero_y + (bbox2[3] - bbox3[3])), unit_text,
                   font=f_hero_unit, fill=(255, 255, 255, 240))
        hero_y += 210

        others = tiers[1:3]
        if others:
            n = len(others)
            col_w = W / n
            for i, tier in enumerate(others):
                cx = col_w * i + col_w / 2
                text = f"{safe_text(tier.get('label', ''))}  {safe_text(tier.get('price', ''))}元/人"
                bbox = draw.textbbox((0, 0), text, font=f_price_label)
                draw.text((cx - (bbox[2] - bbox[0]) / 2, hero_y), text, font=f_price_label, fill=(255, 255, 255, 220))

    footer_y = H - 130
    fx = 48
    if manifest.get("footer_tag"):
        footer_tag_text = safe_text(manifest["footer_tag"])
        rect = draw_pill(draw, (fx, footer_y), footer_tag_text, f_tag,
                          fill=(240, 210, 30, 255), text_fill=(30, 30, 20, 255))
        fx = rect[2] + 16
    if manifest.get("footer_text"):
        draw.text((fx, footer_y + 4), safe_text(manifest["footer_text"]), font=f_footer, fill=(255, 255, 255, 220))

    draw_phone_number(draw, manifest, f_footer, footer_y + 34)

    contact_img = get_contact_image(manifest, 140)
    if contact_img:
        qr_bg = Image.new("RGBA", (156, 156), (255, 255, 255, 255))
        qr_bg.paste(contact_img, (8, 8))
        canvas.paste(qr_bg, (W - 200, H - 200), qr_bg)

    canvas.convert("RGB").save(out_path, quality=92)


def draw_checkmark(draw, cx, cy, r, color):
    """画一个"打钩"图标：外面一个描边圆圈，里面画勾——不用现成的✓字符（那类符号在
    Noto Sans CJK这套字体里大概率没有对应字形，用了又会变回那种"缺字方块"），
    自己拿线条画一个，稳定不出错，风格还能自己控制粗细颜色"""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=4)
    draw.line([(cx - r * 0.45, cy), (cx - r * 0.1, cy + r * 0.4), (cx + r * 0.5, cy - r * 0.35)],
               fill=color, width=5, joint="curve")


def draw_number_circle(draw, cx, cy, r, number, font, color, text_color):
    """画一个"编号圆圈"：圆圈里放数字——道理跟打钩图标一样，不用①②③这类"带圈数字"
    Unicode符号（同样存在字体不一定收录、变成缺字方块的风险），自己画一个圆+普通数字，
    效果一样，还不会有兼容性问题"""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    text = str(number)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((cx - tw / 2 - bbox[0], cy - th / 2 - bbox[1]), text, font=font, fill=text_color)


def build_poster_recruit(manifest, bg_img, out_path):
    """招生介绍版：打钩清单 + 价格对比（原价划线+优惠价）+ 老师名片 + 编号课程信息，
    走浅色暖色调，是"名师招生海报"这一类常见结构的通用版式（不是照抄某一张具体海报，
    是这类海报共有的功能模块拼出来的），适合招生宣传、名师介绍这类场景，旅游内容也能用。
    """
    checklist = manifest.get("checklist_items") or []
    course_info = manifest.get("course_info_items") or []
    teacher_name = safe_text(manifest.get("teacher_name") or "")
    teacher_bio = safe_text(manifest.get("teacher_bio") or "")
    teacher_photo_url = manifest.get("teacher_photo_url")
    original_price = safe_text(manifest.get("original_price") or "")
    discounted_price = safe_text(manifest.get("discounted_price") or "")
    price_note = safe_text(manifest.get("price_note") or "")
    has_price = bool(discounted_price)
    has_teacher = bool(teacher_name)

    CREAM = (250, 246, 238)
    MAROON = (109, 38, 30)
    GOLD = (196, 142, 42)
    INK = (58, 48, 44)

    # 画布高度按内容动态撑高，跟标准版的思路一样，只是这里是"浅色内容区"为主、照片是
    # 顶部的点缀，跟标准版"照片为主、深色内容区在底部"正好反过来
    photo_h = 640
    y_est = photo_h + 40 + 260 + 70  # 顶部图 + hook行 + 标题区 + 英文小标题
    if checklist:
        y_est += 40 + len(checklist) * 62
    if has_price:
        y_est += 210
    if has_teacher:
        y_est += 260
    if course_info:
        y_est += 90 + len(course_info) * 74
    y_est += 160  # 底部tagline+留白
    canvas_h = max(1920, y_est)

    canvas = Image.new("RGBA", (W, canvas_h), CREAM)
    photo = cover_resize(bg_img, W, photo_h).convert("RGBA")
    canvas.paste(photo, (0, 0))
    add_vertical_gradient(canvas, (0, photo_h - 220, W, photo_h), 0, 255, color=CREAM)
    draw = ImageDraw.Draw(canvas, "RGBA")

    f_hook = font([NOTO_BOLD], 34)
    f_title = font([NOTO_BLACK, NOTO_BOLD], 92)
    f_subtitle_en = font([NOTO_BOLD], 26)
    f_item = font([NOTO_BOLD], 34)
    f_price_note = font([NOTO_REGULAR], 28)
    f_price_small = font([NOTO_BOLD], 34)
    f_price_big = font([NOTO_BLACK, NOTO_BOLD], 88)
    f_price_unit = font([NOTO_BOLD], 30)
    f_teacher_name = font([NOTO_BLACK, NOTO_BOLD], 40)
    f_teacher_bio = font([NOTO_REGULAR], 26)
    f_section_title = font([NOTO_BOLD], 32)
    f_course_item = font([NOTO_BOLD], 30)
    f_number = font([NOTO_BOLD], 26)
    f_bottom = font([NOTO_BOLD], 28)

    y = photo_h - 130

    # 打钩小标语（比如"这个暑假，一起唱响舞台！"），复用highlight_word这个字段
    hook_text = safe_text(manifest.get("highlight_word") or "")
    if hook_text:
        draw_checkmark(draw, 76, y + 16, 18, GOLD)
        draw.text((110, y), hook_text, font=f_hook, fill=MAROON)
        y += 60

    # 大标题（多行），标题换行沿用标准版的textwrap.fill方式
    title = safe_text(manifest.get("title") or "")
    wrapped = textwrap.fill(title, width=8)
    for line in wrapped.split("\n"):
        draw.text((48, y), line, font=f_title, fill=MAROON)
        bbox = draw.textbbox((0, 0), line, font=f_title)
        y += (bbox[3] - bbox[1]) + 26

    subtitle_en = safe_text(manifest.get("subtitle_en") or "")
    if subtitle_en:
        spaced = " ".join(list(subtitle_en.replace(" ", "")))
        draw.text((48, y + 6), spaced, font=f_subtitle_en, fill=(*INK, 190))
        y += 62
    y += 30

    # 打钩清单（"两人即开课·满2人开班"这类招生信息）
    for item in checklist:
        draw_checkmark(draw, 66, y + 22, 20, (60, 150, 90))
        draw.text((104, y), safe_text(item), font=f_item, fill=INK)
        y += 62
    y += 20

    # 价格对比：原价划线 + 优惠说明 + 大字优惠价
    if has_price:
        if original_price:
            orig_text = f"原价 {original_price}元/人"
            draw.text((48, y), orig_text, font=f_price_small, fill=(*INK, 200))
            bbox = draw.textbbox((0, 0), orig_text, font=f_price_small)
            strike_y = y + (bbox[3] - bbox[1]) // 2
            draw.line([(48, strike_y), (48 + (bbox[2] - bbox[0]), strike_y)], fill=(*INK, 200), width=3)
            y += 54
        if price_note:
            draw.text((48, y), price_note, font=f_price_note, fill=(*INK, 220))
            y += 50
        price_line = f"{discounted_price}"
        draw.text((48, y), price_line, font=f_price_big, fill=GOLD)
        bbox = draw.textbbox((0, 0), price_line, font=f_price_big)
        unit_x = 48 + (bbox[2] - bbox[0]) + 10
        draw.text((unit_x, y + (bbox[3] - bbox[1]) - 34), "元/人", font=f_price_unit, fill=MAROON)
        y += (bbox[3] - bbox[1]) + 40

    # 老师名片：圆形头像 + 姓名 + 简介，装在一张浅色卡片里
    if has_teacher:
        card_h = 220
        draw.rounded_rectangle([48, y, W - 48, y + card_h], radius=24, fill=(255, 255, 255, 235))
        avatar_size = 160
        avatar_x, avatar_y = 72, y + 30
        if teacher_photo_url:
            try:
                resp = requests.get(teacher_photo_url, timeout=30)
                resp.raise_for_status()
                photo_img = Image.open(BytesIO(resp.content)).convert("RGBA")
                pw, ph = photo_img.size
                side = min(pw, ph)
                photo_img = photo_img.crop(((pw - side) // 2, (ph - side) // 2, (pw + side) // 2, (ph + side) // 2))
                photo_img = photo_img.resize((avatar_size, avatar_size))
                mask = Image.new("L", (avatar_size, avatar_size), 0)
                ImageDraw.Draw(mask).ellipse([0, 0, avatar_size, avatar_size], fill=255)
                canvas.paste(photo_img, (avatar_x, avatar_y), mask)
            except Exception as e:
                print("老师照片下载失败，跳过（不影响海报其它内容）：", e)
        else:
            draw.ellipse([avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size], fill=(230, 222, 205))
        text_x = avatar_x + avatar_size + 28
        draw.text((text_x, y + 34), teacher_name, font=f_teacher_name, fill=MAROON)
        bio_wrapped = textwrap.fill(teacher_bio, width=17)
        draw.multiline_text((text_x, y + 90), bio_wrapped, font=f_teacher_bio, fill=INK, spacing=8)
        y += card_h + 40

    # 编号课程信息（"上课地点""开课时间"这类，用画的编号圆圈，不用①②③字符）
    if course_info:
        draw.text((48, y), "课程信息", font=f_section_title, fill=MAROON)
        y += 50
        draw.line([(48, y), (W - 48, y)], fill=(*GOLD, 140), width=2)
        y += 30
        for idx, item in enumerate(course_info, 1):
            draw_number_circle(draw, 62, y + 20, 20, idx, f_number, GOLD, (255, 255, 255))
            draw.text((100, y), safe_text(item), font=f_course_item, fill=INK)
            y += 68
        y += 20

    bottom_tagline = safe_text(manifest.get("bottom_tagline") or "")
    if bottom_tagline:
        bbox = draw.textbbox((0, 0), bottom_tagline, font=f_bottom)
        tw = bbox[2] - bbox[0]
        draw.text(((W - tw) / 2, y), bottom_tagline, font=f_bottom, fill=MAROON)
        y += 60

    canvas.convert("RGB").save(out_path, quality=92)


def build_poster_expert_lecture(manifest, bg_img, out_path):
    """师资培训/机构品宣版：深色背景+金色大标题，左侧文字信息、右侧透出人像，
    底部一张"课程概览"卡片——适合师资培训、专家讲堂、机构品牌宣传这类场景。
    人像照片不做抠图（这里没有能力做背景分离），走的是"整张照片铺满+左侧渐深遮罩"
    的思路：文字这一侧几乎全暗保证可读，越往右越透出照片本身，效果上接近"人物在右侧"。
    """
    course_info = manifest.get("course_info_items") or []
    overview_text = safe_text(manifest.get("overview_text") or "")
    locations = manifest.get("locations") or []  # 复用做"底部信息行"（培训地点/培训时间）

    DARK = (20, 24, 34)
    GOLD = (196, 160, 90)
    GOLD_LIGHT = (226, 200, 148)

    extra_h = 0
    if course_info:
        extra_h += len(course_info) * 58
    if overview_text:
        extra_h += 240
    canvas_h = max(1920, 1500 + extra_h)

    canvas = Image.new("RGBA", (W, canvas_h), DARK)
    photo = cover_resize(bg_img, W, canvas_h).convert("RGBA")
    canvas.paste(photo, (0, 0))
    add_horizontal_gradient(canvas, (0, 0, W, canvas_h), 235, 70, color=DARK)
    add_vertical_gradient(canvas, (0, 0, W, 260), 120, 0, color=DARK)
    draw = ImageDraw.Draw(canvas, "RGBA")

    # 几条装饰性金色线框，呼应参考海报里那种几何切割感，纯线条拼出来，不依赖任何图片素材
    draw.line([(W - 420, -40), (W + 40, 460)], fill=(*GOLD, 80), width=3)
    draw.line([(W - 300, -40), (W + 40, 340)], fill=(*GOLD, 50), width=2)

    f_brand = font([NOTO_BOLD], 22)
    f_title = font([NOTO_BLACK, NOTO_BOLD], 64)
    f_subtitle = font([NOTO_BOLD], 32)
    f_info_item = font([NOTO_BOLD], 29)
    f_overview_head = font([NOTO_BLACK, NOTO_BOLD], 32)
    f_overview_body = font([NOTO_REGULAR], 27)
    f_footer_value = font([NOTO_REGULAR], 25)

    y = 56
    brand = safe_text(manifest.get("footer_tag") or "")
    if brand:
        bbox = draw.textbbox((0, 0), brand, font=f_brand)
        tw = bbox[2] - bbox[0]
        draw.text((W - 48 - tw, y), brand, font=f_brand, fill=(*GOLD_LIGHT, 235))
        y += 60
    else:
        y += 20

    title = safe_text(manifest.get("title") or "")
    wrapped = textwrap.fill(title, width=9)
    for line in wrapped.split("\n"):
        draw.text((48, y), line, font=f_title, fill=GOLD_LIGHT, stroke_width=1, stroke_fill=(0, 0, 0, 160))
        bbox = draw.textbbox((0, 0), line, font=f_title)
        y += (bbox[3] - bbox[1]) + 22
    y += 6

    subtitle = safe_text(manifest.get("highlight_word") or "")
    if subtitle:
        draw.text((48, y), "〉 " + subtitle, font=f_subtitle, fill=(255, 255, 255, 235))
        y += 76
    y += 40

    for item in course_info:
        draw.text((48, y), safe_text(item), font=f_info_item, fill=(255, 255, 255, 235))
        y += 50
    y += 30

    if overview_text:
        overview_wrapped = textwrap.fill(overview_text, width=21)
        line_count = overview_wrapped.count("\n") + 1
        card_h = 100 + line_count * 42
        card_y = canvas_h - card_h - 130
        draw.rounded_rectangle([48, card_y, W - 48, card_y + card_h], radius=20, fill=(*GOLD, 235))
        draw.rounded_rectangle([48, card_y - 46, 300, card_y + 12], radius=14, fill=DARK)
        draw.text((70, card_y - 36), "课程概览", font=f_overview_head, fill=GOLD_LIGHT)
        draw.multiline_text((70, card_y + 30), overview_wrapped, font=f_overview_body, fill=(32, 24, 14), spacing=10)

    footer_y = canvas_h - 90
    for i, loc in enumerate(locations[:2]):
        draw.text((48, footer_y + i * 40), safe_text(loc), font=f_footer_value, fill=(255, 255, 255, 220))

    canvas.convert("RGB").save(out_path, quality=92)


def build_poster_teacher_profile(manifest, bg_img, out_path):
    """教师个人简介版：浅色高级感底色 + 教师照片 + "教师简介"/"教学成果"两组清单，
    不需要背景素材（main()里已经跳过了get_background，这里bg_img传进来也是None，不使用），
    纯粹是教师个人履历/名片风格的海报，没有价格、没有招生信息。
    """
    teacher_name = safe_text(manifest.get("teacher_name") or "")
    teacher_photo_url = manifest.get("teacher_photo_url")
    role_label = safe_text(manifest.get("highlight_word") or "")
    studio_name_en = safe_text(manifest.get("subtitle_en") or "")
    experience_badge = safe_text(manifest.get("badge_text") or "")
    courses = manifest.get("locations") or []
    bio_items = manifest.get("checklist_items") or []
    achievement_items = manifest.get("highlights") or []

    CREAM = (238, 230, 216)
    INK = (58, 50, 44)
    GOLD = (162, 124, 74)

    extra_h = 0
    if bio_items:
        extra_h += 56 + len(bio_items) * 52
    if achievement_items:
        rows = (len(achievement_items) + 1) // 2
        extra_h += 56 + rows * 52
    canvas_h = max(1920, 1560 + extra_h)

    canvas = Image.new("RGBA", (W, canvas_h), CREAM)
    draw = ImageDraw.Draw(canvas, "RGBA")

    f_tag = font([NOTO_BOLD], 22)
    f_courses = font([NOTO_REGULAR], 23)
    f_studio_en = font([NOTO_REGULAR], 38)
    f_name = font([NOTO_BLACK, NOTO_BOLD], 54)
    f_role = font([NOTO_BOLD], 28)
    f_badge_num = font([NOTO_BLACK, NOTO_BOLD], 38)
    f_section_head = font([NOTO_BOLD], 30)
    f_item = font([NOTO_REGULAR], 25)

    y = 56
    if courses:
        rect = draw_pill(draw, (48, y), "开设课程", f_tag, fill=(*GOLD, 230), text_fill=(255, 255, 255, 255))
        courses_text = safe_text("丨".join(courses))
        draw.text((rect[2] + 16, y + 8), courses_text, font=f_courses, fill=INK)
        y += 66
    if studio_name_en:
        draw.text((48, y), studio_name_en, font=f_studio_en, fill=(*INK, 200))
        y += 76

    photo_top = y + 16
    photo_h_area = 760
    if teacher_photo_url:
        try:
            resp = requests.get(teacher_photo_url, timeout=30)
            resp.raise_for_status()
            photo_img = Image.open(BytesIO(resp.content)).convert("RGBA")
            photo_img = cover_resize(photo_img, W - 96, photo_h_area).convert("RGBA")
            mask = Image.new("L", (W - 96, photo_h_area), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, W - 96, photo_h_area], radius=24, fill=255)
            canvas.paste(photo_img, (48, photo_top), mask)
        except Exception as e:
            print("教师照片下载失败，用纯色占位（不影响海报其它内容）：", e)
            draw.rounded_rectangle([48, photo_top, W - 48, photo_top + photo_h_area], radius=24, fill=(220, 210, 195))
    else:
        draw.rounded_rectangle([48, photo_top, W - 48, photo_top + photo_h_area], radius=24, fill=(220, 210, 195))
    y = photo_top + photo_h_area + 46

    if experience_badge:
        bbox = draw.textbbox((0, 0), experience_badge, font=f_badge_num)
        badge_w = (bbox[2] - bbox[0]) + 56
        badge_h = 84
        badge_y = photo_top + photo_h_area - badge_h - 24
        draw.rounded_rectangle([W - 48 - badge_w, badge_y, W - 48, badge_y + badge_h], radius=12, fill=(*CREAM, 245))
        draw.text((W - 48 - badge_w + 18, badge_y + 18), experience_badge, font=f_badge_num, fill=GOLD)

    if teacher_name:
        draw.text((48, y), teacher_name, font=f_name, fill=INK)
        bbox = draw.textbbox((0, 0), teacher_name, font=f_name)
        y += (bbox[3] - bbox[1]) + 14
    if role_label:
        draw.text((48, y), role_label, font=f_role, fill=GOLD)
        y += 54
    y += 20
    draw.line([(48, y), (W - 48, y)], fill=(*GOLD, 150), width=2)
    y += 34

    if bio_items:
        draw.text((48, y), "教师简介", font=f_section_head, fill=INK)
        y += 58
        for item in bio_items:
            draw.ellipse([50, y + 11, 58, y + 19], fill=GOLD)
            draw.text((76, y), safe_text(item), font=f_item, fill=INK)
            y += 52
        y += 26

    if achievement_items:
        draw.text((48, y), "教学成果", font=f_section_head, fill=INK)
        y += 58
        col_w = (W - 96) // 2
        for idx, item in enumerate(achievement_items):
            col = idx % 2
            row = idx // 2
            item_x = 48 + col * col_w
            item_y = y + row * 52
            draw.ellipse([item_x + 2, item_y + 11, item_x + 10, item_y + 19], fill=GOLD)
            draw.text((item_x + 26, item_y), safe_text(item), font=f_item, fill=INK)
        rows = (len(achievement_items) + 1) // 2
        y += rows * 52 + 20

    canvas.convert("RGB").save(out_path, quality=92)


def build_poster(manifest, bg_img, out_path):
    """按 manifest['template'] 分发到对应版式，找不到就用标准版兜底。"""
    template = manifest.get("template") or "standard"
    if template == "brand":
        build_poster_brand(manifest, bg_img, out_path)
    elif template == "promo":
        build_poster_promo(manifest, bg_img, out_path)
    elif template == "recruit":
        build_poster_recruit(manifest, bg_img, out_path)
    elif template == "expert_lecture":
        build_poster_expert_lecture(manifest, bg_img, out_path)
    elif template == "teacher_profile":
        build_poster_teacher_profile(manifest, bg_img, out_path)
    else:
        build_poster_standard(manifest, bg_img, out_path)


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    manifest = fetch_manifest()
    # "教师个人简介版"这个版式不需要一张"背景图"——它的主视觉就是老师照片本身，走的是
    # 浅色卡纸底色，不是"照片+文字浮在上面"那种结构。这里跳过背景图获取，省一次下载/AI生成，
    # 用户选这个版式时其实也不需要去选背景素材
    if manifest.get("template") == "teacher_profile":
        bg_img = None
    else:
        bg_img = get_background(manifest)

    out_path = f"{WORKDIR}/poster.jpg"
    build_poster(manifest, bg_img, out_path)

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )
    key = f"posters/{POSTER_ID}.jpg"
    s3.upload_file(out_path, R2_BUCKET_NAME, key, ExtraArgs={"ContentType": "image/jpeg"})
    poster_url = f"{R2_PUBLIC_BASE_URL}/{key}"

    callback("succeeded", poster_url=poster_url)
    print("完成，海报地址:", poster_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("生成失败:", e, file=sys.stderr)
        callback("failed", error=str(e))
        sys.exit(1)
