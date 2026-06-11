import os
import re
import json
import time
import logging
from datetime import datetime, timezone

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import anthropic
import gspread

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

MAX_HISTORY_MESSAGES = 6        # 每位使用者保留最近 6 則訊息（3 組問答）
HISTORY_TTL_SECONDS = 3600      # 超過 1 小時沒互動視為對話結束
CLAUDE_MAX_RETRIES = 3
CLAUDE_RETRY_DELAY_SECONDS = 2

conversation_history = {}
last_active = {}

# ---------------------------------------------------------------------------
# Google Sheets 持久化
# 欄位結構：A=user_id, B=history(JSON), C=updated_at(ISO 8601, UTC)
# 每個 user_id 一列；server 重啟時從 Sheets 還原未過期的對話
# ---------------------------------------------------------------------------
SHEET_HEADER = ["user_id", "history", "updated_at"]

_worksheet = None
_sheet_row_index = {}   # user_id -> 工作表列號
_sheet_next_row = 2     # 下一個可用的列號


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
            if (now - updated_at).total_seconds() > HISTORY_TTL_SECONDS:
                continue  # 過期的對話不還原
            history = json.loads(row[1]) if row[1] else []
        except (IndexError, ValueError, json.JSONDecodeError):
            logger.warning("user=%s 的 Sheets 資料格式異常，略過還原", user_id)
            continue
        if history:
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
        row = _sheet_row_index.get(user_id)
        if row is None:
            row = _sheet_next_row
            _sheet_next_row += 1
            _sheet_row_index[user_id] = row
        _worksheet.update(
            values=[[user_id, history_json, now_iso]],
            range_name=f"A{row}:C{row}",
        )
    except Exception:
        logger.exception("寫入 Google Sheets 失敗 user=%s", user_id)


def cleanup_old_conversations():
    now = datetime.now(timezone.utc)
    for user_id in list(last_active.keys()):
        if (now - last_active[user_id]).total_seconds() > HISTORY_TTL_SECONDS:
            conversation_history.pop(user_id, None)
            del last_active[user_id]


SYSTEM_PROMPT = """
You are a professional consultant AI assistant for Victoria Banquet Hall. Your name is Ria.
Please always reply in Traditional Chinese with a warm and professional tone.

[Venue Information]
- 3F Victoria Hall: max 30 tables
- 3F Lia Hall: max 36 tables
- 1F Kelly Hall: max 50 tables
- 5F VIP Hall (for corporate events)
- Parking: 400 free VIP parking spaces behind the banquet hall
- Name: Victoria Banquet Hall (維多利亞宴會館)
- Address: No. 208, Section 3, Ruiguang Road, Pingtung City (屏東市瑞光路三段208號)
- Location: Pingtung City, Taiwan (屏東市，非台北)

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
To view the full wedding menu, tell customers to type: "wedding menu"

[Year-end and Spring Banquet Menu 2026]
- Starting from NT$6,880 per table (10 persons), 3 options available
- Add NT$120 per table for unlimited orange juice and unsweetened green tea
- To view the full menu, tell customers to type: "spring banquet menu"

[Important Notes]
- Confirm menu and pricing 1 month before wedding
- Confirm table count 14 days before wedding
- Schedule MV preview 10 days before wedding
- Confirm table card names 7 days before wedding
- Guests must bring: guest book, signing book, pen, corsage, thank-you cards
- No alcohol for guests under 18

[Reply Rules]
1. Always reply in Traditional Chinese, warm and professional tone
2. If customer asks about pricing, provide basic info and say detailed quote requires actual needs. Only ask for contact info if the customer has NOT already provided it during this conversation.
3. Proactively ask about event type, guest count, date, and time (lunch or dinner) to recommend suitable hall
4. Keep replies concise, under 50 characters
5. For complex needs, say a specialist will follow up
6. If customer clearly states wedding or banquet, provide wedding info directly
7. If customer clearly states year-end or spring banquet or corporate event, provide corporate info directly
8. If customer mentions table count without specifying event type, ask only ONE question: what type of event is it
9. Stay focused on the topic the customer is discussing
10. Always read entire conversation history before replying. Never ask for info already provided
11. When customer says goodbye or thank you, summarize the inquiry in Traditional Chinese in this format:
12. Never ask for phone number or LINE ID more than once in the same conversation. If customer has already provided contact info, do not ask again.
13. This Victoria Banquet Hall is located in Pingtung City at No. 208, Section 3, Ruiguang Road. It is NOT the Victoria Banquet Hall in Taipei. Always make this clear if customer asks about location.

感謝您的詢問！以下是您的需求摘要：

活動類型：
活動日期：
時段：
桌數：
廳別建議：
其他需求：

我們會盡快安排專人與您聯繫！
"""


def trim_history(history):
    """保留最近 6 則，並確保第一則是 user（Claude API 要求）。"""
    if len(history) > MAX_HISTORY_MESSAGES:
        del history[:-MAX_HISTORY_MESSAGES]
    while history and history[0]["role"] == "assistant":
        del history[0]


def call_claude_with_retry(messages):
    """呼叫 Claude API，可重試的錯誤最多重試 3 次，每次間隔 2 秒。"""
    last_error = None
    for attempt in range(1, CLAUDE_MAX_RETRIES + 1):
        try:
            return anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=300,
                system=SYSTEM_PROMPT,
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


ENDING_KEYWORDS = ["謝謝", "感謝", "再見", "掰掰", "結束", "謝謝你", "謝謝您"]


def get_ai_reply(user_id, user_message):
    start = time.monotonic()
    try:
        cleanup_old_conversations()
        history = conversation_history.setdefault(user_id, [])
        last_active[user_id] = datetime.now(timezone.utc)

        history.append({"role": "user", "content": user_message})
        trim_history(history)

        response = call_claude_with_retry(history)
        reply = response.content[0].text

        history.append({"role": "assistant", "content": reply})
        trim_history(history)

        if any(keyword in user_message for keyword in ENDING_KEYWORDS):
            conversation_history[user_id] = []

        save_history_to_sheet(user_id)

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


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    source = event.source

    if source.type in ("group", "room"):
        if not user_message.startswith("@利亞"):
            return
        stripped = user_message[len("@利亞"):].strip()
        if stripped:
            user_message = stripped

    # 群組/聊天室中 user_id 可能拿不到，退而用群組 ID 當對話 key
    sender = (
        source.user_id
        or getattr(source, "group_id", None)
        or getattr(source, "room_id", None)
    )
    if not sender:
        return

    reply = get_ai_reply(sender, user_message)
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply),
        )
    except LineBotApiError:
        logger.exception("LINE 回覆失敗 user=%s", sender)


@app.route("/")
def index():
    return "Wedding Bot is running"


init_google_sheet()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
