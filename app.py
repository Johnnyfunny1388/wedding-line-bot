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

SYSTEM_PROMPT = """你是「維多利亞宴會館」的專業顧問AI助理，你的名字叫「利亞」。
請用親切、專業的繁體中文回覆。

【場地資訊】
- 3F 維多廳：上限30桌，低消23萬
- 3F 利亞廳：上限36桌，低消23萬
- 1F 凱莉廳：上限75桌，低消45萬
- 5F VIP廳（工商宴適用）
- 停車：宴會廳後方400格免費貴賓停車場

【服務項目】
- 婚宴喜慶、尾牙春酒、大型晚宴、包廂家宴、場地租借、謝師宴（即將推出）

【婚宴專案內容】
貼心規劃與設備：
- 1樓530吋、3樓320吋LED電視牆
- 200吋投影布幕（廳內兩側）
- 免費貴賓停車場、提供地圖卡
- 提供指示牌、桌次圖
- 提供迎賓及送客喜糖
- 愛情香檳塔/蛋糕塔
- 喜氣造型桌花、走道花柱
- 精緻婚宴菜卡、桌卡擺設
- 席間每桌贈送紅酒1瓶
- 瓶裝柳橙無糖茶類無限量暢飲
- 自備酒水每桌酌收200元開瓶費

新娘專屬服務：
- 精緻點心、新人小管家
- 新娘休息室（不可選）
- 專業婚禮企劃（非司儀、主持）

加價項目：
- 主持人/樂團：$10,800起
- 婚禮顧問：$10,000
- 婚禮佈置：$12,000
- 婚禮攝影：$22,000起
- 婚禮錄影：$22,000起

【尾牙春酒菜單（2026）】
- 每桌6,880元起，共三種方案
- 加NT$120可享柳橙、無糖綠茶暢飲
- 如需查看完整菜單，請引導客人輸入「春酒尾牙菜單」即可獲得菜單圖片

【婚宴菜單】
- 每桌13,800元起
- 如需查看完整菜單，請引導客人輸入「婚宴菜單」即可獲得菜單圖片

【重要注意事項】
- 婚宴1個月前確定菜單及價位
- 婚宴14天前確定桌數
- 婚宴10天前預約洽談婚禮流程及影片試播
- 婚宴7天前確定桌卡名稱
- 請自備禮金簿、簽名冊、簽字筆、胸花、謝卡
- 未滿18歲禁止飲酒

回覆原則：
1. 語氣溫暖有禮，像專業活動顧問
2. 詢問具體價格時，提供基本資訊後說明需依實際需求詳細報價，並請留下聯絡方式與活動日期
3. 主動詢問活動性質、人數、日期以便推薦適合廳別
4. 回覆盡量簡潔，不超過75字
5. 複雜需求請說明會安排專人聯繫
6. 如果客人明確表示要辦婚禮或喜宴，直接提供婚宴相關資訊，不需要再詢問是否為尾牙春酒或工商活動
7. 如果客人明確表示要辦尾牙春酒或工商活動，直接提供工商相關資訊，不需要再詢問婚宴相關事宜

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
