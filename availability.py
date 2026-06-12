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
                    "宴席名稱": cell(row, "宴席名稱"),
                    "桌數": cell(row, "葷桌數"),
                    "試菜日期": cell(row, "試菜日期"),
                    "訂席人": cell(row, "聯絡人(主要)"),
                    "電話1": cell(row, "電話(主要)正規化"),
                    "電話2": cell(row, "電話(次要)正規化"),
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


def lookup_bookings_by_phone(phone):
    """用電話號碼找客人自己的訂席。

    給 Ria 的客人身分查詢工具。刻意不回傳任何金額欄位（訂金、桌價），
    金額一律由專人處理。「訂席人」欄位僅供 Ria 核對身分用。
    """
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) < 8:
        return {"錯誤": "電話號碼不完整，請向客人確認完整的訂席電話"}

    rows = _load_rows()
    matches = [
        r for r in rows
        if digits and (r["電話1"] == digits or r["電話2"] == digits)
    ]
    if not matches:
        return {
            "找到筆數": 0,
            "說明": "查無此電話的訂席紀錄，請告知客人將由專人協助確認",
        }

    bookings = []
    for r in sorted(matches, key=lambda r: r["日期"])[:3]:
        bookings.append(
            {
                "宴席日期": r["日期"],
                "時段": r["時段"],
                "廳別": r["廳別"],
                "宴席名稱": r["宴席名稱"],
                "桌數": r["桌數"],
                "試菜日期": r["試菜日期"] or "（未排定）",
                "訂席狀態": r["狀態"],
                "訂席人姓名": r["訂席人"],
            }
        )
    return {
        "找到筆數": len(matches),
        "訂席資料": bookings,
        "說明": (
            "透露任何內容前必須先核對身分：請客人說出訂席人姓名，"
            "與「訂席人姓名」欄位比對相符才可告知；金額一律不透露"
        ),
    }


def _booked_main_halls(slot_rows):
    """從該時段的預訂列算出三大廳（凱莉/維多/利亞）哪些已被佔用。"""
    booked = set()
    for r in slot_rows:
        # 「維多利亞廳」先換成「維多」，避免誤判成「利亞廳」
        hall = r["廳別"].replace("維多利亞", "維多")
        if "凱莉" in hall:
            booked.add("凱莉")
        if "維多" in hall:
            booked.add("維多")
        if "利亞" in hall:
            booked.add("利亞")
    return booked


def query_date(date_str):
    """查詢單一日期的檔期結論。

    刻意只回傳「尚有空檔／已滿檔」的結論，不回傳任何廳別佔用細節，
    讓 AI 結構上不可能洩漏特定廳別的預訂狀況。
    """
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

    result = {"查詢日期": normalized, "公休": False}
    for slot in ("午宴", "晚宴"):
        slot_rows = [
            r for r in day_rows if r["廳別"] and r["時段"] in (slot, "全天")
        ]
        booked = _booked_main_halls(slot_rows)
        result[slot] = "三大廳皆已滿檔" if len(booked) >= 3 else "尚有空檔"
    result["說明"] = (
        "只能告訴客人「尚有空檔」或「已滿檔、建議考慮其他日期」，"
        "不可提及任何廳別的預訂狀況；尚有空檔時仍需專人最終確認"
    )
    return result
