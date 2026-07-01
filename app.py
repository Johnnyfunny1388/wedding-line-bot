import os
import re
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import anthropic
import gspread

import booking_sync
import availability
import lead_capture

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ria")

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))

# retry 由下方 call_claude_with_retry 自行控制（固定間隔 2 秒、最多 3 次），
# 關閉 SDK 內建的自動重試以免兩層重試疊加
anthropic_client = anthropic.Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
    max_retries=0,
)

MAX_HISTORY_MESSAGES = 12       # 每位使用者保留最近 12 則訊息（6 組問答），避免太早講過的需求被擠出視窗
HISTORY_TTL_SECONDS = 3600      # 超過 1 小時沒互動視為對話結束
MAX_PREVIOUS_USERS = 500        # 長期記憶最多保留幾位客人，避免記憶體無上限成長
CLAUDE_MAX_RETRIES = 3
CLAUDE_RETRY_DELAY_SECONDS = 2

conversation_history = {}
previous_history = {}   # 已結束（閒置逾時）的上一段對話，當作客人的長期記憶
customer_phones = {}    # user_id -> 客人在對話中留下的電話（正規化），訂席查詢用
last_active = {}

# 台灣電話格式：手機 09xx-xxx-xxx、市話 0x-xxxxxxx（含各種分隔寫法）
PHONE_PATTERN = re.compile(r"09\d{2}[\s\-]?\d{3}[\s\-]?\d{3}|0\d{1,2}[\s\-]?\d{3,4}[\s\-]?\d{4}")

# ---------------------------------------------------------------------------
# Google Sheets 持久化
# 欄位結構：A=user_id, B=history(JSON), C=updated_at(ISO 8601, UTC)
# 每個 user_id 一列；server 重啟時從 Sheets 還原未過期的對話
# ---------------------------------------------------------------------------
SHEET_HEADER = ["user_id", "history", "updated_at", "customer_phone"]

_worksheet = None
_sheet_row_index = {}   # user_id -> 工作表列號
_sheet_next_row = 2     # 下一個可用的列號
_sheet_lock = threading.Lock()  # 序列化列號配置與寫入，避免多執行緒搶到同一列互相覆蓋


def _extract_sheet_id(value):
    """允許環境變數貼完整網址或純 ID，並去除頭尾空白與引號。"""
    value = value.strip().strip("'\"")
    match = re.search(r"/d/([A-Za-z0-9_-]+)", value)
    return match.group(1) if match else value


def init_google_sheet():
    global _worksheet, _sheet_next_row
    # 同時支援新舊兩組環境變數名稱
    sheets_id = os.environ.get("GOOGLE_SHEETS_ID") or os.environ.get("SPREADSHEET_ID")
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GOOGLE_CREDENTIALS")
    if not sheets_id or not sa_json:
        logger.warning(
            "GOOGLE_SHEETS_ID/SPREADSHEET_ID 或 GOOGLE_SERVICE_ACCOUNT_JSON/GOOGLE_CREDENTIALS 未設定，"
            "對話歷史僅保存在記憶體，重啟後會遺失"
        )
        return
    try:
        sheets_id = _extract_sheet_id(sheets_id)
        sa_info = json.loads(sa_json)
        logger.info(
            "Google Sheets 使用服務帳戶：%s（此 email 必須是試算表的編輯者）",
            sa_info.get("client_email"),
        )
        logger.info("Google Sheets 目標試算表 ID：%s", sheets_id)
        gc = gspread.service_account_from_dict(sa_info)
        _worksheet = gc.open_by_key(sheets_id).sheet1
        rows = _worksheet.get_all_values()
        if not rows:
            _worksheet.append_row(SHEET_HEADER)
            rows = [SHEET_HEADER]
        elif len(rows[0]) < len(SHEET_HEADER):
            _worksheet.update(values=[SHEET_HEADER], range_name="A1:D1")
        _sheet_next_row = len(rows) + 1
        _restore_histories(rows[1:])
        logger.info(
            "Google Sheets 連線成功，還原 %d 位使用者的對話歷史",
            len(conversation_history),
        )
    except Exception:
        _worksheet = None
        logger.exception("Google Sheets 初始化失敗，改用記憶體模式")


def _restore_histories(data_rows):
    now = datetime.now(timezone.utc)
    for i, row in enumerate(data_rows, start=2):
        if not row or not row[0]:
            continue
        user_id = row[0]
        _sheet_row_index[user_id] = i
        try:
            updated_at = datetime.fromisoformat(row[2])
            history = json.loads(row[1]) if row[1] else []
        except (IndexError, ValueError, json.JSONDecodeError):
            logger.warning("user=%s 的 Sheets 資料格式異常，略過還原", user_id)
            continue
        if len(row) > 3 and row[3].strip():
            customer_phones[user_id] = row[3].strip()
        if not history:
            continue
        if (now - updated_at).total_seconds() > HISTORY_TTL_SECONDS:
            _remember_previous(user_id, history)  # 過期對話 → 長期記憶
        else:
            conversation_history[user_id] = history
            last_active[user_id] = updated_at


def save_history_to_sheet(user_id):
    """每次對話後即時把該使用者的歷史寫回 Google Sheets。

    寫入失敗只記 log，不影響回覆客人。
    """
    global _sheet_next_row
    if _worksheet is None:
        return
    history_json = json.dumps(
        conversation_history.get(user_id, []), ensure_ascii=False
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        # 整段列號配置 + 寫入一起鎖，避免兩個執行緒搶到同一列、或同一客人的
        # 兩次寫入交錯。低流量下序列化寫入的成本可忽略。
        with _sheet_lock:
            row = _sheet_row_index.get(user_id)
            if row is None:
                row = _sheet_next_row
                _sheet_next_row += 1
                _sheet_row_index[user_id] = row
            _worksheet.update(
                values=[[user_id, history_json, now_iso, customer_phones.get(user_id, "")]],
                range_name=f"A{row}:D{row}",
            )
    except Exception:
        logger.exception("寫入 Google Sheets 失敗 user=%s", user_id)


def _remember_previous(user_id, history):
    """把一段已結束的對話存進長期記憶，並維持總筆數上限。

    用 dict 的插入順序當作 LRU：重新寫入時先移除舊鍵讓它排到最後，
    超過上限時淘汰最早寫入（最久沒更新）的客人。
    """
    previous_history.pop(user_id, None)
    previous_history[user_id] = history
    while len(previous_history) > MAX_PREVIOUS_USERS:
        oldest = next(iter(previous_history))
        del previous_history[oldest]


def cleanup_old_conversations():
    """閒置逾時的對話不再丟棄，改轉入 previous_history 當長期記憶。"""
    now = datetime.now(timezone.utc)
    for user_id in list(last_active.keys()):
        if (now - last_active[user_id]).total_seconds() > HISTORY_TTL_SECONDS:
            history = conversation_history.pop(user_id, None)
            if history:
                _remember_previous(user_id, history)
            del last_active[user_id]


SYSTEM_PROMPT = """
You are a professional consultant AI assistant for Victoria Banquet Hall. Your name is Ria.
Please always reply in Traditional Chinese with a warm and professional tone.

[Venue Information]
- 3F Victoria Hall: max 30 tables
- 3F Lia Hall: max 36 tables
- 1F Kelly Hall: max 75 tables
- 5F VIP Hall: max 8 tables (for corporate events)
- Parking: 400 free VIP parking spaces behind the banquet hall
- Name: Victoria Banquet Hall (維多利亞宴會館)
- Address: No. 208, Section 3, Ruiguang Road, Pingtung City (屏東市瑞光路三段208號)
- Location: Pingtung City, Taiwan (屏東市，非台北)
- Booking/contact phone (訂席專線): (08)735-1388 — provide this when customers ask how to book or for a phone number
- Business hours (營業時間): normally from 11:00 AM (一般營業時間為上午11點起). However, the venue can often open earlier to accommodate an event's setup/schedule (可配合活動需求提早開館). When asked about opening time: say normal hours start at 11:00 AM, and that earlier access can usually be arranged depending on the event — a specialist will confirm the exact time. Never state an earlier fixed time (such as 10:00) as if it were standard.

[Services]
- Wedding banquets, Year-end/Spring banquets, Large dinners, Private room family banquets, Venue rental, Teacher appreciation banquets

[Wedding Package]
Included amenities:
- 1F 530-inch and 3F 320-inch LED video walls
- 200-inch projection screens (both sides of hall)
- Free VIP parking, venue map provided
- Directional signs and seating chart
- Welcome and farewell candies
- Love champagne tower or cake tower
- Decorative table flowers and aisle flower columns
- Wedding menu cards and table card setup
- One bottle of red wine per table
- Unlimited bottled orange juice and unsweetened tea
- Corkage fee NT$200 per table for outside alcohol

Bride exclusive services:
- Delicate snacks, personal butler for couple
- Bridal lounge (mandatory)
- Professional wedding planner (not MC or host)

Add-on services:
- Host/band: NT$8,800+
- Wedding consultant: NT$10,000
- Wedding decoration: NT$12,000
- Wedding photography: NT$22,000+
- Wedding videography: NT$22,000+

Wedding menu: starting from NT$13,800 per table
To view the full wedding menu, tell customers to type: "喜宴菜單"

[Teacher Appreciation Banquet (謝師宴) Menu]
- Standard menu: NT$5,880 per table
- Dishes: 蔓越莓亞麻子麵包、牛肝菌蘑菇濃湯、蘿蔓龍蝦沙拉、酥烤德國豬腳、櫻花蝦米糕圓、蝦醬空心菜、酸菜魚片鍋、流沙奶皇包、明果風味甜筒
- Each table includes 2 bottles of orange juice and 2 bottles of green tea (每席贈柳橙、綠茶各兩瓶)
- Booking line: (08)735-1388
- Actual menu and pricing may have exceptions depending on requirements; a specialist will confirm

[Year-end and Spring Banquet Menu 2026]
- Standard menu starting from NT$6,880 per table (10 persons), 3 options available
- NOTE: exceptions to the standard menu/pricing are common — present NT$6,880 as the standard starting price, and add that actual pricing depends on requirements and will be confirmed by a specialist
- Add NT$120 per table for unlimited orange juice and unsweetened green tea
- To view the full menu, tell customers to type: "春酒菜單"

[Important Notes]
- Confirm menu and pricing 1 month before wedding
- Confirm table count 14 days before wedding
- Schedule MV preview 10 days before wedding
- Confirm table card names 7 days before wedding
- Guests must bring: guest book, signing book, pen, corsage, thank-you cards
- No alcohol for guests under 18

[MOST IMPORTANT — Read history first]
Before EVERY reply, read the ENTIRE conversation history above and note what the customer has ALREADY told you: name, phone, event type, date, time slot (lunch/dinner), table count, special needs. Reuse that information directly. NEVER ask again for anything the customer has already provided — re-asking makes us look unprofessional. Only ask about the items that are still genuinely missing. If you are about to ask a question, first scan the history to confirm the answer is not already there.

[Human handoff detection — STAY SILENT when a colleague is handling it]
A human colleague may be handling this customer in parallel via manual chat, and the colleague's messages do NOT appear in your history. Detect this and stay out of the way:

STRONG handoff signal → output EXACTLY the token [靜默] and NOTHING else (no greeting, no explanation). Signs of a strong signal: the customer's message clearly responds to something that is NOT in your conversation history — answering a question you never asked, agreeing to or choosing something you never offered ("那娃娃菜好了", "就選第二個", "好,那就這樣"), confirming an arrangement/price/date/dish you have no record of, or continuing a topic you never started. In these cases a colleague is mid-conversation; you must say nothing so you don't interrupt them.

When you are NOT sure it is a handoff but the customer references something you cannot see (a dish, arrangement, name, decision), do NOT deny or correct it — respond gently and defer: e.g.「好的,這部分我為您確認後再回覆/由專人為您處理喔！」.

Reply normally only when the message is something you genuinely can serve from this prompt or your own history (greetings, venue/menu questions, availability, the customer's own booking lookup). Only state facts explicitly in this prompt or your history. When truly in doubt between replying and staying silent, prefer [靜默] — a brief silence is far better than interrupting a colleague with wrong information.

[Reply Rules]
1. Always reply in Traditional Chinese, warm and professional tone
2. If customer asks about pricing, provide basic info and say detailed quote requires actual needs. Only ask for contact info if the customer has NOT already provided it during this conversation.
3. Proactively ask about event type, guest count, date, and time (lunch or dinner) to recommend suitable hall — BUT only for items not already mentioned in the conversation. Never re-ask something already answered.
4. Keep replies concise, under 50 characters
5. For complex needs, say a specialist will follow up
6. If customer clearly states wedding or banquet, provide wedding info directly
7. If customer clearly states year-end or spring banquet or corporate event, provide corporate info directly
8. If customer mentions table count without specifying event type, ask only ONE question: what type of event is it
9. Stay focused on the topic the customer is discussing
10. Always read entire conversation history before replying. Never ask for info already provided
11. When customer says goodbye or thank you, summarize the inquiry in Traditional Chinese using the 【需求摘要】 format shown at the bottom of this prompt. Fill in every field you can from the conversation; leave a field blank only if it was never mentioned.
12. Never ask for phone number or LINE ID more than once in the same conversation. If customer has already provided contact info, do not ask again.
13. This Victoria Banquet Hall is located in Pingtung City at No. 208, Section 3, Ruiguang Road. It is NOT the Victoria Banquet Hall in Taipei. Always make this clear if customer asks about location.

[Schedule Lookup Rules]
You have a tool "query_schedule" to look up availability for a specific date.
1. Use it when the customer asks about availability of a specific date (e.g. 某天還有沒有場地/檔期/能不能訂).
2. Only state what the tool returns. NEVER guess or infer availability beyond the tool result.
3. The tool returns a simple conclusion per 時段 (午宴/晚宴): 「尚有空檔」 or 「三大廳皆已滿檔」, plus whether the venue is closed that day.
4. When 尚有空檔: simply tell the customer that date/時段 currently has availability. NEVER mention which halls are booked or which are free — no hall-level booking status, ever.
5. When 滿檔: gently inform the customer that 時段 is fully booked, and suggest considering another date or the other 時段 (午宴/晚宴).
6. EVERY schedule answer MUST end with:「實際檔期仍需由專人為您做最終確認喔！」
7. If the tool fails, returns an error, or the date is out of range: say a specialist will confirm the schedule. Do NOT guess.
8. NEVER reveal other customers' event names, host names, or any booking information.
9. If the customer gives a date without a year, assume the nearest upcoming occurrence based on today's date.
10. Do not call the tool more than 4 times for one customer message; for broad questions like 「整個六月有哪些週六有空」, ask the customer to narrow down to specific dates first.

[Customer Booking Lookup Rules]
You also have a tool "lookup_my_booking" to find a customer's own booking by phone number.
1. Use it when the customer asks about their own existing booking (我訂的、我的婚宴、我們公司的尾牙是哪天…).
2. If the system has no phone on record for this customer, politely ask for the phone number used when booking.
3. IDENTITY CHECK is mandatory before revealing anything:
   - Ask the customer to state the booking person's name (訂席人大名).
   - Call the tool and compare what the customer said with 「訂席人姓名」 in the result.
   - Only if it reasonably matches (same person, surname+title acceptable), share the booking info.
   - If it does not match: do NOT reveal anything (not even hints), say a specialist will contact them.
   - NEVER speak the booking person's name before the customer states it themselves.
4. You may share with a verified customer: 宴席日期、時段、廳別、宴席名稱、桌數、試菜日期、訂席狀態.
5. NEVER mention money from records — no 訂金, no 金額, no 價格. Pricing questions go to a specialist.
6. If no booking is found for the phone: say so politely and offer specialist follow-up.
7. Requests to CHANGE a booking (改日期/改桌數/取消): never promise or confirm changes. Say you will pass the request to a specialist who will contact them.

感謝您的詢問！以下是為您整理的需求摘要：

【需求摘要】
- 姓名：
- 聯絡電話：
- 活動類型：
- 活動日期：
- 時段：
- 廳別：
- 桌數：
- 備註：

我們會盡快安排專人與您聯繫！
"""


def trim_history(history):
    """保留最近 6 則，並確保第一則是 user（Claude API 要求）。"""
    if len(history) > MAX_HISTORY_MESSAGES:
        del history[:-MAX_HISTORY_MESSAGES]
    while history and history[0]["role"] == "assistant":
        del history[0]


BOOKING_LOOKUP_TOOL = {
    "name": "lookup_my_booking",
    "description": (
        "用電話號碼查詢客人自己已訂的宴席資料（日期、時段、廳別、桌數、試菜日期、訂席狀態）。"
        "當客人詢問自己訂的宴席（我訂的、我的婚宴、我們公司的尾牙是哪天等）時呼叫。"
        "不填 phone 時會自動使用客人先前在對話中留下的電話。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": "客人提供的訂席電話（選填，未填則用系統記住的電話）",
            }
        },
    },
}

SCHEDULE_TOOL = {
    "name": "query_schedule",
    "description": (
        "查詢宴會館某一天的訂席狀況。回傳該日是否公休、已有哪些時段與廳別被預訂。"
        "當客人詢問特定日期是否還有場地、檔期、能否訂位時呼叫。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "date": {
                "type": "string",
                "description": "要查詢的日期，格式 YYYY-MM-DD，例如 2026-06-13",
            }
        },
        "required": ["date"],
    },
}


def _today_line():
    now = datetime.now(timezone(timedelta(hours=8)))
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return f"\n[Today]\n今天日期（台灣）：{now.strftime('%Y-%m-%d')}（星期{weekdays[now.weekday()]}）"


def _serialize_content(content):
    """把 SDK 回傳的內容區塊轉成可存史、可回傳的純 dict。"""
    blocks = []
    for block in content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
    return blocks


def _execute_tool(name, tool_input, user_id):
    if name == "query_schedule":
        return availability.query_date(tool_input.get("date", ""))
    if name == "lookup_my_booking":
        phone = tool_input.get("phone") or customer_phones.get(user_id, "")
        if not phone:
            return {
                "錯誤": "目前沒有這位客人的電話紀錄，請先詢問客人訂席時留的電話號碼"
            }
        return availability.lookup_bookings_by_phone(phone)
    return {"錯誤": f"未知的工具：{name}"}


def _previous_context_note(user_id):
    """客人先前對話的長期記憶，附加進 system prompt。"""
    prev = previous_history.get(user_id)
    if not prev:
        return ""
    lines = []
    for m in prev[-MAX_HISTORY_MESSAGES:]:
        speaker = "客人" if m.get("role") == "user" else "利亞"
        content = str(m.get("content", ""))[:300]
        lines.append(f"{speaker}：{content}")
    return (
        "\n[Previous Inquiry Context]\n"
        "此客人先前曾詢問過，以下是上次對話的最後紀錄（供延續服務參考）：\n"
        + "\n".join(lines)
        + "\n\nRules for using this context:\n"
        "1. NEVER tell the customer you have no record of previous conversations.\n"
        "2. Do NOT ask again for information already provided above (name, phone, "
        "event type, date, table count).\n"
        "3. If the customer continues the previous topic, continue naturally, "
        "e.g. acknowledge their earlier inquiry.\n"
        "4. If the customer starts a brand-new topic, just serve them normally."
    )


def _known_customer_note(user_id):
    """對話開場自動辨識老客人：用 line_user_id 查訂席，查不到再用電話備援。

    保守（A 方案）：帶入已知資訊避免重複詢問，但不主動報出敏感細節。
    """
    bookings = availability.lookup_bookings_by_line_id(user_id)
    matched_by = "line_id"
    if not bookings:
        phone = customer_phones.get(user_id, "")
        if phone:
            result = availability.lookup_bookings_by_phone(phone)
            if isinstance(result, dict) and result.get("找到筆數"):
                bookings = result.get("訂席資料")
                matched_by = "phone"
    if not bookings:
        return ""

    logger.info(
        "辨識老客人 user=%s matched_by=%s 筆數=%d", user_id, matched_by, len(bookings)
    )
    lines = []
    for b in bookings:
        lines.append(
            f"- 訂席人：{b.get('訂席人姓名','') or '（未填）'}、宴席日期：{b.get('宴席日期','')} "
            f"{b.get('時段','')}、廳別：{b.get('廳別','')}、桌數：{b.get('桌數','')}、"
            f"宴席名稱：{b.get('宴席名稱','')}、狀態：{b.get('訂席狀態','')}"
        )
    return (
        "\n[Known Customer — THIS customer is ALREADY identified by their LINE account]\n"
        "This customer's LINE account is linked to the booking(s) below. Their identity is "
        "ALREADY confirmed by the LINE account itself. This OVERRIDES the [Customer Booking "
        "Lookup Rules]: you must NOT ask for their phone number or their name to verify "
        "identity — that would be redundant and annoying. Do not call lookup_my_booking for "
        "them; the data is already here:\n"
        + "\n".join(lines)
        + "\n\nRules:\n"
        "1. When the customer asks about their booking (我的訂席/我訂的...), directly and warmly "
        "tell them the 宴席日期 / 時段 / 廳別 / 桌數 from above. Do NOT ask for phone or name first.\n"
        "2. Never re-ask information already known above.\n"
        "3. Never reveal money/deposit/pricing. For changes, cancellations, menu or "
        "arrangement details, say a specialist will follow up — never confirm changes yourself.\n"
        "4. If the customer's message looks like a reply to a human colleague (handoff "
        "signal), the [靜默] rule still applies — stay silent."
    )


def call_claude_with_retry(messages, system_extra=""):
    """呼叫 Claude API，可重試的錯誤最多重試 3 次，每次間隔 2 秒。"""
    last_error = None
    for attempt in range(1, CLAUDE_MAX_RETRIES + 1):
        try:
            return anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                system=SYSTEM_PROMPT + _today_line() + system_extra,
                tools=[SCHEDULE_TOOL, BOOKING_LOOKUP_TOOL],
                messages=messages,
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError) as e:
            last_error = e
        except anthropic.APIStatusError as e:
            if e.status_code < 500:
                raise  # 4xx 是請求本身的問題，重試也不會成功
            last_error = e
        logger.warning(
            "Claude API 第 %d/%d 次呼叫失敗：%s",
            attempt, CLAUDE_MAX_RETRIES, last_error,
        )
        if attempt < CLAUDE_MAX_RETRIES:
            time.sleep(CLAUDE_RETRY_DELAY_SECONDS)
    raise last_error


# 利亞判定有真人專員接手時，模型會輸出這個 token，程式據此「完全不回覆」客人
SILENT_TOKEN = "[靜默]"


def get_ai_reply(user_id, user_message):
    """回傳要發給客人的文字；若回傳 None 代表判定真人接手，保持安靜不回覆。"""
    start = time.monotonic()
    try:
        cleanup_old_conversations()
        history = conversation_history.setdefault(user_id, [])
        last_active[user_id] = datetime.now(timezone.utc)

        # 自動記住客人在訊息中留下的電話（訂席查詢的身分橋樑）
        phone_match = PHONE_PATTERN.search(user_message)
        if phone_match:
            digits = re.sub(r"\D", "", phone_match.group())
            if len(digits) >= 9:
                customer_phones[user_id] = digits
                logger.info("記錄客人電話 user=%s phone=%s***", user_id, digits[:4])

        history.append({"role": "user", "content": user_message})
        trim_history(history)

        # 工具回合（查檔期）用獨立的工作清單，只把最終文字回覆存進對話歷史
        system_extra = _previous_context_note(user_id) + _known_customer_note(user_id)
        working_messages = list(history)
        response = call_claude_with_retry(working_messages, system_extra)
        tool_rounds = 0
        while response.stop_reason == "tool_use" and tool_rounds < 3:
            tool_rounds += 1
            working_messages.append(
                {"role": "assistant", "content": _serialize_content(response.content)}
            )
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                logger.info(
                    "工具呼叫 user=%s tool=%s input=%s",
                    user_id, block.name, json.dumps(block.input, ensure_ascii=False),
                )
                try:
                    result = _execute_tool(block.name, block.input, user_id)
                except Exception:
                    logger.exception("工具執行失敗 tool=%s", block.name)
                    result = {"錯誤": "查詢系統暫時無法使用，請改請專人確認檔期"}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
            working_messages.append({"role": "user", "content": tool_results})
            response = call_claude_with_retry(working_messages, system_extra)

        reply = next(
            (block.text for block in response.content if block.type == "text"),
            "感謝您的詢問！詳細資訊由專人為您確認",
        )

        # 判定真人專員接手 → 保持安靜，不回覆客人，也不在歷史留下助理訊息
        if SILENT_TOKEN in reply:
            save_history_to_sheet(user_id)  # 仍記錄客人這則訊息
            logger.info(
                "利亞判定真人接手，保持安靜 user=%s msg_len=%d elapsed=%.2fs",
                user_id, len(user_message), time.monotonic() - start,
            )
            return None

        history.append({"role": "assistant", "content": reply})
        trim_history(history)

        save_history_to_sheet(user_id)

        # 客人道別 → 背景抽取需求資訊，寫入「LINE新詢問」並通知管理者
        if any(k in user_message for k in lead_capture.ENDING_KEYWORDS):
            lead_capture.maybe_capture_async(
                user_id, history, customer_phones.get(user_id, "")
            )

        logger.info(
            "user=%s msg_len=%d reply_len=%d elapsed=%.2fs",
            user_id, len(user_message), len(reply), time.monotonic() - start,
        )
        return reply + "\n\n— 以上由AI助理利亞回覆 👩‍💼"

    except Exception:
        logger.exception(
            "AI 回覆失敗 user=%s msg_len=%d elapsed=%.2fs",
            user_id, len(user_message), time.monotonic() - start,
        )
        return "感謝您的訊息！我們會盡快請專人與您聯繫"


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400)
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.warning("LINE 簽章驗證失敗")
        abort(400)
    return "OK"


def _respond_in_background(sender, user_message, reply_token):
    """背景產生 AI 回覆並回傳給客人。

    AI 回覆可能較慢（多輪工具呼叫 + 重試），若放在 webhook 內同步處理，
    容易撐爆 LINE 的 reply token 時效，導致客人收不到回覆。
    這裡先嘗試 reply（免費額度），逾時或失敗再退回 push（會計入推播額度）。
    """
    reply = get_ai_reply(sender, user_message)
    if reply is None:
        return  # 判定真人專員接手，保持安靜不回覆
    message = TextSendMessage(text=reply)
    try:
        line_bot_api.reply_message(reply_token, message)
    except LineBotApiError as e:
        logger.warning("reply 失敗，改用 push 補送 user=%s：%s", sender, e)
        try:
            line_bot_api.push_message(sender, message)
        except LineBotApiError:
            logger.exception("push 也失敗，客人未收到回覆 user=%s", sender)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    source = event.source

    if source.type in ("group", "room"):
        if not user_message.startswith("@利亞"):
            return
        stripped = user_message[len("@利亞"):].strip()
        if not stripped:
            return  # 只 tag 利亞但沒帶問題，無內容可回覆
        user_message = stripped

    # 群組/聊天室中 user_id 可能拿不到，退而用群組 ID 當對話 key
    sender = (
        source.user_id
        or getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
    )
    if not sender:
        return

    # 管理者指令（回覆很快，維持同步處理即可）
    if booking_sync.ADMIN_LINE_USER_ID and sender == booking_sync.ADMIN_LINE_USER_ID:
        command = user_message.strip()
        if command in ("同步", "sync"):
            logger.info("管理者觸發手動同步 user=%s", sender)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="收到，開始同步訂席總表，完成後通知您…"),
            )
            booking_sync.run_sync_async(sender)
            return
        if command in ("狀態", "status"):
            logger.info("管理者查詢系統狀態 user=%s", sender)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=booking_sync.status_report()),
            )
            return
        if command in ("推播測試", "push test"):
            logger.info("管理者觸發推播測試 user=%s", sender)
            line_bot_api.reply_message(
                event.reply_token, TextSendMessage(text="3 秒後測試主動推播…")
            )
            booking_sync.push_test_async(sender)
            return

    # AI 回覆移到背景處理，webhook 立即回 200，避免阻塞與 reply token 逾時
    threading.Thread(
        target=_respond_in_background,
        args=(sender, user_message, event.reply_token),
        daemon=True,
    ).start()


@app.route("/")
def index():
    return "Wedding Bot is running"


# 注意：對話歷史、長期記憶、排程都存在「單一進程」的記憶體裡，
# 因此必須以單 worker 啟動（見 Procfile 的 --workers 1）。
# 若改成多 worker，各進程會各自跑一份排程、且狀態不共享，造成重複同步與資料不一致。
init_google_sheet()
booking_sync.start_scheduler()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
