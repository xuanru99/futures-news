#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货外盘新闻简报推送机器人
================================
抓取国外主流财经媒体 RSS（WSJ / CNBC / MarketWatch / FXStreet / OilPrice / Mining.com / World Grain 等），
按主题分类去重后生成中文结构化简报，推送到微信（Server酱 / PushPlus）。

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
        "energy department", "eia", "fuel",
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
    return re.sub(r"[^a-z0-9]", "", t.lower())[:80]


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
            out.append((title, link, dt, feed["name"]))
    return out


def fetch_feed(opener, feed, timeout=15):
    """返回 [(title, link, dt, source)]；失败抛异常。"""
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
            if title and link:
                out.append((title, link, dt, src))
    else:      # Atom
        for it in root.findall(".//atom:entry", ns):
            title = clean_title(it.findtext("atom:title", default="", namespaces=ns) or "")
            le = it.find("atom:link", ns)
            link = le.get("href", "") if le is not None else ""
            dt = parse_date(it.findtext("atom:published", default="", namespaces=ns)
                            or it.findtext("atom:updated", default="", namespaces=ns) or "")
            if title and link:
                out.append((title, link, dt, feed["name"]))
    return out


def collect(cfg):
    opener = build_opener(cfg.get("proxy") or None)
    seen, pool = set(), []
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
        for title, link, dt, src in rows:
            k = norm_key(title)
            if not k or k in seen:
                continue
            if FOREX_DROP.search(title):
                continue  # 纯外汇货币对分析，与商品期货无关
            seen.add(k)
            topic = classify(title) or feed.get("topic") or "other"
            if feed.get("strict") and classify(title) is None:
                continue  # 严格源：关键词不命中就丢弃（过滤生活方式类内容）
            pool.append({"title": title, "link": link, "dt": dt,
                         "src": src, "topic": topic})
            added += 1
        log(f"抓取成功 {feed['name']}: {len(rows)} 条, 新增 {added}")
    return pool

# ---------------------------------------------------------------- 简报生成

def build_digest(pool, hours, mode_label, max_per_section, max_total):
    cutoff = now_cn() - timedelta(hours=hours)
    fresh = [x for x in pool if x["dt"] is None or x["dt"] >= cutoff]
    fresh.sort(key=lambda x: x["dt"] or now_cn(), reverse=True)
    sections = {t: [] for t in TOPIC_ORDER}
    for x in fresh:
        sections[x["topic"]].append(x)

    total = 0
    lines = []
    for t in TOPIC_ORDER:
        rows = sections[t][:max_per_section]
        if not rows or total >= max_total:
            continue
        lines.append(f"\n## {TOPIC_CN[t]}（{len(rows)}）\n")
        for x in rows:
            tm = x["dt"].strftime("%H:%M") if x["dt"] else "--:--"
            lines.append(f"- **{tm}** [{x['src']}] [{x['title']}]({x['link']})")
            total += 1
    if total == 0:
        lines.append("\n（本次时间窗口内没有抓到符合条件的新闻）")

    head = (
        f"# 期货外盘{mode_label} · {now_cn().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"> 回溯最近 **{hours} 小时** · 共 **{total}** 条 · "
        f"来源：WSJ / CNBC / MarketWatch / FXStreet / OilPrice / Mining / World Grain\n"
    )
    return head + "\n".join(lines) + "\n"

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
    md = build_digest(pool, hours, mode_label,
                      cfg.get("max_per_section", 8), cfg.get("max_total", 45))

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
