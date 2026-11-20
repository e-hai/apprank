# -*- coding: utf-8 -*-
"""
generate.py — 应用榜单 SEO 静态页生成器
消费 e-hai/Appstore_Top_30 的静态 JSON API，生成合规的程序化 SEO 页面。

合规护栏（来自研究结论）：
1. 每页必须有随数据变化的原创洞察（insight）——不是变量替换
2. 方法论 + 数据来源归因 + Last updated
3. 7d/30d 排名变化 + 趋势图
4. 交互筛选器（抵抗 AI 摘要吞掉）
5. schema 与可见内容一致；FAQ schema 已停用不做
6. 单站、小批量、可清理

运行： python3 src/generate.py --out site --sample
      python3 src/generate.py --out site   # 全量
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.request
import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import config as C
import incremental as INC

# ---------------------------------------------------------------------------
# 数据层
# ---------------------------------------------------------------------------
def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": C.UA})
    with urllib.request.urlopen(req, timeout=C.REQUEST_TIMEOUT) as r:
        return json.load(r)

def get_latest_dates(n: int = 1) -> list[str]:
    d = fetch_json(f"{C.API_BASE}/dates.json")
    return [x["date"] for x in d["dates"][-n:]]

def get_ranking(store: str, market: str, chart: str, date: str, genre: str | None = None) -> dict:
    parts = [store, market, chart]
    if genre:
        parts.append(genre)
    parts.append(date)
    name = "_".join(parts) + ".json"
    return fetch_json(f"{C.API_BASE}/rankings/{name}")

def get_trend(store: str, market: str, chart: str) -> dict:
    return fetch_json(f"{C.API_BASE}/trend/{store}_{market}_{chart}.json")

# ---------------------------------------------------------------------------
# 洞察计算 —— 这是"原创内容"的核心，全部基于真实数据计算
# ---------------------------------------------------------------------------
def make_description(insight: str, limit: int = 158) -> str:
    """把洞察段压成一条完整、不半截断句的 meta description。
    优先在句子边界截断，句内超长时退到词边界并加省略号，绝不切出 "No apps " 这种残句。"""
    text = re.sub(r"\s+", " ", insight).strip()
    if len(text) <= limit:
        return text
    # 在 limit 内找最后一个句子结束点（句号/分号/问号/感叹号 + 空格）
    window = text[: limit + 1]
    ends = [m.end() for m in re.finditer(r"[.!?;]\s", window)]
    if ends and ends[-1] >= limit * 0.55:
        return text[: ends[-1] - 1].rstrip(" .!?;:") + "."
    # 否则退到词边界
    cut = text[:limit]
    sp = cut.rfind(" ")
    if sp >= limit * 0.5:
        return cut[:sp].rstrip() + "…"
    return cut.rstrip(" ,;:") + "…"

def compute_insights(rows: list[dict], trend: dict, date: str, market: str, chart: str, lang: str) -> dict:
    """从真实数据算出可写进正文的洞察点。避免模板套话。"""
    new_apps = [r for r in rows if r.get("status") == "new"]
    risers = sorted([r for r in rows if (r.get("rank_change") or 0) > 0],
                    key=lambda r: r["rank_change"], reverse=True)[:3]
    fallers = sorted([r for r in rows if (r.get("rank_change") or 0) < 0],
                     key=lambda r: r["rank_change"])[:3]
    stable = [r for r in rows if r.get("status") == "stable"]
    top = rows[0] if rows else {}
    devs: dict[str, int] = {}
    genres: dict[str, int] = {}
    for r in rows:
        devs[r.get("developer", "")] = devs.get(r.get("developer", ""), 0) + 1
        genres[r.get("genre_name", "")] = genres.get(r.get("genre_name", ""), 0) + 1
    top_dev, top_dev_n = max(devs.items(), key=lambda kv: kv[1]) if devs else ("", 0)
    top_genre, top_genre_n = max(genres.items(), key=lambda kv: kv[1]) if genres else ("", 0)

    # 趋势：新进榜的 app 在趋势里的最长在榜天数
    longest = 0
    trend_map = trend.get("trends", {})
    for r in rows[:10]:
        series = trend_map.get(r.get("app_id"), [])
        if series:
            longest = max(longest, len(series))

    mkt = C.market_name(market, lang)
    ch = C.CHARTS[chart][lang]
    n = len(rows)
    # 当 new 占比过高（如数据源首次抓取该榜），"N new entries" 会失真且无意义，
    # 改用稳健描述：只陈述榜单构成事实，不夸大"异动"。
    new_ratio = len(new_apps) / n if n else 0
    new_phrase = (
        (f"{len(new_apps)} of the listed apps are newly tracked" if lang == "en"
         else f"榜上 {len(new_apps)} 款为新增追踪")
        if new_ratio <= 0.5
        else ("several entries are being observed for the first time, so day-over-day "
              "movement is limited" if lang == "en" else "本次为该榜单的首次采样，日环比变动有限")
    )
    mover_phrase = (
        f"The biggest climber was {risers[0]['name']} (+{risers[0]['rank_change']} spots). "
        if risers else ("No apps gained positions in this snapshot. " if lang == "en"
                        else "本次采样中无应用名次上升。")
    )
    # 品类若全是"总榜"，不写"X 类别占比最高"这种无信息量的话
    # 数据源品类名为中文，英文页需翻译后展示
    top_genre_disp = C.genre_label(top_genre, lang)
    genre_phrase = (
        (f"{top_genre_disp} is the most represented category ({top_genre_n}/{n}). " if lang == "en"
         else f"{top_genre_disp} 类别占比最高（{top_genre_n}/{n}）。")
        if top_genre and top_genre_n < n else ""
    )
    if lang == "zh":
        insight = (
            f"截至 {date}，{mkt} {ch} Top {n} 中{new_phrase}；{mover_phrase}"
            f"{top_dev or '—'} 以 {top_dev_n} 款入榜成为上榜产品最多的开发者。"
            f"{genre_phrase}榜首为 {top.get('name', '—')}，评分 {top.get('rating', '—')}。"
        )
    else:
        insight = (
            f"As of {date}, {new_phrase} in the {mkt} {ch} Top {n}. {mover_phrase}"
            f"{top_dev or '—'} leads developers with {top_dev_n} apps on the board. "
            f"{genre_phrase}The current #1 is {top.get('name', '—')} with a "
            f"{top.get('rating', '—')} rating."
        )
    return {
        "insight": insight,
        "new_count": len(new_apps),
        "risers": risers,
        "fallers": fallers,
        "top_dev": top_dev, "top_dev_n": top_dev_n,
        "top_genre": top_genre, "top_genre_n": top_genre_n,
        "longest_streak": longest,
    }

# ---------------------------------------------------------------------------
# SVG 趋势图（服务端生成，无第三方库）
# ---------------------------------------------------------------------------
def trend_svg(series: list[dict], width: int = 640, height: int = 180) -> str:
    """series: [{date, best_rank, name}, ...]，排名越小越靠前（y 轴反转）。"""
    if not series:
        return ""
    pts = []
    n = len(series)
    for i, p in enumerate(series):
        x = 10 + i * (width - 20) / max(n - 1, 1)
        rank = p.get("best_rank") or 30
        y = 10 + (min(rank, 30) - 1) / 29 * (height - 20)
        pts.append(f"{x:.1f},{y:.1f}")
    polyline = " ".join(pts)
    first = html.escape(str(series[0].get("date", "")))
    last = html.escape(str(series[-1].get("date", "")))
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Rank history from {first} to {last}" '
        f'style="width:100%;height:auto;background:#fff">'
        f'<polyline fill="none" stroke="#1a73e8" stroke-width="2.5" points="{polyline}"/>'
        f'<text x="10" y="{height-2}" font-size="11" fill="#666">{first}</text>'
        f'<text x="{width-70}" y="{height-2}" font-size="11" fill="#666">{last}</text>'
        f'</svg>'
    )

# ---------------------------------------------------------------------------
# JSON-LD —— 与可见内容一致
# ---------------------------------------------------------------------------
def dataset_schema(rows: list[dict], date: str, market: str, chart: str, lang: str) -> dict:
    name = f"{C.CHARTS[chart][lang]} in {C.market_name(market, lang)} — {date} Snapshot"
    desc = (
        f"Daily snapshot of the {C.CHARTS[chart][lang]} Top 30 in {C.market_name(market, lang)}. "
        f"Rankings are sourced from Apple App Store / Google Play official charts via "
        f"the e-hai/Appstore_Top_30 open dataset, sampled at {date}. "
        f"Ratings and developer data are shown as provided by the source. "
        f"Rank positions reflect a point-in-time sample, not real-time placement."
    )
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": name,
        "description": desc[:5000],
        "creator": {"@type": "Organization", "name": C.SITE["name"]},
        "temporalCoverage": date,
        "spatialCoverage": C.market_name(market, lang),
        "isAccessibleForFree": True,
        "sameAs": f"{C.API_BASE}/rankings/{C.STORES['app_store']['en']}_{market}_{chart}_{date}.json",
    }

def software_schema(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows[:3]:  # 仅为可见的前 3 名生成，避免 schema 与可见内容不一致
        s = {
            "@context": "https://schema.org",
            "@type": "MobileApplication",
            "name": r.get("name", ""),
            "applicationCategory": "GameApplication" if r.get("genre_name") in ("游戏", "Games") else "UtilitiesApplication",
        }
        price = r.get("price_amount")
        s["offers"] = {"@type": "Offer", "price": float(price) if price is not None else 0,
                       "priceCurrency": r.get("price_currency") or "USD"}
        # aggregateRating 仅在真的有评分数据时加（须页面可见）
        if r.get("rating") is not None and r.get("rating_count"):
            s["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": float(r["rating"]),
                "ratingCount": int(r["rating_count"]),
            }
        out.append(s)
    return out

# ---------------------------------------------------------------------------
# hreflang 生成 + 校验
# ---------------------------------------------------------------------------
def hreflang_tags(sibling_urls: dict[str, str]) -> tuple[str, dict[str, str]]:
    """生成自引用 + 双向互指 + x-default 的 hreflang 标签组。
    sibling_urls: {market_code: full_url} —— 同一内容组（同 chart/品类）在各市场的 URL。
    只有实际生成的页面才进入互指集合，避免指向不存在页面。
    返回 (HTML 标签串, {lang_code: url} 映射) 供校验使用。"""
    mapping: dict[str, str] = {}
    for m, url in sibling_urls.items():
        mapping[C.hreflang_code(m, "en")] = url
    if "us" in sibling_urls:
        mapping["x-default"] = sibling_urls["us"]
    tags = "\n".join(
        f'    <link rel="alternate" hreflang="{html.escape(code)}" href="{html.escape(url)}"/>'
        for code, url in mapping.items()
    )
    return tags, mapping

def validate_hreflang(groups: dict[str, dict[str, str]]) -> list[str]:
    """校验双向互指与自引用。
    groups: {url: {lang_code: target_url}} —— 每个页面声明的 hreflang→URL 映射。
    检查：① 页面 URL 必须出现在自己的映射值里（自引用）
          ② 若 A 声明了 B 的 URL，B 也必须声明 A 的 URL（双向）
          ③ 必须包含 x-default
    """
    errors = []
    for url, mapping in groups.items():
        if url not in mapping.values():
            errors.append(f"[self-ref] {url} not in its own hreflang set")
        if "x-default" not in mapping:
            errors.append(f"[x-default] {url} missing x-default")
    # 双向互指：建立 url -> set(urls it points to)
    for a, map_a in groups.items():
        for target in map_a.values():
            if target in groups and a not in groups[target].values():
                errors.append(f"[bidirectional] {a} -> {target} but not reciprocated")
    return errors

# ---------------------------------------------------------------------------
# 页面渲染
# ---------------------------------------------------------------------------
TPL = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<meta name="description" content="{description}"/>
<meta name="robots" content="index,follow"/>
<link rel="canonical" href="{canonical}"/>
{hreflang}
<script type="application/ld+json">{jsonld}</script>
<style>
:root{{--fg:#1a1a1a;--muted:#5f6368;--line:#e0e0e0;--blue:#1a73e8}}
*{{box-sizing:border-box}}
body{{font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--fg);max-width:960px;margin:0 auto;padding:24px;background:#fff}}
h1{{font-size:1.9rem;line-height:1.25}} h2{{font-size:1.3rem;margin-top:2rem;border-bottom:2px solid var(--line);padding-bottom:.3rem}}
.meta{{color:var(--muted);font-size:.9rem}} .insight{{background:#f1f7fe;border-left:4px solid var(--blue);padding:14px 18px;border-radius:4px;margin:1.2rem 0}}
table{{border-collapse:collapse;width:100%;font-size:.92rem}} th,td{{border-bottom:1px solid var(--line);padding:9px 8px;text-align:left}} th{{cursor:pointer;background:#fafafa}}
.up{{color:#137333}} .down{{color:#c5221f}} .new{{color:#1a73e8;font-weight:600}}
.ctrl{{display:flex;gap:10px;flex-wrap:wrap;margin:1rem 0}} .ctrl input,.ctrl select{{padding:8px;border:1px solid var(--line);border-radius:4px}}
footer{{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}}
code{{background:#f5f5f5;padding:1px 4px;border-radius:3px}}
</style>
</head>
<body>
<nav class="meta"><a href="/">Home</a> / {breadcrumb}</nav>
<h1>{h1}</h1>
<p class="meta">Last updated: {updated} · Data source: {source_note} · {streak_note}</p>

<div class="insight"><strong>What changed today</strong><br/>{insight}</div>

<h2>Methodology</h2>
<p>{methodology}</p>

<h2>Top 30 right now</h2>
<div class="ctrl">
  <input id="q" placeholder="Filter by app or developer…" oninput="render()"/>
  <select id="chg" onchange="render()">
    <option value="all">All movements</option>
    <option value="up">Climbers only</option>
    <option value="down">Falling only</option>
    <option value="new">New entries</option>
  </select>
</div>
<table id="t"><thead><tr>
<th onclick="sortBy('rank')">#</th><th onclick="sortBy('name')">App</th><th onclick="sortBy('developer')">Developer</th>
<th onclick="sortBy('category')">Category</th><th onclick="sortBy('rating')">Rating</th><th onclick="sortBy('change')">Move</th>
</tr></thead><tbody id="tb"></tbody></table>

<h2>Rank history (top 5)</h2>
{spark}

<h2>FAQ</h2>
{faq}

<footer>
{attribution}
<p>Last updated {updated}. Rankings are a point-in-time sample and may differ from real-time store placement.</p>
</footer>
<script>
const ROWS={rows_json};
let sortKey='rank',sortDir=1;
function sortBy(k){{sortDir=(sortKey===k)?-sortDir:1;sortKey=k;render();}}
function render(){{
  const q=document.getElementById('q').value.toLowerCase();
  const f=document.getElementById('chg').value;
  let rs=ROWS.filter(r=>{{
    const m=(r.name+' '+r.developer).toLowerCase().includes(q);
    const c=f==='all'||(f==='up'&&r.rank_change>0)||(f==='down'&&r.rank_change<0)||(f==='new'&&r.status==='new');
    return m&&c;
  }});
  rs.sort((a,b)=>{{
    let va=a[sortKey],vb=b[sortKey];
    if(sortKey==='change'){{va=a.rank_change||0;vb=b.rank_change||0;}}
    if(va<vb)return -1*sortDir; if(va>vb)return 1*sortDir; return 0;
  }});
  document.getElementById('tb').innerHTML=rs.map(r=>{{
    const ch=r.status==='new'?'<span class="new">NEW</span>':
      (r.rank_change>0?'<span class="up">▲ +'+r.rank_change+'</span>':
       r.rank_change<0?'<span class="down">▼ '+r.rank_change+'</span>':'—');
    return `<tr><td>${{r.curr_rank}}</td><td>${{esc(r.name)}}</td><td>${{esc(r.developer)}}</td>`+
      `<td>${{esc(r.genre_name||'')}}</td><td>${{r.rating!=null?r.rating:'—'}}</td><td>${{ch}}</td></tr>`;
  }}).join('')||'<tr><td colspan="6">No apps match.</td></tr>';
}}
function esc(s){{const d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}}
render();
</script>
</body>
</html>
"""

def render_page(store, market, chart, date, genre, rows, trend, insights, sibling_urls, out_root):
    lang = C.SITE["lang"]
    mkt = C.market_name(market, lang)
    ch = C.CHARTS[chart][lang]
    gn = C.genre_label("游戏", lang) if genre else None  # 品类页当前仅"游戏"，显示可读标签
    h1 = f"{ch} Apps in {mkt} — {date}" + (f" ({gn})" if gn else "")
    title = f"{ch} Apps in {mkt} ({date}) | {C.SITE['name']}"
    description = make_description(insights["insight"])

    # URL 结构：/{store}/{market}/top/{chart}/（区分双平台，避免撞车；品类页加 /{genre}/）
    parts = [store, market, "top", chart] + ([genre] if genre else [])
    rel_dir = "/".join(parts)
    url = f"{C.SITE['base_url']}/{rel_dir}/"
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    source_note = "Apple App Store official charts (via e-hai/Appstore_Top_30 open dataset)"
    if store == "play":
        source_note = "Google Play official charts (via e-hai/Appstore_Top_30 open dataset)"
    methodology = (
        f"This page ranks the top 30 apps on the {ch} chart in {mkt} for {date}. "
        f"Data is pulled from a daily snapshot of official storefront charts, sampled once per day. "
        f"Rank movement compares against the previous available day. "
        f"Ratings and developer names are shown as provided by the source. "
        f"This is a point-in-time sample, not a real-time feed."
    )
    streak_note = (f"Longest observed streak among top 10: {insights['longest_streak']} days"
                   if insights['longest_streak'] else "")

    # 趋势 SVG：取前 5 名中在趋势里有数据的
    spark_parts = []
    trend_map = trend.get("trends", {})
    for r in rows[:5]:
        s = trend_map.get(r.get("app_id"), [])
        if s:
            spark_parts.append(
                f'<h3>{html.escape(r["name"])}</h3>{trend_svg(s[-30:])}'
                f'<p class="meta">Best rank over {len(s[-30:])} observed days</p>'
            )
    spark = "\n".join(spark_parts) or "<p class='meta'>Trend data not yet available for this market.</p>"

    faq = (
        "<details><summary>How often is this data updated?</summary>"
        "<p>The source dataset publishes a new snapshot daily. This page shows the latest available date shown above.</p></details>"
        "<details><summary>Is this the official App Store ranking?</summary>"
        "<p>Rankings are sourced from official storefront charts via a third-party open dataset and represent a daily sample, not the live store. For real-time placement, check the official store.</p></details>"
        "<details><summary>Do you own this data?</summary>"
        "<p>No. App names, icons and ratings belong to their respective owners. This page presents factual ranking data with attribution and links to the official stores.</p></details>"
    )

    attribution = (
        "Data source: <a href='https://github.com/e-hai/Appstore_Top_30' rel='nofollow'>e-hai/Appstore_Top_30</a> "
        "(daily snapshots of Apple App Store / Google Play official charts). "
        "App Store is a trademark of Apple Inc. Google Play is a trademark of Google LLC. "
        "All app names and marks belong to their respective owners."
    )

    hl, hl_map = hreflang_tags(sibling_urls)
    jsonld = json.dumps([dataset_schema(rows, date, market, chart, lang)] + software_schema(rows),
                        ensure_ascii=False)

    # 给 JS 的精简行数据
    js_rows = [{
        "curr_rank": r.get("curr_rank"), "name": r.get("name", ""),
        "developer": r.get("developer", ""),
        "genre_name": C.genre_label(r.get("genre_name", ""), lang),
        "rating": r.get("rating"), "rank_change": r.get("rank_change") or 0,
        "status": r.get("status", ""),
    } for r in rows]

    page = TPL.format(
        lang=lang, title=html.escape(title), description=html.escape(description),
        canonical=url, hreflang=hl, jsonld=jsonld,
        breadcrumb=f"{mkt} / {ch}", h1=html.escape(h1),
        updated=updated, source_note=source_note, streak_note=streak_note,
        insight=insights["insight"], methodology=methodology,
        spark=spark, faq=faq, attribution=attribution,
        rows_json=json.dumps(js_rows, ensure_ascii=False),
    )
    dest = Path(out_root) / rel_dir / "index.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(page, encoding="utf-8")
    return str(dest), url, hl_map

# ---------------------------------------------------------------------------
# Sitemap
# ---------------------------------------------------------------------------
def write_homepage(urls: list[tuple[str, str]], out_root: str, date: str):
    """生成根首页 /index.html：作为全站导航枢纽，链接到所有已生成的榜单页。
    只输出实际存在的页面链接（urls 来自本轮 fetch 结果），避免死链。"""
    base = C.SITE["base_url"].rstrip("/")
    existing = {u.rstrip("/") for u, _ in urls}

    def cell(store, m, ch, g=None):
        parts = [store, m, "top", ch] + ([g] if g else [])
        url = f"{base}/{'/'.join(parts)}/"
        if url.rstrip("/") not in existing:
            return "—"
        label = C.CHARTS[ch]["en"] + (f" · {C.genre_label('游戏', 'en')}" if g else "")
        return f'<a href="{html.escape(url)}">{html.escape(label)}</a>'

    def platform_table(store: str) -> str:
        genre = C.GENRE_BY_STORE.get(store)
        head = ("<tr><th>Market</th><th>Top Free</th><th>Top Paid</th><th>Top Grossing</th>"
                "<th>Games · Free</th><th>Games · Paid</th><th>Games · Grossing</th></tr>")
        rows = []
        for m in C.ALL_MARKETS:
            cells = [html.escape(C.market_name(m, "en"))]
            cells += [cell(store, m, ch) for ch in C.ALL_CHARTS]
            cells += [cell(store, m, ch, genre) if genre else "—" for ch in C.ALL_CHARTS]
            rows.append("<tr><td>" + "</td><td>".join(cells) + "</td></tr>")
        return f"<table>{head}{''.join(rows)}</table>"

    css = (":root{--fg:#1a1a1a;--muted:#5f6368;--line:#e0e0e0;--blue:#1a73e8}"
           "*{box-sizing:border-box}"
           "body{font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
           "color:var(--fg);max-width:960px;margin:0 auto;padding:24px;background:#fff}"
           "h1{font-size:2rem;line-height:1.25} h2{font-size:1.4rem;margin-top:2rem;"
           "border-bottom:2px solid var(--line);padding-bottom:.3rem}"
           ".meta{color:var(--muted);font-size:.9rem}"
           "table{border-collapse:collapse;width:100%;font-size:.82rem;margin-top:1rem}"
           "th,td{border-bottom:1px solid var(--line);padding:7px 6px;text-align:left}"
           "th{background:#fafafa} a{color:var(--blue);text-decoration:none}"
           "a:hover{text-decoration:underline} footer{margin-top:3rem;padding-top:1rem;"
           "border-top:1px solid var(--line);color:var(--muted);font-size:.85rem}")
    n = len(existing)
    desc = (f"Daily Top 30 app charts for {len(C.ALL_MARKETS)} markets across the App Store and "
            f"Google Play: free, paid, grossing and games, with rank history and insights. "
            f"Snapshot {date}.")
    store_tables = "".join(
        f"<h2>{html.escape(C.STORES[s]['en'])}</h2>{platform_table(s)}" for s in C.ALL_STORES)
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{html.escape(C.SITE['name'])} — Daily App Store &amp; Google Play Top 30 Charts</title>
<meta name="description" content="{html.escape(desc)}"/>
<meta name="robots" content="index,follow"/>
<link rel="canonical" href="{html.escape(base)}/"/>
<link rel="alternate" hreflang="x-default" href="{html.escape(base)}/"/>
<script type="application/ld+json">{{
  "@context":"https://schema.org","@type":"Dataset",
  "name":"{html.escape(C.SITE['name'])} — Daily App Charts",
  "description":"{html.escape(desc)}",
  "creator":{{"@type":"Organization","name":"{html.escape(C.SITE['name'])}"}},
  "temporalCoverage":"{date}","isAccessibleForFree":true,
  "sameAs":"https://e-hai.github.io/Appstore_Top_30/api/"
}}</script>
<style>{css}</style>
</head>
<body>
<h1>{html.escape(C.SITE['name'])}</h1>
<p class="meta">Daily snapshots of the Top 30 apps in {len(C.ALL_MARKETS)} markets — {n} chart pages. Last updated {date}.</p>
<p>Explore free, paid and grossing rankings plus game charts for the App Store and Google Play.
Every page shows the current top 30, rank movement, a rank-history chart and a short insight
computed from that day's real data. Rankings are a daily sample from the
<a href="https://github.com/e-hai/Appstore_Top_30" rel="nofollow">e-hai/Appstore_Top_30</a>
open dataset, not a live feed.</p>
{store_tables}
<footer>
Data source: <a href="https://github.com/e-hai/Appstore_Top_30" rel="nofollow">e-hai/Appstore_Top_30</a>.
App Store is a trademark of Apple Inc. Google Play is a trademark of Google LLC.
All app names and marks belong to their respective owners.
</footer>
</body>
</html>"""
    Path(out_root, "index.html").write_text(page, encoding="utf-8")

def write_sitemap(urls: list[tuple[str, str]], out_root: str):
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
             'xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for url, updated in urls:
        lines.append(f"  <url><loc>{html.escape(url)}</loc><lastmod>{updated}</lastmod></url>")
    lines.append("</urlset>")
    Path(out_root, "sitemap.xml").write_text("\n".join(lines), encoding="utf-8")

def write_robots(out_root: str):
    Path(out_root, "robots.txt").write_text(
        "User-agent: *\nAllow: /\nSitemap: " + C.SITE["base_url"] + "/sitemap.xml\n",
        encoding="utf-8")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_jobs(mode: str):
    """返回 jobs: [(store, market, chart, genre)]"""
    if mode == "sample":
        return [("app_store", m, C.PILOT_CHART, g)
                for m in C.PILOT_MARKETS for g in C.PILOT_GENRES][:10]
    if mode == "full":
        jobs = []
        for store in C.ALL_STORES:
            for m in C.ALL_MARKETS:
                for ch in C.ALL_CHARTS:
                    jobs.append((store, m, ch, None))          # 总榜
                    g = C.GENRE_BY_STORE.get(store)
                    if g:
                        jobs.append((store, m, ch, g))         # 游戏榜
        return jobs
    raise ValueError(f"unknown mode: {mode}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--mode", choices=["sample", "full"], default="sample",
                    help="sample=10页验证; full=全量总榜+游戏榜")
    ap.add_argument("--state", default=".build_state.json", help="增量状态文件")
    ap.add_argument("--no-indexnow", action="store_true", help="跳过 IndexNow 提交（本地测试用）")
    ap.add_argument("--submit-indexnow", action="store_true", help="即使非新数据日也提交变化 URL")
    args = ap.parse_args()

    out = args.out
    Path(out).mkdir(parents=True, exist_ok=True)
    dates = get_latest_dates(1)
    date = dates[0]
    print(f"[i] latest source date: {date}  mode={args.mode}")

    jobs = build_jobs(args.mode)
    print(f"[i] jobs to process: {len(jobs)}")

    # 分 store+chart+genre 组构建 sibling URL（hreflang 互指）
    def page_url(store: str, m: str, ch: str, g: str | None) -> str:
        parts = [store, m, "top", ch] + ([g] if g else [])
        return f"{C.SITE['base_url']}/{'/'.join(parts)}/"

    groups_by_key: dict[tuple, dict[str, str]] = {}
    markets_all = C.ALL_MARKETS
    for store in C.ALL_STORES:
        for ch in C.ALL_CHARTS:
            for g in [None] + ([C.GENRE_BY_STORE[store]] if store in C.GENRE_BY_STORE else []):
                groups_by_key[(store, ch, g or "total")] = {
                    m: page_url(store, m, ch, g) for m in markets_all
                }

    # 增量状态
    state = INC.load_state(args.state)
    prev_pages: dict[str, str] = state.get("pages", {})
    is_new_day = state.get("date") != date
    print(f"[i] state date={state.get('date')}  is_new_day={is_new_day}")

    # ---- 并发拉取所有 ranking（网络等待从 N×延迟 降到 ~N/workers 批）----
    def fetch_one(job):
        store, market, ch, genre = job
        try:
            return job, get_ranking(store, market, ch, date, genre), None
        except Exception as e:
            return job, None, str(e)

    print(f"[i] fetching {len(jobs)} rankings concurrently...")
    fetched = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for job, data, err in ex.map(fetch_one, jobs):
            if err:
                print(f"[ERR] fetch {job}: {err}", file=sys.stderr)
            elif not data.get("rows"):
                print(f"[!] no data: {job}")
            else:
                fetched.append((job, data))
    print(f"[i] fetched {len(fetched)} rankings with data")

    # ---- 并发拉取所需 trend（按 store:market:ch 去重）----
    trend_keys = sorted({(j[0], j[1], j[2]) for j, _ in fetched})
    trend_cache: dict[str, dict] = {}
    def fetch_trend(tk):
        try:
            return tk, get_trend(*tk), None
        except Exception as e:
            return tk, {"trends": {}}, str(e)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for tk, td, err in ex.map(fetch_trend, trend_keys):
            trend_cache[":".join(tk)] = td
    print(f"[i] fetched {len(trend_cache)} trends")

    # ---- 串行渲染（本地 IO，快）----
    urls, groups = [], {}
    new_state_pages: dict[str, str] = {}
    changed_urls: list[str] = []
    skipped = 0

    for (store, market, ch, genre), data in fetched:
        try:
            rows = data.get("rows", [])
            tkey = f"{store}:{market}:{ch}"
            url = page_url(store, market, ch, genre)
            fp = INC.fingerprint(rows)
            dest_check = Path(out) / "/".join(
                [store, market, "top", ch] + ([genre] if genre else [])) / "index.html"
            changed = INC.is_changed(prev_pages.get(url), fp, is_new_day) or not dest_check.exists()
            if not changed:
                skipped += 1
                new_state_pages[url] = fp
                urls.append((url, date))
                key = (store, ch, genre or "total")
                groups[url] = _map_to_codes(groups_by_key.get(key, {}))
                continue

            ins = compute_insights(rows, trend_cache[tkey], date, market, ch, lang="en")
            sibling_urls = groups_by_key[(store, ch, genre or "total")]
            dest, url, hl_map = render_page(store, market, ch, date, genre,
                                            rows, trend_cache[tkey], ins, sibling_urls, out)
            groups[url] = hl_map
            urls.append((url, date))
            new_state_pages[url] = fp
            changed_urls.append(url)
            print(f"[ok] {dest}")
        except Exception as e:
            print(f"[ERR] render {store}/{market}/{ch}/{genre}: {e}", file=sys.stderr)

    write_homepage(urls, out, date)          # 根首页 /index.html（导航枢纽）
    # sitemap 纳入首页
    home_url = f"{C.SITE['base_url'].rstrip('/')}/"
    write_sitemap([(home_url, date)] + urls, out)
    write_robots(out)
    INC.save_state(args.state, date, new_state_pages)

    errs = validate_hreflang(groups)
    if errs:
        print(f"[!] hreflang issues ({len(errs)}):", errs[:5], file=sys.stderr)

    print(f"\n[done] {len(urls)} pages in sitemap; {len(changed_urls)} changed/created; "
          f"{skipped} unchanged skipped → {out}/")

    # IndexNow 提交（仅变化 URL）
    if changed_urls and not args.no_indexnow:
        key_file = INC.write_key_file(out, C.INDEXNOW_KEY)
        if key_file:
            print(f"[i] IndexNow key file written: {key_file} (deploy it with the site)")
        res = INC.submit_indexnow(changed_urls, C.INDEXNOW_KEY)
        print(f"[indexnow] {res}")
    elif changed_urls:
        print(f"[indexnow] skipped by flag; {len(changed_urls)} URLs would be submitted.")


def _map_to_codes(sibling_urls: dict[str, str]) -> dict[str, str]:
    """从 {market: url} 反推 {hreflang_code: url}，用于未重渲染页的校验记录。"""
    mapping = {}
    for m, url in sibling_urls.items():
        mapping[C.hreflang_code(m, "en")] = url
    if "us" in sibling_urls:
        mapping["x-default"] = sibling_urls["us"]
    return mapping

if __name__ == "__main__":
    main()
