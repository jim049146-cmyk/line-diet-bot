import os
import io
import threading
import requests
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
    PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    ImageMessageContent
)
import yfinance as yf

# ⚠️ 關鍵：宣告 Flask 應用程式物件，讓 Gunicorn 找得到 'app'
app = Flask(__name__)

# 讀取環境變數
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 初始化新版 Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY)
MODEL_ID = "gemini-3.5-flash-lite"

# 初始化 LINE SDK 與 handler
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 系統 AI 提示詞設定 (飲食教練)
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

# ----------------------------------------------------
# 路由：防止 Render 雲端主機冷啟動休眠 (Health Check)
# ----------------------------------------------------
@app.route("/health", methods=['GET'])
def health_check():
    return "Bot is alive and healthy!", 200

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
    return "Diet & Stock Bot is running with Smart Image & Valuation Modules!"

# ----------------------------------------------------
# 核心功能：台股價值評估模組 (含 TTM EPS 備援機制)
# ----------------------------------------------------
import requests

def analyze_stock_value(ticker_symbol: str) -> str:
    clean_ticker = ticker_symbol.strip().upper()
    
    try:
        # 1. 抓取證交所上市股票本益比與收盤價公開資料 (BWIBBU_ALL)
        url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
        response = requests.get(url, timeout=10)
        
        stock_data = None
        if response.status_code == 200:
            all_stocks = response.json()
            # 在清單中尋找對應的代號
            for item in all_stocks:
                if item.get("Code") == clean_ticker:
                    stock_data = item
                    break
        
        # 如果上市找不到，可以去櫃買中心(上櫃)找，這邊先以上市為例
        if not stock_data:
            return f"⚠️ 找不到股票代號 [{clean_ticker}] 的官方數據，請確認代號是否正確（目前支援上市股票）。"
        
        # 2. 從官方資料解析數據
        # 證交所欄位名稱通常為：Code, Name, PE (本益比), Dividend, Yield 等
        # 註：有些股票本益比為「發布不適用」或負數會顯示為 '-' 或 '-'
        current_price = float(stock_data.get("ClosePrice", 0) or stock_data.get("MonthlyAveragePrice", 0))
        pe_str = stock_data.get("PE", "").strip()
        
        if not pe_str or pe_str == "-" or pe_str == "0":
            return f"⚠️ [{clean_ticker}] 目前本益比數據為負或不適用（無本益比資料），無法進行估價。"
            
        pe_ratio = float(pe_str)
        
        # 3. 自動反推 EPS (EPS = 股價 / 本益比)
        est_annual_eps = round(current_price / pe_ratio, 2)
        
        if est_annual_eps <= 0:
            return f"⚠️ [{clean_ticker}] 計算出的 EPS 異常，不適用本益比估價法。"

        # 4. 設定歷史本益比基準乘數 (低 15x / 均 22x / 高 28x)
        low_pe = 15.0
        avg_pe = 22.0
        high_pe = 28.0
        
        green_price = round(est_annual_eps * low_pe, 1)   # 🟢 綠燈 (特價區)
        yellow_price = round(est_annual_eps * avg_pe, 1)  # 🟡 黃燈 (合理區)
        red_price = round(est_annual_eps * high_pe, 1)    # 🔴 紅燈 (昂貴區)
        
        if current_price <= green_price:
            status_light = "🟢 綠燈（特價區 / 安全邊際）"
            action_advice = "現價低於安全邊際，具備較高的價值投資買點！"
        elif current_price <= yellow_price:
            status_light = "🟡 黃燈（合理區 / 定期定額）"
            action_advice = "現價處於合理評價區，適合分批定期定額長線佈局。"
        else:
            status_light = "🔴 紅燈（昂貴區 / 居高思危）"
            action_advice = "市場評價偏高，建議暫時觀望或避免追高。"

        report = (
            f"📊 [{clean_ticker}] 智慧價值評估報告 (官方數據)\n"
            f"---------------------------------\n"
            f"💵 目前收盤/參考價：{current_price} 元\n"
            f"📈 官方本益比換算 EPS：{est_annual_eps} 元\n\n"
            f"🚦 歷史本益比估價區間：\n"
            f"🟢 綠燈 (特價區)  ：{green_price} 元 ({low_pe}x PE)\n"
            f"🟡 黃燈 (合理區)  ：{yellow_price} 元 ({avg_pe}x PE)\n"
            f"🔴 紅燈 (昂貴區)  ：{red_price} 元 ({high_pe}x PE)\n\n"
            f"🎯 當前評級：\n"
            f"{status_light}\n"
            f"💡 建議：{action_advice}"
        )
        return report
        
    except Exception as e:
        return f"❌ 讀取證交所官方資料發生錯誤：{str(e)}"
# ----------------------------------------------------
# 文字訊息處理（支援「估價」指令與原本的飲食減脂教練）
# ----------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    if not user_text:
        return

    # 1. 攔截「估價」指令 (例如: 估價2330, 估價 2801)
    if user_text.startswith("估價"):
        ticker = user_text.replace("估價", "").strip()
        if not ticker:
            reply_text = "請輸入正確的股票代號，例如：`估價2330` 或 `估價2801`"
        else:
            reply_text = analyze_stock_value(ticker)
        
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )
        return

    # 2. 原本的飲食減脂教練路由過濾
    source_type = event.source.type
    keywords = ["飲食", "減脂", "熱量", "熱量估估", "能不能吃", "可以吃嗎", "幫我看", "審查", "飲控", "卡路里"]
    is_keyword_triggered = any(kw in user_text for kw in keywords)
    is_tagged = user_text.startswith("@") or "@" in user_text

    if source_type != 'user' and not is_keyword_triggered and not is_tagged:
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

# ----------------------------------------------------
# 圖片訊息處理（採用 Threading 背景執行，解決 LINE 逾時問題）
# ----------------------------------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id
    reply_token = event.reply_token
    
    source_type = event.source.type
    if source_type == 'group':
        dest_id = event.source.group_id
    elif source_type == 'room':
        dest_id = event.source.room_id
    else:
        dest_id = event.source.user_id

    # 1. 立即回覆提示，避免 LINE 逾時
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="📸 收到餐點照片！教練正在為你進行精準熱量與營養分析中，請稍候... ⏳")]
            )
        )

    # 2. 背景執行緒
    threading.Thread(target=process_image_in_background, args=(message_id, dest_id)).start()

def process_image_in_background(message_id, dest_id):
    try:
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            image_bytes = line_bot_blob_api.get_message_content(message_id)

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type='image/jpeg',
        )

        check_prompt = "請判斷這張圖片裡面是否有食物、飲料、菜單或任何與飲食相關的內容？請只回答「是」或「否」這兩個字。"
        try:
            check_response = call_gemini_with_retry([check_prompt, image_part])
            answer = check_response.text.strip()
            if "否" in answer:
                return
        except Exception:
            return

        response = call_gemini_with_retry([SYSTEM_PROMPT, image_part])
        reply_text = response.text
    except Exception as e:
        reply_text = "⚠️ 目前圖片分析流量較大（503 忙碌中），請稍後再傳一次照片即可！"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=dest_id,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
