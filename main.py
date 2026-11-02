import os
import io
import re
import sqlite3
import traceback
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
# 🗄️ SQLite 資料庫模組（支援個人檔案、飲食與運動紀錄，全面以 user_id 隔離）
# ----------------------------------------------------
DB_FILE = "health_logs.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 1. 每日飲食紀錄表（加入 user_id 確保多用戶隔離）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
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
    # 2. 個人獨立飲食暫存表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_logs (
            user_id TEXT PRIMARY KEY,
            description TEXT,
            calories REAL,
            protein REAL,
            carbs REAL,
            fat REAL,
            created_at TEXT
        )
    ''')
    # 3. 使用者身體數據與目標表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            user_name TEXT,
            height REAL,
            weight REAL,
            body_fat REAL,
            goal TEXT
        )
    ''')
    # 4. 每日運動紀錄表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS workout_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            user_name TEXT,
            log_date TEXT,
            workout_desc TEXT,
            est_calories REAL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- 資料庫操作輔助函式 ---
def save_user_profile(user_id, user_name, height, weight, body_fat, goal):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO user_profiles (user_id, user_name, height, weight, body_fat, goal)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, user_name, height, weight, body_fat, goal))
    conn.commit()
    conn.close()

def get_user_profile(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT user_name, height, weight, body_fat, goal FROM user_profiles WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def save_pending_analysis(user_id, description, calories, protein, carbs, fat):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute('''
        INSERT OR REPLACE INTO pending_logs (user_id, description, calories, protein, carbs, fat, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, description, calories, protein, carbs, fat, now_str))
    conn.commit()
    conn.close()

def get_pending_analysis(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT description, calories, protein, carbs, fat FROM pending_logs WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row

def confirm_and_save_log(user_id, user_name, meal_type):
    row = get_pending_analysis(user_id)
    if not row:
        return "⚠️ 找不到你最近的餐點分析記錄，請先傳送照片或文字讓機器人評估喔！"
    
    desc, calories, protein, carbs, fat = row
    today = datetime.now().strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO daily_logs (user_id, user_name, log_date, meal_type, description, calories, protein, carbs, fat)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, user_name, today, meal_type, desc, calories, protein, carbs, fat))
    cursor.execute('DELETE FROM pending_logs WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()
    
    return (
        f"✅ 已成功將【{meal_type}】記錄至 [{user_name}] 的今日帳本！\n"
        f"---------------------------------\n"
        f"🍱 餐點：{desc}\n"
        f"🔥 熱量：{calories} kcal\n"
        f"🥩 蛋白質：{protein}g | 🍞 碳水：{carbs}g | 🥑 脂肪：{fat}g"
    )

def save_workout_log(user_id, user_name, workout_desc, est_calories):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO workout_logs (user_id, user_name, log_date, workout_desc, est_calories)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, user_name, today, workout_desc, est_calories))
    conn.commit()
    conn.close()

# 🛠️ 升級版動態刪除函式（支援刪除指定餐別、點心、運動或備援刪除最後一筆）
def delete_log_dynamically(user_id, user_text):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 1. 處理運動刪除
    if "運動" in user_text or "健身" in user_text:
        cursor.execute('''
            SELECT id, workout_desc, est_calories FROM workout_logs 
            WHERE user_id = ? AND log_date = ? 
            ORDER BY id DESC LIMIT 1
        ''', (user_id, today))
        row = cursor.fetchone()
        if row:
            log_id, workout_desc, est_cal = row
            cursor.execute('DELETE FROM workout_logs WHERE id = ?', (log_id,))
            conn.commit()
            conn.close()
            return f"🗑️ 已成功刪除你今天最後一筆運動紀錄：{workout_desc} (-{est_cal} kcal)"
        else:
            conn.close()
            return "⚠️ 你今天尚無任何已記錄的運動資料可刪除。"

    # 2. 處理飲食刪除（支援動態抓取「第X餐」、點心、宵夜、早餐、午餐、晚餐）
    target_meal = None
    match = re.search(r'第([一二三四五六七八九十0-9]+)餐', user_text)
    if match:
        meal_num = match.group(1)
        target_meal = f"第{meal_num}餐"
    elif "點心" in user_text or "宵夜" in user_text:
        target_meal = "點心" if "點心" in user_text else "宵夜"
    elif "早餐" in user_text or "第一餐" in user_text:
        target_meal = "第一餐"
    elif "午餐" in user_text or "第二餐" in user_text:
        target_meal = "第二餐"
    elif "晚餐" in user_text or "第三餐" in user_text:
        target_meal = "第三餐"

    if target_meal:
        # 尋找該學員今日有沒有這一餐的紀錄
        cursor.execute('''
            SELECT id, description FROM daily_logs 
            WHERE user_id = ? AND log_date = ? AND meal_type = ?
            ORDER BY id DESC LIMIT 1
        ''', (user_id, today, target_meal))
        row = cursor.fetchone()
        if row:
            log_id, desc = row
            cursor.execute('DELETE FROM daily_logs WHERE id = ?', (log_id,))
            conn.commit()
            conn.close()
            return f"🗑️ 已成功幫你刪除今日【{target_meal}】的紀錄：{desc}"
        else:
            conn.close()
            return f"⚠️ 找不到今日【{target_meal}】的紀錄，請確認名稱是否正確。"
    else:
        # 備援：若只打「刪除飲食」或「刪除」，則直接刪除最後一筆飲食紀錄
        cursor.execute('''
            SELECT id, meal_type, description FROM daily_logs 
            WHERE user_id = ? AND log_date = ? 
            ORDER BY id DESC LIMIT 1
        ''', (user_id, today))
        row = cursor.fetchone()
        if row:
            log_id, meal_type, desc = row
            cursor.execute('DELETE FROM daily_logs WHERE id = ?', (log_id,))
            conn.commit()
            conn.close()
            return f"🗑️ 已成功刪除你今天最後一筆飲食紀錄：【{meal_type}】{desc}"
        else:
            conn.close()
            return "⚠️ 你今天尚無任何已記錄的飲食資料可刪除。"

def get_daily_summary(user_name, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT meal_type, description, calories, protein, carbs, fat 
        FROM daily_logs 
        WHERE user_id = ? AND log_date = ?
    ''', (user_id, today))
    food_rows = cursor.fetchall()
    
    cursor.execute('''
        SELECT workout_desc, est_calories 
        FROM workout_logs 
        WHERE user_id = ? AND log_date = ?
    ''', (user_id, today))
    workout_rows = cursor.fetchall()
    
    cursor.execute('SELECT height, weight, body_fat, goal FROM user_profiles WHERE user_id = ?', (user_id,))
    profile_row = cursor.fetchone()
    conn.close()
    
    if not food_rows and not workout_rows:
        return f"📊 [{user_name}] 今天尚無任何飲食與運動紀錄喔！"
    
    total_cal = sum(r[2] for r in food_rows)
    total_p = sum(r[3] for r in food_rows)
    total_c = sum(r[4] for r in food_rows)
    total_f = sum(r[5] for r in food_rows)
    total_workout_cal = sum(r[1] for r in workout_rows)
    net_calories = total_cal - total_workout_cal
    
    profile_text = "👤 尚未設定身體數據（可輸入：`設定 哲瑜 身高 175 體重 70 體脂 18 目標 減脂`）"
    tdee = 2100
    bmr = 1500
    goal = "維持"
    if profile_row:
        h, w, bf, goal = profile_row
        bmr = 10 * w + 6.25 * h - 5 * 34 + 5
        tdee = bmr * 1.375
        profile_text = f"👤 身高：{h}cm | 體重：{w}kg | 體脂：{bf}% | 目標：{goal}\n🔥 基礎代謝 (BMR)：{int(bmr)} kcal | 總消耗 (TDEE)：{int(tdee)} kcal"
    
    food_detail = ""
    for r in food_rows:
        food_detail += f"🍳 {r[0]}：{r[1]}\n    └ {r[2]} kcal | P:{r[3]}g | C:{r[4]}g | F:{r[5]}g\n"
        
    workout_detail = ""
    for r in workout_rows:
        workout_detail += f"🏃 {r[0]} (消耗約 -{r[1]} kcal)\n"

    coach_comment = "表現不錯，繼續保持！"
    try:
        comment_prompt = (
            f"你是一位專業且嚴格的AI健身教練。請根據以下學員今日的數據，給予一段 3~4 句具體、專業且帶有鼓勵性質的總結講評（包含赤字是否理想、蛋白質是否達標、飲食內容的調整建議）：\n"
            f"- 學員目標：{goal}\n"
            f"- 估算 TDEE：{int(tdee)} kcal\n"
            f"- 今日總攝取熱量：{total_cal} kcal\n"
            f"- 運動總消耗：{total_workout_cal} kcal\n"
            f"- 淨熱量：{net_calories} kcal (熱量赤字：{int(tdee - net_calories)} kcal)\n"
            f"- 蛋白質攝取：{total_p}g\n"
            f"請直接給出講評文字，語氣要專業、激勵。"
        )
        res = call_gemini_with_retry(comment_prompt)
        coach_comment = res.text.strip()
    except Exception:
        pass
    
    return (
        f"📊 [{user_name}] {today} 綜合健康與熱量報告\n"
        f"---------------------------------\n"
        f"{profile_text}\n"
        f"---------------------------------\n"
        f"🏋️ **今日運動明細**：\n{workout_detail if workout_detail else '    (今日尚無運動紀錄)\n'}"
        f"🔥 運動總消耗：-{round(total_workout_cal, 1)} kcal\n"
        f"---------------------------------\n"
        f"🍱 **今日飲食明細**：\n{food_detail}"
        f"---------------------------------\n"
        f"🎯 **熱量結算**：\n"
        f"• 攝取總熱量：{round(total_cal, 1)} kcal\n"
        f"• 蛋白質：{round(total_p, 1)}g | 碳水：{round(total_c, 1)}g | 脂肪：{round(total_f, 1)}g\n"
        f"• 淨熱量 (攝取 - 消耗)：{round(net_calories, 1)} kcal\n"
        f"---------------------------------\n"
        f"💡 **教練總結講評**：\n{coach_comment}"
    )

SYSTEM_PROMPT = "你是一個專業的飲食控制與健身教練機器人。請針對使用者發送的食物照片或飲食文字進行精準估算。\n\n【強制輸出格式規範】：\n1. 🍱 估計餐點內容與熱量：\n   - 內容描述：簡述辨識出的食物與份量\n   - 熱量：XXX 大卡\n   - 三大營養素：蛋白質 XXg、碳水化合物 XXg、脂肪 XXg\n2. 🚦 飲控適合度評級：\n   - 🟢 綠燈 / 🟡 黃燈 / 🔴 紅燈\n3. 💡 調整建議：給予 1~2 句具體建議。"

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

# 📈 股票價值評估 + K線均線技術分析
def analyze_stock_value(ticker_symbol: str) -> str:
    clean_ticker = ticker_symbol.strip().upper()
    full_ticker = clean_ticker if (clean_ticker.endswith(".TW") or clean_ticker.endswith(".TWO")) else f"{clean_ticker}.TW"

    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        stock = yf.Ticker(full_ticker, session=session)
        
        hist = stock.history(period="3mo")
        if hist.empty:
            full_ticker = f"{clean_ticker}.TWO"
            stock = yf.Ticker(full_ticker, session=session)
            hist = stock.history(period="3mo")
            if hist.empty:
                return f"⚠️ 找不到股票代號 [{clean_ticker}] 的股價與 K 線數據。"
        
        current_price = round(hist['Close'].iloc[-1], 2)
        
        closes = hist['Close']
        ma20 = round(closes.rolling(window=20).mean().iloc[-1], 2) if len(closes) >= 20 else current_price
        ma60 = round(closes.rolling(window=60).mean().iloc[-1], 2) if len(closes) >= 60 else ma20
        
        if current_price > ma20 and ma20 > ma60:
            tech_status = "🟢 技術面：多頭排列（強勢上升趨勢）"
            tech_advice = "短中期均線向上，若股價回檔至月線(MA20)附近有支撐，為良好買點。"
        elif current_price < ma20 and current_price > ma60:
            tech_status = "🟡 技術面：回檔整理 / 月線附近震盪"
            tech_advice = "股價處於月線與季線之間，屬於中期整理區，觀察能否在季線(MA60)守住。"
        elif current_price < ma60:
            tech_status = "🔴 技術面：空頭排列 / 跌破季線"
            tech_advice = "短中期均線向下且跌破季線，技術面偏弱，切勿急著接刀，建議等待築底。"
        else:
            tech_status = "🟡 技術面：盤整打底中"
            tech_advice = "均線糾
