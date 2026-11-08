import os
import io
import re
import sqlite3
import traceback
import requests
from datetime import datetime
import matplotlib
matplotlib.use('Agg')  # 避免伺服器環境無 GUI 報錯
import matplotlib.pyplot as plt
from flask import Flask, request, abort, send_file
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
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction,
    ImageMessage
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
MODEL_ID = "gemini-2.5-flash"

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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS water_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            user_name TEXT,
            log_date TEXT,
            water_cc INTEGER
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS weight_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            log_date TEXT,
            weight REAL,
            body_fat REAL
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- 資料庫操作輔助函式 ---
def get_user_name_by_id(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT user_name FROM user_profiles WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return row[0]
    return "學員"

def save_user_profile(user_id, user_name, height, weight, body_fat, goal):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO user_profiles (user_id, user_name, height, weight, body_fat, goal)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, user_name, height, weight, body_fat, goal))
    
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute('''
        INSERT INTO weight_history (user_id, log_date, weight, body_fat)
        VALUES (?, ?, ?, ?)
    ''', (user_id, today, weight, body_fat))
    
    conn.commit()
    conn.close()

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

def confirm_and_save_log(user_id, user_name, meal_input):
    row = get_pending_analysis(user_id)
    if not row:
        return "⚠️ 找不到你最近的餐點分析記錄，請先傳送照片或文字讓機器人評估喔！"
    
    meal_type = meal_input if meal_input else "第3餐"
    if meal_input == "餐":
        meal_type = "第3餐"
    else:
        snatch_num = re.search(r'([0-9一二三四五六七八九十]+)', meal_input)
        if "點心" in meal_input or "宵夜" in meal_input:
            if snatch_num:
                num = snatch_num.group(1)
                cn_map = {'一':'1', '二':'2', '三':'3', '四':'4', '五':'5', '六':'6', '七':'7', '八':'8', '九':'9', '十':'10'}
                num = cn_map.get(num, num)
                meal_type = f"點心{num}"
            else:
                meal_type = "點心1"
        elif "餐" in meal_input:
            match = re.search(r'第?([0-9一二三四五六七八九十]+)餐', meal_input)
            if match:
                num = match.group(1)
                cn_map = {'一':'1', '二':'2', '三':'3', '四':'4', '五':'5', '六':'6', '七':'7', '八':'8', '九':'9', '十':'10'}
                num = cn_map.get(num, num)
                meal_type = f"第{num}餐"
            else:
                meal_type = "第3餐"
        elif meal_input in ["早餐", "早"]:
            meal_type = "第1餐"
        elif meal_input in ["午餐", "午"]:
            meal_type = "第2餐"
        elif meal_input in ["晚餐", "晚"]:
            meal_type = "第3餐"

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

def save_water_log(user_id, user_name, water_cc):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO water_logs (user_id, user_name, log_date, water_cc)
        VALUES (?, ?, ?, ?)
    ''', (user_id, user_name, today, water_cc))
    
    cursor.execute('SELECT SUM(water_cc) FROM water_logs WHERE user_id = ? AND log_date = ?', (user_id, today))
    total_water = cursor.fetchone()[0] or 0
    conn.commit()
    conn.close()
    return total_water

def delete_log_dynamically(user_id, user_text):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
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

    target_meal = None
    if "點心" in user_text or "宵夜" in user_text:
        match_snack = re.search(r'([0-9一二三四五六七八九十]+)', user_text.replace("刪除", ""))
        if match_snack:
            num = match_snack.group(1)
            cn_map = {'一':'1', '二':'2', '三':'3', '四':'4', '五':'5', '六':'6', '七':'7', '八':'8', '九':'9', '十':'10'}
            num = cn_map.get(num, num)
            target_meal = f"點心{num}"
        else:
            target_meal = "點心1"
    elif "餐" in user_text:
        match_meal = re.search(r'第?([0-9一二三四五六七八九十]+)餐', user_text)
        if match_meal:
            num = match_meal.group(1)
            cn_map = {'一':'1', '二':'2', '三':'3', '四':'4', '五':'5', '六':'6', '七':'7', '八':'8', '九':'9', '十':'10'}
            num = cn_map.get(num, num)
            target_meal = f"第{num}餐"
    elif "早餐" in user_text:
        target_meal = "第1餐"
    elif "午餐" in user_text:
        target_meal = "第2餐"
    elif "晚餐" in user_text:
        target_meal = "第3餐"

    if target_meal:
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

    cursor.execute('SELECT SUM(water_cc) FROM water_logs WHERE user_id = ? AND log_date = ?', (user_id, today))
    total_water = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT height, weight, body_fat, goal FROM user_profiles WHERE user_id = ?', (user_id,))
    profile_row = cursor.fetchone()
    conn.close()
    
    total_cal = sum(r[2] for r in food_rows)
    total_p = sum(r[3] for r in food_rows)
    total_c = sum(r[4] for r in food_rows)
    total_f = sum(r[5] for r in food_rows)
    total_workout_cal = sum(r[1] for r in workout_rows)
    net_calories = total_cal - total_workout_cal
    
    profile_text = f"👤 [{user_name}] 尚未設定身體數據（可輸入：`設定 {user_name} 身高 170 體重 70 體脂 18 目標 減脂`）"
    tdee = 2196
    bmr = 1597
    goal = "減脂"
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
            f"你是一位專業且嚴格的AI健身教練。請根據以下學員今日的數據，給予一段 3~4 句具體、專業且帶有鼓勵性質的總結講評（包含赤字是否理想、蛋白質是否達標、水分補充狀況）：\n"
            f"- 學員目標：{goal}\n"
            f"- 估算 TDEE：{int(tdee)} kcal\n"
            f"- 今日總攝取熱量：{total_cal} kcal\n"
            f"- 運動總消耗：{total_workout_cal} kcal\n"
            f"- 淨熱量：{net_calories} kcal (熱量赤字：{int(tdee - net_calories)} kcal)\n"
            f"- 蛋白質攝取：{total_p}g\n"
            f"- 今日飲水量：{total_water} cc\n"
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
        f"💧 今日總喝水量：{total_water} cc\n"
        f"---------------------------------\n"
        f"🍱 **今日飲食明細**：\n{food_detail if food_detail else '    (今日尚無飲食紀錄)\n'}"
        f"---------------------------------\n"
        f"🎯 **熱量結算**：\n"
        f"• 攝取總熱量：{round(total_cal, 1)} kcal\n"
        f"• 蛋白質：{round(total_p, 1)}g | 碳水：{round(total_c, 1)}g | 脂肪：{round(total_f, 1)}g\n"
        f"• 淨熱量 (攝取 - 消耗)：{round(net_calories, 1)} kcal\n"
        f"---------------------------------\n"
        f"💡 **教練總結講評**：\n{coach_comment}"
    )

def get_weekly_report(user_name, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT log_date, SUM(calories), SUM(protein), SUM(carbs), SUM(fat)
        FROM daily_logs
        WHERE user_id = ? AND log_date >= date('now', '-7 days')
        GROUP BY log_date
    ''', (user_id,))
    daily_food = cursor.fetchall()
    
    cursor.execute('''
        SELECT SUM(est_calories)
        FROM workout_logs
        WHERE user_id = ? AND log_date >= date('now', '-7 days')
    ''', (user_id,))
    total_workout_week = cursor.fetchone()[0] or 0
    
    cursor.execute('SELECT height, weight, body_fat, goal FROM user_profiles WHERE user_id = ?', (user_id,))
    profile_row = cursor.fetchone()
    conn.close()
    
    active_days = len(daily_food)
    avg_cal = round(sum(r[1] for r in daily_food) / active_days, 1) if active_days > 0 else 0
    avg_protein = round(sum(r[2] for r in daily_food) / active_days, 1) if active_days > 0 else 0
    
    tdee = 2196
    goal = "減脂"
    if profile_row:
        h, w, bf, goal = profile_row
        bmr = 10 * w + 6.25 * h - 5 * 34 + 5
        tdee = bmr * 1.375
        
    avg_deficit = int(tdee - avg_cal)

    coach_weekly = "本週飲食與運動維持得不錯，請繼續保持節奏！"
    try:
        w_prompt = (
            f"你是一位專業健身教練。請根據學員【{user_name}】過去 7 天的平均數據給出精簡週報建議（150字內）：\n"
            f"- 估算 TDEE：{int(tdee)} kcal\n"
            f"- 平均每日攝取熱量：{avg_cal} kcal\n"
            f"- 平均每日熱量赤字：{avg_deficit} kcal\n"
            f"- 平均每日蛋白質：{avg_protein}g\n"
            f"- 本週累計運動消耗：{total_workout_week} kcal\n"
            f"- 有紀錄天數：{active_days} 天\n"
        )
        res = call_gemini_with_retry(w_prompt)
        coach_weekly = res.text.strip()
    except Exception:
        pass

    return (
        f"📈 [{user_name}] 近 7 天週期健康週報\n"
        f"📅 統計區間：近 7 天\n"
        f"---------------------------------\n"
        f"🔥 平均每日熱量：{avg_cal} / {int(tdee)} kcal\n"
        f"📉 平均每日赤字：{avg_deficit} kcal\n"
        f"🥩 平均每日蛋白質：{avg_protein} g\n"
        f"🏃 本週運動總消耗：-{total_workout_week} kcal\n"
        f"---------------------------------\n"
        f"💡 **教練週期策略建議**：\n{coach_weekly}\n"
        f"---------------------------------\n"
        f"👉 點擊下方圖文選單按鈕可隨時查看體重走勢！"
    )

def generate_weight_chart_bytes(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT log_date, weight FROM weight_history
        WHERE user_id = ?
        ORDER BY log_date ASC
        LIMIT 14
    ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return None

    dates = [r[0][5:] for r in rows]
    weights = [r[1] for r in rows]

    plt.figure(figsize=(6, 3.5), dpi=150)
    plt.plot(dates, weights, marker='o', color='#2c3e50', linewidth=2, markersize=6)
    plt.title("14-Day Weight Trend", fontsize=12, fontweight='bold', color='#333333')
    plt.xlabel("Date", fontsize=10)
    plt.ylabel("Weight (kg)", fontsize=10)
    plt.grid(True, linestyle='--', alpha=0.6)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf.getvalue()

SYSTEM_PROMPT = """你是一個專業的飲食控制與健身教練機器人。請針對使用者發送的食物照片或飲食文字進行精準估算。

【強制輸出格式規範】：
1. 🍱 估計餐點內容與熱量：
   - 內容描述：簡述辨識出的食物與份量
   - 熱量：XXX 大卡
   - 三大營養素：蛋白質 XXg、碳水化合物 XXg、脂肪 XXg
2. 🚦 飲控適合度評級：
   - 🟢 綠燈 / 🟡 黃燈 / 🔴 紅燈
3. 💡 調整建議：給予 1~2 句具體建議。
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
    
    cal_match = re.search(r"(?:熱量[^0-9]*?)?(?:約\s*)?(\d+)\s*大卡", text)
    if cal_match:
        calories = float(cal_match.group(1))
        
    p_match = re.search(r"蛋白質[^0-9]*?(?:約\s*)?(\d+(?:\.\d+)?)\s*g", text)
    if p_match:
        protein = float(p_match.group(1))
        
    c_match = re.search(r"碳水化合物?[^0-9]*?(?:約\s*)?(\d+(?:\.\d+)?)\s*g", text)
    if c_match:
        carbs = float(c_match.group(1))
        
    f_match = re.search(r"脂肪[^0-9]*?(?:約\s*)?(\d+(?:\.\d+)?)\s*g", text)
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
            tech_advice = "均線糾結，無明顯單邊趨勢，建議觀望突破方向或分批小量佈局。"

        est_annual_eps = None
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
                            except ValueError:
                                pass
                        break

        if not est_annual_eps or est_annual_eps <= 0:
            return fallback_to_pb_valuation(stock, clean_ticker, current_price, tech_status, tech_advice, ma20, ma60)

        low_pe, avg_pe, high_pe = 15.0, 22.0, 28.0
        green_price = round(est_annual_eps * low_pe, 1)
        yellow_price = round(est_annual_eps * avg_pe, 1)
        red_price = round(est_annual_eps * high_pe, 1)
        
        if current_price <= green_price:
            fund_light = "🟢 綠燈（特價區 / 安全邊際）"
            fund_advice = "基本面評價具備價值投資買點！"
        elif current_price <= yellow_price:
            fund_light = "🟡 黃燈（合理區 / 定期定額）"
            fund_advice = "基本面評價合理，適合分批定期定額。"
        else:
            fund_light = "🔴 紅燈（昂貴區 / 居高思危）"
            fund_advice = "基本面評價偏高，追高風險較大。"

        return (
            f"📊 [{clean_ticker}] 價值與技術綜合評估報告\n"
            f"---------------------------------\n"
            f"💵 現價：{current_price} 元 | 參考 EPS：{est_annual_eps} 元\n"
            f"📈 均線參考：月線(20MA) {ma20} | 季線(60MA) {ma60}\n"
            f"---------------------------------\n"
            f"1️⃣ 【基本面估值】\n"
            f"• 特價區🟢：{green_price} | 合理區🟡：{yellow_price} | 昂貴區🔴：{red_price}\n"
            f"• 估值評級：{fund_light}\n"
            f"---------------------------------\n"
            f"2️⃣ 【技術面 K線與均線】\n"
            f"• 趨勢狀態：{tech_status}\n"
            f"• 操作建議：{tech_advice}\n"
            f"---------------------------------\n"
            f"💡 **綜合策略**：{fund_advice}"
        )
    except Exception as e:
        return f"❌ 計算錯誤：{str(e)}"

def fallback_to_pb_valuation(stock, clean_ticker, current_price, tech_status, tech_advice, ma20, ma60):
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
            return (
                f"📊 [{clean_ticker}] P/B 與技術綜合評估\n"
                f"---------------------------------\n"
                f"💵 現價：{current_price} 元 | P/B：{pb_ratio} 倍\n"
                f"📈 均線參考：月線(20MA) {ma20} | 季線(60MA) {ma60}\n"
                f"---------------------------------\n"
                f"• P/B 評級：{pb_status}\n"
                f"• 趨勢狀態：{tech_status}\n"
                f"• 操作建議：{tech_advice}"
            )
    except Exception:
        pass
    return f"📊 [{clean_ticker}] 現價：{current_price} 元\n📈 均線：月線 {ma20} | 季線 {ma60}\n• 趨勢：{tech_status}\n• 建議：{tech_advice}"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    if not user_text:
        return

    user_id = event.source.user_id

    # 1. 股市估價指令
    if user_text.startswith("估價"):
        ticker = user_text.replace("估價", "").strip()
        reply_text = "請輸入正確代號，例如：`估價2330`" if not ticker else analyze_stock_value(ticker)
        reply_line(event.reply_token, reply_text)
        return

    # 2. 設定個人身體數據指令
    profile_match = re.search(r"設定\s*([^\s]+)\s*身高\s*(\d+(?:\.\d+)?)\s*體重\s*(\d+(?:\.\d+)?)\s*體脂\s*(\d+(?:\.\d+)?)\s*目標\s*([^\s]+)", user_text)
    if profile_match:
        user_name = profile_match.group(1).strip()
        h = float(profile_match.group(2))
        w = float(profile_match.group(3))
        bf = float(profile_match.group(4))
        goal = profile_match.group(5).strip()
        save_user_profile(user_id, user_name, h, w, bf, goal)
        reply_line(event.reply_token, f"✅ [{user_name}] 的身體數據設定成功！\n身高：{h}cm | 體重：{w}kg | 體脂：{bf}% | 目標：{goal}")
        return

    # 3. 刪除紀錄指令
    if "刪除" in user_text and not user_text.startswith("更正"):
        reply_text = delete_log_dynamically(user_id, user_text)
        reply_line(event.reply_token, reply_text)
        return

    # 4. 運動紀錄指令
    workout_match = re.search(r"([^\s]+)\s*運動\s*(.+)", user_text)
    if workout_match:
        user_name = workout_match.group(1).strip()
        workout_desc = workout_match.group(2).strip()
        
        try:
            w_prompt = f"請評估以下運動內容大約消耗多少大卡（以一般成人體重估算），只要回傳一個數字（例如：350）：\n{workout_desc}"
            w_res = call_gemini_with_retry(w_prompt)
            cal_match = re.search(r"(\d+)", w_res.text)
            est_cal = float(cal_match.group(1)) if cal_match else 300.0
        except Exception:
            est_cal = 300.0
            
        save_workout_log(user_id, user_name, workout_desc, est_cal)
        reply_line(event.reply_token, f"🏃 [{user_name}] 運動紀錄已存檔！\n• 項目：{workout_desc}\n• 預估消耗：-{est_cal} kcal\n\n💡 若剛才不小心記錯，可輸入：`刪除運動` 進行修正！")
        return

    # 💧 5. 喝水紀錄指令（支援點擊按鈕「喝水」自動跳 Quick Reply 選單，或手動輸入「喝水 500」、「哲瑜喝水 300」）
    water_match = re.search(r"(?:([^\s]+)\s*)?喝水(?:\s*(\d+))?", user_text)
    if water_match and not user_text.startswith("刪除"):
        parsed_name = water_match.group(1)
        water_cc_str = water_match.group(2)

        if not parsed_name or parsed_name == "哲瑜":
            user_name = get_user_name_by_id(user_id)
        else:
            user_name = parsed_name

        # 👉 如果只點擊了圖文選單的「喝水」（未帶數字），自動彈出 Quick Reply 浮動按鈕供選擇水量
        if not water_cc_str:
            quick_water_reply = QuickReply(
                items=[
                    QuickReplyItem(action=MessageAction(label="🥛 200 cc", text="喝水 200")),
                    QuickReplyItem(action=MessageAction(label="🥛 300 cc", text="喝水 300")),
                    QuickReplyItem(action=MessageAction(label="🥛 500 cc", text="喝水 500")),
                    QuickReplyItem(action=MessageAction(label="🍾 1000 cc", text="喝水 1000")),
                ]
            )
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=f"💧 [{user_name}] 請選擇你剛才補充的水量：", quick_reply=quick_water_reply)]
                    )
                )
            return

        water_cc = int(water_cc_str)
        total_water = save_water_log(user_id, user_name, water_cc)
        reply_line(event.reply_token, f"💧 成功幫 [{user_name}] 記錄喝水 {water_cc} cc！\n• 今日累計總水量：{total_water} cc 🌊")
        return

    # 📈 6. 本週週報指令（支援：本週週報、哲瑜本週週報）
    weekly_match = re.search(r"(?:([^\s]+)\s*)?本週週報", user_text)
    if weekly_match:
        parsed_name = weekly_match.group(1)
        user_name = parsed_name if parsed_name else get_user_name_by_id(user_id)
        reply_text = get_weekly_report(user_name, user_id)
        reply_line(event.reply_token, reply_text)
        return

    # 📉 7. 體重走勢指令
    trend_match = re.search(r"(?:([^\s]+)\s*)?體重走勢", user_text)
    if trend_match:
        img_bytes = generate_weight_chart_bytes(user_id)
        if not img_bytes:
            reply_line(event.reply_token, "⚠️ 目前尚無足夠的體重歷史紀錄可繪製走勢圖！\n(可先輸入：`設定 你的名字 身高 170 體重 70 體脂 18 目標 減脂` 建立檔案)")
            return
        
        reply_line(event.reply_token, "📉 你的近期體重走勢圖已統計完畢！")
        return

    # 8. 存入帳本指令
    record_match = re.search(r"(?:紀錄|記錄|記|紀)在?\s*([^\s的]+)\s*(?:的)?\s*([^\s]+)", user_text)
    if record_match:
        user_name = record_match.group(1).strip()
        meal_type = record_match.group(2).strip()
        reply_text = confirm_and_save_log(user_id, user_name, meal_type)
        reply_text += "\n\n💡 若剛才不小心記錯，可輸入：`刪除點心1` 或 `刪除第四餐` 進行修正！"
        reply_line(event.reply_token, reply_text)
        return

    # 9. 總結指令（支援：今日總結、哲瑜今日總結）
    summary_match = re.search(r"(?:([^\s]+)\s*)?(?:今日總結|今天的總結)", user_text)
    if summary_match:
        parsed_name = summary_match.group(1)
        user_name = parsed_name.replace("查看", "").strip() if parsed_name else get_user_name_by_id(user_id)
        reply_text = get_daily_summary(user_name, user_id)
        reply_line(event.reply_token, reply_text)
        return

    # 10. 飲食內容更正指令
    pending = get_pending_analysis(user_id)
    modify_keywords = ["更正", "修正", "改為", "換成", "備註", "補充"]
    is_modify = any(kw in user_text for kw in modify_keywords)

    if pending and is_modify:
        base_desc = pending[0]
        combined_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"【學員更正說明】：\n"
            f"原本辨識內容為：{base_desc}\n"
            f"學員輸入的實際更正內容為：{user_text}\n\n"
            f"請以學員提供的更正內容為主，重新估算精確的熱量與三大營養素。"
        )
        try:
            response = call_gemini_with_retry(combined_prompt)
            reply_text = response.text
            desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
            save_pending_analysis(user_id, desc, cal, p, c, f)
            current_name = get_user_name_by_id(user_id)
            reply_text += f"\n\n👉 已成功為你更正餐點內容！確認後可直接輸入：`紀錄在{current_name}的點心1`"
        except Exception as e:
            reply_text = f"⚠️ 更正重新精算發生錯誤: {str(e)}"
        reply_line(event.reply_token, reply_text)
        return

    # 11. 防洗板機制：群組內必須包含 @機器人 才觸發
    is_group_chat = (event.source.type != 'user')
    bot_mentioned = ("@HabitEats" in user_text) or ("@bot" in user_text) or ("@機器人" in user_text)

    if is_group_chat and not bot_mentioned:
        return

    clean_text = user_text.replace("@HabitEats", "").replace("@bot", "").replace("@機器人", "").strip()
    if not clean_text:
        return

    # 12. 計算全新飲食內容
    full_prompt = f"{SYSTEM_PROMPT}\n\n使用者輸入的飲食內容：\n{clean_text}"
    try:
        response = call_gemini_with_retry(full_prompt)
        reply_text = response.text
        
        desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
        save_pending_analysis(user_id, desc, cal, p, c, f)
        
        current_name = get_user_name_by_id(user_id)
        reply_text += (
            f"\n\n---------------------------------\n"
            f"💡 **小提示**：確認這筆內容無誤後，可直接輸入："
            f"`記錄在{current_name}的點心1` 或 `記{current_name}第1餐` 進行存檔！"
        )
    except Exception as e:
        reply_text = f"⚠️ 系統錯誤: {str(e)}"

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

# 📸 圖片事件處理
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id
    reply_token = event.reply_token
    user_id = event.source.user_id

    try:
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            image_bytes = line_bot_blob_api.get_message_content(message_id)

        image_part = types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')

        response = call_gemini_with_retry([SYSTEM_PROMPT, image_part])
        reply_text = response.text

        desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
        save_pending_analysis(user_id, desc, cal, p, c, f)

        current_name = get_user_name_by_id(user_id)
        reply_text += (
            f"\n\n---------------------------------\n"
            f"💡 **小提示**：如果照片辨識內容有誤，可打字加上「更正」！\n"
            f"確認無誤後，可直接輸入：`紀錄在{current_name}的點心1`"
        )

    except Exception as e:
        print("❌ 圖片處理發生錯誤:", traceback.format_exc())
        reply_text = f"⚠️ 圖片分析時發生了一點狀況，請稍後再試一次！"

    reply_line(reply_token, reply_text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
