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
BOSS_WATCH_PATH = ROOT / "boss_watchlist.json"
OUT_PATH = ROOT / "intraday_pulse.json"
IDEA_COPY = Path("/home/antigravity/idea-team/stock-division/data/intraday_pulse.json")
WARROOM_URL = "https://howardkung.github.io/alpha-stock-warroom/"
TZ_TW = timezone(timedelta(hours=8))


def fetch_intraday_bar(symbol: str) -> dict | None:
    url_1d = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=1d&interval=5m"
    req = urllib.request.Request(url_1d, headers={"User-Agent": "Mozilla/5.0"})
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

        recent_high = max(highs) if highs else float(meta.get("regularMarketDayHigh") or cur_px)
        recent_low = min(lows) if lows else float(meta.get("regularMarketDayLow") or cur_px)
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


TW_FEE_RATE = 0.001425
TW_STOCK_TAX_RATE = 0.003
TW_ETF_TAX_RATE = 0.001
US_FRICTION_RATE = 0.001


def is_market_open_for(market: str, now_tw: datetime) -> tuple[bool, bool]:
    """Returns (is_open, is_preclose_decision_window) in Taiwan Time."""
    wd = now_tw.weekday()
    hm = now_tw.hour * 60 + now_tw.minute
    if market.startswith("TW"):
        is_open = (0 <= wd <= 4) and (540 <= hm <= 810)        # Mon-Fri 09:00~13:30 TWT
        is_preclose = (0 <= wd <= 4) and (780 <= hm <= 810)    # Mon-Fri 13:00~13:30 TWT
        return is_open, is_preclose
    # US market (covers both US DST 21:30~04:00 & Standard 22:30~05:00 TWT, Mon night ~ Sat early morning)
    if (0 <= wd <= 4 and hm >= 1290) or (1 <= wd <= 5 and hm <= 300):
        is_preclose = (1 <= wd <= 5) and (225 <= hm <= 300)    # 03:45~05:00 TWT pre-close
        return True, is_preclose
    return False, False


def calc_sell_proceeds_twd(pos: dict, exit_price: float, fx: float) -> tuple[int, int]:
    gross_twd = pos["shares"] * exit_price * (fx if pos["currency"] == "USD" else 1.0)
    if pos["market"] == "TW":
        fee = gross_twd * (TW_FEE_RATE + TW_STOCK_TAX_RATE)
    elif pos["market"] == "TW_ETF":
        fee = gross_twd * (TW_FEE_RATE + TW_ETF_TAX_RATE)
    else:
        fee = gross_twd * US_FRICTION_RATE
    fee_int = max(1, round(fee))
    return round(gross_twd) - fee_int, fee_int


def calc_buy_cost_twd(market: str, shares: int, price: float, fx: float) -> tuple[int, int]:
    is_usd = market in ("US", "US_ETF")
    gross_twd = shares * price * (fx if is_usd else 1.0)
    fee_rate = TW_FEE_RATE if market.startswith("TW") else US_FRICTION_RATE
    fee_int = max(1, round(gross_twd * fee_rate))
    return round(gross_twd) + fee_int, fee_int


def execute_intraday_paper_trades(
    pf: dict,
    quotes: dict,
    fx: float,
    vix: float,
    twii_px: float,
    now_tw: datetime,
    ts_str: str,
) -> list[str]:
    """
    Live Intraday Order Matching & Mark-to-Market Engine during TW & US open hours:
    1. Limit Take-Profit (`dayHigh >= targetPrice` or `price >= targetPrice`): fills immediately at limit!
    2. Hard Flash-Crash Stop (`price <= stopLoss * 0.975`) OR Pre-Close Confirmed Stop (`is_preclose and price <= stopLoss`): sells live!
    3. Limit Pullback Buy (`dayLow <= pullbackBuyBelow` or `price <= pullbackBuyBelow`): buys immediately at limit!
    4. Pre-Close Confirmed Breakout Buy (`is_preclose and price >= breakoutBuyAbove` + wick < 2.5% + CRO macro filters): buys live!
    """
    today_str = now_tw.strftime("%Y-%m-%d")
    pf["fxUsdTwd"] = fx
    executed_trades: list[str] = []

    # 1. Check open positions (mark-to-market + intraday TP / SL execution)
    remaining_positions = []
    for pos in pf.get("positions", []):
        sym = pos["symbol"]
        q = quotes.get(sym)
        mkt_open, mkt_preclose = is_market_open_for(pos["market"], now_tw)
        px = q["price"] if q else float(pos["currentPrice"])
        day_high = q["dayHigh"] if q else px
        pos["currentPrice"] = px

        stop = float(pos["stopLoss"])
        target = float(pos["targetPrice"])

        # (A) Intraday Limit Take-Profit (if market is open and session high/price touched targetPrice)
        if mkt_open and (px >= target or day_high >= target):
            fill_px = round(max(px, target), 2)
            proceeds, fee = calc_sell_proceeds_twd(pos, fill_px, fx)
            pnl = proceeds - pos["costBasisTwd"]
            pf["summary"]["cashTwd"] += proceeds
            pf["summary"]["realizedPnlTwd"] += pnl
            pf["summary"]["totalFeesPaidTwd"] += fee
            pf["summary"]["winCount"] += 1
            pf.setdefault("closedTrades", []).insert(0, {
                **pos, "exitDate": today_str, "exitPrice": fill_px, "realizedPnlTwd": pnl, "exitReason": "INTRADAY_TARGET_HIT"
            })
            msg = f"🎯【盤中限價達標停利成交】賣出 {sym} {pos['name']} {pos['shares']}股 @ {fill_px}（已實現獲利 NT${pnl:+,}）"
            executed_trades.append(msg)
            continue

        # (B) Intraday Hard Flash-Crash Stop (-2.5% past stop) OR Pre-Close Confirmed Stop-Loss
        hard_crash_stop = round(stop * 0.975, 2)
        if mkt_open and (px <= hard_crash_stop or (mkt_preclose and px <= stop)):
            reason_tag = "盤中閃崩硬停損" if px <= hard_crash_stop else "尾盤確認破線停損"
            proceeds, fee = calc_sell_proceeds_twd(pos, px, fx)
            pnl = proceeds - pos["costBasisTwd"]
            pf["summary"]["cashTwd"] += proceeds
            pf["summary"]["realizedPnlTwd"] += pnl
            pf["summary"]["totalFeesPaidTwd"] += fee
            if pnl >= 0:
                pf["summary"]["winCount"] += 1
            else:
                pf["summary"]["lossCount"] += 1
            pf.setdefault("closedTrades", []).insert(0, {
                **pos, "exitDate": today_str, "exitPrice": px, "realizedPnlTwd": pnl, "exitReason": "INTRADAY_STOP_LOSS"
            })
            msg = f"🛑【{reason_tag}成交】賣出 {sym} {pos['name']} {pos['shares']}股 @ {px}（已實現損益 NT${pnl:+,}）"
            executed_trades.append(msg)
            continue

        mkt_val = round(pos["shares"] * px * (fx if pos["currency"] == "USD" else 1.0))
        pos["marketValueTwd"] = mkt_val
        pos["unrealizedPnlTwd"] = mkt_val - pos["costBasisTwd"]
        pos["unrealizedPnlPct"] = round((pos["unrealizedPnlTwd"] / pos["costBasisTwd"]) * 100, 2)
        remaining_positions.append(pos)

    pf["positions"] = remaining_positions

    # 2. Check pending conditional buy orders during active market hours
    cash_floor = pf.get("cashReserveFloorTwd", 120000)
    still_pending = []
    for po in pf.get("pendingOrders", []):
        sym = po["symbol"]
        q = quotes.get(sym)
        mkt_open, mkt_preclose = is_market_open_for(po["market"], now_tw)
        if not q or not mkt_open:
            still_pending.append(po)
            continue

        px = q["price"]
        day_low = q["dayLow"]
        wick = q["upperWickPct"]
        pull = po.get("pullbackBuyBelow")
        brk = po.get("breakoutBuyAbove")

        # CRO macro filter gates
        macro_ok = True
        if sym == "3711.TW" and fx >= 32.90:
            macro_ok = False
        elif sym == "ANET" and vix >= 25.0:
            macro_ok = False
        elif sym == "6223.TWO" and twii_px < 40200:
            macro_ok = False

        fill_px = None
        fill_label = ""
        if pull and (px <= pull or day_low <= pull) and macro_ok:
            fill_px = round(min(px, float(pull)), 2)
            fill_label = "盤中限價低接成交"
        elif brk and mkt_preclose and px >= float(brk) and wick < 2.5 and macro_ok:
            fill_px = px
            fill_label = "尾盤確認突破買進成交"

        tw_count = sum(1 for p in pf["positions"] if p["market"].startswith("TW"))
        is_tw = po["market"].startswith("TW")

        if fill_px is not None:
            if is_tw and tw_count >= 6:
                still_pending.append(po)
                continue
            cost_twd, fee = calc_buy_cost_twd(po["market"], po["shares"], fill_px, fx)
            if (pf["summary"]["cashTwd"] - cost_twd) >= cash_floor:
                pf["summary"]["cashTwd"] -= cost_twd
                pf["summary"]["totalFeesPaidTwd"] += fee
                mkt_val = cost_twd - fee
                pf["positions"].append({
                    "id": f"POS-{today_str}-{sym}",
                    "symbol": sym,
                    "name": po["name"],
                    "market": po["market"],
                    "category": fill_label,
                    "entryDate": today_str,
                    "shares": po["shares"],
                    "entryPrice": fill_px,
                    "currentPrice": px,
                    "currency": "USD" if po["market"].startswith("US") else "TWD",
                    "fxRateAtEntry": fx if po["market"].startswith("US") else 1.0,
                    "costBasisTwd": cost_twd,
                    "marketValueTwd": mkt_val,
                    "unrealizedPnlTwd": -fee,
                    "unrealizedPnlPct": round((-fee / cost_twd) * 100, 2),
                    "targetPrice": po["targetPrice"],
                    "stopLoss": po["stopLoss"],
                    "riskRewardRatio": round((po["targetPrice"] - fill_px) / max(0.01, fill_px - po["stopLoss"]), 2),
                    "thesis": po["note"],
                    "status": "OPEN",
                })
                executed_trades.append(
                    f"⚡【{fill_label}】買進 {sym} {po['name']} {po['shares']}股 @ {fill_px}（投入 NT${cost_twd:,}）"
                )
            else:
                still_pending.append(po)
        else:
            still_pending.append(po)

    pf["pendingOrders"] = still_pending

    # 3. Live Mark-to-Market NAV & Benchmark Alpha Update
    total_mkt_val = sum(p["marketValueTwd"] for p in pf["positions"])
    total_unrealized = sum(p["unrealizedPnlTwd"] for p in pf["positions"])
    nav = pf["summary"]["cashTwd"] + total_mkt_val
    init_cap = pf["initialCapitalTwd"]
    total_ret_twd = nav - init_cap
    total_ret_pct = round((total_ret_twd / init_cap) * 100, 2)

    bench = pf.get("benchmarks", {})
    base_0050 = float(bench.get("tw0050BasePrice", 92.85))
    init_0050_net = float(bench.get("tw0050InitNetTwd", 998575))
    px_0050 = quotes.get("0050.TW", {}).get("price", base_0050)
    tw0050_nav = round(init_0050_net * (px_0050 / base_0050))
    tw0050_ret = round(((tw0050_nav - init_cap) / init_cap) * 100, 2)
    alpha_pct = round(total_ret_pct - tw0050_ret, 2)

    pf["lastUpdated"] = ts_str
    pf["summary"]["navTwd"] = nav
    pf["summary"]["marketValueTwd"] = total_mkt_val
    pf["summary"]["unrealizedPnlTwd"] = total_unrealized
    pf["summary"]["totalReturnTwd"] = total_ret_twd
    pf["summary"]["totalReturnPct"] = total_ret_pct
    pf["summary"]["investedRatioPct"] = round((total_mkt_val / nav) * 100, 1) if nav else 0.0
    pf["summary"]["cashRatioPct"] = round((pf["summary"]["cashTwd"] / nav) * 100, 1) if nav else 0.0
    pf["summary"]["tw0050NavTwd"] = tw0050_nav
    pf["summary"]["tw0050ReturnPct"] = tw0050_ret
    pf["summary"]["alphaVs0050Pct"] = alpha_pct

    # Log any executed intraday trades into today's dailyReports[0]
    if executed_trades:
        reports = pf.setdefault("dailyReports", [])
        if reports and reports[0].get("date") == today_str:
            existing_acts = [a for a in reports[0].get("actions", []) if "無觸發" not in a]
            reports[0]["actions"] = executed_trades + existing_acts
            reports[0]["navTwd"] = nav
            reports[0]["cashTwd"] = pf["summary"]["cashTwd"]
            reports[0]["totalReturnPct"] = total_ret_pct
        else:
            reports.insert(0, {
                "date": today_str,
                "navTwd": nav,
                "dailyChangeTwd": 0,
                "dailyChangePct": 0.0,
                "totalReturnPct": total_ret_pct,
                "cashTwd": pf["summary"]["cashTwd"],
                "actions": executed_trades,
                "commentary": f"盤中即時撮合成交更新：最新總淨值 NT${nav:,}（Alpha {alpha_pct:+.2f}%）。",
            })

    PORTFOLIO_PATH.write_text(json.dumps(pf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return executed_trades


def detect_session_checkpoint(now_tw: datetime) -> tuple[str, str]:
    hm = now_tw.hour * 60 + now_tw.minute
    if 552 <= hm <= 572:    # ~09:20 TWT milestone
        return "TW_OPEN", "🔔 台股早盤 20m 盯盤＆買賣建議哨"
    if 672 <= hm <= 692:    # ~11:20 TWT milestone
        return "TW_MID", "🕛 台股午盤 20m 盯盤＆買賣建議哨"
    if 792 <= hm <= 815:    # ~13:20 TWT pre-close final decision milestone
        return "TW_PRECLOSE", "⚡ 台股尾盤收盤前決策＆買賣執行哨"
    if 535 <= hm <= 815:    # All other 20-min ticks during TW open hours (09:00, 09:40, 10:00, 10:20, 10:40, 11:00, 11:40, 12:00, 12:20, 12:40, 13:00)
        return "TW_QUIET_SCAN", f"🔎 台股盤中 20m 巡檢撮合 ({now_tw.strftime('%H:%M')})"
    if 1390 <= hm <= 1415:  # ~23:20 TWT US open milestone
        return "US_INTRADAY", "🇺🇸 美股開盤 20m 盯盤＆買賣建議哨"
    if 265 <= hm <= 300:    # ~04:30~05:00 TWT US pre-close window
        return "US_PRECLOSE", "⚡ 美股尾盤收盤前決策＆買賣執行哨"
    if hm >= 1290 or hm <= 300:
        return "US_QUIET_SCAN", f"🔎 美股盤中 20m 巡檢撮合 ({now_tw.strftime('%H:%M')})"
    return "OFF_HOURS", "📡 盤中策略雷達快照"


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


def evaluate_boss_watchlist(boss_items: list[dict], quotes: dict, session_code: str) -> list[dict]:
    out = []
    is_preclose = session_code == "TW_PRECLOSE"
    for item in boss_items:
        sym = item["symbol"]
        q = quotes.get(sym)
        if not q:
            continue
        px = float(q["price"])
        chg = float(q["dayChangePct"])
        ma20 = float(item.get("ma20") or 0.0)
        ma200 = float(item.get("ma200") or 0.0)
        defense = float(item.get("defenseStop") or 0.0)
        resist = float(item.get("resistanceOrTrim") or ma20 or 0.0)

        vs_ma20_pct = round(((px - ma20) / ma20) * 100, 2) if ma20 else 0.0
        vs_ma200_pct = round(((px - ma200) / ma200) * 100, 2) if ma200 else 0.0
        dist_def_pct = round(((px - defense) / px) * 100, 2) if defense and px else 99.0
        dist_res_pct = round(((resist - px) / px) * 100, 2) if resist and px else 99.0

        if defense and px <= defense:
            badge = "🚨 跌破前低防守警戒"
            advice = (
                f"現價 {px} 跌破關鍵防守線 {defense}！嚴禁向下攤平，留意{'13:10 尾盤站不回' if not is_preclose else '收盤前'}汰弱降曝險。"
            )
            priority = 1
        elif ma20 and px >= ma20 and (not resist or px < resist * 0.985 or resist <= ma20 * 1.02):
            if resist and px >= resist and resist > ma20 * 1.03:
                badge = "🎯 站上月線・逼近壓力停利區"
                advice = f"現價 {px} 站穩月線 {ma20}（{vs_ma20_pct:+.2f}%）並接近上檔觀察價 {resist}，可分批停利鎖住獲利。"
                priority = 2
            else:
                badge = "🟢 站上月線強勢續抱"
                advice = f"現價 {px} 強勢站上月線 {ma20}（{vs_ma20_pct:+.2f}%），結構領先群組；上檔觀察 {resist}、防守 {defense}。"
                priority = 2
        elif resist and (px >= resist * 0.975 or dist_res_pct <= 2.5):
            badge = "⚡ 強彈攻抵月線減碼觀察區"
            advice = f"現價 {px}（{chg:+.2f}%）逼近／觸及壓力觀察價 {resist}，若量縮留上影線可逢彈分批減碼降風險。"
            priority = 1
        elif defense and dist_def_pct <= 3.0:
            badge = "🟠 貼近前低支撐・嚴禁攤平"
            advice = f"現價 {px} 距防守價 {defense} 僅 {dist_def_pct}%，暫以觀望為主、切勿提前接刀。"
            priority = 2
        elif chg >= 3.0:
            badge = "📈 跌深強彈・看月線反壓"
            advice = f"盤中強彈 {chg:+.2f}% 至 {px}，但距月線 {ma20} 仍有距離（{vs_ma20_pct:+.2f}%），反彈先看 {resist} 壓力反應。"
            priority = 3
        elif chg <= -2.5:
            badge = "⚠️ 盤中偏弱・緊盯防守價"
            advice = f"盤中走弱 {chg:+.2f}%（{px}），仍壓在月線 {ma20} 下方，緊守 {defense} 不盲目低接。"
            priority = 3
        else:
            badge = "🔵 月線下整理・守前低觀望"
            advice = f"現價 {px}（{chg:+.2f}%），位於月線 {ma20} 下方（{vs_ma20_pct:+.2f}%），守穩 {defense} 續觀察反彈至 {resist} 表現。"
            priority = 4

        out.append({
            "symbol": sym,
            "code": item.get("code", sym.split(".")[0]),
            "name": item["name"],
            "sector": item.get("sector", ""),
            "intradayPrice": px,
            "dayChangePct": chg,
            "ma20": ma20,
            "ma50": float(item.get("ma50") or 0.0),
            "ma200": ma200,
            "vsMa20Pct": vs_ma20_pct,
            "vsMa200Pct": vs_ma200_pct,
            "defenseStop": defense,
            "resistanceOrTrim": resist,
            "badge": badge,
            "advice": advice,
            "priority": priority,
        })

    out.sort(key=lambda x: (x["priority"], -abs(x["dayChangePct"])))
    return out


def extract_boss_actionable_signals(watch_advices: list[dict], boss_advices: list[dict], now_tw: datetime) -> list[dict]:
    """
    Identifies concrete intraday BUY / ADD / TRIM / DEFENSE-STOP recommendations for Đại Boss
    during active TW or US market hours so any new opportunity pushes within 20 minutes!
    """
    signals: list[dict] = []
    tw_open, _ = is_market_open_for("TW", now_tw)
    us_open, _ = is_market_open_for("US", now_tw)

    # 1. New Buy Opportunities for Đại Boss (from CRO-approved Stage 2 & Right-Side Watchlist)
    for w in watch_advices:
        mkt_active = tw_open if w["market"].startswith("TW") else us_open
        if not mkt_active and os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            continue
        if w["priority"] == 1:
            signals.append({
                "key": f"NEWBUY:{w['symbol']}:TRIGGER",
                "type": "🎯 新買進觸價建議",
                "title": f"{w['symbol']} {w['name']} 現價 {w['intradayPrice']} ({w['dayChangePct']:+.2f}%)",
                "detail": w["advice"],
            })
        elif w["priority"] == 2 and w["gapToTriggerPct"] <= 1.8:
            signals.append({
                "key": f"NEWBUY:{w['symbol']}:NEAR",
                "type": "👀 新買進逼近好球帶",
                "title": f"{w['symbol']} {w['name']} 現價 {w['intradayPrice']}（距買點 {w['breakoutBuyAbove']} 僅差 {w['gapToTriggerPct']}%）",
                "detail": w["advice"],
            })

    # 2. Đại Boss's 10 TW Holdings: Intraday Add-on-Pullback, Trim-at-Resistance, or Defense-Stop Alerts
    if tw_open or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        for b in boss_advices:
            px = b["intradayPrice"]
            ma20 = b["ma20"]
            resist = b["resistanceOrTrim"]
            defense = b["defenseStop"]
            # Special leader pullback buy-zone check on Q1 leader (e.g., 8112 至上 holding 0%~+1.8% above rising 20MA)
            if b["code"] == "8112" and ma20 > 0 and ma20 <= px <= round(ma20 * 1.018, 2):
                signals.append({
                    "key": f"BOSS:{b['code']}:PULLBACK_ADD",
                    "type": "🟢 強勢股回測月線買點",
                    "title": f"{b['code']} {b['name']} 現價 {px}（緊貼月線支撐 {ma20}）",
                    "detail": f"全倉唯一多頭領頭羊盤中量縮回測月線 {ma20} 有守，若想換股集中火力可留意低接，防守設 {defense}。",
                })
            elif defense and px <= defense:
                signals.append({
                    "key": f"BOSS:{b['code']}:STOP_BREACH",
                    "type": "🚨 持股跌破防守線建議",
                    "title": f"{b['code']} {b['name']} 現價 {px} <= 防守價 {defense}",
                    "detail": b["advice"],
                })
            elif resist and px >= round(resist * 0.985, 2):
                signals.append({
                    "key": f"BOSS:{b['code']}:TRIM_ZONE",
                    "type": "💰 持股達減碼／停利區",
                    "title": f"{b['code']} {b['name']} 現價 {px} 觸及觀察區 {resist}",
                    "detail": b["advice"],
                })
            elif defense and round(((px - defense) / px) * 100, 2) <= 2.2:
                signals.append({
                    "key": f"BOSS:{b['code']}:NEAR_DEFENSE",
                    "type": "🟠 持股貼近防守底線",
                    "title": f"{b['code']} {b['name']} 現價 {px}（逼近防守死線 {defense}）",
                    "detail": b["advice"],
                })

    return signals


def send_telegram_intraday(pulse: dict, new_boss_signals: list[dict]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("[TG-INTRADAY] Secrets not configured — skipping push.")
        return

    trades = pulse.get("executedTrades") or []
    is_quiet_shift = "QUIET" in pulse.get("sessionCode", "") or pulse.get("sessionCode") == "US_PRECLOSE"
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    # Push immediately on ANY 20-min tick if simulated trades executed OR new Boss buy/sell signals emerged!
    if is_quiet_shift and not trades and not new_boss_signals and not is_manual:
        print("[TG-INTRADAY] 20m scan: 0 new trades & 0 new Boss signals — updated web silently without spamming TG.")
        return

    m = pulse["marketPulse"]
    f_nav = pulse.get("fundLiveSummary", {})
    lines: list[str] = []

    if trades:
        lines.append("🚨 <b>【百萬模擬室・盤中即時撮合成交快報！】</b> 🚨")
        for t in trades:
            lines.append(f"• <b>{html.escape(t)}</b>")
        lines.append("")

    if new_boss_signals:
        lines.append("🎯 <b>【大 Boss 盤中即時買賣操作建議！】</b>")
        for sig in new_boss_signals[:6]:
            lines.append(
                f"• <b>{html.escape(sig['type'])}｜{html.escape(sig['title'])}</b>\n"
                f"  └ {html.escape(sig['detail'])}"
            )
        lines.append("")

    lines.extend([
        f"⚡ <b>【盤中 20m 實戰盯盤戰況】</b> {html.escape(pulse['checkpointLabel'])}",
        f"🕒 <code>{pulse['updatedAt']}</code>",
        f"💼 <b>模擬倉即時淨值</b>：<code>NT${int(f_nav.get('navTwd', 0)):,}</code> (<code>{float(f_nav.get('totalReturnPct', 0)):+.2f}%</code> ｜ 🏆 Alpha <code>{float(f_nav.get('alphaVs0050Pct', 0)):+.2f}%</code>)",
        f"📊 <b>盤中指數</b>：加權 <code>{m['twiiPrice']:,.0f}</code> (<code>{m['twiiChangePct']:+.2f}%</code>) ｜ 0050 <code>{m['tw0050Price']}</code> (<code>{m['tw0050ChangePct']:+.2f}%</code>) ｜ 台幣 <code>{m['usdTwd']}</code> ｜ VIX <code>{m['vix']}</code>",
        f"🧭 <b>盤中策略</b>：{html.escape(pulse['overallStrategy'])}",
    ])

    # On scheduled milestone checks (or if no new_boss_signals block was printed yet), show concise summary
    if not is_quiet_shift or is_manual:
        hot_watch = [w for w in pulse["watchlistAdvices"] if w["priority"] <= 2]
        lines.append("")
        lines.append("<b>🎯 盤中新買進候選雷達（好球帶監測）</b>")
        if hot_watch:
            for w in hot_watch[:3]:
                lines.append(f"• <b>{html.escape(w['symbol'])} {html.escape(w['name'])}</b> <code>{w['intradayPrice']}</code> ➔ {html.escape(w['badge'])}")
        else:
            closest = pulse["watchlistAdvices"][0] if pulse["watchlistAdvices"] else None
            if closest:
                lines.append(
                    f"• 最接近買點：<b>{html.escape(closest['symbol'])} {html.escape(closest['name'])}</b> 現價 <code>{closest['intradayPrice']}</code>（距突破門檻 <code>{closest['breakoutBuyAbove']}</code> 差 <code>{closest['gapToTriggerPct']}%</code>）"
                )

        boss_watch = pulse.get("bossWatchAdvices") or []
        if boss_watch:
            top_boss = sorted(boss_watch, key=lambda x: (x.get("priority", 4), -abs(float(x.get("dayChangePct", 0.0)))))[:5]
            lines.append("")
            lines.append("<b>👑 大 Boss 台股 10 檔盤中動態（Top 5）</b>")
            for b in top_boss:
                lines.append(
                    f"• <b>{html.escape(b['code'])} {html.escape(b['name'])}</b> <code>{b['intradayPrice']}</code> (<code>{b['dayChangePct']:+.2f}%</code>) ➔ <b>{html.escape(b['badge'])}</b> [守{b['defenseStop']} / 壓{b['resistanceOrTrim']}]"
                )

    lines.extend(["", f'🔗 <a href="{WARROOM_URL}?tab=boss">開啟戰情網 ＆ 大 Boss 處方箋 ↗</a>'])
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3840] + "\n...\n" + f'🔗 <a href="{WARROOM_URL}?tab=boss">開啟戰情網 ↗</a>'

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
    today_str = now_tw.strftime("%Y-%m-%d")
    ts_str = now_tw.strftime("%Y-%m-%d %H:%M:%S (台灣時間)")
    session_code, session_label = detect_session_checkpoint(now_tw)

    # Load today's already-alerted signal keys so 20-min loop never spams duplicate alerts
    prev_alerted_keys: set[str] = set()
    if OUT_PATH.exists():
        try:
            prev_pulse = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            at = prev_pulse.get("alertedToday", {})
            if at.get("date") == today_str:
                prev_alerted_keys = set(at.get("keys") or [])
        except Exception:
            pass

    boss_cfg = json.loads(BOSS_WATCH_PATH.read_text(encoding="utf-8")) if BOSS_WATCH_PATH.exists() else {}
    boss_items = boss_cfg.get("items", [])

    symbols = ["^TWII", "0050.TW", "TWD=X", "^VIX"]
    symbols += [p["symbol"] for p in pf.get("positions", [])]
    symbols += [o["symbol"] for o in pf.get("pendingOrders", [])]
    symbols += [b["symbol"] for b in boss_items]

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

    executed_trades = execute_intraday_paper_trades(pf, quotes, fx, vix, twii_px, now_tw, ts_str)
    if Path("/home/antigravity/idea-team/stock-division").exists():
        Path("/home/antigravity/idea-team/stock-division/portfolio.json").write_text(
            json.dumps(pf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    pos_advices = evaluate_positions(pf.get("positions", []), quotes, twii_chg, session_code)
    watch_advices = evaluate_pending_orders(pf.get("pendingOrders", []), quotes, fx, vix, twii_px, session_code)
    boss_advices = evaluate_boss_watchlist(boss_items, quotes, session_code)

    all_boss_signals = extract_boss_actionable_signals(watch_advices, boss_advices, now_tw)
    is_manual_dispatch = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    has_tg_secrets = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() and os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    new_boss_signals = [s for s in all_boss_signals if s["key"] not in prev_alerted_keys]
    if is_manual_dispatch and not new_boss_signals:
        new_boss_signals = list(all_boss_signals)
    updated_alerted_keys = (
        sorted(prev_alerted_keys | {s["key"] for s in all_boss_signals})
        if has_tg_secrets
        else sorted(prev_alerted_keys)
    )

    urgent_stops = sum(1 for x in pos_advices if x["priority"] == 1 and "停損" in x["badge"])
    hot_triggers = sum(1 for x in watch_advices if x["priority"] == 1)

    if executed_trades:
        overall = f"🚨 盤中已即時撮合成交 {len(executed_trades)} 筆指令！（{executed_trades[0]}）"
    elif new_boss_signals:
        overall = f"🎯 盤中浮現 {len(new_boss_signals)} 檔大 Boss 可執行買賣／防守訊號，請查看建議明細！"
    elif urgent_stops > 0:
        overall = f"🚨 盤中有 {urgent_stops} 檔持股觸及停損警戒區！嚴禁攤平，鎖定 13:20 尾盤確認是否執行停損。"
    elif hot_triggers > 0:
        overall = f"⚡ 盤中有 {hot_triggers} 檔新買進候選股進入狙擊區！切忌早盤亂追，尾盤站穩再出手。"
    elif twii_chg <= -1.2:
        overall = f"🛡️ 大盤盤中偏弱 ({twii_chg:+.2f}%)，手握 32% 現金＋美債 TLT 抗跌保護中，多看少動不接刀！"
    elif twii_chg >= 1.0:
        overall = f"🔥 大盤盤中強勢 ({twii_chg:+.2f}%)，強勢股續抱讓獲利奔跑，留意突破好球帶買點！"
    else:
        overall = f"⚖️ 大盤盤中震盪 ({twii_chg:+.2f}%)，每 20 分鐘自動巡檢買點與減碼位，沒進好球帶前安心觀望。"

    pulse = {
        "updatedAt": ts_str,
        "sessionCode": session_code,
        "checkpointLabel": session_label,
        "overallStrategy": overall,
        "executedTrades": executed_trades,
        "bossActionableSignals": all_boss_signals,
        "alertedToday": {
            "date": today_str,
            "keys": updated_alerted_keys,
        },
        "fundLiveSummary": {
            "navTwd": pf["summary"]["navTwd"],
            "totalReturnPct": pf["summary"]["totalReturnPct"],
            "alphaVs0050Pct": pf["summary"].get("alphaVs0050Pct", 0.0),
            "cashTwd": pf["summary"]["cashTwd"],
        },
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
        "bossWatchAdvices": boss_advices,
    }

    OUT_PATH.write_text(json.dumps(pulse, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if IDEA_COPY.parent.exists():
        IDEA_COPY.write_text(json.dumps(pulse, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"[INTRADAY-20M] {session_label} | NAV=NT${pf['summary']['navTwd']:,} "
        f"(Alpha {pf['summary'].get('alphaVs0050Pct', 0):+.2f}%) | Trades={len(executed_trades)} | NewBossSignals={len(new_boss_signals)}"
    )
    send_telegram_intraday(pulse, new_boss_signals)


if __name__ == "__main__":
    main()
