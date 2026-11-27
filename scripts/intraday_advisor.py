#!/usr/bin/env python3
"""
Alpha Stock Division — Intraday Sniper & Tactical Action Advisor (`intraday_advisor.py`).
Runs at key intraday checkpoints ($0 cost, 0 AI quota):
- Taiwan Intraday: 09:35 (Opening Pulse), 11:30 (Midday Check), 13:10 (Pre-Close Final Decision Window!)
- US Intraday    : 23:30 Taiwan Time (1 hour after US open)

Outputs:
1. `intraday_pulse.json` -> Live intraday tactical panel on `alpha-stock-warroom` webpage
2. Direct Telegram push (`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` if configured) with actionable
   hold / trim / stop-loss watch / pre-close buy confirmation advice!
"""

import html
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_PATH = ROOT / "portfolio.json"
OUT_PATH = ROOT / "intraday_pulse.json"
IDEA_COPY = Path("/home/antigravity/idea-team/stock-division/data/intraday_pulse.json")
WARROOM_URL = "https://howardkung.github.io/alpha-stock-warroom/"
TZ_TW = timezone(timedelta(hours=8))


def fetch_intraday_bar(symbol: str) -> dict | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=5m"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        res = d["chart"]["result"][0]
        meta = res.get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")

        q = res.get("indicators", {}).get("quote", [{}])[0]
        highs = [x for x in (q.get("high") or []) if x is not None]
        lows = [x for x in (q.get("low") or []) if x is not None]
        closes = [x for x in (q.get("close") or []) if x is not None]

        cur_px = float(price if price is not None else (closes[-1] if closes else 0))
        if cur_px <= 0:
            return None

        # Today's recent session high/low (last ~60 5-min bars)
        recent_high = max(highs[-55:]) if highs else cur_px
        recent_low = min(lows[-55:]) if lows else cur_px
        prev = float(prev_close) if prev_close else cur_px
        chg_pct = round(((cur_px - prev) / prev) * 100, 2) if prev else 0.0
        upper_wick_pct = round(((recent_high - cur_px) / cur_px) * 100, 2) if recent_high > cur_px else 0.0

        return {
            "symbol": symbol,
            "price": round(cur_px, 2),
            "prevClose": round(prev, 2),
            "dayHigh": round(float(recent_high), 2),
            "dayLow": round(float(recent_low), 2),
            "dayChangePct": chg_pct,
            "upperWickPct": upper_wick_pct,
        }
    except Exception:
        return None


def detect_session_checkpoint(now_tw: datetime) -> tuple[str, str]:
    hm = now_tw.hour * 60 + now_tw.minute
    if 535 <= hm <= 630:  # 08:55 ~ 10:30
        return "TW_OPEN", "🔔 台股早盤強弱定調哨（防早盤假突破）"
    if 631 <= hm <= 760:  # 10:31 ~ 12:40
        return "TW_MID", "🕛 台股午盤換手與上影線檢視哨"
    if 761 <= hm <= 820:  # 12:41 ~ 13:40
        return "TW_PRECLOSE", "⚡ 台股尾盤 20 分鐘黃金決策哨（確認買進／停損！）"
    if hm >= 1320 or hm <= 280:  # 22:00 ~ 04:40
        return "US_INTRADAY", "🇺🇸 美股盤中實戰盯盤哨"
    return "OFF_HOURS", "📡 盤中策略雷達快照（非開盤時段預覽）"


def evaluate_positions(positions: list[dict], quotes: dict, twii_chg: float, session_code: str) -> list[dict]:
    out = []
    is_preclose = session_code == "TW_PRECLOSE"
    for p in positions:
        sym = p["symbol"]
        q = quotes.get(sym)
        px = q["price"] if q else float(p["currentPrice"])
        chg = q["dayChangePct"] if q else 0.0
        wick = q["upperWickPct"] if q else 0.0
        stop = float(p["stopLoss"])
        target = float(p["targetPrice"])

        dist_stop_pct = round(((px - stop) / px) * 100, 2)
        dist_target_pct = round(((target - px) / px) * 100, 2)
        pnl_from_entry_pct = round(((px - float(p["entryPrice"])) / float(p["entryPrice"])) * 100, 2)

        if px <= stop:
            if is_preclose and p["market"].startswith("TW"):
                badge = "🛑 尾盤破線・建議停損"
                advice = f"現價 {px} 於尾盤決策窗仍低於停損防線 {stop}，建議收盤前紀律執行停損出場，保護百萬本金！"
                priority = 1
            else:
                badge = "🚨 盤中破停損警戒"
                advice = f"盤中暫破停損線 {stop}（現價 {px}）！先別被盤中洗盤嚇跑，緊盯 13:10 尾盤若站不回 {stop} 再砍倉。"
                priority = 1
        elif dist_stop_pct <= 2.2:
            badge = "🟠 逼近防守線"
            advice = f"距停損價 {stop} 僅剩 {dist_stop_pct}%，盤中嚴禁逢低攤平加碼，觀察尾盤低檔承接力道。"
            priority = 2
        elif px >= target or dist_target_pct <= 2.0:
            badge = "🎯 進入分批停利區"
            advice = f"現價 {px} 已達／逼近目標價 {target}（僅差 {max(0.0, dist_target_pct)}%），盤中急拉可先分批停利 1/3～1/2！"
            priority = 1
        elif wick >= 2.8 and chg > 0:
            badge = "⚠️ 衝高留上影線"
            advice = f"盤中最高衝至 {q['dayHigh']} 後拉回留 {wick}% 上影線，短線有當沖賣壓，續抱原倉但切勿盤中追高。"
            priority = 3
        elif chg >= twii_chg:
            badge = "🟢 強於大盤・安心續抱"
            advice = f"盤中 {chg:+.2f}% 表現優於／穩於大盤，多方結構穩健，續抱讓獲利奔跑（防守線守 {stop}）。"
            priority = 4
        else:
            badge = "🔵 正常震盪・紀律續抱"
            advice = f"盤中 {chg:+.2f}% 溫和整理，距停損 {stop} 尚有 {dist_stop_pct}% 安全緩衝，維持原計畫續抱。"
            priority = 4

        out.append({
            "symbol": sym,
            "name": p["name"],
            "market": p["market"],
            "entryPrice": p["entryPrice"],
            "intradayPrice": px,
            "dayChangePct": chg,
            "pnlFromEntryPct": pnl_from_entry_pct,
            "stopLoss": stop,
            "targetPrice": target,
            "distToStopPct": dist_stop_pct,
            "badge": badge,
            "advice": advice,
            "priority": priority,
        })
    out.sort(key=lambda x: x["priority"])
    return out


def evaluate_pending_orders(pending: list[dict], quotes: dict, fx: float, vix: float, twii_px: float, session_code: str) -> list[dict]:
    out = []
    is_preclose = session_code == "TW_PRECLOSE"
    for o in pending:
        sym = o["symbol"]
        q = quotes.get(sym)
        if not q:
            continue
        px = q["price"]
        chg = q["dayChangePct"]
        wick = q["upperWickPct"]
        brk = o.get("breakoutBuyAbove")
        pull = o.get("pullbackBuyBelow")

        # Extra CRO macro filter check
        macro_warn = ""
        if sym == "3711.TW" and fx >= 32.90:
            macro_warn = f"（⚠️ 注意台幣目前 {fx} 尚未升破 <32.90 濾網）"
        elif sym == "ANET" and vix >= 25.0:
            macro_warn = f"（⚠️ 注意 VIX 目前 {vix} 尚未降至 <25 濾網）"
        elif sym == "6223.TWO" and twii_px < 40200:
            macro_warn = f"（⚠️ 注意大盤 {twii_px:,.0f} 尚未站上 40,200 濾網）"

        if brk and px >= brk:
            if is_preclose and o["market"].startswith("TW") and wick < 2.5 and not macro_warn:
                badge = "🔥 尾盤確認突破・建議買進！"
                advice = f"尾盤穩站觸發價 {brk} 之上（現價 {px}，當日 {chg:+.2f}%），符合右側進場條件，建議依預算買進 {o['shares']} 股（停損設 {o['stopLoss']}）！"
                priority = 1
            else:
                badge = "⚡ 盤中越過觸發價（等尾盤確認）"
                advice = f"現價 {px} 已越過 {brk}！依防假突破紀律：切勿早盤衝動追價，請於 13:10 尾盤確認未留長上影線且站穩 {brk} 再進場{macro_warn}。"
                priority = 1
            gap_pct = 0.0
        elif pull and px <= pull:
            badge = "🎯 進入低接狙擊區"
            advice = f"現價 {px} 已回落至右側低接區 <= {pull}，觀察盤中縮量止跌訊號，可分批承接 {o['shares']} 股（嚴守停損 {o['stopLoss']}）。"
            priority = 1
            gap_pct = 0.0
        else:
            gap_pct = round(((brk - px) / px) * 100, 2) if brk else 99.0
            if gap_pct <= 2.8:
                badge = "👀 兵臨城下・重點盯盤"
                advice = f"現價 {px}（{chg:+.2f}%）距右側突破價 {brk} 僅差 {gap_pct}%！隨時可能點火攻關，列入今日頭號觀察名單{macro_warn}。"
                priority = 2
            else:
                badge = "⏳ 尚未進射程・觀望"
                advice = f"現價 {px}（{chg:+.2f}%）距突破門檻 {brk} 尚差 {gap_pct}%，靜待右側轉強，盤中不摸底。"
                priority = 4

        out.append({
            "symbol": sym,
            "name": o["name"],
            "market": o["market"],
            "intradayPrice": px,
            "dayChangePct": chg,
            "breakoutBuyAbove": brk,
            "pullbackBuyBelow": pull,
            "gapToTriggerPct": gap_pct,
            "shares": o["shares"],
            "stopLoss": o["stopLoss"],
            "targetPrice": o["targetPrice"],
            "badge": badge,
            "advice": advice,
            "priority": priority,
        })
    out.sort(key=lambda x: (x["priority"], x["gapToTriggerPct"]))
    return out


def send_telegram_intraday(pulse: dict) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[TG-INTRADAY] Secrets not configured — skipping push.")
        return

    m = pulse["marketPulse"]
    lines = [
        f"⚡ <b>【二部盤中實戰盯盤建議】</b>",
        f"<b>{html.escape(pulse['checkpointLabel'])}</b>",
        f"🕒 <code>{pulse['updatedAt']}</code>",
        "",
        f"📊 <b>盤中大盤溫度</b>：加權 <code>{m['twiiPrice']:,.0f}</code> (<code>{m['twiiChangePct']:+.2f}%</code>) ｜ 0050 <code>{m['tw0050Price']}</code> (<code>{m['tw0050ChangePct']:+.2f}%</code>) ｜ 台幣 <code>{m['usdTwd']}</code> ｜ VIX <code>{m['vix']}</code>",
        f"🧭 <b>盤中總體策略</b>：{html.escape(pulse['overallStrategy'])}",
        "",
        "<b>💼 現有 6 檔持股盤中操作建議</b>",
    ]

    for p in pulse["positionAdvices"]:
        lines.append(
            f"• <b>{html.escape(p['symbol'])} {html.escape(p['name'])}</b> <code>{p['intradayPrice']}</code> (<code>{p['dayChangePct']:+.2f}%</code>) ➔ <b>{html.escape(p['badge'])}</b>\n"
            f"  └ {html.escape(p['advice'])}"
        )

    hot_watch = [w for w in pulse["watchlistAdvices"] if w["priority"] <= 2]
    lines.append("")
    lines.append("<b>🎯 右側條件單盤中狙擊雷達</b>")
    if hot_watch:
        for w in hot_watch[:4]:
            lines.append(
                f"• <b>{html.escape(w['symbol'])} {html.escape(w['name'])}</b> <code>{w['intradayPrice']}</code> ➔ <b>{html.escape(w['badge'])}</b>\n"
                f"  └ {html.escape(w['advice'])}"
            )
    else:
        closest = pulse["watchlistAdvices"][0] if pulse["watchlistAdvices"] else None
        if closest:
            lines.append(
                f"• 目前 7 檔條件單皆未進入 2.8% 突破射程（最接近：<b>{html.escape(closest['symbol'])} {html.escape(closest['name'])}</b> 現價 <code>{closest['intradayPrice']}</code>，距門檻 <code>{closest['breakoutBuyAbove']}</code> 差 <code>{closest['gapToTriggerPct']}%</code>）➔ <b>盤中安心觀望不追價</b>。"
            )

    lines.extend(["", f'🔗 <a href="{WARROOM_URL}">開啟戰情網查看即時盤中雷達 ↗</a>'])
    text = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if not res.get("ok"):
            raise RuntimeError(f"Telegram error: {res}")
    print(f"[TG-INTRADAY] Sent intraday advice push ({len(text)} chars).")


def main() -> None:
    pf = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    now_tw = datetime.now(TZ_TW)
    ts_str = now_tw.strftime("%Y-%m-%d %H:%M:%S (台灣時間)")
    session_code, session_label = detect_session_checkpoint(now_tw)

    symbols = ["^TWII", "0050.TW", "TWD=X", "^VIX"]
    symbols += [p["symbol"] for p in pf.get("positions", [])]
    symbols += [o["symbol"] for o in pf.get("pendingOrders", [])]

    quotes = {}
    for sym in dict.fromkeys(symbols):
        bar = fetch_intraday_bar(sym)
        if bar:
            quotes[sym] = bar

    twii_px = quotes.get("^TWII", {}).get("price", 39151.81)
    twii_chg = quotes.get("^TWII", {}).get("dayChangePct", 0.0)
    tw0050_px = quotes.get("0050.TW", {}).get("price", 92.85)
    tw0050_chg = quotes.get("0050.TW", {}).get("dayChangePct", 0.0)
    fx = quotes.get("TWD=X", {}).get("price", pf.get("fxUsdTwd", 32.94))
    vix = quotes.get("^VIX", {}).get("price", 27.5)

    pos_advices = evaluate_positions(pf.get("positions", []), quotes, twii_chg, session_code)
    watch_advices = evaluate_pending_orders(pf.get("pendingOrders", []), quotes, fx, vix, twii_px, session_code)

    urgent_stops = sum(1 for x in pos_advices if x["priority"] == 1 and "停損" in x["badge"])
    hot_triggers = sum(1 for x in watch_advices if x["priority"] == 1)

    if urgent_stops > 0:
        overall = f"🚨 盤中有 {urgent_stops} 檔持股觸及停損警戒區！嚴禁攤平，請鎖定 13:10 尾盤確認是否執行停損。"
    elif hot_triggers > 0:
        overall = f"⚡ 盤中有 {hot_triggers} 檔觀察股進入狙擊觸發區！切忌早盤追高，等 13:10 尾盤站穩再扣扳機。"
    elif twii_chg <= -1.2:
        overall = f"🛡️ 大盤盤中偏弱 ({twii_chg:+.2f}%)，手握 32% 現金＋美債 TLT 發揮抗跌保護力，多看少動、不接刀！"
    elif twii_chg >= 1.0:
        overall = f"🔥 大盤盤中強勢 ({twii_chg:+.2f}%)，現有強勢持股續抱讓獲利奔跑，留意兵臨城下之條件單尾盤突破機會！"
    else:
        overall = f"⚖️ 大盤盤中震盪換手 ({twii_chg:+.2f}%)，6 檔持股未破防守線前安心續抱，條件單嚴守右側突破才出手。"

    pulse = {
        "updatedAt": ts_str,
        "sessionCode": session_code,
        "checkpointLabel": session_label,
        "overallStrategy": overall,
        "marketPulse": {
            "twiiPrice": twii_px,
            "twiiChangePct": twii_chg,
            "tw0050Price": tw0050_px,
            "tw0050ChangePct": tw0050_chg,
            "usdTwd": fx,
            "vix": vix,
        },
        "positionAdvices": pos_advices,
        "watchlistAdvices": watch_advices,
    }

    OUT_PATH.write_text(json.dumps(pulse, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if IDEA_COPY.parent.exists():
        IDEA_COPY.write_text(json.dumps(pulse, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[INTRADAY] {session_label} | TWII={twii_px} ({twii_chg:+.2f}%) | UrgentStops={urgent_stops} | HotTriggers={hot_triggers}")
    send_telegram_intraday(pulse)


if __name__ == "__main__":
    main()
