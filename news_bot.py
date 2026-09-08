#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货外盘新闻简报推送机器人
================================
抓取国外主流财经媒体与机构 RSS（Reuters / Bloomberg / FT / WSJ / CNBC / EIA / OPEC / Argus / Platts /
Mining.com / World Grain 等），按主题分类去重，机器翻译成中文，
生成"今日要点速览 + 分板块中文化全文简报"，推送到微信（Server酱 / PushPlus）。

简报结构：
    1. 今日要点速览 —— 按重大性打分（地缘/天气/行情剧变/央行/供需）挑出的大事，一句话中文概括
    2. 分板块详情 —— 每条：中文标题 + 中文正文摘录（前 N 条）+ 原始英文链接

翻译：主通道 Google 翻译免费接口（GitHub Actions 美国机房可用），
      兜底 MyMemory（本机/云端都可用，免费额度有限）。翻译失败自动回退英文原文。

用法：
    python news_bot.py --test            # 只生成简报到 digests/ 目录，不推送（先跑这个看效果）
    python news_bot.py                   # 生成并推送（需在 config.json 填好推送 key）
    python news_bot.py morning           # 晨报模式：回溯最近 16 小时（覆盖隔夜美盘）
    python news_bot.py evening           # 晚报模式：回溯最近 9 小时（覆盖白天时段）
    python news_bot.py --hours 12        # 自定义回溯窗口
    python news_bot.py --config config.cloud.json   # 使用云端配置

配置：同目录 config.json
推送 key 优先级：环境变量 SERVERCHAN_KEY / PUSHPLUS_TOKEN > config.json（云端部署用环境变量，避免 key 进代码库）
"""

import argparse
import difflib
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
    r"kidnapping|robbery|indicted)\b", re.I)

# ---------------------------------------------------------------- 重大性打分（"今日要点速览"用）
# (类别, 权重, 关键词正则)：命中的新闻进入"今日要点"候选，权重高者优先入选
IMPORTANCE_CATS = [
    ("地缘", 3, re.compile(
        r"\b(war|wars|missile\w*|airstrike|air strike|attack\w*|drone strike|"
        r"invasion|invade\w*|escalat\w*|sanction\w*|embargo|hormuz|red sea|"
        r"houthi|blockade|ceasefire|nuclear)\b", re.I)),
    ("天气", 3, re.compile(
        r"\b(el ni[nñ]o|la ni[nñ]a|drought\w*|flood\w*|heatwave|heat wave|"
        r"hurricane\w*|typhoon\w*|cyclone\w*|frost|monsoon|wildfire\w*)\b", re.I)),
    ("行情", 2, re.compile(
        r"\b(surge[sd]?|surging|soar\w*|plunge[sd]?|plunging|tumble\w*|"
        r"spike[sd]?|slump\w*|skyrocket\w*|record (high|low)|"
        r"all[- ]time (high|low)|selloff|crash\w*|rally)\b", re.I)),
    ("央行", 2, re.compile(
        r"\b(rate cut\w*|rate hike\w*|interest[- ]rate|fomc|powell|"
        r"fed chair\w*|ecb|boj|central bank\w*|beige book|stimulus|"
        r"policy minutes)\b", re.I)),
    ("供需", 2, re.compile(
        r"\b(opec\+?|production cut\w*|output cut\w*|quota\w*|"
        r"export (ban\w*|curb\w*|restriction\w*)|force majeure|outage\w*|"
        r"shortage\w*|deficit|surplus|halt\w*|disruption\w*)\b", re.I)),
]


def importance(title):
    """返回 (总分, 命中类别列表)；总分 >= 2 才有资格进"今日要点"。"""
    score, cats = 0, []
    for cat, w, pat in IMPORTANCE_CATS:
        if pat.search(title):
            score += w
            cats.append(cat)
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
            if NOISE_DROP.search(title):
                continue  # 体育/娱乐/社会案件类噪音
            # 模糊去重：与已收录标题相似度 > 0.8 视为同一事件（仅留最新一条）
            if any(difflib.SequenceMatcher(None, k, norm_key(t)).ratio() > 0.8
                   for t in kept_titles):
                continue
            seen.add(k)
            kept_titles.append(title)
            topic = classify(title) or feed.get("topic") or "other"
            if feed.get("strict") and classify(title) is None:
                continue  # 严格源：关键词不命中就丢弃（过滤生活方式类内容）
            pool.append({"title": title, "link": link, "dt": dt,
                         "src": src, "topic": topic,
                         "desc": _usable_desc(desc)})
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


def fetch_excerpt(opener, link, timeout=10):
    """抓原文页面提取正文（og:description + 前几段，约 600 字符供翻译）；失败返回空串。"""
    real_link = link
    try:
        # Google News 跳转页：先解出真实出版方链接
        if "news.google.com" in urllib.parse.urlparse(link).netloc:
            req = urllib.request.Request(link, headers={"User-Agent": UA})
            with opener.open(req, timeout=timeout) as r:
                body = r.read(200000).decode("utf-8", "ignore")
            m = re.search(r'<a[^>]+href="(https?://(?!news\.google\.com|support\.google\.com|policies\.google\.com)[^"]+)"',
                          body, re.I)
            if not m:
                return ""
            real_link = htmllib.unescape(m.group(1))
        req = urllib.request.Request(real_link, headers={"User-Agent": UA})
        with opener.open(req, timeout=timeout) as r:
            body = r.read(400000).decode("utf-8", "ignore")
    except Exception:
        return ""
    # 先剥掉脚本和样式块，防止 JS 代码混进正文
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    m = _META_DESC_RE.search(body) or _META_DESC_RE2.search(body)
    parts = []
    if m:
        d = _clean_text(m.group(1))
        if len(d) >= 40 and not _PARA_CODE.search(d):
            parts.append(d)
    # 兜底/补充：正文前几段（凑够 ~600 字符供翻译）
    for pm in re.finditer(r"<p[^>]*>(.*?)</p>", body, re.S | re.I):
        t = _clean_text(pm.group(1))
        if len(t) < 60 or _PARA_JUNK.match(t) or _PARA_CODE.search(t) or _junk_para(t):
            continue
        if parts and (t in parts[-1] or parts[-1] in t):
            continue  # 与 meta 描述重复
        parts.append(t)
        if sum(len(p) for p in parts) >= 600:
            break
    text = " ".join(parts)
    if len(text) < 40:
        return ""
    if len(text) > 700:
        text = text[:700]
    return text

# ---------------------------------------------------------------- 机器翻译（中文化简报）
# 主接口 Google 翻译免费通道（GitHub Actions 美国机房可用，本机被墙），
# 兜底 MyMemory（本机/云端都可用，免费额度有限，仅在主接口失败时调用）。
# 全部失败返回空串，调用方回退英文原文，不影响简报生成。

TRANS_CACHE = {}
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
_MM_JUNK = re.compile(r"MYMEMORY WARNING|QUERY LENGTH LIMIT|INVALID (SOURCE|TARGET)", re.I)


def _google_translate(opener, text):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           "&sl=en&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return "".join(seg[0] for seg in data[0] if seg and seg[0])


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


def fetch_calendar(opener, days=2, limit=14):
    """返回格式化好的财经日历文本块；失败返回空串（不影响简报生成）。"""
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
            picked.append(it)
    picked.sort(key=lambda x: x.get("public_date") or 0)
    if not picked:
        return ""
    lines = []
    for it in picked[:limit]:
        try:
            tm = datetime.fromtimestamp(it["public_date"], CN_TZ).strftime("%m-%d %H:%M")
        except (KeyError, TypeError, ValueError, OSError):
            continue
        stars = "★" * int(it.get("importance") or 0)
        extra = []
        if it.get("forecast"):
            extra.append(f"预期 {it['forecast']}")
        if it.get("previous"):
            extra.append(f"前值 {it['previous']}")
        tail = ("（" + "，".join(extra) + "）") if extra else ""
        lines.append(f"- **{tm}**【{it.get('country', '')}】{it.get('title', '')} {stars}{tail}")
    log(f"财经日历：{len(picked)} 个重要事件（{days} 天内）")
    return "\n".join(lines)

# ---------------------------------------------------------------- 简报生成

def _trim_zh(text, limit):
    """中文文本截断（在标点处收尾，加省略号）。"""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。；、！？,.;:！？ ") + "…"


def build_digest(pool, hours, mode_label, max_per_section, max_total,
                 opener=None, fetch_excerpts=True, translate_on=True,
                 body_chars=150, body_per_section=4, calendar_md=""):
    cutoff = now_cn() - timedelta(hours=hours)
    fresh = [x for x in pool if x["dt"] is None or x["dt"] >= cutoff]
    fresh.sort(key=lambda x: x["dt"] or now_cn(), reverse=True)
    sections = {t: [] for t in TOPIC_ORDER}
    for x in fresh:
        sections[x["topic"]].append(x)

    # 先确定入选条目，再并发抓正文
    selected = []
    for t in TOPIC_ORDER:
        rows = sections[t][:max_per_section]
        if rows and len(selected) < max_total:
            selected.extend(rows)
    total = min(sum(min(len(sections[t]), max_per_section) for t in TOPIC_ORDER),
                max_total)

    excerpts = {}
    if fetch_excerpts and opener is not None and selected:
        log(f"并发抓取 {len(selected)} 条新闻的原文正文…")
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(fetch_excerpt, opener, x["link"]): x["link"]
                    for x in selected}
            for fut, lnk in futs.items():
                try:
                    excerpts[lnk] = fut.result(timeout=40)
                except Exception:
                    excerpts[lnk] = ""
        got = sum(1 for v in excerpts.values() if v)
        log(f"正文抓取完成：成功 {got}/{len(selected)}")

    # 机器翻译：中文标题 + 中文正文（失败自动回退英文）
    tr = {}
    for x in selected:
        x["body_en"] = ""
        x["body_zh"] = ""
        x["title_zh"] = x["title"]
    if translate_on and opener is not None:
        texts = []
        for x in selected:
            src = excerpts.get(x["link"], "") or x.get("desc", "")
            x["body_en"] = src[:600]
            texts.append(x["title"])
            if x["body_en"]:
                texts.append(x["body_en"])
        uniq = list({t for t in texts if t})
        log(f"并发翻译 {len(uniq)} 段文本（标题+正文）…")
        tr = translate_many(opener, texts)
        got = sum(1 for v in tr.values() if v)
        log(f"翻译完成：成功 {got}/{len(tr)}")
        for x in selected:
            x["title_zh"] = tr.get(x["title"]) or x["title"]
            x["body_zh"] = tr.get(x["body_en"], "") if x["body_en"] else ""

    # 今日要点速览：重大性打分（>=2 分入选），每类别最多 2 条，总共最多 8 条
    scored = []
    for x in selected:
        s, cats = importance(x["title"])
        if s >= 2:
            scored.append((s, cats, x))
    scored.sort(key=lambda r: (-r[0], -(r[2]["dt"] or now_cn()).timestamp()))
    overview, cat_count = [], {}
    for s, cats, x in scored:
        if len(overview) >= 8:
            break
        if any(cat_count.get(c, 0) >= 2 for c in cats):
            continue
        for c in cats:
            cat_count[c] = cat_count.get(c, 0) + 1
        overview.append((cats[0], x))

    def render(bps):
        lines = []
        lines.append("\n## 📌 今日要点速览\n")
        if overview:
            for i, (cat, x) in enumerate(overview, 1):
                lines.append(f"**{i}.【{cat}】{_trim_zh(x['title_zh'], 46)}** "
                             f"—— [阅读原文]({x['link']})")
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
                if x["body_zh"] and i < bps:
                    lines.append(f"\n{_trim_zh(x['body_zh'], body_chars)}")
                    lines.append(f"\n> 原文：{x['title']}\n> [阅读英文原文]({x['link']})")
                else:
                    lines.append(f"[阅读原文]({x['link']})")
                lines.append("")
        if count == 0:
            lines.append("\n（本次时间窗口内没有抓到符合条件的新闻）")
        return lines

    head = (
        f"# 期货外盘{mode_label} · {now_cn().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"> 回溯最近 **{hours} 小时** · 共 **{total}** 条 · 全文中文摘要版 · "
        f"来源：Reuters / Bloomberg / FT / WSJ / CNBC / EIA / OPEC / Argus / Platts 等\n"
    )
    # 推送长度保护：Server酱上限约 32KB，超长时逐级减少带正文的条数
    bps = body_per_section
    while True:
        md = head + "\n".join(render(bps)) + "\n"
        if len(md.encode("utf-8")) <= 30000 or bps <= 0:
            break
        bps -= 1
        log(f"简报超长，压缩正文条数至每板块 {bps} 条")
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

# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", choices=["morning", "evening"],
                    help="morning=晨报(16h) evening=晚报(9h)")
    ap.add_argument("--hours", type=int, help="自定义回溯小时数")
    ap.add_argument("--test", action="store_true", help="只生成不推送")
    ap.add_argument("--config", default="config.json",
                    help="配置文件名（默认 config.json，云端用 config.cloud.json）")
    args = ap.parse_args()

    cfg = json.loads((BASE / args.config).read_text(encoding="utf-8"))
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
    want_ex = cfg.get("fetch_excerpts", True)
    want_tr = cfg.get("translate", True)
    opener = build_opener(cfg.get("proxy") or None) if (want_ex or want_tr) else None
    cal_md = ""
    if cfg.get("calendar", True) and opener is not None:
        cal_md = fetch_calendar(opener)
    md = build_digest(pool, hours, mode_label,
                      cfg.get("max_per_section", 8), cfg.get("max_total", 30),
                      opener=opener, fetch_excerpts=want_ex,
                      translate_on=want_tr,
                      body_chars=cfg.get("body_chars", 150),
                      body_per_section=cfg.get("body_per_section", 4),
                      calendar_md=cal_md)

    digests = BASE / "digests"
    digests.mkdir(exist_ok=True)
    fname = digests / f"digest_{now_cn().strftime('%Y%m%d_%H%M')}.md"
    fname.write_text(md, encoding="utf-8")
    log(f"简报已保存: {fname}")

    if args.test:
        print("\n" + "=" * 60 + "\n")
        print(md)
        return

    # 推送 key：环境变量优先（云端部署），其次配置文件（本机运行）
    sc_key = os.environ.get("SERVERCHAN_KEY") or cfg.get("push", {}).get("serverchan_key", "")
    pp_token = os.environ.get("PUSHPLUS_TOKEN") or cfg.get("push", {}).get("pushplus_token", "")
    if not sc_key and not pp_token:
        print("未配置推送 key（环境变量 SERVERCHAN_KEY/PUSHPLUS_TOKEN 或 config.json -> push），"
              "本次只保存了文件未推送。请先运行 --test 查看效果，再配置推送。", file=sys.stderr)
        sys.exit(1)

    title = f"期货外盘{mode_label} {now_cn().strftime('%m-%d %H:%M')}"
    ok = False
    if sc_key:
        try:
            ok = push_serverchan(sc_key, title, md) or ok
            log("Server酱 推送已发送")
        except Exception:
            traceback.print_exc()
    if pp_token:
        try:
            ok = push_pushplus(pp_token, title, md) or ok
            log("PushPlus 推送已发送")
        except Exception:
            traceback.print_exc()
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
