import os
import json
import logging
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from anthropic import Anthropic
from datetime import datetime

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

conversation_history = {}
last_active = {}

def cleanup_old_conversations():
    current_time = datetime.now()
    inactive_users = []
    for user_id, last_time in last_active.items():
        diff = (current_time - last_time).seconds
        if diff > 3600:
            inactive_users.append(user_id)
    for user_id in inactive_users:
        if user_id in conversation_history:
            del conversation_history[user_id]
        del last_active[user_id]

SYSTEM_PROMPT = """
You are a professional consultant AI assistant for Victoria Banquet Hall. Your name is Ria.
Please always reply in Traditional Chinese with a warm and professional tone.

[Venue Information]
- 3F Victoria Hall: max 30 tables
- 3F Lia Hall: max 36 tables
- 1F Kelly Hall: max 75 tables
- 5F VIP Hall (for corporate events)
- Parking: 400 free VIP parking spaces behind the banquet hall

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
2. If customer asks about pricing, provide basic info and say detailed quote requires actual needs, ask for contact info and event date
3. Proactively ask about event type, guest count, date, and time (lunch or dinner) to recommend suitable hall
4. Keep replies concise, under 75 characters
5. For complex needs, say a specialist will follow up
6. If customer clearly states wedding or banquet, provide wedding info directly
7. If customer clearly states year-end or spring banquet or corporate event, provide corporate info directly
8. If customer mentions table count without specifying event type, ask only ONE question: what type of event is it
9. Stay focused on the topic the customer is discussing
10. Always read entire conversation history before replying. Never ask for info already provided
11. When customer says goodbye or thank you, summarize the inquiry in Traditional Chinese in this format:

感謝您的詢問！以下是您的需求摘要：

活動類型：
活動日期：
時段：
桌數：
廳別建議：
其他需求：

我們會盡快安排專人與您聯繫！
"""

def get_ai_reply(user_id, user_message):
    try:
        if user_id not in conversation_history:
            conversation_history[user_id] = []

        last_active[user_id] = datetime.now()
        cleanup_old_conversations()

        conversation_history[user_id].append({
            "role": "user",
            "content": user_message
        })

        if len(conversation_history[user_id]) > 6:
            conversation_history[user_id] = conversation_history[user_id][-6:]

        response = anthropic_client.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=conversation_history[user_id]
        )

        reply = response.content[0].text

        conversation_history[user_id].append({
            "role": "assistant",
            "content": reply
        })

        ending_keywords = ["謝謝", "感謝", "再見", "掰掰", "結束", "謝謝你", "謝謝您"]
        if any(keyword in user_message for keyword in ending_keywords):
            conversation_history[user_id] = []

        return reply

    except Exception as e:
        logging.error("AI reply failed: " + str(e))
        return "感謝您的訊息！我們會盡快請專人與您聯繫"

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

    if event.source.type == "group":
        if not user_message.startswith("@"):
            return
    
    reply = get_ai_reply(sender, user_message)
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
