#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货外盘新闻简报推送机器人
================================
抓取国外主流财经媒体与机构 RSS（Reuters / Bloomberg / FT / WSJ / CNBC / EIA / OPEC / Argus / Platts /
Mining.com / World Grain 等），按主题分类去重，整篇正文机器翻译成中文并生成独立网页（GitHub Pages 托管），
推送到微信（Server酱 / PushPlus，支持多个微信）。

简报结构（v4）：
    1. 今日要闻 —— 只放真正的大事（地缘冲突 / 美联储 / 美国政局 / 商品突发供需 / 行情剧变 / 极端天气），
       按重大性打分 + 星级，同一事件绝不重复
    2. 财经日历 —— 今明两天重要事件，同国家同指标族合并成一条，带实际值/预期值/前值
    3. 分板块详情 —— 每条：中文标题 + 中文摘要 + 两个链接：
       【📖 中文全文】= 该新闻整篇翻译的中文网页（点开即读，托管在 GitHub Pages）
       【🌐 英文原文】= 出版方原始链接

翻译：主通道 Google 翻译免费接口（GitHub Actions 美国机房可用），长文自动分块；
      兜底 MyMemory（本机/云端都可用，免费额度有限）。翻译失败自动回退英文原文。

用法：
    python news_bot.py --test            # 只生成简报和中文网页到本地，不推送（先跑这个看效果）
    python news_bot.py                   # 生成并推送（需在 config.json 填好推送 key）
    python news_bot.py morning           # 晨报模式：回溯最近 16 小时（覆盖隔夜美盘）
    python news_bot.py evening           # 晚报模式：回溯最近 9 小时（覆盖白天时段）
    python news_bot.py --hours 12        # 自定义回溯窗口
    python news_bot.py --out-only ...    # 只生成（digests/pending.md + pending.json），不推送
    python news_bot.py --push-file digests/pending.json   # 推送已生成的简报（云端先发布网页再推送用）
    python news_bot.py --config config.cloud.json   # 使用云端配置

配置：同目录 config.json
推送 key 优先级：环境变量（SERVERCHAN_KEY / SERVERCHAN_KEY_2 / SERVERCHAN_KEYS，支持逗号分隔多个）
                > config.json（云端部署用环境变量，避免 key 进代码库）
"""

import argparse
import difflib
import hashlib
import html as htmllib
import json
import os
import re
import ssl
import sys
import traceback
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
CN_TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TOPIC_ORDER = ["macro", "energy", "metals", "agri", "other"]
TOPIC_CN = {
    "macro": "宏观 / 央行 / 地缘",
    "energy": "能源化工（原油 / 天然气）",
    "metals": "金属与贵金属",
    "agri": "农产品",
    "other": "其他商品与市场",
}

KEYWORDS = {
    "energy": [
        "oil", "crude", "opec", "brent", "wti", "natural gas", "lng",
        "refinery", "refineries", "diesel", "gasoline", "propane", "barrel",
        "barrels", "petroleum", "pipeline", "shale", "drilling", "rig count",
        "energy department", "eia", "fuel", "platts", "argus",
    ],
    "metals": [
        "gold", "silver", "copper", "aluminum", "aluminium", "nickel",
        "zinc", "iron ore", "lithium", "cobalt", "steel", "platinum",
        "palladium", "ore", "mining", "miner", "miners", "smelter",
        "lme", "precious metal", "base metal",
    ],
    "agri": [
        "soybean", "soybeans", "corn", "wheat", "crop", "crops", "harvest",
        "usda", "farm", "farmer", "grain", "cotton", "sugar", "coffee",
        "cocoa", "palm oil", "planting", "drought", "flood", "export sales",
        "agricultur*", "brazil bean", "argentina",
    ],
    "macro": [
        "fed", "federal reserve", "fomc", "powell", "inflation", "cpi",
        "ppi", "jobs report", "payroll", "payrolls", "unemployment",
        "jobless", "interest rate", "rate cut", "rate hike", "tariff",
        "tariffs", "treasury", "yields", "dollar", "yen", "euro", "pound",
        "gdp", "ecb", "boj", "bank of japan", "central bank", "recession",
        "trade war", "china", "beijing", "washington", "sanction", "imf",
        "stimulus", "hormuz", "strait",
        # 日韩港市场
        "japan", "japanese", "tokyo", "nikkei", "jgb",
        "korea", "korean", "kospi", "samsung", "hynix", "seoul",
        "hong kong", "hang seng", "hkex", "yuan",
    ],
    "other": [
        "commodit*", "futures", "cme", "supply", "demand", "inventory",
        "stockpile", "freight", "shipping", "supply chain", "materials",
        "stock market", "stocks",
    ],
}

# 编译成整词匹配（前缀用 * 结尾），避免 "Korea/Forecast" 误匹配 "ore" 这类问题
_COMPILED = {
    topic: [re.compile(r"\b" + re.escape(k.rstrip("*")) +
                       (r"" if k.endswith("*") else r"\b"), re.I)
            for k in kws]
    for topic, kws in KEYWORDS.items()
}

# 纯外汇货币对的行情分析对期货无用，直接丢弃
FOREX_DROP = re.compile(
    r"\b[A-Z]{3}/[A-Z]{3}\b|"
    r"\b(EURUSD|GBPUSD|USDJPY|AUDUSD|USDCAD|USDCHF|NZDUSD|EURGBP|EURJPY|"
    r"GBPJPY|AUDJPY|CADJPY|CHFJPY|EURCHF|GBPCHF)\b")

# 体育 / 娱乐 / 社会案件类新闻是噪音（韩联社等综合源里特别多），直接丢弃
NOISE_DROP = re.compile(
    r"\b(football|soccer|baseball|basketball|volleyball|golf|taekwondo|"
    r"olympic\w*|asian games|world cup|k-pop|kpop|pop star|pop group|"
    r"actor|actress|celebrity|singer|film|movie|drama series|variety show|"
    r"prosecutor\w*|prison|jailed|sentenced|sexual assault|murder|"
    r"kidnapping|robbery|indicted|museum|heist|painting|sculpture|"
    r"art theft|art gallery|art world|art market|stolen|thieves|thief|"
    r"burglar\w*|vandalis\w*|picasso|monet|renoir|vermeer|jewel\w* theft)\b",
    re.I)
# 中文噪音（GNews-CN 等中文源）：博物馆盗窃 / 娱乐八卦 / 体育社会新闻
NOISE_DROP_ZH = re.compile(
    r"博物馆|盗窃|抢劫|小偷|名画|艺术品|画展|拍卖行|明星|演员|歌手|电影|票房|"
    r"电视剧|综艺|足球|篮球|棒球|排球|奥运|亚运|世界杯|演唱会|导演|出轨|离婚|"
    r"文学奖|小说|游戏|电竞|旅游|美食")

# 中文关键词分类（中文标题专用，配合 GNews-CN 源）
KEYWORDS_ZH = {
    "energy": r"原油|油价|石油|欧佩克|OPEC|天然气|LNG|柴油|汽油|炼油|燃油|"
              r"油品|油田|钻井|油轮|EIA|API库存",
    "metals": r"黄金|白银|铜价|电解铜|铝价|镍|锌|铁矿石|铁矿|锂|钴|钢铁|"
              r"贵金属|基本金属|LME|矿业|金价",
    "agri":   r"大豆|豆粕|豆油|玉米|小麦|棉花|白糖|咖啡|可可|棕榈油|农产品|"
              r"粮食|干旱|洪涝|霜冻|厄尔尼诺|拉尼娜|美国农业部|播种|收割|产量",
    "macro":  r"美联储|加息|降息|利率|通胀|CPI|PPI|PCE|GDP|关税|制裁|央行|"
              r"美元|日元|欧元|人民币|国债|收益率|制造业|PMI|中国|美国|日本|"
              r"韩国|欧元区|港股|恒生|地缘|贸易战|非农|就业",
    "other":  r"大宗商品|期货|库存|供给|需求|航运|运价|美股|A股|股市|股票|"
              r"标普|纳斯达克|道琼斯|供应|波罗的海",
}
_COMPILED_ZH = {t: re.compile(p) for t, p in KEYWORDS_ZH.items()}

# ---------------------------------------------------------------- 重大性打分（"今日要闻"用）
# 只放真正的大事：地缘冲突 / 美联储 / 美国政局 / 商品突发供需 / 行情剧变 / 极端天气。
# (类别, 权重, 关键词正则)：命中的新闻进入"今日要闻"候选；总分 >= 3 才有资格入选。
IMPORTANCE_CATS = [
    ("地缘冲突", 3, re.compile(
        r"\b(war|wars|missile\w*|airstrike|air strike|attack\w*|drone strike|"
        r"invasion|invade\w*|escalat\w*|sanction\w*|embargo|hormuz|red sea|"
        r"houthi|blockade|ceasefire|nuclear|military|troops?|strike[sd] on)\b", re.I)),
    ("美联储", 3, re.compile(
        r"\b(fed|feds|fomc|powell|fed chair\w*|federal reserve|rate cut\w*|"
        r"rate hike\w*|interest[- ]rate|beige book|policy minutes|jackson hole|"
        r"rate decision)\b", re.I)),
    ("美国政局", 3, re.compile(
        r"\b(white house|executive order|president|administration|treasury "
        r"department|state department|congress|senate|government shutdown|"
        r"trade deal|tariff\w*|election|signs? into law)\b", re.I)),
    ("供需突发", 3, re.compile(
        r"\b(opec\+?|production cut\w*|output cut\w*|export (ban\w*|curb\w*|"
        r"restriction\w*)|force majeure|outage\w*|shortage\w*|deficit|surplus|"
        r"halt\w*|disruption\w*|strategic reserve|quota\w*)\b", re.I)),
    ("极端天气", 2, re.compile(
        r"\b(el ni[nñ]o|la ni[nñ]a|drought\w*|flood\w*|heatwave|heat wave|"
        r"hurricane\w*|typhoon\w*|cyclone\w*|frost|monsoon|wildfire\w*)\b", re.I)),
    ("其他央行", 2, re.compile(
        r"\b(ecb|boj|bank of japan|pboc|central bank\w*|stimulus|"
        r"policy (meeting|decision))\b", re.I)),
]
# 行情剧变词 + 市场名词：两者同时出现才算"暴涨暴跌"级大事（单独一个 move 动词太常见）
_MOVE_RE = re.compile(
    r"\b(surge[sd]?|surging|soar\w*|plunge[sd]?|plunging|tumble\w*|spike[sd]?|"
    r"slump\w*|skyrocket\w*|record (high|low)|all[- ]time (high|low)|selloff|"
    r"crash\w*|rally|rout)\b", re.I)
_MARKET_RE = re.compile(
    r"\b(oil|crude|brent|wti|gold|silver|copper|aluminum|aluminium|nickel|zinc|"
    r"iron ore|wheat|corn|soybean\w*|natural gas|lng|commodit\w*|nasdaq|"
    r"s&p ?500|dow jones|tech stock\w*|wall street|stock market|shares|"
    r"treasury yields?)\b", re.I)


def importance(title):
    """返回 (总分, 命中类别列表)；总分 >= 3 才有资格进"今日要闻"。"""
    score, cats = 0, []
    for cat, w, pat in IMPORTANCE_CATS:
        if pat.search(title):
            score += w
            cats.append(cat)
    # 暴涨暴跌 + 具体市场同时命中才算行情剧变大事
    if _MOVE_RE.search(title) and _MARKET_RE.search(title):
        score += 2
        cats.append("行情剧变")
    return score, cats

# ---------------------------------------------------------------- 工具函数

def now_cn():
    return datetime.now(CN_TZ)


def log(msg):
    print(f"[{now_cn().strftime('%H:%M:%S')}] {msg}", flush=True)


def sanitize_xml(raw: bytes) -> bytes:
    """把未定义的 HTML 实体（如 &nbsp;）转义成可解析形式，容错部分不规范的 RSS。"""
    return re.sub(rb"&(?!amp;|lt;|gt;|quot;|apos;|#)", b"&amp;", raw)


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(CN_TZ)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(CN_TZ)
    except Exception:
        return None


def clean_title(t):
    t = htmllib.unescape(t or "").strip()
    t = re.sub(r"\s+", " ", t)
    # Google News 标题格式: "headline - Publisher"
    if " - " in t:
        head, _, tail = t.rpartition(" - ")
        if 2 <= len(tail) <= 30 and tail.replace(" ", "").isalnum():
            t = head
    return t


def norm_key(t):
    """标题归一化去重键：保留字母数字与中日韩字符（中文标题不能归成空串）。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", t.lower())[:80]


def classify(title):
    best, best_hits = None, 0
    for topic in ("energy", "metals", "agri", "macro", "other"):
        hits = sum(1 for p in _COMPILED[topic] if p.search(title))
        if _COMPILED_ZH[topic].search(title):  # 中文关键词命中
            hits += 1
        if hits > best_hits:
            best, best_hits = topic, hits
    return best if best_hits > 0 else None

# ---------------------------------------------------------------- 抓取

def build_opener(proxy):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({
            "http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # 绕过系统代理，直连
    handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def _regex_parse_items(text, feed):
    """XML 解析失败时的容错解析：直接用正则抠 <item> 块。"""
    out = []

    def grab(block, tag):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S | re.I)
        if not m:
            return ""
        s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S)
        return htmllib.unescape(s).strip()

    for m in re.finditer(r"<item>(.*?)</item>", text, re.S | re.I):
        block = m.group(1)
        title, link = grab(block, "title"), grab(block, "link")
        if not link:
            link = grab(block, "guid")
        dt = parse_date(grab(block, "pubDate"))
        if title and link:
            out.append((title, link, dt, feed["name"], grab(block, "description")))
    return out


def fetch_feed(opener, feed, timeout=15):
    """返回 [(title, link, dt, source, desc)]；失败抛异常。"""
    req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
    try:
        root = ET.fromstring(sanitize_xml(raw))
    except ET.ParseError:
        return _regex_parse_items(raw.decode("utf-8", "ignore"), feed)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    items = root.findall(".//item")
    out = []
    if items:  # RSS 2.0
        for it in items:
            title = clean_title(it.findtext("title") or "")
            link = (it.findtext("link") or "").strip()
            dt = parse_date(it.findtext("pubDate") or "")
            src = (it.findtext("source") or "").strip() or feed["name"]
            desc = it.findtext("description") or ""
            if title and link:
                out.append((title, link, dt, src, desc))
    else:      # Atom
        for it in root.findall(".//atom:entry", ns):
            title = clean_title(it.findtext("atom:title", default="", namespaces=ns) or "")
            le = it.find("atom:link", ns)
            link = le.get("href", "") if le is not None else ""
            dt = parse_date(it.findtext("atom:published", default="", namespaces=ns)
                            or it.findtext("atom:updated", default="", namespaces=ns) or "")
            desc = (it.findtext("atom:summary", default="", namespaces=ns)
                    or it.findtext("atom:content", default="", namespaces=ns) or "")
            if title and link:
                out.append((title, link, dt, feed["name"], desc))
    return out


def collect(cfg):
    opener = build_opener(cfg.get("proxy") or None)
    seen, pool = set(), []
    kept_titles = []  # 用于模糊去重（跨源标题相似度 > 80% 视为同一新闻）
    for feed in cfg["feeds"]:
        if not feed.get("enabled", True):
            log(f"跳过(未启用): {feed['name']}")
            continue
        try:
            rows = fetch_feed(opener, feed)
        except Exception as e:
            log(f"抓取失败 {feed['name']}: {type(e).__name__} {e}")
            continue
        added = 0
        for title, link, dt, src, desc in rows:
            k = norm_key(title)
            if not k or k in seen:
                continue
            if FOREX_DROP.search(title):
                continue  # 纯外汇货币对分析，与商品期货无关
            if NOISE_DROP.search(title) or NOISE_DROP_ZH.search(title):
                continue  # 体育/娱乐/社会案件类噪音（中英文）
            # 模糊去重：与已收录标题相似度 > 0.8 视为同一事件（仅留最新一条）
            if any(difflib.SequenceMatcher(None, k, norm_key(t)).ratio() > 0.8
                   for t in kept_titles):
                continue
            seen.add(k)
            kept_titles.append(title)
            desc_txt = _usable_desc(desc)
            # 相关性总门槛：标题/描述都与宏观、商品、市场无关的新闻直接丢弃
            topic = classify(title) or classify(desc_txt)
            if topic is None:
                continue
            pool.append({"title": title, "link": link, "dt": dt,
                         "src": src, "topic": topic, "desc": desc_txt})
            added += 1
        log(f"抓取成功 {feed['name']}: {len(rows)} 条, 新增 {added}")
    return pool

# ---------------------------------------------------------------- 摘要提取

_TAG_RE = re.compile(r"<[^>]+>")

def _usable_desc(s):
    """清洗 RSS 自带的 description，能当摘要用就返回文本，否则空串。
    （FT 的 description 就是文章导语；Google News 的是纯链接杂讯要丢弃）"""
    text = _clean_text(s)
    if len(text) < 40:
        return ""
    if "http://" in text or "https://" in text or "www." in text:
        return ""
    if len(text) > 220:
        text = text[:220].rsplit(" ", 1)[0] + "…"
    return text

_META_DESC_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description|twitter:description)["\'][^>]+'
    r'content=["\']([^"\']{40,600})["\']', re.I)
_META_DESC_RE2 = re.compile(
    r'<meta[^>]+content=["\']([^"\']{40,600})["\'][^>]+'
    r'(?:property|name)=["\'](?:og:description|description|twitter:description)["\']', re.I)

# 正文里常见的订阅提示 / 广告 / 版权声明等垃圾段落，不能进摘要
_PARA_JUNK = re.compile(
    r"^(subscribe|subscription|sign in|sign up|log in|register|advertisement"
    r"|advertisements|ad |sponsored|read more|follow us|share this|listen "
    r"|watch live|newsletter|all rights reserved|©|reprints|your browser "
    r"|cookie|javascript is|get browser alerts|click here|please |thank you"
    r"|we use |view comments|comments |photo |image |credit |getty|rex/"
    r"|bloomberg|this article (is|was)|story continues|recommended|most "
    r"|trending|you may |what i cover )", re.I)
# 段落里混有脚本代码 / 广告标记的，整段丢弃
_PARA_CODE = re.compile(
    r"advertisement|if \(|window\.|document\.|write_html|function\s*\(|"
    r"var\s+\w+\s*=|`|\$\{|<!\[CDATA\[|javascript:", re.I)

# 英文虚词集合：正常新闻正文虚词占比高，导航菜单/标签云/行情数字表几乎没有
_STOPS = {"the", "of", "and", "to", "in", "a", "on", "for", "as", "is",
          "are", "was", "were", "has", "have", "had", "that", "this",
          "with", "from", "by", "at", "be", "it", "its", "said", "says",
          "after", "amid", "than", "will", "would", "could", "into",
          "over", "about", "or", "an", "be", "their", "they"}


def _junk_para(t):
    """识别导航菜单 / 标签云 / 行情数字表等非正文段落。"""
    words = t.split()
    n = len(words)
    if n == 0:
        return True
    stops = sum(1 for w in words if w.lower() in _STOPS)
    nums = sum(1 for w in words if re.match(r"^[\d$+.%•:-]+$", w))
    if n > 40 and stops / n < 0.12:      # 导航/标签云：几乎没有虚词
        return True
    if n > 15 and nums / n > 0.3:        # 行情数字表
        return True
    if t.count("%") >= 3:
        return True
    return False


def _clean_text(s):
    s = htmllib.unescape(_TAG_RE.sub("", s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _resolve_google_news(opener, link, timeout=10):
    """Google News 跳转页：解出真实出版方链接；失败返回原链接。"""
    if "news.google.com" not in urllib.parse.urlparse(link).netloc:
        return link
    try:
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        with opener.open(req, timeout=timeout) as r:
            body = r.read(200000).decode("utf-8", "ignore")
        m = re.search(r'<a[^>]+href="(https?://(?!news\.google\.com|support\.google\.com|policies\.google\.com)[^"]+)"',
                      body, re.I)
        return htmllib.unescape(m.group(1)) if m else ""
    except Exception:
        return ""


def fetch_full_article(opener, link, timeout=12, max_chars=15000, max_paras=80):
    """抓原文页面，提取整篇正文段落（用于全文翻译）。
    返回 (段落列表, 真实链接)；拿不到正文返回 ([], 真实链接)。"""
    real_link = _resolve_google_news(opener, link)
    if not real_link:
        return [], link
    try:
        req = urllib.request.Request(real_link, headers={"User-Agent": UA})
        with opener.open(req, timeout=timeout) as r:
            body = r.read(600000).decode("utf-8", "ignore")
    except Exception:
        return [], real_link
    # 剥掉脚本 / 样式 / 导航页脚等非正文块
    body = re.sub(r"<(script|style|nav|footer|aside|header|form)[^>]*>.*?</\1>", " ",
                  body, flags=re.S | re.I)
    paras = []
    for pm in re.finditer(r"<p[^>]*>(.*?)</p>", body, re.S | re.I):
        t = _clean_text(pm.group(1))
        if len(t) < 40 or _PARA_JUNK.match(t) or _PARA_CODE.search(t) or _junk_para(t):
            continue
        if paras and (t in paras[-1] or paras[-1] in t):
            continue  # 与上一段重复
        paras.append(t)
        if len(paras) >= max_paras or sum(len(p) for p in paras) >= max_chars:
            break
    # 正文太少时，用 meta 描述（通常是导语）补一段
    if sum(len(p) for p in paras) < 400:
        m = _META_DESC_RE.search(body) or _META_DESC_RE2.search(body)
        if m:
            d = _clean_text(m.group(1))
            if len(d) >= 60 and not _PARA_CODE.search(d) and d not in paras:
                paras.insert(0, d)
    return paras, real_link

# ---------------------------------------------------------------- 机器翻译（中文化简报）
# 主接口 Google 翻译免费通道（GitHub Actions 美国机房可用，本机被墙），
# 兜底 MyMemory（本机/云端都可用，免费额度有限，仅在主接口失败时调用）。
# 全部失败返回空串，调用方回退英文原文，不影响简报生成。

TRANS_CACHE = {}
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
_MM_JUNK = re.compile(r"MYMEMORY WARNING|QUERY LENGTH LIMIT|INVALID (SOURCE|TARGET)", re.I)


def _google_translate(opener, text):
    """Google 免费通道；长文自动按句分块（每块 <=1200 字符），429/5xx 重试一次。"""
    out = []
    for chunk in _split_sentences(text, 1200) or [text]:
        url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
               "&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(chunk))
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with opener.open(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8", "ignore"))
                out.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
                break
            except urllib.error.HTTPError as e:
                if attempt == 2 or e.code not in (429, 500, 502, 503):
                    raise
                import time
                time.sleep(2.5)
            except Exception:
                if attempt == 2:
                    raise
    return "".join(out)


def _split_sentences(text, limit=400):
    """MyMemory 单次查询长度有限，按句子切段。"""
    parts, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        while len(sent) > limit:          # 超长句硬切
            parts.append(sent[:limit])
            sent = sent[limit:]
        if len(cur) + len(sent) + 1 <= limit:
            cur = (cur + " " + sent).strip()
        else:
            if cur:
                parts.append(cur)
            cur = sent
    if cur:
        parts.append(cur)
    return parts


def _mymemory_translate(opener, text):
    out = []
    for chunk in _split_sentences(text):
        url = ("https://api.mymemory.translated.net/get?q="
               + urllib.parse.quote(chunk) + "&langpair=en|zh-CN")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener.open(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        if str(data.get("responseStatus")) not in ("1", "200"):
            raise RuntimeError(f"MyMemory status {data.get('responseStatus')}")
        t = (data.get("responseData") or {}).get("translatedText", "")
        if not t or _MM_JUNK.search(t):
            raise RuntimeError("MyMemory junk response")
        out.append(t)
    return "".join(out)


def translate(opener, text):
    """英文 -> 中文；全部失败返回空串（调用方回退英文原文）。"""
    if not text or not re.search(r"[A-Za-z]", text):
        return text or ""
    key = text[:200]
    if key in TRANS_CACHE:
        return TRANS_CACHE[key]
    for fn in (_google_translate, _mymemory_translate):
        try:
            t = fn(opener, text)
            if t and _HAS_CJK.search(t):
                TRANS_CACHE[key] = t
                return t
        except Exception:
            continue
    return ""


def translate_many(opener, texts, workers=6):
    """并发翻译一批文本，返回 {原文: 译文}（失败的原文对应空串）。"""
    uniq = list({t for t in texts if t})
    out = {t: "" for t in uniq}
    if not uniq:
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(translate, opener, t): t for t in uniq}
        for f, t in futs.items():
            try:
                out[t] = f.result(timeout=120)
            except Exception:
                out[t] = ""
    return out

# ---------------------------------------------------------------- 财经事件日历
# 数据源：华尔街见闻公开 API（免鉴权，本机/云端都可用），整合自 invest-calendar skill。
# 拉取今天+明天的重要性 >= 3 星事件，以及命中期货商品关键词的 2 星事件（EIA 库存等）。
WSCN_CAL_URL = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"
# 2 星事件里跟商品期货直接相关的关键词（数据发布类）
CAL_COMMO_KW = re.compile(r"原油|库存|EIA|OPEC|天然气|农产品|大豆|玉米|小麦|铜|铝|"
                          r"锌|镍|铁矿石|黄金|PMI|CPI|PPI|PCE|非农|就业|失业|零售|"
                          r"GDP|贸易|进出口|社融|M2|美联储|利率", re.I)
# 指标族：同一国家同族指标（如日本 GDP 系列的多个分项）合并为一行，不重复罗列
_CAL_FAMILY = [
    ("GDP", re.compile(r"GDP|国内生产总值|平减指数|经济增速")),
    ("物价", re.compile(r"CPI|PPI|PCE|通胀|物价|消费者价格|生产者价格")),
    ("利率", re.compile(r"利率|LPR|降息|加息|货币政策|决议")),
    ("就业", re.compile(r"非农|就业|失业|职位|劳工|初请|薪资")),
    ("PMI", re.compile(r"PMI|采购经理人")),
    ("消费", re.compile(r"零售|消费者信心|消费")),
    ("贸易", re.compile(r"贸易|进出口|出口|进口|顺差|逆差|贸易帐")),
]
# 个股层面的事件（上市/挂牌/IPO）不是宏观大事，丢弃
_CAL_DROP = re.compile(r"上市|挂牌|新股|IPO|停牌|复牌")


def _cal_family(title):
    for name, pat in _CAL_FAMILY:
        if pat.search(title):
            return name
    return None


def fetch_calendar(opener, days=2, limit=12):
    """财经日历：同一国家同一指标族合并成一条，带实际值/预期值/前值；失败返回空串。"""
    try:
        t0 = now_cn().replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = int(t0.timestamp()), int(t0.timestamp()) + days * 86400
        url = f"{WSCN_CAL_URL}?start={start}&end={end}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener.open(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        items = (data.get("data") or {}).get("items") or []
    except Exception as e:
        log(f"财经日历拉取失败: {type(e).__name__} {e}")
        return ""
    picked = []
    for it in items:
        try:
            imp = int(it.get("importance") or 0)
        except (TypeError, ValueError):
            continue
        if imp >= 3 or (imp == 2 and CAL_COMMO_KW.search(it.get("title") or "")):
            if not _CAL_DROP.search(it.get("title") or ""):
                picked.append(it)
    if not picked:
        return ""
    # 分组：同国家 + 同指标族（或独立事件标题）
    groups = {}
    for it in picked:
        title = (it.get("title") or "").strip()
        fam = _cal_family(title)
        key = (it.get("country") or "", fam) if fam else (it.get("country") or "", title)
        groups.setdefault(key, []).append(it)

    def group_key(grp):
        return (-max(int(i.get("importance") or 0) for i in grp),
                min(i.get("public_date") or 0 for i in grp))

    lines = []
    for grp in sorted(groups.values(), key=group_key)[:limit]:
        grp.sort(key=lambda x: x.get("public_date") or 0)
        head = grp[0]
        country = (head.get("country") or "").strip()
        title = (head.get("title") or "").strip()
        fam = _cal_family(title)
        try:
            tm = datetime.fromtimestamp(head["public_date"], CN_TZ).strftime("%m-%d %H:%M")
        except (KeyError, TypeError, ValueError, OSError):
            continue
        stars = "★" * int(head.get("importance") or 0)
        # 每个分项去掉国家前缀后带上数值信息
        parts = []
        for it in grp[:3]:
            t = (it.get("title") or "").strip()
            if country and t.startswith(country):
                t = t[len(country):].lstrip(" ：:")
            vals = []
            if str(it.get("actual") or "").strip():
                vals.append(f"公布 {it['actual']}")
            if str(it.get("forecast") or "").strip():
                vals.append(f"预期 {it['forecast']}")
            if str(it.get("previous") or "").strip():
                vals.append(f"前值 {it['previous']}")
            if vals:
                t += "（" + "，".join(vals) + "）"
            parts.append(t)
        label = fam or "事件"
        lines.append(f"- **{tm}** {stars}【{country}】**{label}**：" + "；".join(parts))
    log(f"财经日历：{len(picked)} 个事件合并为 {len(lines)} 条（{days} 天内）")
    return "\n".join(lines)

# ---------------------------------------------------------------- 中文全文网页（GitHub Pages 托管）

_PAGE_TMPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
max-width:720px;margin:0 auto;padding:18px 16px 40px;line-height:1.95;color:#1a1a1a;background:#fff}}
h1{{font-size:1.32em;line-height:1.5;margin:10px 0 4px}}
.meta{{color:#888;font-size:.85em;margin-bottom:4px}}
a{{color:#2563eb}}
.btn{{display:inline-block;background:#2563eb;color:#fff;text-decoration:none;border-radius:8px;
padding:8px 16px;font-size:.9em;margin:10px 0 14px}}
p{{margin:0 0 1.05em;text-align:justify}}
.note{{color:#999;font-size:.8em;border-top:1px solid #eee;padding-top:12px;margin-top:28px;word-break:break-all}}
.s{{color:#999;font-size:.82em}}
li{{margin:.45em 0}}
</style>
</head>
<body>
<a class="btn" href="{orig}" rel="noopener noreferrer">🌐 查看英文原文</a>
<h1>{title}</h1>
<div class="meta">{src} · {time} · 机器翻译，仅供参考</div>
{paras}
<div class="note">英文原文：{orig_text}</div>
</body>
</html>"""

_PAGE_INDEX_TMPL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>期货外盘简报 · {date} 全文索引</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
max-width:720px;margin:0 auto;padding:18px 16px 40px;line-height:1.8;color:#1a1a1a;background:#fff}}
h1{{font-size:1.25em}} a{{color:#2563eb}}
.s{{color:#999;font-size:.82em}} li{{margin:.45em 0}}
</style>
</head>
<body>
<h1>期货外盘简报 · {date} 中文全文索引</h1>
<ul>
{items}
</ul>
</body>
</html>"""


def write_article_pages(selected, pages_base):
    """把每条已全文翻译的新闻写成独立 HTML 页（docs/articles/<日期>/<hash>.html，
    交给 GitHub Pages 托管），并在每条上记录 page_url；返回生成页数。"""
    date_dir = now_cn().strftime("%Y-%m-%d")
    adir = BASE / "docs" / "articles" / date_dir
    n = 0
    for x in selected:
        if not x.get("paras_zh"):
            continue
        orig = x.get("real_link") or x["link"]
        slug = hashlib.md5(orig.encode("utf-8")).hexdigest()[:10]
        adir.mkdir(parents=True, exist_ok=True)
        tm = x["dt"].strftime("%Y-%m-%d %H:%M") if x["dt"] else now_cn().strftime("%Y-%m-%d")
        paras_html = "\n".join(f"<p>{htmllib.escape(p)}</p>" for p in x["paras_zh"])
        (adir / f"{slug}.html").write_text(_PAGE_TMPL.format(
            title=htmllib.escape(x["title_zh"]),
            orig=htmllib.escape(orig, quote=True),
            src=htmllib.escape(x["src"]), time=tm,
            paras=paras_html, orig_text=htmllib.escape(orig)), encoding="utf-8")
        x["_slug"] = slug
        x["page_url"] = f"{pages_base.rstrip('/')}/articles/{date_dir}/{slug}.html"
        n += 1
    # 当日索引页：列出本次运行的全部文章
    if n:
        rows = []
        for x in selected:
            if x.get("_slug"):
                rows.append(
                    f'<li><a href="{x["_slug"]}.html">{htmllib.escape(x["title_zh"])}</a>'
                    f' <span class="s">{htmllib.escape(x["src"])}</span></li>')
        (adir / "index.html").write_text(
            _PAGE_INDEX_TMPL.format(date=date_dir, items="\n".join(rows)), encoding="utf-8")
    return n


def _trim_zh(text, limit):
    """中文文本截断（在标点处收尾，加省略号）。"""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。；、！？,.;:！？ ") + "…"


def _first_zh(text, limit=200):
    """取译文开头 1-2 句当摘要（在句号处收尾）。"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, 40, -1):
        if cut[i] in "。！？":
            return cut[:i + 1]
    return _trim_zh(cut, limit)

# ---------------------------------------------------------------- 简报生成


def build_digest(pool, hours, mode_label, max_per_section, max_total,
                 opener=None, translate_on=True, summary_chars=130,
                 calendar_md="", pages_base="", max_overview=8):
    cutoff = now_cn() - timedelta(hours=hours)
    fresh = [x for x in pool if x["dt"] is None or x["dt"] >= cutoff]
    fresh.sort(key=lambda x: x["dt"] or now_cn(), reverse=True)
    sections = {t: [] for t in TOPIC_ORDER}
    for x in fresh:
        sections[x["topic"]].append(x)

    # 先确定入选条目，再并发抓整篇正文
    selected = []
    for t in TOPIC_ORDER:
        rows = sections[t][:max_per_section]
        if rows and len(selected) < max_total:
            selected.extend(rows)
    total = min(sum(min(len(sections[t]), max_per_section) for t in TOPIC_ORDER),
                max_total)

    for x in selected:
        x["paras"], x["paras_zh"], x["real_link"] = [], [], x["link"]
        x["title_zh"], x["page_url"], x["summary"] = x["title"], "", ""

    # 1) 并发抓整篇正文（Google News 链接会先解出真实出版方链接）
    if opener is not None and selected:
        log(f"并发抓取 {len(selected)} 条新闻的整篇正文…")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fetch_full_article, opener, x["link"]): x
                    for x in selected}
            for fut, x in futs.items():
                try:
                    paras, real = fut.result(timeout=60)
                    x["paras"], x["real_link"] = paras, real
                except Exception:
                    pass
        for x in selected:  # 正文抓不到时回退 RSS 描述
            if not x["paras"] and x.get("desc"):
                x["paras"] = [x["desc"]]
        log(f"正文抓取完成：成功 {sum(1 for x in selected if x['paras'])}/{len(selected)}")

    # 2) 全文翻译：标题 + 每个正文段落并发翻译（整篇翻完，不只翻一两句）
    if translate_on and opener is not None:
        texts = []
        for x in selected:
            texts.append(x["title"])
            texts.extend(x["paras"])
        uniq = list({t for t in texts if t})
        log(f"并发翻译 {len(uniq)} 段文本（标题 + 全部正文段落）…")
        tr = translate_many(opener, uniq, workers=8)
        log(f"翻译完成：成功 {sum(1 for v in tr.values() if v)}/{len(tr)}")
        for x in selected:
            x["title_zh"] = tr.get(x["title"]) or x["title"]
            x["paras_zh"] = [tr.get(p) or p for p in x["paras"]]
    else:
        for x in selected:  # 翻译关闭时直接用原文（页面照常生成）
            x["paras_zh"] = list(x["paras"])

    # 3) 每条新闻生成独立中文全文网页（点开即读整篇中文翻译）
    if pages_base:
        log(f"生成中文全文网页 → docs/articles/{now_cn().strftime('%Y-%m-%d')}/")
        log(f"共生成 {write_article_pages(selected, pages_base)} 个中文全文网页")

    # 4) 每条提炼一句中文摘要（正文译文的前 1-2 句）
    for x in selected:
        x["summary"] = _first_zh("".join(x["paras_zh"]))

    # 5) 今日要闻：重大性打分（>=3 分入选，权重 3 的类目才算"要"），最多 max_overview 条；
    #    同一事件的不同表述（相似度 > 0.6）只保留分数最高的一条，绝不重复
    scored = []
    for x in selected:
        s, cats = importance(x["title"])
        if s >= 3:
            scored.append((s, cats, x))
    scored.sort(key=lambda r: (-r[0], -(r[2]["dt"] or now_cn()).timestamp()))
    overview, seen_keys = [], []
    for s, cats, x in scored:
        if len(overview) >= max_overview:
            break
        k = norm_key(x["title"])
        if any(difflib.SequenceMatcher(None, k, sk).ratio() > 0.6 for sk in seen_keys):
            continue  # 同一事件不同报道，只留分数最高的一条
        seen_keys.append(k)
        overview.append((cats[0], s, x))

    def links_md(x):
        parts = []
        if x.get("page_url"):
            parts.append(f"[📖 中文全文]({x['page_url']})")
        parts.append(f"[🌐 英文原文]({x['real_link'] or x['link']})")
        return " ｜ ".join(parts)

    def render(schars):
        lines = []
        lines.append("\n## 🚨 今日要闻\n")
        if overview:
            for i, (cat, s, x) in enumerate(overview, 1):
                stars = "★★★" if s >= 5 else "★★"
                lines.append(f"**{i}.【{cat}】{stars} {_trim_zh(x['title_zh'], 46)}**")
                if schars > 0 and x["summary"]:
                    lines.append(f"\n{_trim_zh(x['summary'], min(schars, 90))}")
                lines.append(f"\n> {links_md(x)}")
                lines.append("")
        else:
            lines.append("（本时段没有命中的重大事件，详见下方分板块简报）")
        if calendar_md:
            lines.append("\n## 📅 今明两天重要事件（财经日历）\n")
            lines.append(calendar_md)
        count = 0
        for t in TOPIC_ORDER:
            rows = sections[t][:max_per_section]
            if not rows or count >= max_total:
                continue
            lines.append(f"\n## {TOPIC_CN[t]}（{len(rows)}）\n")
            for i, x in enumerate(rows):
                count += 1
                tm = x["dt"].strftime("%H:%M") if x["dt"] else "--:--"
                lines.append(f"**{i + 1}. {tm}【{x['src']}】{x['title_zh']}**")
                if schars > 0 and x["summary"]:
                    lines.append(f"\n{_trim_zh(x['summary'], schars)}")
                lines.append(f"\n> {links_md(x)}")
                lines.append("")
        if count == 0:
            lines.append("\n（本次时间窗口内没有抓到符合条件的新闻）")
        return lines

    head = (
        f"# 期货外盘{mode_label} · {now_cn().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"> 回溯最近 **{hours} 小时** · 共 **{total}** 条 · 每条可点【📖 中文全文】读整篇翻译 · "
        f"来源：Reuters / Bloomberg / FT / WSJ / CNBC / EIA / OPEC / Argus / Platts 等\n"
    )
    # 推送长度保护：Server酱上限约 32KB，超长时逐级压缩每条摘要字数
    schars = summary_chars
    while True:
        md = head + "\n".join(render(schars)) + "\n"
        if len(md.encode("utf-8")) <= 30000 or schars <= 60:
            break
        schars = max(60, schars - 40)
        log(f"简报超长，压缩每条摘要字数至 {schars}")
    return md

# ---------------------------------------------------------------- 推送

def push_serverchan(key, title, md):
    data = urllib.parse.urlencode({"title": title, "desp": md}).encode()
    req = urllib.request.Request(f"https://sctapi.ftqq.com/{key}.send", data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "ignore")).get("code") == 0


def push_pushplus(token, title, md):
    payload = json.dumps({"token": token, "title": title,
                          "content": md, "template": "markdown"}).encode()
    req = urllib.request.Request("https://www.pushplus.plus/send", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "ignore")).get("code") == 200


def get_push_keys(cfg):
    """收集全部 Server酱 SendKey（支持多个微信）+ PushPlus token。
    来源：环境变量 SERVERCHAN_KEYS（逗号分隔）/ SERVERCHAN_KEY / SERVERCHAN_KEY_2，
    以及 config.json 的 push.serverchan_keys（列表或逗号分隔）。"""
    sc_keys = []
    sources = [os.environ.get("SERVERCHAN_KEYS", ""),
               os.environ.get("SERVERCHAN_KEY", ""),
               os.environ.get("SERVERCHAN_KEY_2", ""),
               cfg.get("push", {}).get("serverchan_key", ""),
               cfg.get("push", {}).get("serverchan_keys", "")]
    for src in sources:
        vals = src if isinstance(src, list) else str(src).split(",")
        for k in vals:
            k = k.strip()
            if k and k not in sc_keys:
                sc_keys.append(k)
    pp = os.environ.get("PUSHPLUS_TOKEN") or cfg.get("push", {}).get("pushplus_token", "")
    return sc_keys, pp


def do_push(cfg, title, md):
    sc_keys, pp = get_push_keys(cfg)
    if not sc_keys and not pp:
        print("未配置推送 key（环境变量 SERVERCHAN_KEY/SERVERCHAN_KEY_2/SERVERCHAN_KEYS "
              "或 config.json -> push），本次只保存了文件未推送。", file=sys.stderr)
        sys.exit(1)
    ok = False
    for i, key in enumerate(sc_keys, 1):
        try:
            ok = push_serverchan(key, title, md) or ok
            log(f"Server酱 推送已发送（微信 {i}/{len(sc_keys)}）")
        except Exception:
            traceback.print_exc()
    if pp:
        try:
            ok = push_pushplus(pp, title, md) or ok
            log("PushPlus 推送已发送")
        except Exception:
            traceback.print_exc()
    return ok

# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", choices=["morning", "evening"],
                    help="morning=晨报(16h) evening=晚报(9h)")
    ap.add_argument("--hours", type=int, help="自定义回溯小时数")
    ap.add_argument("--test", action="store_true", help="只生成不推送")
    ap.add_argument("--out-only", action="store_true",
                    help="只生成（简报 + 中文全文网页），保存 digests/pending.*，不推送")
    ap.add_argument("--push-file", metavar="JSON",
                    help="推送之前 --out-only 生成的简报（参数为 digests/pending.json）")
    ap.add_argument("--config", default="config.json",
                    help="配置文件名（默认 config.json，云端用 config.cloud.json）")
    args = ap.parse_args()

    cfg = json.loads((BASE / args.config).read_text(encoding="utf-8"))

    # --push-file：推送之前生成好的简报（云端先发布网页再推送，避免链接 404）
    if args.push_file:
        meta = json.loads((BASE / args.push_file).read_text(encoding="utf-8"))
        md = (BASE / meta["file"]).read_text(encoding="utf-8")
        sys.exit(0 if do_push(cfg, meta["title"], md) else 2)

    if args.hours:
        hours = args.hours
    elif args.mode == "morning":
        hours = 16
    elif args.mode == "evening":
        hours = 9
    else:
        h = now_cn().hour
        hours = 16 if h < 12 else 9
    mode_label = "晨报" if hours > 12 else "晚报"

    log(f"开始抓取（回溯 {hours} 小时）…")
    pool = collect(cfg)
    log(f"去重后共 {len(pool)} 条，开始过滤生成简报")
    want_tr = cfg.get("translate", True)
    opener = build_opener(cfg.get("proxy") or None)  # 抓正文/日历/翻译都需要
    cal_md = ""
    if cfg.get("calendar", True) and opener is not None:
        cal_md = fetch_calendar(opener)
    pages_base = cfg.get("pages_base", "") or os.environ.get("PAGES_BASE", "")
    md = build_digest(pool, hours, mode_label,
                      cfg.get("max_per_section", 6), cfg.get("max_total", 24),
                      opener=opener, translate_on=want_tr,
                      summary_chars=cfg.get("summary_chars", 130),
                      calendar_md=cal_md, pages_base=pages_base,
                      max_overview=cfg.get("max_overview", 8))

    digests = BASE / "digests"
    digests.mkdir(exist_ok=True)
    stamp = now_cn().strftime("%Y%m%d_%H%M")
    fname = digests / f"digest_{stamp}.md"
    fname.write_text(md, encoding="utf-8")
    log(f"简报已保存: {fname}")

    title = f"期货外盘{mode_label} {now_cn().strftime('%m-%d %H:%M')}"

    if args.test:
        print("\n" + "=" * 60 + "\n")
        print(md)
        return

    if args.out_only:
        # 保存待推送清单：工作流先 commit&push 中文网页（GitHub Pages 构建），再回头推送
        (digests / "pending.md").write_text(md, encoding="utf-8")
        (digests / "pending.json").write_text(
            json.dumps({"title": title, "file": "digests/pending.md",
                        "stamp": stamp}, ensure_ascii=False), encoding="utf-8")
        log("已保存 digests/pending.md + pending.json（等待网页发布后推送）")
        return

    sys.exit(0 if do_push(cfg, title, md) else 2)


if __name__ == "__main__":
    main()
