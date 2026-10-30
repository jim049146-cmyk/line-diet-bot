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
import yfinance as yf

def analyze_stock_value(ticker_symbol: str) -> str:
    clean_ticker = ticker_symbol.strip().upper()
    if not (clean_ticker.endswith(".TW") or clean_ticker.endswith(".TWO")):
        full_ticker = f"{clean_ticker}.TW"
    else:
        full_ticker = full_ticker

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        stock = yf.Ticker(full_ticker, session=session)
        
        # 1. 抓取即時股價
        price_history = stock.history(period="1d")
        if price_history.empty:
            full_ticker = f"{clean_ticker}.TWO"
            stock = yf.Ticker(full_ticker, session=session)
            price_history = stock.history(period="1d")
            if price_history.empty:
                return f"⚠️ 找不到股票代號 [{clean_ticker}] 的股價數據，請確認代號是否正確。"
        
        current_price = round(price_history['Close'].iloc[-1], 2)

        # ==========================================
        # 2. 【強制優先】嘗試從季報表直接抓取每股盈餘 (Basic EPS) 計算動能
        # ==========================================
        est_annual_eps = None
        eps_source_desc = "官方歷史本益比反推"
        
        try:
            q_fin = stock.quarterly_financials
            if not q_fin.empty:
                eps_row = None
                # 尋找財報中的每股盈餘欄位
                for name in ['Basic EPS', 'Diluted EPS', 'Basic Earnings Per Share']:
                    if name in q_fin.index:
                        eps_row = q_fin.loc[name].dropna()
                        break
                
                if eps_row is not None and len(eps_row) >= 2:
                    # 最近兩季（例如最新 H1）的 EPS 總和
                    recent_h1_eps = eps_row.iloc[:2].sum()
                    forward_run_rate_eps = round(recent_h1_eps * 2, 2) # 半年度年化
                    
                    # 近四季 (TTM) EPS 總和
                    if len(eps_row) >= 4:
                        ttm_eps = round(eps_row.iloc[:4].sum(), 2)
                    else:
                        ttm_eps = forward_run_rate_eps

                    # 強制採用反應現狀較快的數值（取 H1 年化與 TTM 的較高者或最新動能）
                    if forward_run_rate_eps > 0:
                        est_annual_eps = forward_run_rate_eps
                        eps_source_desc = f"最新財報動能年化 (H1累計 {round(recent_h1_eps, 2)}元 × 2)"
                    elif ttm_eps > 0:
                        est_annual_eps = ttm_eps
                        eps_source_desc = "近四季滾動總和 (TTM)"
        except Exception:
            pass

        # 3. 如果季報動能因網路完全抓不到，才退回使用證交所官方本益比反推
        pe_ratio = None
        if not est_annual_eps or est_annual_eps <= 0:
            url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                for item in response.json():
                    if item.get("Code") == clean_ticker:
                        pe_str = item.get("PEratio", "").strip()
                        if pe_str and pe_str != "-" and pe_str != "0":
                            try:
                                pe_ratio = float(pe_str)
                                est_annual_eps = round(current_price / pe_ratio, 2)
                                eps_source_desc = "官方歷史本益比反推 (備用)"
                            except ValueError:
                                pass
                        break

        if not est_annual_eps or est_annual_eps <= 0:
            return fallback_to_pb_valuation(stock, clean_ticker, current_price)

        # 4. 設定歷史本益比基準乘數並計算區間 (低 15x / 均 22x / 高 28x)
        low_pe, avg_pe, high_pe = 15.0, 22.0, 28.0
        green_price = round(est_annual_eps * low_pe, 1)      # 🟢 特價區
        yellow_price = round(est_annual_eps * avg_pe, 1)  # 🟡 合理區
        red_price = round(est_annual_eps * high_pe, 1)    # 🔴 昂貴區
        
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
            f"📊 [{clean_ticker}] 智慧價值評估報告 (動能優先版)\n"
            f"---------------------------------\n"
            f"💵 目前盤中/收盤價：{current_price} 元\n"
            f"📈 參考 EPS ({eps_source_desc})：{est_annual_eps} 元\n"
            f"---------------------------------\n"
            f"🚦 預估本益比估價區間：\n"
            f"🟢 綠燈 (特價區)  ：{green_price} 元 ({low_pe}x PE)\n"
            f"🟡 黃燈 (合理區)  ：{yellow_price} 元 ({avg_pe}x PE)\n"
            f"🔴 紅燈 (昂貴區)  ：{red_price} 元 ({high_pe}x PE)\n\n"
            f"🎯 當前評級：\n{status_light}\n💡 建議：{action_advice}"
        )
        return report
    except Exception as e:
        return f"❌ 計算過程發生錯誤：{str(e)}"

def fallback_to_pb_valuation(stock, clean_ticker, current_price):
    """ 傳產與景氣循環股專用的 P/B 淨值比備用邏輯 """
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
        response = requests.get(url, timeout=10)
        pb_ratio = None
        if response.status_code == 200:
            for item in response.json():
                if item.get("Code") == clean_ticker:
                    for pb_key in ["PBratio", "PB", "PriceToBookRatio"]:
                        pb_str = item.get(pb_key, "").strip()
                        if pb_str and pb_str != "-" and pb_str != "0":
                            try:
                                pb_ratio = float(pb_str)
                                break
                            except ValueError:
                                pass
                    break
        
        if pb_ratio and pb_ratio > 0:
            if pb_ratio < 1.0:
                pb_status = "🟢 綠燈（淨值以下 / 資產防禦區）"
                pb_advice = "股價低於每股淨值，具備強大下檔資產保護力！"
            elif pb_ratio <= 1.3:
                pb_status = "🟡 黃燈（合理評價區）"
                pb_advice = "股價處於合理淨值區間，可依殖利率長線佈局。"
            else:
                pb_status = "🔴 紅燈（相對高檔區）"
                pb_advice = "股價對應淨值偏高，建議留意波動風險。"

            return (
                f"📊 [{clean_ticker}] 智慧價值評估報告 (股價淨值比法)\n"
                f"---------------------------------\n"
                f"💵 目前盤中/收盤價：{current_price} 元\n"
                f"📉 目前官方股價淨值比 (P/B)：{pb_ratio} 倍\n\n"
                f"🎯 評價分級：\n{pb_status}\n💡 建議：{pb_advice}"
            )
    except Exception:
        pass
    return f"⚠️ [{clean_ticker}] 目前財報與官方估價數據皆無法取得，暫無法進行量化評估。"
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
