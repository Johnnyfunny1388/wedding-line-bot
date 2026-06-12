# -*- coding: utf-8 -*-
"""新詢問自動落地：客人道別時，從對話抽取結構化需求，寫入試算表並通知管理者。

流程（全部在背景執行，不拖慢給客人的回覆）：
1. 客人訊息含道別關鍵字 → 觸發
2. 用 Claude 從對話抽取：活動類型/宴席日期/時段/桌數/聯絡人/電話/廳別建議/其他需求
3. 有實質內容才寫入「LINE新詢問」分頁（避免純閒聊也進表）
4. LINE 推播通知管理者
"""
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import anthropic
import gspread

from booking_sync import (
    MACHINE_SHEET_ID,
    _load_credentials,
    _get_or_create_tab,
    _notify_admin,
)

logger = logging.getLogger("ria.lead")

TAIPEI_TZ = timezone(timedelta(hours=8))
LEAD_TAB = "LINE新詢問"
LEAD_HEADER = [
    "時間(台灣)", "user_id", "聯絡人姓名", "聯絡電話", "活動類型",
    "宴席日期", "時段", "桌數", "廳別建議", "其他需求", "對話摘要",
]

ENDING_KEYWORDS = ["謝謝", "感謝", "再見", "掰掰", "結束", "辛苦了"]

# 同一位客人 30 分鐘內只落地一次，避免連說兩次謝謝就重複寫入
_recent_leads = {}
LEAD_COOLDOWN_SECONDS = 1800

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "活動類型": {"type": "string", "description": "婚宴/尾牙/春酒/謝師宴/周歲宴/會議等，未提及填空字串"},
        "宴席日期": {"type": "string", "description": "YYYY-MM-DD，只有月份就 YYYY-MM，未提及填空字串"},
        "時段": {"type": "string", "description": "午宴或晚宴，未提及填空字串"},
        "桌數": {"type": "string", "description": "例如 30 或 25-30，未提及填空字串"},
        "聯絡人姓名": {"type": "string", "description": "客人留下的稱呼或姓名，未提及填空字串"},
        "聯絡電話": {"type": "string", "description": "客人留下的電話，未提及填空字串"},
        "廳別建議": {"type": "string", "description": "對話中建議過的廳別，未提及填空字串"},
        "其他需求": {"type": "string", "description": "素食桌、佈置、預算等特殊需求摘要，未提及填空字串"},
    },
    "required": [
        "活動類型", "宴席日期", "時段", "桌數",
        "聯絡人姓名", "聯絡電話", "廳別建議", "其他需求",
    ],
    "additionalProperties": False,
}


def _conversation_text(history):
    lines = []
    for m in history:
        speaker = "客人" if m.get("role") == "user" else "利亞"
        lines.append(f"{speaker}：{str(m.get('content', ''))[:500]}")
    return "\n".join(lines)


def _extract(history):
    """用 Claude 從對話抽取結構化欄位。失敗回傳 None。"""
    import os

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    today = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")
    system = (
        "你是宴會館的客服對話資料擷取器。從對話中擷取客人的詢問需求，"
        f"無法確定的欄位一律填空字串，不要猜測。今天日期是 {today}，"
        "相對日期（如下個月、明年三月）請換算成實際日期。"
    )
    prompt = "請從以下對話擷取客人需求資訊：\n\n" + _conversation_text(history)
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
        )
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)
    except Exception:
        logger.exception("結構化抽取失敗，改用一般模式重試")
    # 後備：一般模式要求純 JSON
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=system + " 只輸出 JSON 物件，鍵為：活動類型、宴席日期、時段、桌數、"
            "聯絡人姓名、聯絡電話、廳別建議、其他需求。不要輸出其他文字。",
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in response.content if b.type == "text")
        text = text.strip().strip("`").lstrip("json").strip()
        return json.loads(text)
    except Exception:
        logger.exception("新詢問資訊抽取失敗")
        return None


def _write_lead(user_id, data, summary):
    creds = _load_credentials()
    gc = gspread.Client(auth=creds)
    try:
        gc.http_client.set_timeout(60)
    except AttributeError:
        pass
    spreadsheet = gc.open_by_key(MACHINE_SHEET_ID)
    ws = _get_or_create_tab(spreadsheet, LEAD_TAB, rows=2000, cols=12)
    if not ws.acell("A1").value:
        ws.update(values=[LEAD_HEADER], range_name="A1")
    ws.append_row(
        [
            datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M"),
            user_id,
            data.get("聯絡人姓名", ""),
            data.get("聯絡電話", ""),
            data.get("活動類型", ""),
            data.get("宴席日期", ""),
            data.get("時段", ""),
            data.get("桌數", ""),
            data.get("廳別建議", ""),
            data.get("其他需求", ""),
            summary[:500],
        ],
        value_input_option="RAW",
    )


def _worker(user_id, history, fallback_phone):
    try:
        data = _extract(history)
        if data is None:
            return
        if not data.get("聯絡電話") and fallback_phone:
            data["聯絡電話"] = fallback_phone
        # 沒有實質內容（純打招呼/問路）就不落地
        if not (data.get("活動類型") or data.get("宴席日期") or data.get("聯絡電話")):
            logger.info("對話無實質詢問內容，不落地 user=%s", user_id)
            return

        summary = ""
        for m in reversed(history):
            if m.get("role") == "assistant":
                summary = str(m.get("content", ""))
                break

        _write_lead(user_id, data, summary)
        logger.info("新詢問已落地 user=%s", user_id)

        parts = ["📋 LINE 新詢問"]
        if data.get("活動類型"):
            parts.append(f"活動：{data['活動類型']}")
        if data.get("宴席日期"):
            line = f"日期：{data['宴席日期']}"
            if data.get("時段"):
                line += f" {data['時段']}"
            parts.append(line)
        if data.get("桌數"):
            parts.append(f"桌數：{data['桌數']}")
        contact = " ".join(
            v for v in (data.get("聯絡人姓名"), data.get("聯絡電話")) if v
        )
        if contact:
            parts.append(f"聯絡：{contact}")
        parts.append("（已寫入 LINE新詢問 分頁）")
        _notify_admin("\n".join(parts))
    except Exception:
        logger.exception("新詢問落地失敗 user=%s", user_id)


def maybe_capture_async(user_id, history, fallback_phone=""):
    """對話出現道別時呼叫；背景執行抽取與落地。"""
    if not MACHINE_SHEET_ID or not history:
        return
    now = time.time()
    if now - _recent_leads.get(user_id, 0) < LEAD_COOLDOWN_SECONDS:
        return
    _recent_leads[user_id] = now
    threading.Thread(
        target=_worker,
        args=(user_id, list(history), fallback_phone),
        daemon=True,
    ).start()
