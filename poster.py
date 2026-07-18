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
        prompt = manifest.get("ai_bg_prompt") or (
            f"abstract travel poster background, {manifest.get('title', '')}, "
            "soft gradient, mountains and grassland silhouette, minimal illustration, "
            "no text, no letters, no words, vertical composition"
        )
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


def build_poster(manifest, bg_img, out_path):
    """按 manifest['template'] 分发到对应版式，找不到就用标准版兜底。"""
    template = manifest.get("template") or "standard"
    if template == "brand":
        build_poster_brand(manifest, bg_img, out_path)
    elif template == "promo":
        build_poster_promo(manifest, bg_img, out_path)
    else:
        build_poster_standard(manifest, bg_img, out_path)


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    manifest = fetch_manifest()
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
