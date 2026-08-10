import os
import re
import subprocess
import sys
import wave
import requests
from PIL import Image, ImageStat

try:
    import numpy as np
except Exception:
    np = None
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
TTS_VOICE = os.environ.get("TTS_VOICE") or "zh-CN-YunxiNeural"

WORKDIR = "work"
FONTS_DIR = "fonts"  # render.yml会把站酷快乐体下载到这个目录，跟poster.py用的是同一套约定


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


def callback(status, video_url=None, cover_url=None, cover_options=None, error=None, render_metrics=None):
    payload = {"task_id": TASK_ID, "secret": RENDER_SECRET, "status": status}
    if video_url:
        payload["video_url"] = video_url
    if cover_url:
        payload["cover_url"] = cover_url
    if cover_options:
        payload["cover_options"] = cover_options
    if error:
        payload["error"] = redact_urls(error)[:2000]
    if render_metrics:
        payload["render_metrics"] = render_metrics

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
    "green": (110, 220, 140),
    "purple": (200, 150, 255),
    "pink": (255, 150, 190),
    "orange": (255, 160, 80),
}

# 字幕字体选项："标准黑体"是Noto Sans CJK的最粗字重，规规矩矩、清晰易读；"活泼艺术字"是
# 站酷快乐体（跟海报标题用的是同一份字体，Google Fonts官方OFL开源协议分发，免费商用），
# 圆润饱满，短视频平台上常见的那种"更漂亮"的字幕大多是这类风格的字体，不是靠描边/阴影做出来的。
# 后面5款是给"诗意品牌版"海报配的那一套书法/手写体字体库，同一份字体文件视频这边也能直接用——
# 字幕这种场景不是每次都适合书法体（笔画细、连笔多的字体，字号小的时候容易糊成一团看不清），
# 所以没有把它们设成默认，作为额外的可选风格，想用的时候自己在"字幕设置"里选
def resolve_subtitle_font(font_style, bold):
    if font_style == "artistic":
        return "ZCOOL KuaiLe"  # 站酷快乐体，圆润饱满、偏可爱活泼；本身只有一个常规字重，没有"加粗"这个概念，bold参数对它不生效
    if font_style == "artistic2":
        return "ZCOOL QingKe HuangYou"  # 站酷庆科黄油体，比快乐体更粗壮扎实、更有冲击力，同样只有常规字重
    if font_style == "xiaowei":
        return "ZCOOL XiaoWei"  # 站酷小薇体，细笔画，清新文艺
    if font_style == "mashanzheng":
        return "Ma Shan Zheng"  # 马善政毛笔体，粗细分明的毛笔字，传统喜庆
    if font_style == "zhimangxing":
        return "Zhi Mang Xing"  # 知末行书，行云流水，柔和飘逸
    if font_style == "longcang":
        return "Long Cang"  # 龙藏体，古朴雅致，偏古风韵味
    if font_style == "liujianmaocao":
        return "Liu Jian Mao Cao"  # 刘建毛草，洒脱写意的草书，笔画连得比较厉害，字号小的时候不太好辨认，字号大、字数少的场景（比如开头标题）更合适
    return "Noto Sans CJK SC Black" if bold else "Noto Sans CJK SC"


# 字幕背景框的几个配色方案：黑色半透明是原来那款，低调百搭；后面三个是仿照新闻类账号常见的
# "色块贴片"标题条效果（黄底黑字/白底黑字/蓝底白字），色块是不透明的实色，视觉冲击力更强
SUBTITLE_BOX_SCHEMES = {
    "black": {"back": "&H80000000&", "text_override": None},
    "yellow": {"back": "&H0022D4FC&", "text_override": (20, 20, 20)},
    "white_box": {"back": "&H00FFFFFF&", "text_override": (20, 20, 20)},
    "blue": {"back": "&H00C46A2E&", "text_override": (255, 255, 255)},
}


def rgb_to_ass_bgr(rgb):
    """ASS字幕颜色是 BGR 顺序（不是常见的RGB），这里做一次转换"""
    r, g, b = rgb
    return f"&H{b:02X}{g:02X}{r:02X}&"


def rgb_to_ass_back_colour(rgb, alpha_hex="00"):
    """BackColour（背景色块）比普通文字颜色多一位alpha透明度前缀，格式是&HAABBGGRR&——
    alpha_hex传"00"是完全不透明（新闻字幕式那种实色信息条用这个），传比较大的十六进制值
    可以做半透明效果"""
    r, g, b = rgb
    return f"&H{alpha_hex}{b:02X}{g:02X}{r:02X}&"


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


def split_text_into_single_line_chunks(text, font_size, canvas_width=1080, margin=60):
    """把一句解说词切成若干段，保证每一段单独显示的时候都能在一行内放完，不会挤成两行——
    跟下面这个思路是配套的：与其把一句话硬塞进同一屏幕、用\\N换行挤成两三行文字块，
    不如把这句话拆成几个短句，按时间顺序一段一段单独显示，每次画面上只有一行字，
    这也是抖音/小红书这类短视频最常见的字幕呈现方式。这里的断句规则（优先标点、
    找不到标点就按字数硬断）跟原来的wrap_subtitle_text是同一套，只是返回的是
    一个列表（后面每一项都会单独生成一条时间不同的字幕，而不是拼成一整块文字）。
    """
    usable_width = canvas_width - margin * 2
    max_chars = max(4, int(usable_width / font_size))
    if len(text) <= max_chars:
        return [text]

    break_chars = "，。！？、,."
    chunks = []
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
        chunks.append(remaining[:split_pos])
        remaining = remaining[split_pos:]
    if remaining:
        chunks.append(remaining)

    # 按标点切完之后，偶尔会剩下一两个字的"孤字"小段（比如恰好在句尾标点前一两个字的地方
    # 被切开），单独显示成一段的时候，画面上会突然只剩一个字，看着很突兀，这就是这次反馈的
    # "解说字幕单独出现一个字"的真正原因。这里补一道合并处理：长度不够的小段直接并到
    # 相邻的一段里（优先并到前一段，如果自己就是切出来的第一段就并到后一段），宁可某一段
    # 稍微超出"刚好一行宽度"这个理想尺寸，也不会再出现孤零零一个字单独展示的情况
    min_chunk_len = max(2, max_chars // 3)
    merged = []
    for chunk in chunks:
        if merged and len(chunk) < min_chunk_len:
            merged[-1] = merged[-1] + chunk
        else:
            merged.append(chunk)
    if len(merged) > 1 and len(merged[0]) < min_chunk_len:
        merged[1] = merged[0] + merged[1]
        merged = merged[1:]
    return merged


def wrap_subtitle_text(text, font_size, canvas_width=1080, margin=60):
    """中文字幕手动换行：ASS/libass的自动换行是按空格断词的，中文没有空格，
    一整句会被当成一个不可拆分的词，超出画面宽度不会自动折行，而是直接溢出被裁掉。
    这里按字号估算每行大概能放多少个字，优先在标点处断行，找不到合适标点就硬断。
    注：解说字幕现在已经改用上面那个split_text_into_single_line_chunks（一次只显示
    一行、按时间切成多段），不再用这个函数的\\N多行拼接结果；这个函数继续保留是给
    极少数还需要"多行拼一块"效果的地方兜底用，目前实际没有调用方在用它。"""
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


TITLE_CAPTION_COLOR_SCHEMES = {
    # 每个配色方案是(主色, 副色)，逐行交替使用，white固定打头，第二个颜色决定整体的"味道"——
    # 标题字幕、底部字幕的"彩色字幕式"用的是同一份配色表，选项一致
    "gold": ((255, 255, 255), (255, 204, 0)),      # 白金，默认，新闻/热门内容通用
    "red": ((255, 255, 255), (255, 66, 66)),       # 白红，更醒目/紧迫，适合促销、热点类
    "blue": ((255, 255, 255), (100, 190, 255)),    # 白蓝，更冷静/信息感，适合科普、攻略类
    "green": ((255, 255, 255), (80, 220, 130)),    # 白绿，清新自然，适合旅游、健康类内容
    "purple": ((255, 255, 255), (190, 130, 255)),  # 白紫，梦幻高级，适合美妆、艺术类内容
    "pink": ((255, 255, 255), (255, 130, 180)),    # 白粉，甜美活泼，适合美食、萌宠类内容
    "orange": ((255, 255, 255), (255, 140, 60)),   # 白橙，热情有活力，适合运动、促销类内容
    "silver": ((255, 255, 255), (200, 200, 205)),  # 白银，低调高级，适合商务、科技类内容
    "white": ((255, 255, 255), (255, 255, 255)),   # 纯白不交替，低调款，适合内容本身已经很有冲击力的素材
}


def build_title_caption_ass(lines, out_path, duration_sec=4.5, font_size=90, color_scheme="gold", font_style="standard", canvas_w=1080, canvas_h=1920):
    """开头顶部大字标题字幕（悬念式大标题样式）：仿照新闻/热门短视频账号常见的开头字幕——
    多行文字叠在画面顶部，两色逐行交替（配色方案可选，见TITLE_CAPTION_COLOR_SCHEMES），
    加粗+黑色描边，只在视频最开头这几秒出现一次，之后自动消失，不会跟下面逐句解说的字幕
    （build_subtitle_ass）冲突——这是完全独立的第二层ASS字幕，渲染的时候在ffmpeg里跟主字幕
    链式叠加(-vf "subtitles=A,subtitles=B")，互不干扰。逐句解说字幕本身还是保持"统一一种颜色"
    不变，多色交替是这层"标题字幕"独有的风格，不会影响到解说字幕那边。
    canvas_w/canvas_h：字号/边距原本是照着1080×1920这个竖屏尺寸调出来的，选了16:9/1:1这类
    别的比例时，按画布宽高比例把字号和边距同步缩放一下，不然套用原本给竖屏调的数值，
    在横屏画面上会显得太小、位置也不对
    """
    if not lines:
        return False
    scale_w = canvas_w / 1080
    scale_h = canvas_h / 1920
    font_size = round(font_size * scale_w)
    font_name = resolve_subtitle_font(font_style, True)
    primary_rgb, secondary_rgb = TITLE_CAPTION_COLOR_SCHEMES.get(color_scheme, TITLE_CAPTION_COLOR_SCHEMES["gold"])
    white_tag = rgb_to_ass_bgr(primary_rgb)
    gold_tag = rgb_to_ass_bgr(secondary_rgb)

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
        # 每一条标题字幕自己也可能超出屏幕宽度（AI没完全卡住字数上限，或者字号选得比较大），
        # 之前这里没做任何限宽处理，字数一多就直接从画面左右两边溢出裁掉。这里跟解说字幕
        # 用的是同一套按字号折算"一行大概能放多少字"的逻辑，超出的话在这一条内部再换行，
        # 保证不会跑出屏幕；同一条原始标题换行出来的几个小行颜色保持一致，只有不同的
        # 原始标题行之间才会交替颜色
        sub_lines = split_text_into_single_line_chunks(clean_line, font_size, canvas_width=canvas_w)
        wrapped_line = "\\N".join(sub_lines)
        styled_parts.append(f"{{\\c{color}}}{wrapped_line}")
    if not styled_parts:
        return False
    text = "\\N".join(styled_parts)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\n"
        f"PlayResY: {canvas_h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{white_tag},{white_tag},&H000000&,&H000000&,0,0,0,0,100,100,0,0,"
        f"1,4,1,8,{round(60*scale_w)},{round(60*scale_w)},{round(120*scale_h)},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    end_time = sec_to_ass_time(duration_sec)
    dialogue = f"Dialogue: 0,0:00:00.00,{end_time},Default,,0,0,0,,{text}\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + dialogue)
    return True


# 底部字幕三种风格的具体样式参数：(字体风格, 主色, 副色/交替色或None, 是否要背景色块, 背景色)
# - news：新闻字幕式，仿照电视新闻下方的"信息条"，一块实色底配白字，庄重、信息感强
# - colorful：彩色字幕式，白色/金色逐行交替（跟标题字幕的多色交替是同一个思路），不加背景块
# - artistic：艺术字幕式，站酷快乐体+金色，不加背景块，走活泼路线
# 新闻字幕式的信息条背景色选项——特意用比较深、比较沉的色调（不是鲜艳原色），实色大面积
# 铺在画面上才不会显得刺眼，跟标题字幕/彩色字幕式那种"鲜艳交替色"是完全不同的用色思路
NEWS_BAR_COLOR_RGB = {
    "red": (130, 24, 24),
    "blue": (22, 58, 110),
    "green": (24, 82, 44),
    "gold": (110, 82, 24),
    "purple": (70, 30, 90),
    "pink": (120, 40, 70),
    "orange": (130, 66, 20),
    "silver": (70, 70, 76),
    "white": (28, 28, 32),  # "纯白"配色方案在信息条上没法直接用白底白字，这里退回深灰底，文字保持白色
}

BOTTOM_CAPTION_STYLE_PRESETS = {
    # font_style 和 box 是每种风格固定不变的部分（是否用艺术字体、是否要背景块）；
    # 具体用什么颜色由 color_scheme 参数决定，几种风格各自从不同的调色板里取色：
    # news 从 NEWS_BAR_COLOR_RGB 取信息条底色，colorful/artistic/newsflash 从
    # TITLE_CAPTION_COLOR_SCHEMES 取字色
    "news": {"font_style": "standard", "box": True},
    "colorful": {"font_style": "standard", "box": False},
    "artistic": {"font_style": "artistic", "box": False},
    # 新闻快讯风：呼应海报那边新加的"新闻快讯风"版式——加粗描边更重、每行前面加一个
    # 三角警示符号（不用emoji字符，emoji在不同系统/字体下渲染很不稳定，这里用的是普通
    # 几何符号▲，属于基础Unicode符号，跟文字用的是同一套字体渲染，不会有缺字问题）
    "newsflash": {"font_style": "standard", "box": False},
}


def build_bottom_caption_ass(lines, out_path, duration_sec=6, font_size=64, style="news", color_scheme="gold", max_lines=2, position="bottom", canvas_w=1080, canvas_h=1920):
    """底部字幕：跟开头顶部的标题字幕是同一个思路的另一层，位置在画面底部，内容也是AI写脚本
    时额外生成的一组文字（自己独立的内容，跟解说词、标题字幕都不是同一段话）。三种风格靠
    BOTTOM_CAPTION_STYLE_PRESETS分别配置颜色/背景/字体，视觉效果完全不同：
    - 新闻字幕式：底部一块实色信息条，仿电视新闻下方那种"字幕条"
    - 彩色字幕式：白金两色逐行交替，不加背景块，风格上呼应标题字幕
    - 艺术字幕式：站酷快乐体+金色，走活泼路线
    显示时长支持传具体秒数，也支持"persist"（在main()里已经转换成总时长的具体秒数了，这里
    只处理数字）。这层是完全独立的第三层ASS字幕，在ffmpeg里跟解说字幕、标题字幕链式叠加，
    互不冲突；生成失败/没内容不影响主流程。

    max_lines: 底部字幕最多显示几行——Worker那边生成脚本的时候已经卡过AI最多给2条、每条
    最多12个字了，但这里是真正决定"最终画面上到底显示几行"的地方（同一条内容在不同字号下
    折算出来的实际行数不一样），所以在这里再兜底一次：不管上游传来多少条、每条多长，最终
    渲染出来的总行数严格不超过max_lines，超出的部分直接截断，不会为了塞下更多内容硬挤成
    三行以上、占用太多画面空间。
    """
    if not lines:
        return False
    scale_w = canvas_w / 1080
    scale_h = canvas_h / 1920
    font_size = round(font_size * scale_w)
    preset = BOTTOM_CAPTION_STYLE_PRESETS.get(style, BOTTOM_CAPTION_STYLE_PRESETS["news"])
    font_name = resolve_subtitle_font(preset["font_style"], True)

    # 三种风格各自从不同的调色板里取色：news取信息条底色（配白字），colorful是白色+配色方案的
    # 第二色交替，artistic是单独用配色方案的第二色（金/红/蓝那些鲜艳色，套上艺术字体更好看，
    # 不用白色单独显示，视觉上更有辨识度）
    if style == "news":
        bar_rgb = NEWS_BAR_COLOR_RGB.get(color_scheme, NEWS_BAR_COLOR_RGB["red"])
        primary_tag = rgb_to_ass_bgr((255, 255, 255))
        secondary_tag = None
    else:
        _, secondary_rgb = TITLE_CAPTION_COLOR_SCHEMES.get(color_scheme, TITLE_CAPTION_COLOR_SCHEMES["gold"])
        if style == "artistic":
            primary_tag = rgb_to_ass_bgr(secondary_rgb)
            secondary_tag = None
        else:  # colorful、newsflash 都是白色+配色方案第二色交替
            primary_tag = rgb_to_ass_bgr((255, 255, 255))
            secondary_tag = rgb_to_ass_bgr(secondary_rgb)

    def escape_ass_text(t):
        return str(t).replace("{", "").replace("}", "").replace("\\", "").strip()

    styled_parts = []
    lines_used = 0
    for idx, line in enumerate(lines):
        if lines_used >= max_lines:
            break
        clean_line = escape_ass_text(line)
        if not clean_line:
            continue
        if style == "newsflash":
            clean_line = "▲ " + clean_line
        color = secondary_tag if (secondary_tag and idx % 2 == 1) else primary_tag
        sub_lines = split_text_into_single_line_chunks(clean_line, font_size, canvas_width=canvas_w)
        remaining_budget = max_lines - lines_used
        if len(sub_lines) > remaining_budget:
            sub_lines = sub_lines[:remaining_budget]
        lines_used += len(sub_lines)
        wrapped_line = "\\N".join(sub_lines)
        styled_parts.append(f"{{\\c{color}}}{wrapped_line}")
    if not styled_parts:
        return False
    text = "\\N".join(styled_parts)

    border_style = 3 if preset["box"] else 1
    outline_val = 10 if preset["box"] else (6 if style == "newsflash" else 4)
    back_colour = rgb_to_ass_back_colour(bar_rgb) if preset["box"] else "&H000000&"
    # 位置：top是真正的顶部对齐（从上往下长，行数变化不会导致位置跳动，适合"纯风光+音乐"
    # 模式那种整段话从头显示到尾的用法）；middle不用真正的正中心对齐（Alignment=5）——跟解说
    # 字幕的"middle"选项踩过的坑一样，行数不一致时正中心对齐看起来会来回跳，这里改用
    # "从底部往上撑一大截"的方式模拟居中，位置更稳定；bottom是这个函数原来的默认行为，
    # 不动，保证AI生成的补充底部字幕这条老路径不受影响
    _pos_map = {"top": (8, 90), "middle": (2, 850), "bottom": (2, 260)}
    alignment, margin_v = _pos_map.get(position, _pos_map["bottom"])
    margin_v = round(margin_v * scale_h)

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\n"
        f"PlayResY: {canvas_h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{primary_tag},{primary_tag},&H000000&,{back_colour},0,0,0,0,100,100,0,0,"
        f"{border_style},{outline_val},0,{alignment},{round(60*scale_w)},{round(60*scale_w)},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    end_time = sec_to_ass_time(duration_sec)
    dialogue = f"Dialogue: 0,0:00:00.00,{end_time},Default,,0,0,0,,{text}\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + dialogue)
    return True


NUMBER_HIGHLIGHT_PATTERN = re.compile(r"\d+\.?\d*%?")


def apply_number_highlight(text, base_color_tag, highlight_color_tag):
    """把文本里的数字（含百分号）用ASS内联颜色标签包起来改成高亮色，其余部分保持底色不变——
    这是纯粹按正则规则识别数字模式、在渲染阶段由程序自己上色，不依赖AI输出任何markup，
    跟之前"自动加emoji"那次的问题是完全不同性质的东西：emoji是内容层面的字符，可能被AI
    生成又可能目标字体没收录、变成缺字方块；这里操作的是ASCII数字字符，不存在缺字风险，
    也不需要经过AI的手，稳定可控。
    """
    def repl(m):
        return f"{{\\c{highlight_color_tag}}}{m.group(0)}{{\\c{base_color_tag}}}"
    return NUMBER_HIGHLIGHT_PATTERN.sub(repl, text)


def build_subtitle_ass(srt_content, out_path, font_size=76, position="bottom", color_key="white", bold=True,
                        highlight_numbers=True, bg_box=False, font_style="standard", box_scheme="black",
                        canvas_w=1080, canvas_h=1920):
    """生成逐句解说字幕的ASS文件

    font_size: 字号（数字越大字越大）
    position: 'top' / 'middle' / 'bottom'
    color_key: SUBTITLE_COLOR_MAP 里的一个key，如 'white'/'yellow'/'cyan'/'red'
    bold: 是否加粗
    highlight_numbers: 是否把句子里的数字/百分比自动高亮成金色，其余文字保持统一颜色不变——
        这不是"逐句变色"（整句话换颜色），是"句子里的数字单独换颜色，其余文字不变"，
        两者是完全不同的效果，数字高亮更精准、更不容易出问题
    bg_box: 是否给字幕加一块背景框（更适合背景比较杂乱、文字描边也压不住的素材）
    font_style: 'standard'（Noto Sans CJK黑体，规矩清晰）/ 'artistic'（站酷快乐体，圆润饱满，
        短视频平台常见的"更漂亮"字幕大多是这类风格，跟海报标题是同一份字体）
    box_scheme: bg_box开启时用哪种配色，SUBTITLE_BOX_SCHEMES里的一个key
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
        "lower": (2, 420),
        "bottom": (2, 110),
    }
    scale_w = canvas_w / 1080
    scale_h = canvas_h / 1920
    font_size = round(font_size * scale_w)
    alignment, margin_v = position_map.get(position, position_map["bottom"])
    margin_v = round(margin_v * scale_h)
    color_rgb = SUBTITLE_COLOR_MAP.get(color_key, SUBTITLE_COLOR_MAP["white"])
    box_info = SUBTITLE_BOX_SCHEMES.get(box_scheme, SUBTITLE_BOX_SCHEMES["black"])
    # 背景框选了黄底/白底这类浅色方案的时候，文字还用原来的颜色（比如黄色）会跟浅色底几乎融为
    # 一体看不清，这种情况下强制把文字换成方案自带的对比色（黑字或白字），不受用户选的"字幕颜色"
    # 影响；黑色半透明底框不存在这个问题，文字颜色还是照用户选的来
    if bg_box and box_info["text_override"]:
        color_rgb = box_info["text_override"]
    color_tag = rgb_to_ass_bgr(color_rgb)
    highlight_tag = rgb_to_ass_bgr((255, 204, 0))  # 数字高亮固定用金色，跟标题字幕的强调色是同一个色系，风格统一
    font_name = resolve_subtitle_font(font_style, bold)
    # BorderStyle=1是"文字+描边"（默认样式）；BorderStyle=3是"文字+一块实心底色框"，
    # 加了背景框之后描边就不需要了（框本身已经能保证在任何背景下都看得清），Outline/Shadow
    # 这两个数值在BorderStyle=3下含义会变成"背景框的内边距/投影"，这里给了比较克制的数值。
    # 不加背景框的描边粗细从3调到4，配合"活泼艺术字"这类更饱满的字体看起来更扎实、不单薄
    border_style = 3 if bg_box else 1
    outline_val = 8 if bg_box else 4
    back_colour = box_info["back"] if bg_box else "&H000000&"

    cues = parse_srt(srt_content)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {canvas_w}\n"
        f"PlayResY: {canvas_h}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},{color_tag},{color_tag},&H000000&,{back_colour},0,0,0,0,100,100,0,0,"
        f"{border_style},{outline_val},0,{alignment},{round(60*scale_w)},{round(60*scale_w)},{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [header]
    event_count = 0
    for start, end, text in cues:
        # 之前是把整句话用\N拼成一块多行文字、一次性显示——这句话稍微长一点（解说词
        # 通常15-25字），在"特大"这种大字号下一行根本放不下，必然挤成两三行。现在改成
        # 按能塞进一行的长度把这句话切成几段，每段单独算一段独立的显示时间（按每段字数
        # 占全句字数的比例，从这句话原本的[start,end]这段时间里按比例分），实现"画面上
        # 任何时刻只有一行字，跟着语速一段一段往下切"的效果，这也是短视频最常见的字幕呈现方式
        chunks = split_text_into_single_line_chunks(text, font_size, canvas_width=canvas_w)
        total_chars = sum(len(c) for c in chunks) or 1
        total_duration = max(0.01, end - start)
        cursor = start
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip("，。！？、,. ")
            if not chunk:
                continue
            if i == len(chunks) - 1:
                seg_end = end  # 最后一段直接对齐到原本的结束时间，避免累计的浮点误差导致提前结束
            else:
                portion = len(chunk) / total_chars
                seg_end = cursor + total_duration * portion
            seg_text = apply_number_highlight(chunk, color_tag, highlight_tag) if highlight_numbers else chunk
            if position == "middle":
                this_margin_v = margin_v  # 现在保证每段都是单行，不用再按行数做微调，直接用统一锚点就行
            else:
                this_margin_v = 0
            lines.append(f"Dialogue: 0,{sec_to_ass_time(cursor)},{sec_to_ass_time(seg_end)},Default,,0,0,{this_margin_v},,{seg_text}\n")
            event_count += 1
            cursor = seg_end
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    return event_count
# ==================== 字幕生成函数结束 ====================


def detect_rhythm_beats(audio_path, workdir, max_beats=160):
    """轻量级节奏点检测：不依赖librosa，使用短时能量峰值估计重拍位置。"""
    wav_path=os.path.join(workdir,"bgm_rhythm.wav")
    try:
        if np is None: return []
        run(["ffmpeg","-y","-i",audio_path,"-ac","1","-ar","22050","-sample_fmt","s16",wav_path])
        with wave.open(wav_path,"rb") as wf:
            rate=wf.getframerate(); raw=wf.readframes(wf.getnframes())
        samples=np.frombuffer(raw,dtype=np.int16).astype(np.float32)/32768.0
        if len(samples)<rate: return []
        hop=int(rate*0.05); frame=int(rate*0.10)
        e=[]; times=[]
        for start in range(0,len(samples)-frame,hop):
            c=samples[start:start+frame]; e.append(float(np.sqrt(np.mean(c*c)))); times.append(start/rate)
        e=np.asarray(e,dtype=np.float32)
        if len(e)<8: return []
        smooth=np.convolve(e,np.ones(9,dtype=np.float32)/9,mode='same')
        flux=np.maximum(e-smooth,0)
        threshold=max(float(np.percentile(flux,72)),float(np.mean(flux)+0.35*np.std(flux)))
        peaks=[]; last=-999
        for i in range(1,len(flux)-1):
            if flux[i]>=threshold and flux[i]>=flux[i-1] and flux[i]>=flux[i+1]:
                t=times[i]
                if t-last>=0.28: peaks.append(t); last=t
        return peaks[:max_beats]
    except Exception as e:
        print("节奏点分析失败，回退普通镜头节奏：",e); return []


def align_durations_to_beats(durations, beats, total_duration, min_duration=1.5):
    if not beats or len(durations)<2 or total_duration<5: return durations
    n=len(durations); planned=[]; cur=0.0
    for d in durations: cur+=float(d); planned.append(cur)
    beats=[b for b in beats if min_duration<=b<=total_duration-min_duration]
    out=[]; prev=0.0
    for idx,target in enumerate(planned[:-1]):
        remain=n-idx-1; low=prev+min_duration; high=total_duration-remain*min_duration
        c=[b for b in beats if low<=b<=high]
        if c:
            b=min(c,key=lambda x:abs(x-target))
            if abs(b-target)<=0.85: target=b
        out.append(target-prev); prev=target
    out.append(total_duration-prev)
    if any(d<0.9 for d in out): return durations
    return [round(d*total_duration/sum(out),3) for d in out]


def sec_to_srt_time(sec):
    sec=max(0,float(sec)); whole=int(sec); ms=int(round((sec-whole)*1000))
    if ms>=1000: whole+=1; ms=0
    return f"{whole//3600:02d}:{(whole%3600)//60:02d}:{whole%60:02d},{ms:03d}"


def transcribe_custom_voice_to_srt(audio_path,out_path):
    try:
        from faster_whisper import WhisperModel
        model=WhisperModel(os.environ.get("WHISPER_MODEL","small"),device=os.environ.get("WHISPER_DEVICE","cpu"),compute_type=os.environ.get("WHISPER_COMPUTE_TYPE","int8"))
        segments,_=model.transcribe(audio_path,language="zh",beam_size=5,vad_filter=True)
        lines=[]
        for i,seg in enumerate(segments,1):
            text=(seg.text or '').strip()
            if text: lines.append(f"{i}\n{sec_to_srt_time(seg.start)} --> {sec_to_srt_time(seg.end)}\n{text}\n")
        if not lines: return False
        with open(out_path,'w',encoding='utf-8') as f: f.write('\n'.join(lines))
        return True
    except Exception as e:
        print("自定义配音ASR不可用，退回基础字幕：",e); return False


def make_shot_clip(i, shot, duration, canvas_w=1080, canvas_h=1920):
    """生成单个镜头的标准化片段（尺寸跟着canvas_w/canvas_h走，默认还是原来的竖屏1080x1920）。
    图片素材加 Ken Burns 缓慢缩放效果，避免死画面。"""
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
        # 找到了"选了横屏、素材还是竖屏"这个问题的真正原因：这里之前用的是"整体缩小+补黑边"
        # （force_original_aspect_ratio=decrease + pad），这种方式会完整保留素材原始画面、
        # 不裁掉任何内容，但代价是如果素材本身的宽高比例跟选的输出比例差得比较多（比如
        # 竖拍的视频放进横屏画布），画面中间只会有一小块，周围一圈都是黑边——这套系统
        # 从一开始就只支持9:16，用户素材库里积累的视频大概率本来就是竖拍的，选竖屏（9:16）
        # 的时候这个问题基本不会被注意到（源素材和目标画布比例本来就接近），但选横屏/方形
        # 之后，只要素材本身是竖着拍的，就会变成"画面中间一小块、周围一圈黑"，看起来就像
        # "素材还是竖屏"。改成跟下面图片素材同样的"放大填满+裁掉多余部分"（increase + crop，
        # 效果类似网页里的object-fit: cover），画面会撑满整个画布，代价是原始画面四周会被
        # 裁掉一部分——这是短视频平台清一色的标准做法，比"完整保留但周围一圈黑边"更符合预期
        vf = f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,crop={canvas_w}:{canvas_h},setsar=1"
        run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", src_path,
            "-t", str(duration), "-vf", vf, "-r", str(fps),
            "-an", "-pix_fmt", "yuv420p", clip_path,
        ])
    else:
        frames = max(1, round(duration * fps))
        # Ken Burns：缓慢放大，避免图片素材是一张死画面
        vf = (
            f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,"
            f"crop={canvas_w}:{canvas_h},"
            f"zoompan=z='min(zoom+0.0012,1.15)':d={frames}:s={canvas_w}x{canvas_h}:fps={fps},setsar=1"
        )
        run([
            "ffmpeg", "-y", "-loop", "1", "-i", src_path,
            "-vf", vf, "-frames:v", str(frames), "-r", str(fps),
            "-pix_fmt", "yuv420p", clip_path,
        ])
    return clip_path



def build_transitioned_video(clip_paths, durations, out_path, style="fade", transition_duration=0.35):
    """把无音频镜头做成轻量转场。失败时调用方回退到concat，保证渲染稳定。"""
    if style == "none" or len(clip_paths) < 2:
        return False
    d = min(max(float(transition_duration), 0.15), 0.6)
    if any(float(x) <= d + 0.05 for x in durations):
        return False
    transition_map = {"fade": "fade", "smoothleft": "smoothleft", "smoothup": "smoothup"}
    trans = transition_map.get(style, "fade")
    inputs=[]
    for pth in clip_paths:
        inputs += ["-i", pth]
    filters=[]
    current="0:v"
    cumulative=float(durations[0])
    for i in range(1,len(clip_paths)):
        offset=max(0.0,cumulative-d)
        out=f"v{i}"
        filters.append(f"[{current}][{i}:v]xfade=transition={trans}:duration={d}:offset={offset:.3f}[{out}]")
        current=out
        cumulative += float(durations[i])-d
    cmd=["ffmpeg","-y",*inputs,"-filter_complex",";".join(filters),"-map",f"[{current}]","-an","-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",out_path]
    run(cmd)
    return True


def strengthen_hook(input_path, output_path, duration_sec=3.0):
    """前3秒做非常轻的推近+对比增强，不改变素材内容。"""
    d=max(0.8,min(float(duration_sec),3.5))
    vf=(f"scale=iw*1.025:ih*1.025,crop=iw/1.025:ih/1.025:(in_w-out_w)/2:(in_h-out_h)/2," 
        f"eq=contrast=1.05:saturation=1.04:enable='lt(t,{d:.2f})'")
    run(["ffmpeg","-y","-i",input_path,"-vf",vf,"-c:v","libx264","-preset","veryfast","-crf","20","-c:a","copy",output_path])


def score_cover_image(path):
    """轻量视觉评分：清晰度、对比度、色彩丰富度和不过曝/欠曝。"""
    try:
        im=Image.open(path).convert("RGB").resize((320,568))
        import numpy as _np
        a=_np.asarray(im).astype("float32")
        gray=0.299*a[:,:,0]+0.587*a[:,:,1]+0.114*a[:,:,2]
        contrast=float(gray.std())
        # 拉普拉斯近似清晰度，避免引入opencv
        lap=_np.abs(_np.diff(gray,axis=0)).mean()+_np.abs(_np.diff(gray,axis=1)).mean()
        saturation=float((a.max(axis=2)-a.min(axis=2)).mean())
        mean=float(gray.mean())
        exposure=max(0.0,1.0-abs(mean-128.0)/128.0)
        score=contrast*0.35+lap*1.8+saturation*0.18+exposure*25
        return round(score,3)
    except Exception as e:
        print("封面评分失败:",e)
        return 0.0


def choose_best_cover(candidates):
    if not candidates: return None
    ranked=sorted(candidates,key=lambda x:x.get("score",0),reverse=True)
    return ranked[0]


def probe_render_metrics(path):
    """生成成片的轻量技术质检指标，给Cloudflare端AI审片使用；失败不影响成片。"""
    try:
        cmd=["ffprobe","-v","error","-show_streams","-show_format","-of","json",path]
        raw=subprocess.run(cmd,capture_output=True,text=True,check=True).stdout
        data=json.loads(raw)
        streams=data.get("streams",[])
        fmt=data.get("format",{})
        v=next((x for x in streams if x.get("codec_type")=="video"),{})
        a=next((x for x in streams if x.get("codec_type")=="audio"),None)
        duration=float(fmt.get("duration") or v.get("duration") or 0)
        fps=0
        fr=v.get("avg_frame_rate") or "0/1"
        try:
            n,d=fr.split("/"); fps=round(float(n)/float(d),2) if float(d) else 0
        except Exception: pass
        return {
            "duration_sec": round(duration,3),
            "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0),
            "fps": fps,
            "video_codec": v.get("codec_name") or "",
            "audio_present": bool(a),
            "audio_codec": (a or {}).get("codec_name") or "",
            "audio_duration_sec": round(float((a or {}).get("duration") or 0),3),
            "size_bytes": Path(path).stat().st_size if os.path.exists(path) else 0,
        }
    except Exception as e:
        print("成片技术指标检测失败：",e)
        return {}


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
    # 画布尺寸按选的比例走：9:16是原本一直用的竖屏尺寸（默认），16:9是横屏（发视频号横版/
    # B站这类），1:1是方形（朋友圈方形封面）。没传这个字段的老任务，manifest.get(...)拿到的
    # 是None，走ASPECT_RATIO_DIMENSIONS.get(None, 默认值)这条路，效果跟"选了9:16"完全一样，
    # 不会因为加了这个新功能就影响到已经生成过的任务
    ASPECT_RATIO_DIMENSIONS = {
        "9:16": (1080, 1920),
        "16:9": (1920, 1080),
        "1:1": (1080, 1080),
    }
    CANVAS_W, CANVAS_H = ASPECT_RATIO_DIMENSIONS.get(manifest.get("aspect_ratio"), (1080, 1920))
    print(f"输出画布尺寸：{CANVAS_W}x{CANVAS_H}（比例：{manifest.get('aspect_ratio') or '9:16（默认）'}）")
    # 标题字幕/底部字幕选"一直保持"的时候，需要知道整条视频最终大概有多长，让这层字幕的
    # 结束时间对齐到视频结尾，而不是只显示固定的几秒——这里按每个镜头的时长加总估算一下
    total_video_duration = sum(float(s.get("duration_sec") or 3) for s in shots)

    def resolve_caption_duration(value, fallback):
        """标题字幕/底部字幕的显示时长，除了具体秒数，还支持"persist"这个特殊值（一直保持，
        对齐到整条视频结束），这里统一处理，不是数字的情况就不能直接float()转换"""
        if value == "persist":
            return total_video_duration
        try:
            return float(value) if value else fallback
        except (TypeError, ValueError):
            return fallback

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
    title_caption_color_scheme = manifest.get("title_caption_color_scheme") or "gold"
    title_caption_duration = manifest.get("title_caption_duration")
    title_caption_duration = resolve_caption_duration(title_caption_duration, 4.5)
    # 标题字幕的字号/字体，之前是直接借用解说字幕那两个设置，跟着"字幕字号/字体样式"变——
    # 但标题字幕（开头几秒的悬念大标题）和解说字幕（逐句同步的说明文字）本来就是两种不同用途的
    # 东西，样式没必要绑在一起，这里改成读取各自独立的字段，互不影响
    title_caption_font_size = manifest.get("title_caption_font_size")
    title_caption_font_size = int(title_caption_font_size) if title_caption_font_size else 76
    title_caption_font_style = manifest.get("title_caption_font_style") or "artistic"
    subtitle_highlight_numbers = manifest.get("subtitle_highlight_numbers")
    subtitle_highlight_numbers = True if subtitle_highlight_numbers is None else bool(subtitle_highlight_numbers)
    subtitle_bg_box = bool(manifest.get("subtitle_bg_box"))
    subtitle_font_style = manifest.get("subtitle_font_style") or "standard"
    subtitle_box_scheme = manifest.get("subtitle_box_scheme") or "black"
    bottom_caption_lines = manifest.get("bottom_caption_lines") or []
    enable_bottom_caption = bool(manifest.get("enable_bottom_caption"))
    bottom_caption_style = manifest.get("bottom_caption_style") or "news"
    bottom_caption_color_scheme = manifest.get("bottom_caption_color_scheme") or "gold"
    bottom_caption_duration = manifest.get("bottom_caption_duration")
    bottom_caption_duration = resolve_caption_duration(bottom_caption_duration, 6)
    # "纯风光+音乐"模式：选题/AI写解说词/素材匹配这些前置步骤完全不变，只是渲染这一步
    # 不走TTS配音，把逐镜头解说词原文直接当字幕文字显示在画面上，最终成片只有背景音乐、
    # 没有人声
    no_voice_mode = bool(manifest.get("no_voice_mode"))
    no_voice_max_lines = manifest.get("no_voice_max_lines")
    try:
        no_voice_max_lines = int(no_voice_max_lines) if no_voice_max_lines else 8
    except (TypeError, ValueError):
        no_voice_max_lines = 8
    # 这几个是"纯风光+音乐"模式字幕专用的样式设置，跟AI补充生成的"底部字幕"（bottom_caption_*）
    # 是完全独立的一套字段，不共用——避免用户以前给"底部字幕"调好的颜色/样式，在开启这个模式
    # 之后被不知不觉地拿去用在完全不同的场景上
    no_voice_position = manifest.get("no_voice_caption_position") or "top"
    no_voice_style = manifest.get("no_voice_caption_style") or "colorful"
    no_voice_color_scheme = manifest.get("no_voice_caption_color_scheme") or "gold"
    no_voice_font_size = manifest.get("no_voice_caption_font_size")
    try:
        no_voice_font_size = int(no_voice_font_size) if no_voice_font_size else 52
    except (TypeError, ValueError):
        no_voice_font_size = 52

    beat_sync = bool(manifest.get("beat_sync", True))
    custom_voice_asr = bool(manifest.get("custom_voice_asr", True))
    try: cover_count=min(max(int(manifest.get("cover_count") or 3),1),3)
    except (TypeError,ValueError): cover_count=3
    cover_style=manifest.get("cover_style") or "auto"
    transition_style=manifest.get("transition_style") or "fade"
    try: transition_duration=min(max(float(manifest.get("transition_duration") or 0.35),0.15),0.6)
    except (TypeError,ValueError): transition_duration=0.35
    audio_ducking=bool(manifest.get("audio_ducking",True))
    hook_emphasis=bool(manifest.get("hook_emphasis",True))
    smart_cover=bool(manifest.get("smart_cover",True))

    # 视频水印/Logo：/api/render-manifest那边已经把enable_watermark和watermark_url
    # 绑在一起判断过了（水印素材文件真的存在才会是true），这里直接信任这个结果，
    # 不用再额外判断watermark_url是不是空
    enable_watermark = bool(manifest.get("enable_watermark"))
    watermark_url = manifest.get("watermark_url")
    watermark_position = manifest.get("watermark_position") or "bottom-right"
    watermark_opacity = manifest.get("watermark_opacity")
    try:
        watermark_opacity = float(watermark_opacity) if watermark_opacity else 0.8
    except (TypeError, ValueError):
        watermark_opacity = 0.8
    watermark_scale = manifest.get("watermark_scale")
    try:
        watermark_scale = float(watermark_scale) if watermark_scale else 0.15
    except (TypeError, ValueError):
        watermark_scale = 0.15
    # 文字水印（比如"Copyright @ 账号名"这种签名式小字）——跟图片水印是二选一，
    # 有watermark_url就按图片处理，没有但有watermark_text就按文字处理，两个都没有
    # 就是没开水印（前面enable_watermark已经把这几种情况统一判断过了）
    watermark_text = manifest.get("watermark_text")

    full_text = "。".join(s["narration"] for s in shots if s.get("narration"))

    # 配音来源：AI合成语音（默认）/ 用户自己上传的配音文件 / 不配音（纯风光模式，前面已经
    # 处理过了）。自定义配音和"纯风光模式"是互斥的两个开关，"纯风光模式"优先级更高——
    # 如果两个都被传true了（理论上前端已经做了互斥，这里是双重保险），按"纯风光模式"处理
    voice_source = manifest.get("voice_source") or "ai"
    custom_voice_url = manifest.get("custom_voice_url")
    use_custom_voice = (not no_voice_mode) and voice_source == "custom" and bool(custom_voice_url)

    # 2. 配音生成——"纯风光+音乐"模式完全跳过这一步：没有配音就没有真实的语音时长可以对齐，
    # 画面时长直接用每个镜头自己的duration_sec（scale固定为1.0，不做任何缩放）
    if no_voice_mode:
        audio_path = None
        srt_path = None
        audio_duration = None
        scale = 1.0
        print("纯风光+音乐模式：跳过配音生成，画面时长按镜头原本的duration_sec走")
    elif use_custom_voice:
        # 用用户自己上传的配音文件，跳过edge-tts合成。这里能拿到真实的音频时长，画面节奏
        # 还是能跟着配音时长缩放（跟AI配音模式一样）；但没有真实的逐字时间轴——那是edge-tts
        # 合成语音时的副产品，没法凭空对着一段现成的录音文件反推出"每个字具体是哪一秒说的"，
        # 字幕这块下一步会退回跟"纯风光模式"一样的处理方式（整段解说词当持续显示的字幕，
        # 不是跟着语速一句一句精确同步）
        audio_path = f"{WORKDIR}/audio.mp3"
        srt_path = None
        r = requests.get(custom_voice_url, timeout=60)
        r.raise_for_status()
        with open(audio_path, "wb") as f:
            f.write(r.content)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, check=True,
        )
        audio_duration = float(probe.stdout.strip())
        planned_total = sum(float(s["duration_sec"]) for s in shots)
        scale = audio_duration / planned_total if planned_total > 0 else 1.0
        if custom_voice_asr:
            asr_srt=f"{WORKDIR}/custom_voice.srt"
            if transcribe_custom_voice_to_srt(audio_path,asr_srt):
                srt_path=asr_srt
                print("自定义配音ASR字幕生成成功，将使用真实时间轴")
            else:
                print("自定义配音ASR未生成字幕，继续使用整段字幕兜底")
        print(f"使用自定义配音文件，时长 {audio_duration:.2f}s，分镜计划总时长 {planned_total:.2f}s，缩放系数 {scale:.3f}")
    else:
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

    # BGM要裁剪/循环到多长，正常模式下跟着真实配音时长走；纯风光模式没有配音，
    # 用按镜头时长加总出来的total_video_duration代替，两种模式下面步骤6都统一用这一个变量，
    # 不用再各自判断走哪条路
    mix_target_duration = audio_duration if audio_duration is not None else total_video_duration

    # 2.5 V3.0第二阶段：预分析BGM节奏，后面混音复用同一个下载文件
    bgm_src_path=None; rhythm_beats=[]
    if beat_sync and bgm_url:
        try:
            bgm_src_path=f"{WORKDIR}/bgm_src.mp3"
            r=requests.get(bgm_url,timeout=60); r.raise_for_status()
            with open(bgm_src_path,"wb") as f: f.write(r.content)
            rhythm_beats=detect_rhythm_beats(bgm_src_path,WORKDIR)
            print(f"检测到 {len(rhythm_beats)} 个节奏点")
        except Exception as e:
            print("预分析BGM失败，回退普通节奏：",e); bgm_src_path=None; rhythm_beats=[]

    # 3. 逐镜头生成标准化分段；节拍同步只改变视觉镜头边界，不改变整条配音字幕时间轴。
    planned_durations=[max(1.0,float(shot["duration_sec"])*scale) for shot in shots]
    if beat_sync and rhythm_beats:
        planned_durations=align_durations_to_beats(planned_durations,rhythm_beats,sum(planned_durations))
        print("节拍同步后的镜头时长：",[round(x,2) for x in planned_durations])

    # xfade会让相邻镜头重叠 d 秒，因此直接拿原计划时长做转场会让最终视频
    # 比配音短 (镜头数-1)*d。这里先把各镜头总时长按比例放大，再由xfade扣掉重叠，
    # 保证转场后的成片长度仍然等于原来的目标时长，字幕/配音不会出现尾部不同步。
    render_durations = list(planned_durations)
    if transition_style != "none" and len(render_durations) > 1:
        d=min(max(float(transition_duration),0.15),0.6)
        target=sum(render_durations)
        extra=(len(render_durations)-1)*d
        if target > 0:
            factor=(target+extra)/target
            render_durations=[x*factor for x in render_durations]
            print(f"转场时长补偿：目标 {target:.2f}s，重叠 {extra:.2f}s，镜头时长缩放 {factor:.4f}")

    clip_paths=[]
    for i, shot in enumerate(shots):
        clip_paths.append(make_shot_clip(i, shot, render_durations[i], canvas_w=CANVAS_W, canvas_h=CANVAS_H))

    clip_list_path = f"{WORKDIR}/concat_list.txt"
    with open(clip_list_path, "w") as f:
        for p in clip_paths:
            f.write(f"file '{os.path.abspath(p)}'\n")

    concat_path = f"{WORKDIR}/concat.mp4"
    used_transition=False
    if transition_style != "none":
        try:
            used_transition=build_transitioned_video(clip_paths, render_durations, concat_path, transition_style, transition_duration)
            print(f"专业转场：{transition_style} / {transition_duration:.2f}s / 成功={used_transition}")
        except Exception as e:
            print("转场渲染失败，自动回退无转场：",e)
            used_transition=False
    if not used_transition:
        run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", clip_list_path,
            "-c", "copy", concat_path,
        ])

    # V3.0第三阶段：黄金开头强化。只处理视频前几秒，失败则保留原片。
    if hook_emphasis and len(clip_paths) > 0:
        try:
            hook_path=f"{WORKDIR}/hook_enhanced.mp4"
            strengthen_hook(concat_path,hook_path,3.0)
            concat_path=hook_path
            print("黄金开头强化完成")
        except Exception as e:
            print("黄金开头强化失败，跳过：",e)

    # 5. 生成字幕（简洁样式：统一颜色/字号/粗细/位置，都取自用户在网页里的设置）
    subtitled_path = f"{WORKDIR}/subtitled.mp4"
    if no_voice_mode or (use_custom_voice and not srt_path):
        # "纯风光+音乐"模式，或自定义配音ASR不可用时，都没有真实的语音时间轴（前者压根没配音，
        # 后者虽然有配音但那是用户自己录的，没法反推逐字时间轴），没法走逐句同步字幕这条路。
        # 直接把每个镜头的解说词原文，当成一块从头到尾持续展示的多行字幕——复用底部字幕
        # （build_bottom_caption_ass）这条已经验证过的多行渲染逻辑，不用另外写一套渲染代码。
        # 显示时长用total_video_duration（整条视频的时长），相当于自带"persist"效果；
        # 最多显示几行由no_voice_max_lines控制，超出的部分会被这个函数自动截断，
        # 不会为了塞下更多文字硬挤成一大坨、占用太多画面空间
        subtitle_filter = None
        narration_lines = [s.get("narration") for s in shots if s.get("narration")]
        if narration_lines:
            no_voice_caption_path = f"{WORKDIR}/no_voice_caption.ass"
            try:
                if build_bottom_caption_ass(
                    narration_lines, no_voice_caption_path, duration_sec=total_video_duration,
                    font_size=no_voice_font_size, style=no_voice_style,
                    color_scheme=no_voice_color_scheme, max_lines=no_voice_max_lines,
                    position=no_voice_position, canvas_w=CANVAS_W, canvas_h=CANVAS_H,
                ):
                    subtitle_filter = f"subtitles={no_voice_caption_path}:fontsdir={FONTS_DIR}"
                    print(f"纯风光+音乐模式字幕生成完成，共 {len(narration_lines)} 条解说词、最多显示{no_voice_max_lines}行")
            except Exception as e:
                # 字幕这层出问题，不能让整条视频直接渲染失败——退回"只有画面+背景音乐、没有字幕"
                # 这个更保守但至少能正常出片的结果
                print("纯风光+音乐模式字幕生成失败，退回无字幕（不影响视频合成）：", e)

        if subtitle_filter:
            run(["ffmpeg", "-y", "-i", concat_path, "-vf", subtitle_filter,
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", subtitled_path])
        else:
            run(["ffmpeg", "-y", "-i", concat_path, "-c", "copy", subtitled_path])
    else:
        ass_path = f"{WORKDIR}/audio.ass"
        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                srt_content = f.read()
            cue_count = build_subtitle_ass(
                srt_content, ass_path, font_size=subtitle_size, position=subtitle_position,
                color_key=subtitle_color, bold=subtitle_bold,
                highlight_numbers=subtitle_highlight_numbers, bg_box=subtitle_bg_box,
                font_style=subtitle_font_style, box_scheme=subtitle_box_scheme,
                canvas_w=CANVAS_W, canvas_h=CANVAS_H,
            )
            print(f"字幕生成完成，共 {cue_count} 条")
            # fontsdir指向下载好的站酷快乐体所在目录，libass渲染的时候如果ASS样式里指定的字体名
            # 是"ZCOOL KuaiLe"，会优先来这个目录找，找不到才退回系统字体库；选的是标准黑体的话
            # 这个参数不会有任何影响（系统本来就装了Noto Sans CJK），加上也没有副作用
            subtitle_filter = f"subtitles={ass_path}:fontsdir={FONTS_DIR}"
        except Exception as e:
            # 生成失败就退回最基础的样式，不能因为字幕这一步把整条视频搞挂
            print("字幕生成失败，退回基础字幕样式：", e)
            # "middle"这里也用跟主路径一样的"从底部往上锚定"方式，理由跟build_subtitle_ass里注释的一样：
            # 真正的正中心对齐在换行行数不一致时会显得位置来回跳。这个兜底路径没法像主路径那样逐条
            # 微调，只能给一个折中的固定锚点，但至少比之前的"5"（正中心）稳定。
            # 这条兜底路径直接把force_style套在原始srt文件上（不像主路径那样生成一份带PlayResX/
            # PlayResY的完整ASS文件），libass遇到没有指定播放分辨率的字幕源，会直接拿视频本身的
            # 实际像素尺寸当坐标系——所以这里的MarginV如果不按实际画布高度换算一下，选了16:9/1:1
            # 这类矮一些的画布时，同样的像素值相对画面的比例会变得完全不对，字幕可能被推到画面外
            _fallback_scale_w = CANVAS_W / 1080
            _fallback_scale_h = CANVAS_H / 1920
            _pos_map = {"top": (8, 90), "middle": (2, 850), "lower": (2, 420), "bottom": (2, 110)}
            _align, _mv = _pos_map.get(subtitle_position, _pos_map["bottom"])
            _mv = round(_mv * _fallback_scale_h)
            _fallback_font_size = round(subtitle_size * _fallback_scale_w)
            _color_rgb = SUBTITLE_COLOR_MAP.get(subtitle_color, SUBTITLE_COLOR_MAP["white"])
            _color_tag = rgb_to_ass_bgr(_color_rgb)
            _font_name = resolve_subtitle_font(subtitle_font_style, subtitle_bold)
            _border_style = 3 if subtitle_bg_box else 1
            _outline_val = 8 if subtitle_bg_box else 4
            _box_info = SUBTITLE_BOX_SCHEMES.get(subtitle_box_scheme, SUBTITLE_BOX_SCHEMES["black"])
            _back_colour = _box_info["back"] if subtitle_bg_box else "&H000000&"
            if subtitle_bg_box and _box_info["text_override"]:
                _color_tag = rgb_to_ass_bgr(_box_info["text_override"])
            style = (f"FontName={_font_name},FontSize={_fallback_font_size},PrimaryColour={_color_tag},"
                     f"OutlineColour=&H000000&,BackColour={_back_colour},BorderStyle={_border_style},"
                     f"Outline={_outline_val},Alignment={_align},MarginV={_mv}")
            subtitle_filter = f"subtitles={srt_path}:force_style='{style}':fontsdir={FONTS_DIR}"

        # 开头顶部大字标题字幕（悬念式大标题样式），是独立于上面逐句解说字幕的第二层，
        # 生成成功就在ffmpeg的滤镜链里跟主字幕串联叠加；不开启/没有内容/生成失败都不影响
        # 主字幕正常工作，这层从设计上就是"锦上添花，出问题就跳过"，不会拖累主流程
        if enable_title_caption and title_caption_lines:
            title_caption_path = f"{WORKDIR}/title_caption.ass"
            try:
                if build_title_caption_ass(title_caption_lines, title_caption_path, duration_sec=title_caption_duration,
                                            font_size=title_caption_font_size, color_scheme=title_caption_color_scheme,
                                            font_style=title_caption_font_style, canvas_w=CANVAS_W, canvas_h=CANVAS_H):
                    subtitle_filter = f"{subtitle_filter},subtitles={title_caption_path}:fontsdir={FONTS_DIR}"
                    print(f"开头标题字幕生成完成，共 {len(title_caption_lines)} 行")
            except Exception as e:
                print("开头标题字幕生成失败，跳过（不影响主字幕和视频合成）：", e)

        # 底部字幕，同样是独立的第三层，跟标题字幕是同一套"锦上添花、出问题就跳过"的处理方式
        if enable_bottom_caption and bottom_caption_lines:
            bottom_caption_path = f"{WORKDIR}/bottom_caption.ass"
            try:
                if build_bottom_caption_ass(bottom_caption_lines, bottom_caption_path, duration_sec=bottom_caption_duration,
                                             font_size=subtitle_size, style=bottom_caption_style, color_scheme=bottom_caption_color_scheme,
                                             canvas_w=CANVAS_W, canvas_h=CANVAS_H):
                    subtitle_filter = f"{subtitle_filter},subtitles={bottom_caption_path}:fontsdir={FONTS_DIR}"
                    print(f"底部字幕生成完成，共 {len(bottom_caption_lines)} 行")
            except Exception as e:
                print("底部字幕生成失败，跳过（不影响主字幕和视频合成）：", e)

        run([
            "ffmpeg", "-y", "-i", concat_path,
            "-vf", subtitle_filter,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", subtitled_path,
        ])

    # 5.5 叠加水印/Logo（如果开启了的话）：单独起一趟ffmpeg，在字幕已经烧录完成的
    # subtitled_path基础上再叠一层，不去改动上面已经写好、测试过的字幕合成逻辑——
    # 水印这个功能出问题的话，最多是"没加上水印"，不会连累字幕和视频本身能不能正常生成。
    # 没开启水印的任务完全走不到这段代码，行为跟以前一模一样
    if enable_watermark and watermark_url:
        try:
            watermark_src_path = f"{WORKDIR}/watermark_src.png"
            wm_resp = requests.get(watermark_url, timeout=30)
            wm_resp.raise_for_status()
            with open(watermark_src_path, "wb") as f:
                f.write(wm_resp.content)
            watermarked_path = f"{WORKDIR}/watermarked.mp4"
            # 水印按视频宽度的百分比缩放（watermark_scale，比如0.15＝视频宽度的15%），
            # 不管用户上传的水印图片原始尺寸是多大，最终呈现的大小都是统一、可预期的；
            # 透明度用colorchannelmixer调整alpha通道，不改变水印本身的颜色
            position_map = {
                "top-left": "20:20",
                "top-right": "W-w-20:20",
                "bottom-left": "20:H-h-20",
                "bottom-right": "W-w-20:H-h-20",
                "center": "(W-w)/2:(H-h)/2",
            }
            overlay_pos = position_map.get(watermark_position, position_map["bottom-right"])
            run([
                "ffmpeg", "-y", "-i", subtitled_path, "-i", watermark_src_path,
                "-filter_complex",
                f"[1:v]scale={round(CANVAS_W * watermark_scale)}:-2,format=rgba,"
                f"colorchannelmixer=aa={watermark_opacity}[wm];"
                f"[0:v][wm]overlay={overlay_pos}",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                watermarked_path,
            ])
            subtitled_path = watermarked_path
            print(f"水印叠加完成：位置={watermark_position}，缩放={watermark_scale}，不透明度={watermark_opacity}")
        except Exception as e:
            # 水印下载/叠加失败，不能让整条视频渲染跟着失败——退回没有水印的subtitled_path，
            # 后面步骤照常往下走
            print("水印叠加失败，跳过（不影响视频正常合成）：", e)
    elif enable_watermark and watermark_text:
        # 文字水印（比如"Copyright @ 账号名"这种签名式小字）——跟图片水印是完全独立的
        # 另一条路径，不用先下载图片再合成，直接用ffmpeg的drawtext把文字写到画面上，
        # 位置/不透明度跟图片水印共用同一套设置项，观感上是同一个功能的两种呈现方式
        try:
            watermarked_path = f"{WORKDIR}/watermarked.mp4"
            # 字号按视频宽度的比例换算，跟图片水印"按视频宽度百分比缩放"是同一个思路，
            # 不管画布是9:16还是16:9，文字水印看起来的相对大小都差不多；watermark_scale
            # 这个参数原本是给图片用的百分比，文字场景没有直接的对应关系，需要一个折算系数。
            # 这个系数不是拍脑袋定的，是真的用ffmpeg渲染测试过挑出来的——最早试过0.85，
            # 默认缩放(0.15)下算出来138px，跟标题字幕差不多大了，完全不像"低调的小签名
            # 水印"该有的样子；改成0.28之后默认缩放对应45px，视觉上跟参考的水印效果
            # （比如"Copyright @ 账号名"这种）比例接近，不会喧宾夺主
            font_size = max(14, round(CANVAS_W * watermark_scale * 0.28))
            # drawtext的文字定位表达式：跟图片水印的position_map是同一个位置体系，
            # 只是坐标表达式语法不同（这里用text_w/text_h，overlay滤镜用w/h）
            text_position_map = {
                "top-left": "x=24:y=24",
                "top-right": "x=w-text_w-24:y=24",
                "bottom-left": "x=24:y=h-text_h-24",
                "bottom-right": "x=w-text_w-24:y=h-text_h-24",
                "center": "x=(w-text_w)/2:y=(h-text_h)/2",
            }
            text_pos = text_position_map.get(watermark_position, text_position_map["bottom-right"])
            # 转义drawtext的text参数——这几个字符在ffmpeg filter语法里有特殊含义，处理不对
            # 的话会导致渲染出问题。这里不是凭感觉写的转义规则，是真的用ffmpeg跑过验证：
            # 冒号(:)用反斜杠转义能正常显示；单引号(')转义后虽然不报错，但那个字符会被
            # 悄悄吞掉（比如"it's"会显示成"its"），百分号(%)不管怎么转义都会导致
            # "Stray %"解析错误、整个drawtext滤镜失效——不是报错终止，是悄悄地什么文字都
            # 不渲染，水印摆设了都不知道。水印本来就是一句简短的签名文字，不值得为了兼容
            # 这几个边缘字符冒renders出问题的风险，直接干脆地把它们从水印文字里去掉，
            # 反斜杠、单引号、百分号都不留，冒号可以留着，用反斜杠转义正常显示
            escaped_text = (
                watermark_text.replace("\\", "")
                .replace("'", "")
                .replace("%", "")
                .replace(":", "\\:")
            )
            run([
                "ffmpeg", "-y", "-i", subtitled_path,
                "-vf",
                f"drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:text='{escaped_text}':fontsize={font_size}:"
                f"fontcolor=white@{watermark_opacity}:{text_pos}:"
                "shadowcolor=black@0.5:shadowx=1:shadowy=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                watermarked_path,
            ])
            subtitled_path = watermarked_path
            print(f"文字水印叠加完成：内容={watermark_text}，位置={watermark_position}，不透明度={watermark_opacity}")
        except Exception as e:
            print("文字水印叠加失败，跳过（不影响视频正常合成）：", e)

    # 6. 准备背景音乐：裁剪到跟成片一样长（正常模式跟配音时长走，纯风光模式跟镜头总时长走，
    # 都已经统一收敛到mix_target_duration这一个变量里了），按设定音量混入（不做淡入淡出/
    # 自动闪避，保持简单直接的听感，这两个效果之前实际使用体验不好，已经去掉）
    bgm_ready_path = None
    if bgm_url:
        try:
            if not bgm_src_path:
                bgm_src_path = f"{WORKDIR}/bgm_src.mp3"
                r = requests.get(bgm_url, timeout=60)
                r.raise_for_status()
                with open(bgm_src_path, "wb") as f:
                    f.write(r.content)
            bgm_ready_path = f"{WORKDIR}/bgm.mp3"
            run([
                "ffmpeg", "-y", "-stream_loop", "-1", "-i", bgm_src_path,
                "-t", str(mix_target_duration),
                bgm_ready_path,
            ])
        except Exception as e:
            print("背景音乐处理失败，跳过：", e)
            bgm_ready_path = None

    # 7. 合入配音（+背景音乐）输出最终视频。人声/BGM各自按设定音量直接混音。
    # "纯风光+音乐"模式没有配音轨道，只有背景音乐（前面/api/render-manifest那一步已经
    # 保证了一定会有背景音乐，找不到就直接取消渲染了，不会走到这里才发现没有BGM——
    # 这里的"没有bgm_ready_path"只处理BGM下载/裁剪这一步本身失败的极端情况，兜底出一个
    # 没有声音但至少画面完整的视频，不会让整条渲染因为音频这层出问题就彻底失败）
    final_path = f"{WORKDIR}/final.mp4"
    if no_voice_mode:
        if bgm_ready_path:
            run([
                "ffmpeg", "-y", "-i", subtitled_path, "-i", bgm_ready_path,
                "-filter_complex", f"[1:a]volume={bgm_volume}[bgm_out]",
                "-map", "0:v", "-map", "[bgm_out]",
                "-c:v", "copy", "-c:a", "aac", "-shortest", final_path,
            ])
        else:
            run(["ffmpeg", "-y", "-i", subtitled_path, "-c", "copy", final_path])
    elif bgm_ready_path:
        if audio_ducking:
            # BGM作为被闪避轨，人声作为侧链控制：说话时BGM自动降低，停顿时自然回升。
            # 人声这路音频要同时喂给两个地方用——一路当侧链控制信号（决定BGM什么时候
            # 该降音量），一路要跟处理完的BGM混在一起当最终人声。FFmpeg的filter graph里，
            # 一个具名管道（[voice_adj]这种方括号标签）只能被下一个滤镜消费一次，是单向
            # 单次的连接关系，不能像变量一样重复引用给两个不同的滤镜当输入——之前这里
            # 就是把[voice_adj]直接喂给了sidechaincompress和amix两处，实际测试会直接报
            # "Invalid stream specifier"错误，运行不起来。要同一路音频同时喂给多个滤镜，
            # 必须先用asplit显式分裂成几路独立的拷贝，各用各的
            duck_filter=(
                f"[1:a]volume={voice_volume}[voice_raw];"
                "[voice_raw]asplit=2[voice_side][voice_mix];"
                f"[2:a]volume=1.0[bgm_base];"
                "[bgm_base][voice_side]sidechaincompress=threshold=0.035:ratio=8:attack=18:release=320:makeup=1[bgm_ducked];"
                "[voice_mix][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
        else:
            duck_filter=(
                f"[1:a]volume={voice_volume}[voice_adj];"
                f"[2:a]volume={bgm_volume}[bgm_adj];"
                "[voice_adj][bgm_adj]amix=inputs=2:duration=first:dropout_transition=2[aout]"
            )
        run([
            "ffmpeg", "-y", "-i", subtitled_path, "-i", audio_path, "-i", bgm_ready_path,
            "-filter_complex", duck_filter,
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

    # 8.5 V3.0第三阶段：智能多封面候选 + 自动优选
    cover_url=None; cover_options=[]
    try:
        cover_titles=manifest.get("cover_titles") or []
        title=str(cover_titles[0]).strip() if cover_titles else "精彩瞬间"
        title=title[:18]
        style=cover_style if cover_style in {"clean","cinematic","sales"} else "clean"
        # 多取几个候选，再由视觉评分选择最佳默认封面；最终仍只上传用户设定数量。
        sample_count=max(3,cover_count+2)
        positions=[]
        for j in range(sample_count):
            ratio=(j+1)/(sample_count+1)
            positions.append(max(0.6,min(mix_target_duration-0.3,mix_target_duration*ratio)))
        ranked=[]
        for idx,pos in enumerate(positions,1):
            cover_path=f"{WORKDIR}/cover_candidate_{idx}.jpg"
            safe=title.replace("\\","").replace("'","").replace(":","\\:").replace("%","")
            if style=="sales":
                vf=f"scale=480:854:force_original_aspect_ratio=increase,crop=480:854,drawbox=x=0:y=650:w=480:h=204:color=black@0.62:t=fill,drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:text='{safe}':fontcolor=white:fontsize=42:x=24:y=684:shadowcolor=black@0.7:shadowx=2:shadowy=2"
            elif style=="cinematic":
                vf=f"scale=480:854:force_original_aspect_ratio=increase,crop=480:854,drawbox=x=0:y=0:w=480:h=150:color=black@0.28:t=fill,drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:text='{safe}':fontcolor=white:fontsize=38:x=24:y=48:shadowcolor=black@0.7:shadowx=2:shadowy=2"
            else:
                vf=f"scale=480:854:force_original_aspect_ratio=increase,crop=480:854,drawbox=x=0:y=670:w=480:h=184:color=black@0.45:t=fill,drawtext=fontfile=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc:text='{safe}':fontcolor=white:fontsize=40:x=24:y=712:shadowcolor=black@0.65:shadowx=2:shadowy=2"
            run(["ffmpeg","-y","-ss",str(pos),"-i",final_path,"-frames:v","1","-vf",vf,cover_path])
            score=score_cover_image(cover_path) if smart_cover else 0.0
            ranked.append({"path":cover_path,"score":score,"position":round(pos,2)})
        if smart_cover:
            ranked=sorted(ranked,key=lambda x:x["score"],reverse=True)
        selected=ranked[:cover_count]
        # 对外展示按“方案1/2/3”稳定编号，但默认封面是评分最高的那一张。
        for idx,item in enumerate(selected,1):
            key=f"videos/{TASK_ID}_cover_{idx}.jpg"
            s3.upload_file(item["path"],R2_BUCKET_NAME,key,ExtraArgs={"ContentType":"image/jpeg"})
            url=f"{R2_PUBLIC_BASE_URL}/{key}"
            cover_options.append({"title":f"封面方案{idx}","url":url,"score":item.get("score",0)})
        if cover_options:
            cover_url=cover_options[0]["url"]
            print("智能封面评分：",[x.get("score") for x in cover_options])
    except Exception as e:
        print("智能封面生成失败，回退普通封面：",e)
        try:
            cover_path=f"{WORKDIR}/cover.jpg"
            run(["ffmpeg","-y","-ss","0.8","-i",final_path,"-frames:v","1","-vf","scale=480:-1",cover_path])
            key=f"videos/{TASK_ID}_cover.jpg"; s3.upload_file(cover_path,R2_BUCKET_NAME,key,ExtraArgs={"ContentType":"image/jpeg"})
            cover_url=f"{R2_PUBLIC_BASE_URL}/{key}"
        except Exception:
            pass

    render_metrics = probe_render_metrics(final_path)
    print("成片技术指标：", render_metrics)
    callback("succeeded", video_url=video_url, cover_url=cover_url, cover_options=cover_options, render_metrics=render_metrics)
    print("完成，视频地址:", video_url)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("渲染失败:", e, file=sys.stderr)
        callback("failed", error=str(e))
        sys.exit(1)
