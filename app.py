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

SYSTEM_PROMPT = """你是「幸福計畫婚禮顧問」的專業AI助理，負責回答客戶關於婚禮服務的問題。
請用親切、專業的繁體中文回覆。

我們的服務包括：
- 婚禮統籌規劃
- 客製化婚宴菜單
- 場地佈置與花藝
- 婚禮顧問諮詢

回覆原則：
1. 語氣溫暖有禮，像專業婚禮顧問
2. 如果客人詢問具體價格，請說明需要依實際需求報價，並請他們留下聯絡方式
3. 如果問題超出婚禮服務範圍，婉拒回答並引導回婚禮相關話題
4. 回覆盡量簡潔，不超過150字"""

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
            range="Sheet1!A:E",
            valueInputOption="RAW",
            body={"values": values}
        ).execute()
    except Exception as e:
        logging.error(f"試算表寫入失敗: {e}")

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
        logging.error(f"AI回覆失敗: {e}")
        return "感謝您的訊息！我們的顧問會盡快與您聯繫 💒"

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
    timestamp = datetime.now().strftime("%Y-%
