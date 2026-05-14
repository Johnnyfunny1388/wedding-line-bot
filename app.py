import os
import json
import logging
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from anthropic import Anthropic
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")

SYSTEM_PROMPT = """你是「維多利亞宴會館」的專業顧問AI助理『利亞』，負責回答客戶關於場地及餐飲服務的問題。
請用親切、專業的繁體中文回覆。

我們的服務包括：
- 婚宴喜慶：婚禮統籌規劃、客製化婚宴菜單、場地佈置與花藝
- 尾牙春酒：企業年終聚餐、春酒活動規劃
- 大型晚宴：企業活動、頒獎典禮、大型聚會
- 包廂家宴：私人包廂、家庭聚餐、小型聚會
- 場地租借：會議、發表會、各類活動場地

回覆原則：
1. 語氣溫暖有禮，像專業活動顧問
2. 如果客人詢問具體價格，請說明需要依實際需求報價，並請留下聯絡方式與活動日期
3. 主動詢問活動性質、人數、日期等資訊以便提供更精準的建議
4. 回覆盡量簡潔，不超過75字"""

def get_sheets_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def log_to_sheets(timestamp, group_name, sender, message, msg_type):
    try:
        service = get_sheets_service()
        values = [[timestamp, group_name, sender, message, msg_type]]
        service.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="A:E",
            valueInputOption="RAW",
            body={"values": values}
        ).execute()
    except Exception as e:
        logging.error("Sheet write failed: " + str(e))

def get_ai_reply(user_message):
    try:
        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}]
        )
        return response.content[0].text
    except Exception as e:
        logging.error("AI reply failed: " + str(e))
        return "感謝您的訊息！我們的專員會盡快與您聯繫"

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text
    sender = event.source.user_id
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if event.source.type == "group":
        group_id = event.source.group_id
        try:
            group_summary = line_bot_api.get_group_summary(group_id)
            group_name = group_summary.group_name
        except Exception:
            group_name = group_id
        try:
            profile = line_bot_api.get_group_member_profile(group_id, sender)
            sender_name = profile.display_name
        except Exception:
            sender_name = sender
    else:
        group_name = "personal"
        try:
            profile = line_bot_api.get_profile(sender)
            sender_name = profile.display_name
        except Exception:
            sender_name = sender

    log_to_sheets(timestamp, group_name, sender_name, user_message, "text")

    if user_message.startswith("@利亞") or event.source.type != "group":
        reply = get_ai_reply(user_message)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )

@app.route("/")
def index():
    return "Wedding Bot is running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
