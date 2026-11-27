#!/usr/bin/env python3
"""
Zero-Cost Telegram Bot Push Notification Engine (`send_telegram_alert.py`).
Uses Python standard library only (`urllib.request`, `json`, `os`).
Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` strictly from environment variables.
If either is unset or empty, exits gracefully with code 0 so GitHub Actions stays green.

Modes:
  --mode auto     : 06:00~11:30 Asia/Taipei -> morning brief; otherwise -> close settlement report
  --mode morning  : Force 3-minute Morning Boss Brief
  --mode settle   : Force Paper-Fund Close Settlement & Alpha Benchmark report
"""

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_PATH = ROOT / "portfolio.json"
NEWS_FEED_PATH = ROOT / "news_feed.json"
INTRADAY_PULSE_PATH = ROOT / "intraday_pulse.json"
BOSS_WATCH_PATH = ROOT / "boss_watchlist.json"
WARROOM_URL = "https://howardkung.github.io/alpha-stock-warroom/"
TZ_TW = timezone(timedelta(hours=8))


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_pct(val: float | int | None) -> str:
    v = float(val or 0.0)
    return f"{v:+.2f}%"


def build_morning_message(pf: dict, news: dict, now_tw: datetime) -> str:
    brief = news.get("executiveBrief", {})
    takeaways = brief.get("boss3MinTakeaways", [])[:5]
    social = news.get("social", {})
    ptt = social.get("pttStock", {})
    reddit = social.get("redditHotTickers", [])

    s = pf.get("summary", {})
    nav = int(s.get("navTwd", 1000000))
    ret_pct = float(s.get("totalReturnPct", 0.0))
    alpha = float(s.get("alphaVs0050Pct", 0.0))
    cash = int(s.get("cashTwd", 0))
    cash_pct = float(s.get("cashRatioPct", 0.0))

    lines = [
        f"🌅 <b>【大 Boss 3 分鐘專屬情報早報】</b>",
        f"🕒 <code>{now_tw.strftime('%Y-%m-%d %H:%M')} (台灣時間)</code>",
        "",
        "<b>📌 今日 5 大核心戰情重點</b>",
    ]
    if takeaways:
        for idx, t in enumerate(takeaways, 1):
            lines.append(f"{idx}. {html.escape(str(t))}")
    else:
        lines.append("• 暫無重點摘要，請開啟戰情網檢視即時雷達。")

    lines.append("")
    ptt_label = html.escape(str(ptt.get("sentimentLabel", "情緒監測中")))
    ptt_bull = ptt.get("sentimentBullPct", "--")
    lines.append(f"<b>💬 PTT 股版散戶溫度計</b>：{ptt_label}（偏多 <code>{ptt_bull}%</code>）")

    if reddit:
        top3 = " ｜ ".join(
            f"<b>${html.escape(str(r.get('ticker', '')))}</b> ({r.get('mentions', 0)}則, {float(r.get('momentum24hPct', 0)):+.0f}%)"
            for r in reddit[:3]
        )
        lines.append(f"<b>👽 Reddit 美股熱議</b>：{top3}")

    div2_impact = brief.get("div2StockImpact", "")
    if div2_impact:
        lines.extend(["", f"<b>🎯 二部持股警戒/催化</b>：{html.escape(str(div2_impact))}"])

    lines.extend([
        "",
        f"<b>💼 百萬模擬倉現況</b>：NAV <code>NT${nav:,}</code> (<code>{fmt_pct(ret_pct)}</code>) ｜ 對決 0050 Alpha <code>{fmt_pct(alpha)}</code> ｜ 現金 <code>NT${cash:,}</code> ({cash_pct:.1f}%)",
        f'🔗 <a href="{WARROOM_URL}">一鍵開啟 Alpha 戰情網 ↗</a>',
    ])
    return "\n".join(lines)


def build_settle_message(pf: dict, news: dict, now_tw: datetime) -> str:
    s = pf.get("summary", {})
    reports = pf.get("dailyReports", [])
    latest = reports[0] if reports else {}
    actions = latest.get("actions", [])

    nav = int(s.get("navTwd", 1000000))
    total_ret_twd = int(s.get("totalReturnTwd", 0))
    total_ret_pct = float(s.get("totalReturnPct", 0.0))
    daily_chg_twd = int(latest.get("dailyChangeTwd", 0))
    daily_chg_pct = float(latest.get("dailyChangePct", 0.0))
    tw0050_ret = float(s.get("tw0050ReturnPct", 0.0))
    tw0050_nav = int(s.get("tw0050NavTwd", 1000000))
    sp500_ret = float(s.get("sp500ReturnPct", 0.0))
    alpha = float(s.get("alphaVs0050Pct", total_ret_pct - tw0050_ret))
    cash = int(s.get("cashTwd", 0))
    cash_pct = float(s.get("cashRatioPct", 0.0))
    fx = pf.get("fxUsdTwd", "--")

    # Check if Stop-Loss / Target Take-Profit / Conditional Order Buy triggered
    alert_triggers = [
        a for a in actions
        if "無觸發" not in str(a)
        and any(k in str(a) for k in ("觸發停損", "達標停利", "條件單觸發買進", "⚡", "🛑", "🎯"))
    ]

    lines: list[str] = []
    if alert_triggers:
        lines.append("🚨 <b>【風控 / 條件單即時觸發警報！】</b> 🚨")
        for act in alert_triggers:
            lines.append(f"• <b>{html.escape(str(act))}</b>")
        lines.append("")

    alpha_verdict = "✅ 贏大盤" if alpha >= 0 else "⚠️ 落後大盤"
    lines.extend([
        f"📊 <b>【百萬模擬倉收盤戰報 ＆ 大盤對決】</b>",
        f"🕒 <code>{pf.get('lastUpdated', now_tw.strftime('%Y-%m-%d %H:%M'))}</code> ｜ <code>USD/TWD = {fx}</code>",
        "",
        f"💰 <b>最新總淨值 (NAV)</b>：<code>NT${nav:,}</code>（累計 <code>{fmt_pct(total_ret_pct)}</code> / <code>NT${total_ret_twd:+,}</code>）",
        f"📈 <b>當日結算損益</b>：<code>NT${daily_chg_twd:+,}</code> (<code>{fmt_pct(daily_chg_pct)}</code>)",
        f"🏆 <b>對決無腦買 0050</b>：模擬倉 <code>{fmt_pct(total_ret_pct)}</code> vs 0050 <code>{fmt_pct(tw0050_ret)}</code> (<code>NT${tw0050_nav:,}</code>) ➔ <b>Alpha <code>{fmt_pct(alpha)}</code> {alpha_verdict}</b>",
        f"🇺🇸 <b>美股 S&amp;P 500 基準</b>：<code>{fmt_pct(sp500_ret)}</code>",
        f"💵 <b>機動現金水位</b>：<code>NT${cash:,}</code> (<code>{cash_pct:.1f}%</code>)",
    ])

    if actions and not alert_triggers:
        lines.extend(["", "<b>📝 今日操盤紀律</b>"])
        for act in actions[:3]:
            lines.append(f"• {html.escape(str(act))}")

    brief = news.get("executiveBrief", {})
    live_pulse = brief.get("livePulse", {})
    top_headline = live_pulse.get("topMarketHeadline")
    if not top_headline:
        market_items = news.get("sections", {}).get("marketAndAi", [])
        if market_items:
            top_headline = market_items[0].get("title", "")
    if top_headline:
        lines.extend(["", f"📰 <b>最新市場頭條</b>：{html.escape(str(top_headline))}"])

    pulse = load_json(INTRADAY_PULSE_PATH)
    boss_advices = pulse.get("bossWatchAdvices") or []
    if boss_advices:
        sorted_boss = sorted(boss_advices, key=lambda x: (x.get("priority", 4), -abs(float(x.get("dayChangePct", 0.0)))))
        lines.extend(["", "<b>👑【大 Boss 台股 10 檔收盤強弱快篩】</b>"])
        for b in sorted_boss[:5]:
            lines.append(
                f"• <b>{html.escape(str(b.get('code', '')))} {html.escape(str(b.get('name', '')))}</b> "
                f"<code>{b.get('intradayPrice', '--')}</code> (<code>{float(b.get('dayChangePct', 0.0)):+.2f}%</code>) ➔ "
                f"{html.escape(str(b.get('badge', '')))}"
            )
        if len(sorted_boss) > 5:
            rest = "、".join(
                f"{b.get('code', '')}({float(b.get('dayChangePct', 0.0)):+.1f}%)"
                for b in sorted_boss[5:]
            )
            lines.append(f"• 其餘追蹤：<code>{html.escape(rest)}</code>")
    else:
        boss_cfg = load_json(BOSS_WATCH_PATH)
        boss_items = boss_cfg.get("items", [])
        if boss_items:
            summary_codes = "、".join(f"{x.get('code', '')}{x.get('name', '')}" for x in boss_items[:5])
            lines.extend(["", f"<b>👑【大 Boss 台股 10 檔收盤強弱快篩】</b>", f"• 常態監測中：{html.escape(summary_codes)} 等 10 檔（詳見戰情網專屬頁籤）"])

    lines.extend(["", f'🔗 <a href="{WARROOM_URL}">查看完整持倉與 SVG 績效曲線 ↗</a>'])
    msg = "\n".join(lines)
    if len(msg) > 3750:
        msg = msg[:3700] + "\n...\n" + f'🔗 <a href="{WARROOM_URL}">查看戰情網 ↗</a>'
    return msg


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error: {body}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Send Telegram alert for Alpha Stock Warroom")
    parser.add_argument("--mode", choices=["auto", "morning", "settle"], default="auto")
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[TG] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured yet — skipping push gracefully.")
        return 0

    now_tw = datetime.now(TZ_TW)
    mode = args.mode
    if mode == "auto":
        # Morning brief window: 06:00 ~ 11:30 Taiwan time
        hhmm = now_tw.hour * 60 + now_tw.minute
        mode = "morning" if (360 <= hhmm <= 690) else "settle"

    pf = load_json(PORTFOLIO_PATH)
    news = load_json(NEWS_FEED_PATH)

    msg = build_morning_message(pf, news, now_tw) if mode == "morning" else build_settle_message(pf, news, now_tw)
    try:
        send_telegram_message(token, chat_id, msg)
        print(f"[TG] Sent {mode} notification successfully ({len(msg)} chars).")
        return 0
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        print(f"[TG] HTTP {e.code} error sending Telegram notification: {err_body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[TG] Failed to send Telegram notification: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
