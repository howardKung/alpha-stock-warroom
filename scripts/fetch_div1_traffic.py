#!/usr/bin/env python3
"""
Division 1 Cashflow & Traffic Radar (`fetch_div1_traffic.py`)
-------------------------------------------------------------
Zero-cost ($0), zero-LLM-quota multi-platform traffic & monetization monitor:
1. Apify Store Public v2 API: Tracks all 5 live Pay-Per-Event Actors (runs, unique users, 7d/30d users, bookmarks, reviews, last run).
2. GitHub Traffic & Repo API: Tracks all 6 Division 1 repositories (stars, forks + 14d views, unique visitors, clones & referrers via `gh api` when available).
3. Gumroad Storefront Health + Optional Sales API (`GUMROAD_ACCESS_TOKEN`): Verifies `https://howardk1.gumroad.com/l/agentguard-pro-kit` is HTTP 200 LIVE, and pulls exact views/sales if token is set.
4. n8n Official Template Library API (`api.n8n.io`): Detects the exact moment submitted n8n workflows pass review & tracks template views.
5. Delta Alerting -> Telegram: Immediately notifies Đại Boss on Telegram when a new customer, API run, GitHub star, Gumroad sale, or n8n template approval occurs!
"""

import html
import json
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ_TW = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "div1_traffic.json"
IDEA_COPY = Path("/home/antigravity/idea-team/products/growth-kit/div1_traffic_latest.json")
WARROOM_TRAFFIC_URL = "https://howardkung.github.io/alpha-stock-warroom/?tab=traffic"
GUMROAD_URL = "https://howardk1.gumroad.com/l/agentguard-pro-kit"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

APIFY_ACTORS = [
    {
        "id": "actor1",
        "slug": "multi-ats-hiring-signals-api-zero-pii",
        "repo": "multi-ats-hiring-signals",
        "pricePer1kUsd": 15.0,
        "eventName": "company-signal-extracted",
    },
    {
        "id": "actor2",
        "slug": "company-dns-email-auth-tech-stack-signals-zero-pii",
        "repo": "domain-dns-tech-trust-signals",
        "pricePer1kUsd": 8.0,
        "eventName": "domain-signal-extracted",
    },
    {
        "id": "actor3",
        "slug": "sec-form-d-funding-signals",
        "repo": "sec-form-d-funding-signals",
        "pricePer1kUsd": 10.0,
        "eventName": "funding-signal-extracted",
    },
    {
        "id": "actor4",
        "slug": "ecommerce-store-app-intel-api-zero-pii",
        "repo": "ecommerce-store-app-intel",
        "pricePer1kUsd": 10.0,
        "eventName": "store-intel-extracted",
    },
    {
        "id": "actor5",
        "slug": "sec-form4-insider-cluster-buys-api-zero-pii",
        "repo": "sec-form4-insider-cluster-buys",
        "pricePer1kUsd": 10.0,
        "eventName": "insider-buy-extracted",
    },
]

GITHUB_REPOS = [
    "agentguard-kit",
    "multi-ats-hiring-signals",
    "domain-dns-tech-trust-signals",
    "sec-form-d-funding-signals",
    "ecommerce-store-app-intel",
    "sec-form4-insider-cluster-buys",
]

N8N_SEARCH_QUERIES = [
    "Enforce daily LLM budget limits with OpenAI and Slack alerts",
    "howardkung",
]


def http_get_json(url: str, headers: dict | None = None, timeout: int = 12) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def fetch_apify_actors() -> list[dict]:
    results = []
    for item in APIFY_ACTORS:
        slug = item["slug"]
        api_url = f"https://api.apify.com/v2/acts/howardkung~{slug}"
        store_url = f"https://apify.com/howardkung/{slug}"
        try:
            data = http_get_json(api_url).get("data", {})
            st = data.get("stats", {})
            last_run_raw = st.get("lastRunStartedAt") or ""
            last_run_tw = ""
            if last_run_raw:
                try:
                    dt = datetime.fromisoformat(last_run_raw.replace("Z", "+00:00")).astimezone(TZ_TW)
                    last_run_tw = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    last_run_tw = last_run_raw[:16]
            results.append({
                "id": item["id"],
                "slug": slug,
                "title": data.get("title") or slug,
                "storeUrl": store_url,
                "isPublic": bool(data.get("isPublic", True)),
                "pricingModel": (data.get("pricingInfos") or [{}])[-1].get("pricingModel", "PAY_PER_EVENT"),
                "pricePer1kUsd": item["pricePer1kUsd"],
                "eventName": item["eventName"],
                "totalRuns": int(st.get("totalRuns") or 0),
                "totalUsers": int(st.get("totalUsers") or 0),
                "users7d": int(st.get("totalUsers7Days") or 0),
                "users30d": int(st.get("totalUsers30Days") or 0),
                "bookmarks": int(st.get("bookmarkCount") or 0),
                "reviews": int(st.get("actorReviewCount") or 0),
                "rating": float(st.get("actorReviewRating") or 0.0),
                "lastRunTw": last_run_tw,
                "status": "LIVE",
            })
        except Exception as e:
            results.append({
                "id": item["id"],
                "slug": slug,
                "title": slug,
                "storeUrl": store_url,
                "isPublic": True,
                "pricingModel": "PAY_PER_EVENT",
                "pricePer1kUsd": item["pricePer1kUsd"],
                "eventName": item["eventName"],
                "totalRuns": 0,
                "totalUsers": 0,
                "users7d": 0,
                "users30d": 0,
                "bookmarks": 0,
                "reviews": 0,
                "rating": 0.0,
                "lastRunTw": "--",
                "status": f"ERR: {e}",
            })
    return results


def gh_api_json(endpoint: str) -> dict | list | None:
    try:
        res = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception:
        pass
    return None


def fetch_github_repos(prev_repos_map: dict[str, dict]) -> list[dict]:
    out = []
    for repo in GITHUB_REPOS:
        prev = prev_repos_map.get(repo, {})
        meta = gh_api_json(f"repos/howardKung/{repo}")
        if not isinstance(meta, dict):
            try:
                meta = http_get_json(f"https://api.github.com/repos/howardKung/{repo}")
            except Exception:
                meta = {}

        stars = int(meta.get("stargazers_count", prev.get("stars", 0)) or 0)
        forks = int(meta.get("forks_count", prev.get("forks", 0)) or 0)

        # 14-day views/clones/referrers require push-access token (`gh` on VM has it; fallback to prev snapshot on Actions)
        views_data = gh_api_json(f"repos/howardKung/{repo}/traffic/views")
        clones_data = gh_api_json(f"repos/howardKung/{repo}/traffic/clones")
        refs_data = gh_api_json(f"repos/howardKung/{repo}/traffic/popular/referrers")

        views_14d = int(views_data["count"]) if isinstance(views_data, dict) and "count" in views_data else int(prev.get("views14d", 0))
        uniques_14d = int(views_data["uniques"]) if isinstance(views_data, dict) and "uniques" in views_data else int(prev.get("uniques14d", 0))
        clones_14d = int(clones_data["count"]) if isinstance(clones_data, dict) and "count" in clones_data else int(prev.get("clones14d", 0))
        clone_uniques_14d = int(clones_data["uniques"]) if isinstance(clones_data, dict) and "uniques" in clones_data else int(prev.get("cloneUniques14d", 0))

        if isinstance(refs_data, list):
            referrers = [
                {"referrer": r.get("referrer", ""), "views": int(r.get("count", 0)), "uniques": int(r.get("uniques", 0))}
                for r in refs_data[:6]
            ]
        else:
            referrers = prev.get("referrers", [])

        out.append({
            "repo": repo,
            "url": f"https://github.com/howardKung/{repo}",
            "stars": stars,
            "forks": forks,
            "views14d": views_14d,
            "uniques14d": uniques_14d,
            "clones14d": clones_14d,
            "cloneUniques14d": clone_uniques_14d,
            "referrers": referrers,
        })
    return out


def fetch_gumroad_status(prev_gumroad: dict) -> dict:
    http_ok = False
    try:
        req = urllib.request.Request(GUMROAD_URL, headers={"User-Agent": UA}, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            http_ok = (200 <= r.status < 400)
    except Exception:
        try:
            req = urllib.request.Request(GUMROAD_URL, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                http_ok = (200 <= r.status < 400)
        except Exception:
            http_ok = False

    token = os.environ.get("GUMROAD_ACCESS_TOKEN", "").strip()
    if token:
        try:
            q = urllib.parse.urlencode({"access_token": token})
            pdata = http_get_json(f"https://api.gumroad.com/v2/products?{q}")
            prods = pdata.get("products", [])
            target = next((p for p in prods if "agentguard" in str(p.get("short_url", "")).lower() or "agentguard" in str(p.get("name", "")).lower()), prods[0] if prods else {})
            return {
                "url": GUMROAD_URL,
                "storeLive": http_ok,
                "apiConnected": True,
                "salesCount": int(target.get("sales_count") or 0),
                "salesUsd": round(float(target.get("sales_usd_cents") or 0) / 100.0, 2),
                "tiers": "$49 Solo / $89 Agency Unlimited",
            }
        except Exception:
            pass

    return {
        "url": GUMROAD_URL,
        "storeLive": http_ok,
        "apiConnected": False,
        "salesCount": prev_gumroad.get("salesCount"),
        "salesUsd": prev_gumroad.get("salesUsd"),
        "tiers": "$49 Solo / $89 Agency Unlimited",
    }


def fetch_n8n_templates() -> dict:
    found = {}
    for q in N8N_SEARCH_QUERIES:
        try:
            url = f"https://api.n8n.io/api/templates/search?search={urllib.parse.quote(q)}"
            data = http_get_json(url)
            for wf in data.get("workflows", []):
                wid = wf.get("id")
                name = str(wf.get("name", ""))
                user = str((wf.get("user") or {}).get("username", "")).lower()
                if "howard" in user or "enforce daily llm budget limits" in name.lower() or "agentguard" in name.lower():
                    found[wid] = {
                        "id": wid,
                        "name": name,
                        "totalViews": int(wf.get("totalViews") or 0),
                        "url": f"https://n8n.io/workflows/{wid}",
                    }
        except Exception:
            pass

    items = list(found.values())
    return {
        "submittedPendingReview": 1 if len(items) == 0 else 0,
        "approvedCount": len(items),
        "totalViews": sum(x["totalViews"] for x in items),
        "templates": items,
        "portalUrl": "https://creators.n8n.io/",
    }


def detect_deltas(prev: dict, curr: dict) -> list[str]:
    events: list[str] = []
    if not prev:
        return events

    prev_actors = {a["slug"]: a for a in prev.get("apify", {}).get("actors", [])}
    for a in curr["apify"]["actors"]:
        pa = prev_actors.get(a["slug"])
        if not pa:
            continue
        if a["totalUsers"] > pa.get("totalUsers", 0):
            diff = a["totalUsers"] - pa.get("totalUsers", 0)
            events.append(f"🕷️ 新客戶上門！Apify《{a['title']}》新增 +{diff} 位獨立用戶（累計 {a['totalUsers']} 人）！")
        if a["totalRuns"] > pa.get("totalRuns", 0):
            diff = a["totalRuns"] - pa.get("totalRuns", 0)
            events.append(f"⚡ API 被呼叫！Apify《{a['title']}》新增 +{diff} 次執行（累計 {a['totalRuns']} 次）！")
        if a["bookmarks"] > pa.get("bookmarks", 0):
            diff = a["bookmarks"] - pa.get("bookmarks", 0)
            events.append(f"⭐ 新增收藏！Apify《{a['title']}》被收藏 +{diff} 次！")

    prev_repos = {r["repo"]: r for r in prev.get("github", {}).get("repos", [])}
    for r in curr["github"]["repos"]:
        pr = prev_repos.get(r["repo"])
        if not pr:
            continue
        if r["stars"] > pr.get("stars", 0):
            diff = r["stars"] - pr.get("stars", 0)
            events.append(f"🌟 GitHub 摘星！`{r['repo']}` 新增 +{diff} 顆 Star（累計 {r['stars']} ⭐）！")

    prev_n8n = int(prev.get("n8n", {}).get("approvedCount", 0) or 0)
    curr_n8n = int(curr["n8n"]["approvedCount"] or 0)
    if curr_n8n > prev_n8n:
        events.append(f"🎉 n8n 官方範本過審上架啦！（目前已上架 {curr_n8n} 支，現在可去 creators.n8n.io 傳下一支！）")

    prev_sales = prev.get("gumroad", {}).get("salesCount")
    curr_sales = curr["gumroad"].get("salesCount")
    if prev_sales is not None and curr_sales is not None and curr_sales > prev_sales:
        diff = curr_sales - prev_sales
        events.append(f"💰 賀成交！！Gumroad《AgentGuard Pro Kit》新進帳 +{diff} 筆訂單（累計 ${curr['gumroad'].get('salesUsd', 0)} USD）！")

    return events


def send_telegram_traffic_alert(curr: dict, events: list[str], force_summary: bool = False) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    if not events and not force_summary:
        return False

    ap = curr["apify"]
    gh = curr["github"]
    gr = curr["gumroad"]
    n8 = curr["n8n"]

    lines: list[str] = []
    if events:
        lines.append("🔔 <b>【第一事業部・新客上門／流量跳動快報！】</b> 🔔")
        for ev in events:
            lines.append(f"• {html.escape(ev)}")
        lines.append("")

    lines.extend([
        "💰 <b>【金流一部・6 座收銀台即時流量總覽】</b>",
        f"🕒 <code>{curr['updatedAt']}</code>",
        f"🕷️ <b>Apify 5 台印鈔機</b>：全數營業中 ｜ 累計呼叫 <code>{ap['totalRuns']}</code> 次 ｜ 獨立用戶 <code>{ap['totalUsers']}</code> 人 ｜ 收藏 <code>{ap['totalBookmarks']}</code>",
        f"🐙 <b>GitHub 6 大導流庫</b>：14天瀏覽 <code>{gh['totalViews14d']}</code> 次（獨立訪客 <code>{gh['totalUniques14d']}</code> 人）｜ Clone <code>{gh['totalClones14d']}</code> 次 ｜ Stars <code>{gh['totalStars']}</code>",
        f"🛍️ <b>Gumroad 收銀台</b>：{'🟢 200 OK 正常營業 ($49/$89)' if gr['storeLive'] else '🔴 連線異常'}",
        f"⚡ <b>n8n 官方範本庫</b>：已上架 <code>{n8['approvedCount']}</code> 支（審核排隊中 <code>{n8['submittedPendingReview']}</code> 支）",
        "",
        f'🔗 <a href="{WARROOM_TRAFFIC_URL}">開啟金流一部流量＆來源監控面板 ↗</a>',
    ])

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        return bool(res.get("ok"))


def main() -> None:
    now_tw = datetime.now(TZ_TW)
    ts_str = now_tw.strftime("%Y-%m-%d %H:%M:%S (台灣時間)")

    prev = {}
    if OUT_JSON.exists():
        try:
            prev = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            prev = {}

    prev_repos_map = {r["repo"]: r for r in prev.get("github", {}).get("repos", [])}
    actors = fetch_apify_actors()
    repos = fetch_github_repos(prev_repos_map)
    gumroad = fetch_gumroad_status(prev.get("gumroad", {}))
    n8n = fetch_n8n_templates()

    # Aggregate top referrers across all 6 repos
    ref_agg: dict[str, dict] = {}
    for r in repos:
        for ref in r.get("referrers", []):
            src = ref["referrer"]
            if src not in ref_agg:
                ref_agg[src] = {"referrer": src, "views": 0, "uniques": 0}
            ref_agg[src]["views"] += ref["views"]
            ref_agg[src]["uniques"] += ref["uniques"]
    top_referrers = sorted(ref_agg.values(), key=lambda x: x["views"], reverse=True)[:10]

    curr = {
        "updatedAt": ts_str,
        "apify": {
            "liveCount": sum(1 for a in actors if a["status"] == "LIVE"),
            "totalRuns": sum(a["totalRuns"] for a in actors),
            "totalUsers": sum(a["totalUsers"] for a in actors),
            "totalBookmarks": sum(a["bookmarks"] for a in actors),
            "actors": actors,
        },
        "github": {
            "repoCount": len(repos),
            "totalStars": sum(r["stars"] for r in repos),
            "totalForks": sum(r["forks"] for r in repos),
            "totalViews14d": sum(r["views14d"] for r in repos),
            "totalUniques14d": sum(r["uniques14d"] for r in repos),
            "totalClones14d": sum(r["clones14d"] for r in repos),
            "totalCloneUniques14d": sum(r["cloneUniques14d"] for r in repos),
            "topReferrers": top_referrers,
            "repos": repos,
        },
        "gumroad": gumroad,
        "n8n": n8n,
    }

    events = detect_deltas(prev, curr)
    is_manual = (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch") and (os.environ.get("TRIGGER_SOURCE", "") != "auto_2h")
    tg_sent = send_telegram_traffic_alert(curr, events, force_summary=is_manual)

    prev_hist = prev.get("history", [])
    hist_entry = {
        "time": now_tw.strftime("%m-%d %H:%M"),
        "apifyRuns": curr["apify"]["totalRuns"],
        "apifyUsers": curr["apify"]["totalUsers"],
        "ghViews14d": curr["github"]["totalViews14d"],
        "ghUniques14d": curr["github"]["totalUniques14d"],
        "ghClones14d": curr["github"]["totalClones14d"],
        "n8nApproved": curr["n8n"]["approvedCount"],
        "events": events,
        "tgSent": tg_sent,
    }
    curr["history"] = ([hist_entry] + prev_hist)[:30]

    OUT_JSON.write_text(json.dumps(curr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if IDEA_COPY.parent.exists():
        IDEA_COPY.write_text(json.dumps(curr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"[DIV1-TRAFFIC] {ts_str} | Apify Runs={curr['apify']['totalRuns']} Users={curr['apify']['totalUsers']} "
        f"| GH Views14d={curr['github']['totalViews14d']} (Uniques={curr['github']['totalUniques14d']}) Clones14d={curr['github']['totalClones14d']} "
        f"| Gumroad={'LIVE' if gumroad['storeLive'] else 'DOWN'} | Events={len(events)} | TG={tg_sent}"
    )


if __name__ == "__main__":
    main()
