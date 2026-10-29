import os
import io
from flask import Flask, request, abort
from google import genai
from google.genai import types
import google.genai.errors

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent
)

app = Flask(__name__)

# 1. 先讀取環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 2. 先初始化 Client 與 LINE Handler（必須在所有 @handler.add 之前！）
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-3.8-flash"

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 系統 AI 提示詞設定
SYSTEM_PROMPT = """
你是一位嚴格但貼心的專業飲食控制與減脂教練。
請針對使用者發送的食物照片或飲食文字，進行精準的飲食評估。

請用繁體中文，並包含以下結構輸出：
1. 🍱 估計餐點內容與熱量：簡述辨識出的食物，並估算熱量（大卡）與三大營養素比例（蛋白質、碳水、脂肪）。
2. 🚦 飲控適合度評級：
   - 🟢 綠燈（極力推薦 / 適合減脂高蛋白）
   - 🟡 黃燈（可以食用 / 需注意控制份量或油脂）
   - 🔴 紅燈（不建議 / 高油高糖高加工，建議避開或偶爾解饞）
3. 💡 教練調整建議：給予 1~2 句具體的飲食微調建議（例如：少喝湯、去掉雞皮、白飯吃半碗、增加蔬菜等）。

語氣請保持專業、簡潔、口語化，適合在社群群組內快速閱讀。
"""

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((google.genai.errors.ServerError, Exception)),
    reraise=True
)
def call_gemini_with_retry(contents):
    return client.models.generate_content(
        model=MODEL_ID,
        contents=contents
    )

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/")
def home():
    return "Diet Bot is lightweight and running!"

# 3. 確保 handler 已經宣告好之後，才能使用裝飾器
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    if not user_text:
        return

    # 嚴格過濾：必須「包含特定關鍵字」或者「訊息以 @ 開頭（代表標記/呼叫）」才觸發
    keywords = ["飲食", "減脂", "熱量", "熱量估估", "能不能吃", "可以吃嗎", "幫我看", "審查", "飲控", "卡路里"]
    is_keyword_triggered = any(kw in user_text for kw in keywords)
    is_tagged = user_text.startswith("@") or "@" in user_text

    # 如果既沒有關鍵字，也沒有標記，直接默默跳過不回應！
    if not is_keyword_triggered and not is_tagged:
        return

    full_prompt = f"{SYSTEM_PROMPT}\n\n使用者詢問的飲食內容如下：\n{user_text}"
    
    reply_text = ""
    try:
        response = call_gemini_with_retry(full_prompt)
        reply_text = response.text
    except Exception as e:
        reply_text = "⚠️ 目前 Google AI 伺服器流量較大（503 忙碌中），請稍候 5 秒鐘後再傳一次給我！"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id

    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        image_bytes = line_bot_blob_api.get_message_content(message_id)

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type='image/jpeg',
    )

    reply_text = ""
    try:
        response = call_gemini_with_retry([SYSTEM_PROMPT, image_part])
        reply_text = response.text
    except Exception as e:
        reply_text = "⚠️ 目前圖片分析流量較大（503 忙碌中），請稍後 5 秒再傳一次照片即可！"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
