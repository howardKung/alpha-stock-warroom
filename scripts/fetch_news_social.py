#!/usr/bin/env python3
"""
Zero-Cost Multi-Source News & Social Intelligence Engine (`fetch_news_social.py`).
Runs via GitHub Actions ($0 cost, 0 AI quota, zero API keys needed):
1. 鉅亨網 cnyes JSON API: 台股財經、美股科技、國際政經關稅、重大頭條、加密貨幣與原物料
2. Google News TW Top Stories + Bing News zh-TW RSS: 台灣熱門時事、AI 新工具商機、X/Threads 熱議焦點、模擬倉持股新聞
3. PTT Stock 股版 (最新 3 頁): 高推數爆文抓取 + 散戶多空情緒溫度計 (Bull/Bear Sentiment Meter)
4. Reddit 美股社群熱度榜 (ApeWisdom r/wallstreetbets + r/stocks + r/investing): 24h 討論度與飆升排行
5. Hacker News Algolia API: 矽谷科技、AI 工具與獨立變現第一手熱榜
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

BULL_WORDS = ["漲停", "噴", "創高", "大漲", "買超", "利多", "看好", "軋空", "突破", "史高", "大爆買", "翻多", "強彈"]
BEAR_WORDS = ["跌停", "崩", "大跌", "賣超", "利空", "逃命", "腰斬", "套牢", "破底", "跳水", "斷頭", "恐慌", "空頭"]
PORTFOLIO_KEYWORDS = [
    ("3661.TW", ["世芯", "3661"]),
    ("3017.TW", ["奇鋐", "3017"]),
    ("8299.TWO", ["群聯", "8299"]),
    ("0050.TW", ["0050", "台灣50", "台積電", "2330"]),
    ("META", ["Meta", "META", "祖克柏"]),
    ("TLT", ["美債", "殖利率", "聯準會", "Fed", "降息"]),
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
                "source": "鉅亨網",
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
            link = (item.findtext("link") or "").strip()
            desc = clean_html(item.findtext("description") or "")[:130]
            pub = (item.findtext("pubDate") or "")[:22]
            src_el = item.find("source")
            src = clean_html(src_el.text) if (src_el is not None and src_el.text) else default_source
            if " - " in raw_title and src == default_source:
                parts = raw_title.rsplit(" - ", 1)
                raw_title, src = parts[0].strip(), parts[1].strip()
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
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={oid}"
            out.append({
                "title": title,
                "points": int(hit.get("points") or 0),
                "comments": int(hit.get("num_comments") or 0),
                "url": url,
                "hnCommentsUrl": f"https://news.ycombinator.com/item?id={oid}",
            })
        return out
    except Exception:
        return []


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
        "台灣熱門焦點",
        8,
    )
    technews_tw = fetch_rss_items("https://technews.tw/feed/", "科技新報 TechNews", 6)
    ithome_tw = fetch_rss_items("https://www.ithome.com.tw/rss", "iThome 科技", 5)
    bing_ai = fetch_rss_items(
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote("AI OpenAI 人工智慧")
        + "&format=rss&mkt=zh-TW",
        "AI 科技商機",
        6,
    )
    ai_indie = (bing_ai[:4] + technews_tw[:4] + ithome_tw[:4])[:10]

    x_threads_buzz = fetch_rss_items(
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote("馬斯克 黃仁勳 川普 股市")
        + "&format=rss&mkt=zh-TW",
        "X / Threads 社群熱議",
        8,
    )
    portfolio_news = fetch_rss_items(
        "https://www.bing.com/news/search?q="
        + urllib.parse.quote("台積電 輝達 散熱 半導體")
        + "&format=rss&mkt=zh-TW",
        "AI 供應鏈追蹤",
        6,
    )

    ptt = fetch_ptt_stock(pages=3)
    reddit = fetch_reddit_apewisdom()
    hn = fetch_hacker_news()

    # Merge & deduplicate Market + AI Supply Chain section
    seen_titles = set()
    market_ai = []
    for item in portfolio_news + tw_stock[:5] + us_stock[:5] + headlines[:3]:
        key = item["title"][:18]
        if key not in seen_titles:
            seen_titles.add(key)
            market_ai.append(item)

    # Preserve existing executiveBrief if curated today, while updating live stats
    existing_brief = None
    if OUT_JSON.exists():
        try:
            old = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            existing_brief = old.get("executiveBrief")
        except Exception:
            pass

    top_tw_headline = (market_ai[0]["title"] if market_ai else "台美股盤後資訊整理中")
    top_ptt = (ptt["hotPosts"][0]["title"] if ptt["hotPosts"] else "股版熱議整理中")
    top_reddit = (
        f"${reddit[0]['ticker']} ({reddit[0]['mentions']} 則討論 / 24h {reddit[0]['momentum24hPct']:+}%)"
        if reddit
        else "美股社群整理中"
    )

    auto_brief = existing_brief or {
        "updatedAt": ts_str,
        "boss3MinTakeaways": [
            f"📈 台美股與持股焦點：{top_tw_headline}",
            f"💬 PTT 股版情緒雷達：{ptt['sentimentLabel']}（最熱爆文：{top_ptt}）",
            f"👽 Reddit 美股散戶熱榜冠軍：{top_reddit}",
        ],
        "div2StockImpact": "自動監測百萬模擬倉 6 檔核心持股與 7 檔右側條件單相關供應鏈新聞與法人風向。",
        "div1CashflowIdea": "持續從 Hacker News 與 AI 工具新聞捕捉開發者高痛點 API 與自動化變現靈感。",
    }
    auto_brief["livePulse"] = {
        "topMarketHeadline": top_tw_headline,
        "pttSentiment": ptt["sentimentLabel"],
        "topPttPost": top_ptt,
        "topRedditTicker": top_reddit,
    }

    payload = {
        "lastUpdated": ts_str,
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
