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
SYSTEM_PROMPT = """
你是一個專業的飲食控制機器人。
請針對使用者發送的食物照片或飲食文字（包含任何特殊備註，例如：飯吃半碗、去皮、醬汁減半等）進行精準估算。

⚠️ **強制輸出格式規範**（請務必明確列出以下數據，絕不可省略）：
1. 🍱 估計餐點內容與熱量：
   - 內容描述：簡述辨識出的食物與份量
   - 熱量：XXX 大卡
   - 三大營養素：蛋白質 XXg、碳水化合物 XXg、脂肪 XXg
2. 🚦 飲控適合度評級：
   - 🟢 綠燈 / 🟡 黃燈 / 🔴 紅燈（擇一並簡述理由）
3. 💡 調整建議：給予 1~2 句具體的飲食微調建議。

語氣請保持客觀、簡潔、實事求是，適合在社群群組內快速閱讀。
"""

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
        
        current_price = round(price_history
