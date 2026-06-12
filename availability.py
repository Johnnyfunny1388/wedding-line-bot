# -*- coding: utf-8 -*-
"""檔期查詢：供 Ria 回答客人「某天還有沒有場地」時讀取機器版訂席總表。

設計原則（與老闆議定的保險絲）：
- 只回傳事實：公休與否、已有哪些時段/廳別被預訂
- 不回傳其他客人的宴席名稱、聯絡資訊（避免洩漏）
- 資料快取 10 分鐘，減少 Sheets 讀取量
"""
import re
import time
import logging
import threading

import gspread

from booking_sync import MACHINE_SHEET_ID, DATA_TAB, _load_credentials

logger = logging.getLogger("ria.availability")

CACHE_TTL_SECONDS = 600

_cache = {"rows": None, "loaded_at": 0.0}
_cache_lock = threading.Lock()


def _load_rows():
    """讀取訂席總表（快取 10 分鐘）。只取查檔期需要的欄位。"""
    with _cache_lock:
        if (
            _cache["rows"] is not None
            and time.time() - _cache["loaded_at"] < CACHE_TTL_SECONDS
        ):
            return _cache["rows"]

        creds = _load_credentials()
        if creds is None or not MACHINE_SHEET_ID:
            raise RuntimeError("機器版總表未設定")
        gc = gspread.Client(auth=creds)
        try:
            gc.http_client.set_timeout(30)
        except AttributeError:
            pass
        ws = gc.open_by_key(MACHINE_SHEET_ID).worksheet(DATA_TAB)
        values = ws.get_all_values()
        if not values:
            raise RuntimeError("訂席總表是空的")

        header = values[0]
        idx = {name: i for i, name in enumerate(header)}

        def cell(row, col):
            i = idx.get(col)
            return row[i].strip() if i is not None and i < len(row) else ""

        rows = []
        for row in values[1:]:
            d = cell(row, "宴席日期")
            if not d:
                continue
            rows.append(
                {
                    "日期": d,
                    "時段": cell(row, "時段"),
                    "廳別": cell(row, "廳別"),
                    "狀態": cell(row, "訂席狀態"),
                }
            )
        _cache["rows"] = rows
        _cache["loaded_at"] = time.time()
        logger.info("檔期資料已更新快取，共 %d 筆", len(rows))
        return rows


def _normalize_date(date_str):
    """把 2026-6-3、2026/06/03 等格式統一成 2026-06-03。"""
    m = re.match(r"^\s*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", str(date_str))
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def query_date(date_str):
    """查詢單一日期的訂席狀況。回傳 dict（給 Claude 當 tool result）。"""
    normalized = _normalize_date(date_str)
    if normalized is None:
        return {"錯誤": f"日期格式無法解析：{date_str}，請用 YYYY-MM-DD"}

    rows = _load_rows()
    known_years = {r["日期"][:4] for r in rows}
    if normalized[:4] not in known_years:
        return {
            "查詢日期": normalized,
            "說明": "該日期超出目前訂席資料的範圍，無法查詢，請轉交專人確認",
        }

    day_rows = [r for r in rows if r["日期"] == normalized]
    closed = any(r["時段"] == "公休" or r["狀態"] == "公休" for r in day_rows)
    if closed:
        return {"查詢日期": normalized, "公休": True, "說明": "該日會館公休"}

    booked = [
        {"時段": r["時段"], "廳別": r["廳別"]}
        for r in day_rows
        if r["廳別"]
    ]
    if not booked:
        return {
            "查詢日期": normalized,
            "公休": False,
            "已有預訂": [],
            "說明": "該日期目前查無預訂紀錄，各廳別看起來尚有空檔（仍需專人最終確認）",
        }
    return {
        "查詢日期": normalized,
        "公休": False,
        "已有預訂": booked,
        "說明": "以上時段/廳別已有預訂，未列出的廳別看起來尚有空檔（仍需專人最終確認）",
    }
