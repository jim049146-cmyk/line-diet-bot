import os
import io
import re
import sqlite3
import threading
import requests
from datetime import datetime
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

# ⚠️ 宣告 Flask 應用程式物件
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

# ----------------------------------------------------
# 🗄️ SQLite 資料庫模組
# ----------------------------------------------------
DB_FILE = "health_logs.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT,
            log_date TEXT,
            meal_type TEXT,
            description TEXT,
            calories REAL,
            protein REAL,
            carbs REAL,
            fat REAL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_logs (
            dest_id TEXT PRIMARY KEY,
            description TEXT,
            calories REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def save_pending_analysis(dest_id, description, calories, protein, carbs, fat):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT OR REPLACE INTO pending_logs (dest_id, description, calories, protein, carbs, fat, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (dest_id, description, calories, protein, carbs, fat, now_str))
    conn.commit()
    conn.close()

def get_pending_analysis(dest_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT description, calories, protein, carbs, fat FROM pending_logs WHERE dest_id = ?', (dest_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def confirm_and_save_log(dest_id, user_name, meal_type):
    row = get_pending_analysis(dest_id)
    if not row:
        return "⚠️ 找不到最近的餐點分析記錄，請先傳送照片或文字讓飲食控制機器人評估喔！"
    
    desc, calories, protein, carbs, fat = row
    today = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO daily_logs (user_name, log_date, meal_type, description, calories, protein, carbs, fat)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_name, today, meal_type, desc, calories, protein, carbs, fat))
    cursor.execute('DELETE FROM pending_logs WHERE dest_id = ?', (dest_id,))
    conn.commit()
    conn.close()
    
    return (
        f"✅ 已成功將【{meal_type}】記錄至 [{user_name}] 的今日帳本！\n"
        f"---------------------------------\n"
        f"🍱 餐點：{desc}\n"
        f"🔥 熱量：{calories} kcal\n"
        f"🥩 蛋白質：{protein}g | 🍞 碳水：{carbs}g | 🥑 脂肪：{fat}g"
    )

def get_daily_summary(user_name):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT meal_type, description, calories, protein, carbs, fat 
        FROM daily_logs 
        WHERE user_name = ? AND log_date = ?
    ''', (user_name, today))
    rows = cursor.fetchall()
    conn.close()
    
    if not rows:
        return f"📊 [{user_name}] 今天尚無任何飲食紀錄喔！"
    
    total_cal = sum(r[2] for r in rows)
    total_p = sum(r[3] for r in rows)
    total_c = sum(r[4] for r in rows)
    total_f = sum(r[5] for r in rows)
    
    detail_text = ""
    for r in rows:
        detail_text += f"🍳 {r[0]}：{r[1]}\n   └ {r[2]} kcal | P:{r[3]}g | C:{r[4]}g | F:{r[5]}g\n"
        
    return (
        f"📊 [{user_name}] {today} 飲食總結報告\n"
        f"---------------------------------\n"
        f"{detail_text}"
        f"---------------------------------\n"
        f"🎯 今日累積總計：\n"
        f"🔥 總熱量：{round(total_cal, 1)} kcal\n"
        f"🥩 總蛋白質：{round(total_p, 1)} g\n"
        f"🍞 總碳水化合物：{round(total_c, 1)} g\n"
        f"🥑 總脂肪：{round(total_f, 1)} g"
    )

# ----------------------------------------------------
# 系統 AI 提示詞
# ----------------------------------------------------
SYSTEM_PROMPT = "你是一個專業的飲食控制機器人。請針對使用者發送的食物照片或飲食文字（包含任何特殊備註，例如：飯吃半碗、去皮、醬汁減半等）進行精準估算。\n\n【強制輸出格式規範】：\n1. 🍱 估計餐點內容與熱量：\n   - 內容描述：簡述辨識出的食物與份量\n   - 熱量：XXX 大卡\n   - 三大營養素：蛋白質 XXg、碳水化合物 XXg、脂肪 XXg\n2. 🚦 飲控適合度評級：\n   - 🟢 綠燈 / 🟡 黃燈 / 🔴 紅燈（擇一並簡述理由）\n3. 💡 調整建議：給予 1~2 句具體的飲食微調建議。\n\n語氣請保持客觀、簡潔、實事求是，適合在社群群組內快速閱讀。"

def parse_nutrition_from_text(text):
    calories, protein, carbs, fat = 0.0, 0.0, 0.0, 0.0
    description = "健康餐點"
    
    desc_match = re.search(r"(?:內容描述|內容與熱量)：\s*([^\n]+)", text)
    if desc_match:
        description = desc_match.group(1).strip()
    else:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        if lines:
            description = lines[0][:30]
    
    cal_match = re.search(r"(\d+)\s*大卡", text)
    if cal_match:
        calories = float(cal_match.group(1))
        
    p_match = re.search(r"蛋白質\s*(\d+(\.\d+)?)g", text)
    if p_match:
        protein = float(p_match.group(1))
        
    c_match = re.search(r"碳水化合物?\s*(\d+(\.\d+)?)g", text)
    if c_match:
        carbs = float(c_match.group(1))
        
    f_match = re.search(r"脂肪\s*(\d+(\.\d+)?)g", text)
    if f_match:
        fat = float(f_match.group(1))
        
    return description, calories, protein, carbs, fat

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
    return "Diet & Stock Bot is running!"

# ----------------------------------------------------
# 股市模組
# ----------------------------------------------------
def analyze_stock_value(ticker_symbol: str) -> str:
    clean_ticker = ticker_symbol.strip().upper()
    full_ticker = clean_ticker if (clean_ticker.endswith(".TW") or clean_ticker.endswith(".TWO")) else f"{clean_ticker}.TW"

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        stock = yf.Ticker(full_ticker, session=session)
        
        price_history = stock.history(period="1d")
        if price_history.empty:
            full_ticker = f"{clean_ticker}.TWO"
            stock = yf.Ticker(full_ticker, session=session)
            price_history = stock.history(period="1d")
            if price_history.empty:
                return f"⚠️ 找不到股票代號 [{clean_ticker}] 的股價數據。"
        
        current_price = round(price_history['Close'].iloc[-1], 2)
        est_annual_eps = None
        eps_source_desc = "官方歷史本益比反推"
        
        try:
            q_fin = stock.quarterly_financials
            if not q_fin.empty:
                eps_row = None
                for name in ['Basic EPS', 'Diluted EPS', 'Basic Earnings Per Share']:
                    if name in q_fin.index:
                        eps_row = q_fin.loc[name].dropna()
                        break
                if eps_row is not None and len(eps_row) >= 2:
                    recent_h1_eps = eps_row.iloc[:2].sum()
                    forward_run_rate_eps = round(recent_h1_eps * 2, 2)
                    if forward_run_rate_eps > 0:
                        est_annual_eps = forward_run_rate_eps
                        eps_source_desc = f"最新財報動能年化 (H1累計 {round(recent_h1_eps, 2)}元 × 2)"
        except Exception:
            pass

        if not est_annual_eps or est_annual_eps <= 0:
            url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                for item in response.json():
                    if item.get("Code") == clean_ticker:
                        pe_str = item.get("PEratio", "").strip()
                        if pe_str and pe_str != "-" and pe_str != "0":
                            try:
                                est_annual_eps = round(current_price / float(pe_str), 2)
                                eps_source_desc = "官方歷史本益比反推 (備用)"
                            except ValueError:
                                pass
                        break

        if not est_annual_eps or est_annual_eps <= 0:
            return fallback_to_pb_valuation(stock, clean_ticker, current_price)

        low_pe, avg_pe, high_pe = 15.0, 22.0, 28.0
        green_price = round(est_annual_eps * low_pe, 1)
        yellow_price = round(est_annual_eps * avg_pe, 1)
        red_price = round(est_annual_eps * high_pe, 1)
        
        if current_price <= green_price:
            status_light = "🟢 綠燈（特價區 / 安全邊際）"
            action_advice = "現價具備價值投資買點！"
        elif current_price <= yellow_price:
            status_light = "🟡 黃燈（合理區 / 定期定額）"
            action_advice = "適合分批定期定額佈局。"
        else:
            status_light = "🔴 紅燈（昂貴區 / 居高思危）"
            action_advice = "評價偏高，建議暫時觀望。"

        return (
            f"📊 [{clean_ticker}] 智慧價值評估報告\n"
            f"---------------------------------\n"
            f"💵 現價：{current_price} 元 | 參考 EPS：{est_annual_eps} 元\n"
            f"🟢 特價區：{green_price} | 🟡 合理區：{yellow_price} | 🔴 昂貴區：{red_price}\n"
            f"🎯 評級：{status_light}\n💡 建議：{action_advice}"
        )
    except Exception as e:
        return f"❌ 計算錯誤：{str(e)}"

def fallback_to_pb_valuation(stock, clean_ticker, current_price):
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
                pb_status = "🟢 綠燈（淨值以下）"
            elif pb_ratio <= 1.3:
                pb_status = "🟡 黃燈（合理區）"
            else:
                pb_status = "🔴 紅燈（相對高檔）"
            return f"📊 [{clean_ticker}] P/B 評估\n💵 現價：{current_price} 元 | P/B：{pb_ratio} 倍\n🎯 評級：{pb_status}"
    except Exception:
        pass
    return f"⚠️ [{clean_ticker}] 無法取得估價數據。"

# ----------------------------------------------------
# 文字訊息與指令處理
# ----------------------------------------------------
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    if not user_text:
        return

    dest_id = event.source.group_id if event.source.type == 'group' else (
        event.source.room_id if hasattr(event.source, 'room_id') and event.source.room_id else event.source.user_id
    )

    # 1. 估價攔截
    if user_text.startswith("估價"):
        ticker = user_text.replace("估價", "").strip()
        reply_text = "請輸入正確代號，例如：`估價2330`" if not ticker else analyze_stock_value(ticker)
        reply_line(event.reply_token, reply_text)
        return

    # 2. 紀錄歸屬攔截
    record_match = re.search(r"(?:記錄在|記在)\s*([^\s的]+)\s*的\s*([^\s]+)", user_text)
    if record_match:
        user_name = record_match.group(1).strip()
        meal_type = record_match.group(2).strip()
        reply_text = confirm_and_save_log(dest_id, user_name, meal_type)
        reply_line(event.reply_token, reply_text)
        return

    # 3. 查詢總結攔截
    summary_match = re.search(r"([^\s]+)\s*(?:今日總結|今天的總結)", user_text)
    if summary_match:
        user_name = summary_match.group(1).replace("查看", "").strip()
        reply_text = get_daily_summary(user_name)
        reply_line(event.reply_token, reply_text)
        return

    # 4. 針對上一張照片或分析的補充備註
    pending = get_pending_analysis(dest_id)
    keywords = ["飲食", "減脂", "熱量", "能不能吃", "可以吃嗎", "幫我看", "飲控", "卡路里", "半碗", "去皮", "少飯", "微調"]
    is_keyword_triggered = any(kw in user_text for kw in keywords)

    if pending and (is_keyword_triggered or len(user_text) < 25) and not user_text.startswith("吃") and not user_text.startswith("喝"):
        base_desc = pending[0]
        combined_prompt = f"{SYSTEM_PROMPT}\n\n上一筆內容：{base_desc}\n使用者的最新補充備註：{user_text}\n請根據使用者的補充備註重新精算熱量與三大營養素。"
        try:
            response = call_gemini_with_retry(combined_prompt)
            reply_text = response.text
            desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
            save_pending_analysis(dest_id, f"{base_desc} (備註: {user_text})", cal, p, c, f)
            reply_text += "\n\n👉 已根據您的備註重新精算！確認後可直接輸入：`記錄在哲瑜的第一餐`"
        except Exception:
            reply_text = "⚠️ 重新精算時發生狀況，請再試一次！"
        reply_line(event.reply_token, reply_text)
        return

    # 5. 一般文字飲食諮詢
    if event.source.type != 'user' and not is_keyword_triggered and not "吃" in user_text and not "喝" in user_text:
        return

    full_prompt = f"{SYSTEM_PROMPT}\n\n使用者輸入的飲食內容：\n{user_text}"
    try:
        response = call_gemini_with_retry(full_prompt)
        reply_text = response.text
        
        desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
        save_pending_analysis(dest_id, desc, cal, p, c, f)
        
        reply_text += (
            f"\n\n---------------------------------\n"
            f"💡 **小提示**：確認這筆內容無誤後，可直接輸入："
            f"`記錄在哲瑜的第一餐` 或 `記在哲瑜的午餐` 進行存檔！"
        )
    except Exception:
        reply_text = "⚠️ 系統忙碌中，請稍後再試！"

    reply_line(event.reply_token, reply_text)

def reply_line(reply_token, text):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

# ----------------------------------------------------
# 圖片訊息處理
# ----------------------------------------------------
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message
