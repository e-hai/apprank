# -*- coding: utf-8 -*-
"""
incremental.py — 增量更新：快照、实质变化 diff、IndexNow 提交
设计目标：只重写真正变化的页，避免"改日期伪装更新"（Google News 明令禁止，
也对程序化站点的 thin content 判定不利）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import config as C

STATE_FILE = ".build_state.json"   # 保存 {page_url: fingerprint} + 日期


# ---------------------------------------------------------------------------
# 指纹：用页面的"实质数据"计算，不含 Last-updated 时间戳
# ---------------------------------------------------------------------------
def fingerprint(rows: list[dict]) -> str:
    """用榜单行的核心字段算指纹。任一实质字段变化 → 指纹变化 → 触发重写+提交。"""
    import hashlib
    core = []
    for r in rows:
        core.append([
            r.get("app_id"), r.get("curr_rank"), r.get("rank_change"),
            r.get("status"), r.get("rating"), r.get("rating_count"),
        ])
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def load_state(state_path: str) -> dict:
    p = Path(state_path)
    if not p.exists():
        return {"date": None, "pages": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"date": None, "pages": {}}


def save_state(state_path: str, date: str, pages: dict[str, str]):
    Path(state_path).write_text(
        json.dumps({"date": date, "pages": pages}, ensure_ascii=False, indent=2),
        encoding="utf-8")


def is_changed(old_fp: str | None, new_fp: str, is_new_day: bool) -> bool:
    """实质变化判定：
    - 全新页（old_fp 为 None）→ 变化
    - 指纹变化 → 变化
    - 数据指纹未变但跨天 → 视为变化（排行榜页的日期本身是实质信息）
    """
    if old_fp is None:
        return True
    if old_fp != new_fp:
        return True
    if is_new_day:
        return True
    return False


# ---------------------------------------------------------------------------
# IndexNow 提交
# ---------------------------------------------------------------------------
def submit_indexnow(urls: list[str], key: str, endpoint: str = None,
                    host: str = None) -> dict:
    """批量提交变化 URL 给 IndexNow。
    返回 {ok: bool, submitted: int, status: int, message: str}
    - 200：已收到
    - 202：key 校验中
    - 429：提交过频（调用方应退避）
    官方限制：单次 ≤10,000 URL；只提交新增/更新/删除的近期变更。
    """
    endpoint = endpoint or C.INDEXNOW_ENDPOINT
    key = key or C.INDEXNOW_KEY
    if not key:
        return {"ok": False, "submitted": 0, "status": 0,
                "message": "INDEXNOW_KEY not configured; skip submission."}
    if not urls:
        return {"ok": True, "submitted": 0, "status": 0, "message": "No changed URLs."}

    # 分批，每批最多 10000
    results = {"ok": True, "submitted": 0, "status": 0, "message": ""}
    batch_size = 10000
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        payload = json.dumps({
            "host": host or _host_from_url(batch[0]),
            "key": key,
            "urlList": batch,
        }).encode("utf-8")
        req = urllib.request.Request(
            endpoint, data=payload, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": C.UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "ignore")[:200]
            if status in (200, 202):
                results["submitted"] += len(batch)
                results["status"] = status
                results["message"] = f"accepted {len(batch)} URLs (HTTP {status}) {body}"
            else:
                results["ok"] = False
                results["status"] = status
                results["message"] = f"unexpected HTTP {status}: {body}"
                break
        except urllib.error.HTTPError as e:
            results["ok"] = False
            results["status"] = e.code
            results["message"] = (f"HTTP {e.code}. If 429: back off and retry later. "
                                  f"Check key file at /{key}.txt")
            break
        except Exception as e:
            results["ok"] = False
            results["message"] = f"network error: {e}"
            break
    return results


def _host_from_url(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc


def write_key_file(out_root: str, key: str):
    """把 IndexNow key 写到站点根目录（本地验证用；上线后需随站点部署）。"""
    if not key:
        return None
    Path(out_root).mkdir(parents=True, exist_ok=True)
    p = Path(out_root) / f"{key}.txt"
    p.write_text(key, encoding="utf-8")
    return str(p)
