#!/usr/bin/env python3
"""
Daily Automated NAV Settlement & Rule Execution Engine for Alpha Stock Division's
NT$1,000,000 Paper-Trading Desk.
Runs via GitHub Actions every weekday ($0 cost, 0 AI quota needed):
1. Fetches real closing prices & USD/TWD FX from Yahoo Finance.
2. Enforces automatic Stop-Loss and Target Take-Profit rules (with realistic TW/US fees & taxes).
3. Triggers pending right-side conditional buy orders when price conditions are met & cash allows.
4. Updates portfolio.json and generates daily-reports/YYYY-MM-DD.md.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_PATH = ROOT / "portfolio.json"
DAILY_DIR = ROOT / "daily-reports"

TW_FEE_RATE = 0.001425
TW_STOCK_TAX_RATE = 0.003
TW_ETF_TAX_RATE = 0.001
US_FRICTION_RATE = 0.001


def fetch_latest_price(symbol: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range=5d&interval=1d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode())
        closes = [c for c in d["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c is not None]
        return round(float(closes[-1]), 2) if closes else None
    except Exception:
        return None


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


def main() -> None:
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    pf = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    tz_tw = timezone(timedelta(hours=8))
    now_tw = datetime.now(tz_tw)
    today_str = now_tw.strftime("%Y-%m-%d")
    ts_str = now_tw.strftime("%Y-%m-%d %H:%M:%S")

    fx = fetch_latest_price("TWD=X") or pf.get("fxUsdTwd", 32.94)
    pf["fxUsdTwd"] = fx
    prev_nav = pf["summary"]["navTwd"]
    actions = []

    # 1. Update open positions & check Stop-Loss / Target Take-Profit
    remaining_positions = []
    for pos in pf["positions"]:
        px = fetch_latest_price(pos["symbol"])
        if px is not None:
            pos["currentPrice"] = px
        else:
            px = pos["currentPrice"]

        # Check Stop-Loss
        if px <= pos["stopLoss"]:
            proceeds, fee = calc_sell_proceeds_twd(pos, px, fx)
            pnl = proceeds - pos["costBasisTwd"]
            pf["summary"]["cashTwd"] += proceeds
            pf["summary"]["realizedPnlTwd"] += pnl
            pf["summary"]["totalFeesPaidTwd"] += fee
            if pnl >= 0:
                pf["summary"]["winCount"] += 1
            else:
                pf["summary"]["lossCount"] += 1
            closed = {**pos, "exitDate": today_str, "exitPrice": px, "realizedPnlTwd": pnl, "exitReason": "STOP_LOSS"}
            pf["closedTrades"].insert(0, closed)
            actions.append(f"🛑【觸發停損出場】賣出 {pos['symbol']} {pos['name']} {pos['shares']}股 @ {px}（已實現損益 NT${pnl:+,}）")
            continue

        # Check Target Take-Profit
        if px >= pos["targetPrice"]:
            proceeds, fee = calc_sell_proceeds_twd(pos, px, fx)
            pnl = proceeds - pos["costBasisTwd"]
            pf["summary"]["cashTwd"] += proceeds
            pf["summary"]["realizedPnlTwd"] += pnl
            pf["summary"]["totalFeesPaidTwd"] += fee
            pf["summary"]["winCount"] += 1
            closed = {**pos, "exitDate": today_str, "exitPrice": px, "realizedPnlTwd": pnl, "exitReason": "TARGET_HIT"}
            pf["closedTrades"].insert(0, closed)
            actions.append(f"🎯【達標停利出場】賣出 {pos['symbol']} {pos['name']} {pos['shares']}股 @ {px}（已實現獲利 NT${pnl:+,}）")
            continue

        mkt_val = round(pos["shares"] * px * (fx if pos["currency"] == "USD" else 1.0))
        pos["marketValueTwd"] = mkt_val
        pos["unrealizedPnlTwd"] = mkt_val - pos["costBasisTwd"]
        pos["unrealizedPnlPct"] = round((pos["unrealizedPnlTwd"] / pos["costBasisTwd"]) * 100, 2)
        remaining_positions.append(pos)

    pf["positions"] = remaining_positions

    # 2. Check pending conditional orders (with CRO NT$120,000 cash floor & TW AI correlation cap)
    cash_floor = pf.get("cashReserveFloorTwd", 120000)
    still_pending = []
    for po in pf.get("pendingOrders", []):
        px = fetch_latest_price(po["symbol"])
        if px is None:
            still_pending.append(po)
            continue
        triggered = False
        if po["ruleType"] == "PULLBACK_OR_BREAKOUT":
            triggered = (px <= po.get("pullbackBuyBelow", -1)) or (px >= po.get("breakoutBuyAbove", 1e12))
        elif po["ruleType"] == "BREAKOUT_ABOVE":
            triggered = px >= po.get("breakoutBuyAbove", 1e12)

        tw_count = sum(1 for p in pf["positions"] if p["market"].startswith("TW"))
        is_tw = po["market"].startswith("TW")

        if triggered:
            cost_twd, fee = calc_buy_cost_twd(po["market"], po["shares"], px, fx)
            if is_tw and tw_count >= 6:
                still_pending.append(po)
                continue
            if (pf["summary"]["cashTwd"] - cost_twd) >= cash_floor:
                pf["summary"]["cashTwd"] -= cost_twd
                pf["summary"]["totalFeesPaidTwd"] += fee
                mkt_val = cost_twd - fee
                new_pos = {
                    "id": f"POS-{today_str}-{po['symbol']}",
                    "symbol": po["symbol"],
                    "name": po["name"],
                    "market": po["market"],
                    "category": "條件單右側觸發",
                    "entryDate": today_str,
                    "shares": po["shares"],
                    "entryPrice": px,
                    "currentPrice": px,
                    "currency": "USD" if po["market"].startswith("US") else "TWD",
                    "fxRateAtEntry": fx if po["market"].startswith("US") else 1.0,
                    "costBasisTwd": cost_twd,
                    "marketValueTwd": mkt_val,
                    "unrealizedPnlTwd": -fee,
                    "unrealizedPnlPct": round((-fee / cost_twd) * 100, 2),
                    "targetPrice": po["targetPrice"],
                    "expectedReturnPct": round(((po["targetPrice"] / px) - 1) * 100, 2),
                    "stopLoss": po["stopLoss"],
                    "riskRewardRatio": round((po["targetPrice"] - px) / max(0.01, px - po["stopLoss"]), 2),
                    "thesis": po["note"],
                    "status": "OPEN",
                }
                pf["positions"].append(new_pos)
                actions.append(f"⚡【條件單觸發買進】{po['symbol']} {po['name']} {po['shares']}股 @ {px}（投入 NT${cost_twd:,}）")
            else:
                still_pending.append(po)
        else:
            still_pending.append(po)
    pf["pendingOrders"] = still_pending

    # 3. Recompute Fund NAV Summary
    total_mkt_val = sum(p["marketValueTwd"] for p in pf["positions"])
    total_unrealized = sum(p["unrealizedPnlTwd"] for p in pf["positions"])
    nav = pf["summary"]["cashTwd"] + total_mkt_val
    init_cap = pf["initialCapitalTwd"]
    total_ret_twd = nav - init_cap
    total_ret_pct = round((total_ret_twd / init_cap) * 100, 2)
    daily_chg_twd = nav - prev_nav
    daily_chg_pct = round((daily_chg_twd / prev_nav) * 100, 2) if prev_nav else 0.0

    pf["lastUpdated"] = ts_str
    pf["summary"]["navTwd"] = nav
    pf["summary"]["marketValueTwd"] = total_mkt_val
    pf["summary"]["unrealizedPnlTwd"] = total_unrealized
    pf["summary"]["totalReturnTwd"] = total_ret_twd
    pf["summary"]["totalReturnPct"] = total_ret_pct
    pf["summary"]["investedRatioPct"] = round((total_mkt_val / nav) * 100, 1) if nav else 0.0
    pf["summary"]["cashRatioPct"] = round((pf["summary"]["cashTwd"] / nav) * 100, 1) if nav else 0.0

    if not actions:
        actions.append("今日無觸發停損/停利或新條件單，現有 6 檔核心與對沖部位續抱追蹤中。")

    # Update or append today's equityCurve & dailyReport
    curve_label = f"{today_str} (收盤)"
    existing_curve = next((c for c in pf["equityCurve"] if c["date"] == curve_label), None)
    if existing_curve:
        existing_curve.update({"navTwd": nav, "cashTwd": pf["summary"]["cashTwd"], "returnPct": total_ret_pct})
    else:
        pf["equityCurve"].append({
            "date": curve_label,
            "navTwd": nav,
            "cashTwd": pf["summary"]["cashTwd"],
            "returnPct": total_ret_pct,
            "note": actions[0],
        })

    report_entry = {
        "date": today_str,
        "navTwd": nav,
        "dailyChangeTwd": daily_chg_twd,
        "dailyChangePct": daily_chg_pct,
        "totalReturnPct": total_ret_pct,
        "cashTwd": pf["summary"]["cashTwd"],
        "actions": actions,
        "commentary": f"自動收盤結算完成（USD/TWD={fx}）：總淨值 NT${nav:,}（累計報酬 {total_ret_pct:+.2f}%），持倉市值 NT${total_mkt_val:,}，現金儲備 NT${pf['summary']['cashTwd']:,}。",
    }
    if pf["dailyReports"] and pf["dailyReports"][0]["date"] == today_str and today_str != pf["inceptionDate"]:
        pf["dailyReports"][0] = report_entry
    elif not pf["dailyReports"] or pf["dailyReports"][0]["date"] != today_str:
        pf["dailyReports"].insert(0, report_entry)

    PORTFOLIO_PATH.write_text(json.dumps(pf, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Write Daily Markdown Report
    md_lines = [
        f"# 百萬實盤模擬基金 · 每日投資結算報告（{today_str}）",
        f"- **結算時間**：`{ts_str}`（Asia/Taipei）",
        f"- **初始本金**：`NT$1,000,000` ｜ **今日總淨值（NAV）**：**`NT${nav:,}`**（累計 `{total_ret_pct:+.2f}%` / `NT${total_ret_twd:+,}`）",
        f"- **持倉總市值**：`NT${total_mkt_val:,}`（`{pf['summary']['investedRatioPct']}%`） ｜ **機動現金**：`NT${pf['summary']['cashTwd']:,}`（`{pf['summary']['cashRatioPct']}%`）",
        f"- **匯率基準**：`USD/TWD = {fx}`",
        "",
        "## 一、今日交易與風控執行紀錄",
        *[f"- {a}" for a in actions],
        "",
        "## 二、當前持倉明細（預期結果 vs 實際結果對照）",
        "| 代號 | 名稱 | 股數 | 進場價 | 最新價 | 預期目標價 | 強制停損價 | 市值 (TWD) | 未實現損益 |",
        "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        *[
            f"| `{p['symbol']}` | {p['name']} | {p['shares']} | {p['entryPrice']} | **{p['currentPrice']}** | {p['targetPrice']} (+{p['expectedReturnPct']}%) | {p['stopLoss']} | NT${p['marketValueTwd']:,} | NT${p['unrealizedPnlTwd']:+,} ({p['unrealizedPnlPct']:+.2f}%) |"
            for p in pf["positions"]
        ],
    ]
    (DAILY_DIR / f"{today_str}.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(f"[SETTLED] {today_str} | NAV=NT${nav:,} ({total_ret_pct:+.2f}%)")


if __name__ == "__main__":
    main()
