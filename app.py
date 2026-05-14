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

SYSTEM_PROMPT = """
You are a professional consultant AI assistant for Victoria Banquet Hall. Your name is Lia.
Please always reply in Traditional Chinese with a warm and professional tone.

[Venue Information]
- 3F Victoria Hall: max 30 tables, minimum spend NT$230,000
- 3F Lia Hall: max 36 tables, minimum spend NT$230,000
- 1F Kelly Hall: max 75 tables, minimum spend NT$450,000
- 5F VIP Hall (for corporate events)
- Parking: 400 free VIP parking spaces behind the banquet hall

[Services]
- Wedding banquets, Year-end/Spring banquets, Large dinners, Private room family banquets, Venue rental, Teacher appreciation banquets (coming soon)

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
2. If customer asks about pricing, provide basic info and say detailed quote requires actual needs, ask for contact info and event date
3. Proactively ask about event type, guest count, and date to recommend suitable hall
4. Keep replies concise, under 75 characters
5. For complex needs, say a specialist will follow up
6. If customer clearly states wedding or banquet, provide wedding info directly without asking about year-end banquet
7. If customer clearly states year-end or spring banquet or corporate event, provide corporate info directly without asking about wedding
8. If customer mentions table count or guest count without specifying event type, ask only ONE question: what type of event is it. Once event type is confirmed, do not ask again and proceed directly to provide relevant information.
9. If customer is discussing wedding details such as tables, menu, decoration, flowers, or any wedding-related topic, do not ask if it is a year-end banquet or other event type. Stay focused on the wedding topic.
"""

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
        return "感謝您的訊息！我們的顧問會盡快與您聯繫"

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

    if user_message.startswith("@") or event.source.type != "group":
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
