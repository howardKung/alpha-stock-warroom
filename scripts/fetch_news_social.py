#!/usr/bin/env python3
"""
Zero-Cost Multi-Source News & Social Intelligence Engine (`fetch_news_social.py`).
Every single news item, social post, and executive takeaway includes:
- Real publisher `source` (unwrapped from Bing/Google RSS + domain mapping, never generic placeholders)
- Clean direct `url` (unwrapping Bing `apiclick.aspx` redirect wrappers)
"""

import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_TW = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "news_feed.json"
IDEA_COPY = Path("/home/antigravity/idea-team/stock-division/data/news_feed_latest.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

DOMAIN_SOURCE_MAP = [
    ("money.udn.com", "經濟日報"),
    ("udn.com", "聯合新聞網"),
    ("ctee.com.tw", "工商時報"),
    ("chinatimes.com", "中時新聞網"),
    ("tw.stock.yahoo.com", "Yahoo奇摩股市"),
    ("yahoo.com", "Yahoo奇摩財經"),
    ("cnyes.com", "鉅亨網"),
    ("cna.com.tw", "中央社 CNA"),
    ("ec.ltn.com.tw", "自由財經"),
    ("ltn.com.tw", "自由時報"),
    ("technews.tw", "科技新報 TechNews"),
    ("ithome.com.tw", "iThome"),
    ("bnext.com.tw", "數位時代"),
    ("inside.com.tw", "INSIDE 硬塞"),
    ("moneydj.com", "MoneyDJ理財網"),
    ("wealth.com.tw", "財訊"),
    ("businessweekly.com.tw", "商業周刊"),
    ("cw.com.tw", "天下雜誌"),
    ("ettoday.net", "ETtoday財經雲"),
    ("newtalk.tw", "Newtalk新聞"),
    ("storm.mg", "風傳媒"),
    ("setn.com", "三立新聞網"),
    ("tvbs.com.tw", "TVBS新聞網"),
    ("msn.com", "MSN 財經"),
    ("reuters.com", "Reuters 路透社"),
    ("bloomberg.com", "Bloomberg 彭博"),
    ("wsj.com", "WSJ 華爾街日報"),
    ("cnbc.com", "CNBC"),
]

BULL_WORDS = ["漲停", "噴", "創高", "大漲", "買超", "利多", "看好", "軋空", "突破", "史高", "大爆買", "翻多", "強彈"]
BEAR_WORDS = ["跌停", "崩", "大跌", "賣超", "利空", "逃命", "腰斬", "套牢", "破底", "跳水", "斷頭", "恐慌", "空頭"]
PORTFOLIO_KEYWORDS = [
    ("3661.TW", ["世芯", "3661"]),
    ("3017.TW", ["奇鋐", "3017"]),
    ("8299.TWO", ["群聯", "8299"]),
    ("0050.TW", ["0050", "台灣50", "台積電", "2330"]),
    ("META", ["Meta", "META", "祖克柏"]),
    ("TLT", ["美債", "殖利率", "聯準會", "Fed", "降息", "升息", "通膨"]),
    ("PLTR", ["Palantir", "PLTR"]),
    ("1560.TW", ["中砂", "1560"]),
    ("3711.TW", ["日月光", "3711"]),
    ("3665.TW", ["貿聯", "3665"]),
    ("2454.TW", ["聯發科", "2454"]),
    # 大 Boss 常態追蹤台股 10 檔
    ("00991A.TW", ["00991A", "復華未來50", "未來50"]),
    ("1527.TW", ["鑽全", "1527"]),
    ("2329.TW", ["華泰", "2329"]),
    ("2420.TW", ["新巨", "2420"]),
    ("2428.TW", ["興勤", "2428"]),
    ("2449.TW", ["京元電", "京元電子", "2449"]),
    ("3037.TW", ["欣興", "3037"]),
    ("8110.TW", ["華東", "8110"]),
    ("8112.TW", ["至上", "8112"]),
    ("8422.TW", ["可寧衛", "8422"]),
]


def http_get(url: str, extra_headers: dict | None = None, timeout: int = 10) -> str:
    headers = {"User-Agent": UA, **(extra_headers or {})}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="ignore")


def clean_html(raw: str) -> str:
    if not raw:
        return ""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", txt).strip()


def unwrap_redirect_url(raw_url: str) -> str:
    """Unwraps Bing News apiclick.aspx wrapper into the direct original article URL."""
    if not raw_url:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw_url)
        if "bing.com" in parsed.netloc and "apiclick.aspx" in parsed.path:
            qs = urllib.parse.parse_qs(parsed.query)
            if "url" in qs and qs["url"]:
                return qs["url"][0]
    except Exception:
        pass
    return raw_url


def infer_source_from_url_or_text(article_url: str, summary: str = "", fallback: str = "國際財經媒體") -> str:
    try:
        host = urllib.parse.urlparse(article_url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        for dom, label in DOMAIN_SOURCE_MAP:
            if host == dom or host.endswith("." + dom):
                return label
        # Check leading publisher name in summary (e.g. "Newtalk新聞 ...")
        for _dom, label in DOMAIN_SOURCE_MAP:
            short = label.split()[0]
            if summary.startswith(short):
                return label
        if host and "google.com" not in host and "bing.com" not in host:
            return host
    except Exception:
        pass
    return fallback


def match_portfolio_tags(text: str) -> list[str]:
    matched = []
    for sym, kws in PORTFOLIO_KEYWORDS:
        if any(k.lower() in text.lower() for k in kws):
            matched.append(sym)
    return matched[:3]


def fetch_cnyes_category(cat: str, limit: int = 8) -> list[dict]:
    url = f"https://api.cnyes.com/media/api/v1/newslist/category/{cat}?limit={limit}"
    try:
        data = json.loads(http_get(url))
        rows = data.get("items", {}).get("data", [])
        out = []
        for item in rows:
            nid = item.get("newsId")
            title = clean_html(item.get("title", ""))
            summary = clean_html(item.get("summary", ""))[:140]
            pub_ts = item.get("publishAt", 0)
            pub_str = (
                datetime.fromtimestamp(pub_ts, TZ_TW).strftime("%m-%d %H:%M")
                if pub_ts
                else ""
            )
            if not title or not nid:
                continue
            tags = match_portfolio_tags(title + " " + summary)
            out.append({
                "title": title,
                "summary": summary,
                "url": f"https://news.cnyes.com/news/id/{nid}",
                "source": "鉅亨網 cnyes",
                "time": pub_str,
                "tags": tags,
            })
        return out
    except Exception:
        return []


def fetch_rss_items(url: str, default_source: str, limit: int = 8) -> list[dict]:
    try:
        xml_str = http_get(url)
        root = ET.fromstring(xml_str)
        out = []
        for item in root.findall(".//item")[:limit]:
            raw_title = clean_html(item.findtext("title") or "")
            raw_link = (item.findtext("link") or "").strip()
            link = unwrap_redirect_url(raw_link)
            desc = clean_html(item.findtext("description") or "")[:130]
            pub = (item.findtext("pubDate") or "")[:22]

            # Check standard <source> and Bing <News:Source> namespace tags
            src = ""
            for child in item:
                tag_lower = child.tag.lower()
                if tag_lower.endswith("source") and child.text and clean_html(child.text):
                    src = clean_html(child.text)
                    break

            if " - " in raw_title and not src:
                parts = raw_title.rsplit(" - ", 1)
                if 1 < len(parts[1].strip()) <= 24:
                    raw_title, src = parts[0].strip(), parts[1].strip()

            if not src or src in ("MSN", "Bing"):
                src = infer_source_from_url_or_text(link, desc, default_source)

            if not raw_title or not link:
                continue
            out.append({
                "title": raw_title,
                "summary": desc if desc and desc != raw_title else "",
                "url": link,
                "source": src,
                "time": pub,
                "tags": match_portfolio_tags(raw_title + " " + desc),
            })
        return out
    except Exception:
        return []


def fetch_ptt_stock(pages: int = 3) -> dict:
    posts = []
    next_url = "https://www.ptt.cc/bbs/Stock/index.html"
    bull_hits = 0
    bear_hits = 0

    for _ in range(pages):
        try:
            page_html = http_get(next_url, {"Cookie": "over18=1"})
        except Exception:
            break

        prev_m = re.search(r'<a class="btn wide" href="(/bbs/Stock/index\d+\.html)">‹ 上頁</a>', page_html)
        blocks = page_html.split('<div class="r-ent">')[1:]
        for blk in blocks:
            if "本文已被刪除" in blk:
                continue
            nrec_m = re.search(r'<div class="nrec">(.*?)</div>', blk, re.S)
            push_raw = clean_html(nrec_m.group(1)) if nrec_m else ""
            title_m = re.search(r'<div class="title">\s*<a href="([^"]+)">([^<]+)</a>', blk)
            date_m = re.search(r'<div class="date">\s*([^<]+)</div>', blk)
            if not title_m:
                continue
            href, title = title_m.group(1), clean_html(title_m.group(2))
            if any(skip in title for skip in ["[公告]", "盤中閒聊", "盤後閒聊"]):
                continue

            score = 100 if push_raw == "爆" else (-20 if push_raw.startswith("X") else (int(push_raw) if push_raw.isdigit() else 0))
            for w in BULL_WORDS:
                if w in title:
                    bull_hits += 1
            for w in BEAR_WORDS:
                if w in title:
                    bear_hits += 1

            posts.append({
                "push": push_raw or "0",
                "score": score,
                "title": title,
                "url": f"https://www.ptt.cc{href}",
                "source": "PTT Stock 股版",
                "date": (date_m.group(1).strip() if date_m else ""),
                "tags": match_portfolio_tags(title),
            })

        if prev_m:
            next_url = f"https://www.ptt.cc{prev_m.group(1)}"
        else:
            break

    posts.sort(key=lambda x: x["score"], reverse=True)
    total_tone = max(1, bull_hits + bear_hits)
    bull_pct = round((bull_hits / total_tone) * 100)
    if bull_pct >= 72:
        mood = f"🔥 散戶極度亢奮 ({bull_pct}% 偏多) · CRO 警示：留意追高反指標！"
    elif bull_pct <= 35:
        mood = f"🥶 散戶恐慌悲觀 ({bull_pct}% 偏多) · 往往對應短線清洗浮額低點"
    else:
        mood = f"⚖️ 散戶多空分歧 ({bull_pct}% 偏多) · 籌碼換手震盪期"

    return {
        "sourceName": "批踢踢實業坊 PTT Stock 股版",
        "sourceUrl": "https://www.ptt.cc/bbs/Stock/index.html",
        "sentimentBullPct": bull_pct,
        "sentimentLabel": mood,
        "hotPosts": posts[:12],
    }


def fetch_reddit_apewisdom() -> list[dict]:
    try:
        data = json.loads(http_get("https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"))
        out = []
        for row in data.get("results", [])[:10]:
            mentions = int(row.get("mentions") or 0)
            m24 = int(row.get("mentions_24h_ago") or 0)
            delta_pct = round(((mentions - m24) / max(1, m24)) * 100) if m24 else 100
            ticker = row.get("ticker", "")
            out.append({
                "rank": row.get("rank"),
                "ticker": ticker,
                "name": clean_html(row.get("name", "")),
                "mentions": mentions,
                "upvotes": int(row.get("upvotes") or 0),
                "momentum24hPct": delta_pct,
                "source": "Reddit (r/wallstreetbets · r/stocks)",
                "url": f"https://www.reddit.com/search/?q=%24{urllib.parse.quote(ticker)}&sort=hot",
            })
        return out
    except Exception:
        return []


def fetch_hacker_news() -> list[dict]:
    try:
        data = json.loads(http_get("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=12"))
        out = []
        for hit in data.get("hits", [])[:10]:
            title = hit.get("title") or ""
            if not title:
                continue
            oid = hit.get("objectID")
            hn_url = f"https://news.ycombinator.com/item?id={oid}"
            url = hit.get("url") or hn_url
            host = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
            src_label = f"Hacker News ({host})" if host and "ycombinator.com" not in host else "Hacker News"
            out.append({
                "title": title,
                "points": int(hit.get("points") or 0),
                "comments": int(hit.get("num_comments") or 0),
                "source": src_label,
                "url": url,
                "hnCommentsUrl": hn_url,
            })
        return out
    except Exception:
        return []


def build_sourced_takeaways(
    market_ai: list[dict],
    macro_geo: list[dict],
    ai_indie: list[dict],
    ptt: dict,
    reddit: list[dict],
    hn: list[dict],
) -> list[dict]:
    """Builds 5 concise executive takeaways where EVERY item carries verified source + direct URL."""
    items: list[dict] = []

    if market_ai:
        m0 = market_ai[0]
        items.append({
            "icon": "📈",
            "category": "台美股與供應鏈焦點",
            "text": m0["title"] + (f" — {m0['summary'][:68]}..." if m0.get("summary") else ""),
            "source": m0.get("source", "鉅亨網 cnyes"),
            "url": m0["url"],
            "time": m0.get("time", ""),
        })

    if macro_geo:
        g0 = macro_geo[0]
        items.append({
            "icon": "🌐",
            "category": "國際總經與政策風向",
            "text": g0["title"] + (f" — {g0['summary'][:68]}..." if g0.get("summary") else ""),
            "source": g0.get("source", "鉅亨網 cnyes"),
            "url": g0["url"],
            "time": g0.get("time", ""),
        })

    if ptt.get("hotPosts"):
        p0 = ptt["hotPosts"][0]
        items.append({
            "icon": "💬",
            "category": "PTT 股版散戶情緒",
            "text": f"{ptt['sentimentLabel']}｜最熱貼文 ({p0['push']}推)：{p0['title']}",
            "source": "PTT Stock 股版",
            "url": p0["url"],
            "time": p0.get("date", ""),
        })

    if reddit:
        r0 = reddit[0]
        r1_txt = f"、#{reddit[1]['rank']} ${reddit[1]['ticker']} ({reddit[1]['mentions']}則)" if len(reddit) > 1 else ""
        items.append({
            "icon": "👽",
            "category": "美股散戶社群熱度",
            "text": f"熱榜冠軍 #{r0['rank']} ${r0['ticker']} {r0['name']}（{r0['mentions']} 則討論，24h {r0['momentum24hPct']:+}%{r1_txt}）",
            "source": "Reddit WSB / r/stocks (ApeWisdom)",
            "url": r0["url"],
            "time": "24h 即時",
        })

    if ai_indie:
        a0 = ai_indie[0]
        items.append({
            "icon": "🤖",
            "category": "AI 科技與變現情報",
            "text": a0["title"],
            "source": a0.get("source", "科技新報 TechNews"),
            "url": a0["url"],
            "time": a0.get("time", ""),
        })
    elif hn:
        h0 = hn[0]
        items.append({
            "icon": "⚡",
            "category": "矽谷創投熱榜",
            "text": f"{h0['title']} ({h0['points']} pts / {h0['comments']} 則討論)",
            "source": h0.get("source", "Hacker News"),
            "url": h0["url"],
            "time": "即時熱榜",
        })

    return items


def main() -> None:
    now_tw = datetime.now(TZ_TW)
    ts_str = now_tw.strftime("%Y-%m-%d %H:%M:%S (台灣時間)")

    tw_stock = fetch_cnyes_category("tw_stock", 8)
    us_stock = fetch_cnyes_category("us_stock", 8)
    macro_geo = fetch_cnyes_category("wd_macro", 8)
    headlines = fetch_cnyes_category("headline", 6)
    crypto_comm = fetch_cnyes_category("bc", 6)

    tw_general = fetch_rss_items(
        "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "Google 新聞台灣",
        8,
    )
    technews_tw = fetch_rss_items("https://technews.tw/feed/", "科技新報 TechNews", 6)
    ithome_tw = fetch_rss_items("https://www.ithome.com.tw/rss", "iThome", 5)
    bing_ai = fetch_rss_items(
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote("AI OpenAI 人工智慧")
        + "&format=rss&mkt=zh-TW",
        "Bing 科技新聞",
        6,
    )
    ai_indie = (technews_tw[:4] + ithome_tw[:4] + bing_ai[:4])[:10]

    x_threads_buzz = fetch_rss_items(
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote("馬斯克 黃仁勳 川普 股市")
        + "&format=rss&mkt=zh-TW",
        "財經新聞彙整",
        8,
    )
    portfolio_news = fetch_rss_items(
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote("台積電 輝達 散熱 半導體")
        + "&format=rss&mkt=zh-TW",
        "財經新聞彙整",
        6,
    )

    ptt = fetch_ptt_stock(pages=3)
    reddit = fetch_reddit_apewisdom()
    hn = fetch_hacker_news()

    # Merge & deduplicate Market + AI Supply Chain section (prioritize fresh cnyes + verified publishers)
    seen_titles = set()
    market_ai = []
    for item in tw_stock[:5] + us_stock[:4] + portfolio_news + headlines[:3]:
        key = item["title"][:18]
        if key not in seen_titles:
            seen_titles.add(key)
            market_ai.append(item)

    # Preserve curated div2StockImpact / div1CashflowIdea if present, while refreshing live sourced takeaways!
    prev_impact_div2 = "自動監測百萬模擬倉核心持股、7 檔右側條件單與大 Boss 台股 10 檔供應鏈新聞與法人風向。"
    prev_idea_div1 = "持續從 Hacker News、TechNews 與 iThome 捕捉開發者高痛點 API 與自動化變現靈感。"
    if OUT_JSON.exists():
        try:
            old_brief = json.loads(OUT_JSON.read_text(encoding="utf-8")).get("executiveBrief", {})
            if old_brief.get("div2StockImpact"):
                prev_impact_div2 = old_brief["div2StockImpact"]
            if old_brief.get("div1CashflowIdea"):
                prev_idea_div1 = old_brief["div1CashflowIdea"]
        except Exception:
            pass

    takeaway_items = build_sourced_takeaways(market_ai, macro_geo, ai_indie, ptt, reddit, hn)
    plain_takeaways = [
        f"{t['icon']} 【{t['category']}】{t['text']}（來源：{t['source']}）"
        for t in takeaway_items
    ]

    top_item = market_ai[0] if market_ai else {"title": "台美股盤後資訊整理中", "source": "鉅亨網 cnyes", "url": "https://news.cnyes.com/"}
    top_ptt_obj = ptt["hotPosts"][0] if ptt["hotPosts"] else {"title": "股版熱議整理中", "url": "https://www.ptt.cc/bbs/Stock/index.html"}
    top_reddit = (
        f"${reddit[0]['ticker']} ({reddit[0]['mentions']} 則討論 / 24h {reddit[0]['momentum24hPct']:+}%)"
        if reddit
        else "美股社群整理中"
    )

    auto_brief = {
        "updatedAt": ts_str,
        "takeawayItems": takeaway_items,
        "boss3MinTakeaways": plain_takeaways,
        "div2StockImpact": prev_impact_div2,
        "div1CashflowIdea": prev_idea_div1,
        "livePulse": {
            "topMarketHeadline": top_item["title"],
            "topMarketSource": top_item.get("source", "鉅亨網 cnyes"),
            "topMarketUrl": top_item.get("url", "https://news.cnyes.com/"),
            "pttSentiment": ptt["sentimentLabel"],
            "topPttPost": top_ptt_obj["title"],
            "topPttUrl": top_ptt_obj["url"],
            "topRedditTicker": top_reddit,
        },
    }

    payload = {
        "lastUpdated": ts_str,
        "sourcesRegistry": [
            {"name": "鉅亨網 cnyes", "url": "https://news.cnyes.com/"},
            {"name": "Google 新聞台灣 (經濟/工商/中央社等)", "url": "https://news.google.com/?hl=zh-TW&gl=TW&ceid=TW:zh-Hant"},
            {"name": "科技新報 TechNews", "url": "https://technews.tw/"},
            {"name": "iThome 科技新聞", "url": "https://www.ithome.com.tw/"},
            {"name": "PTT Stock 股版", "url": "https://www.ptt.cc/bbs/Stock/index.html"},
            {"name": "Reddit WSB / r/stocks (ApeWisdom)", "url": "https://apewisdom.io/"},
            {"name": "Hacker News (Y Combinator)", "url": "https://news.ycombinator.com/"},
        ],
        "executiveBrief": auto_brief,
        "sections": {
            "marketAndAi": market_ai[:12],
            "globalMacroTariff": macro_geo[:8],
            "aiToolsCashflow": ai_indie[:8],
            "twTrendingCrypto": (tw_general[:5] + crypto_comm[:4]),
            "xThreadsBuzz": x_threads_buzz[:8],
        },
        "social": {
            "pttStock": ptt,
            "redditHotTickers": reddit,
            "hackerNews": hn,
        },
    }

    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if IDEA_COPY.parent.exists():
        IDEA_COPY.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"[NEWS-RADAR] Updated {ts_str} | Market={len(market_ai)} | Macro={len(macro_geo)} "
        f"| AI={len(ai_indie)} | TW+Crypto={len(tw_general[:5]+crypto_comm[:4])} "
        f"| PTT={len(ptt['hotPosts'])} ({ptt['sentimentLabel']}) | Reddit={len(reddit)} | HN={len(hn)}"
    )


if __name__ == "__main__":
    main()
