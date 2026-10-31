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
# 🗄️ SQLite 資料庫模組（支援個人檔案、飲食與運動紀錄）
# ----------------------------------------------------
DB_FILE = "health_logs.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # 1. 每日飲食紀錄表
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
    # 2. 個人獨立飲食暫存表（以 user_id 為主鍵，群組多人互不覆蓋）
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
        INSERT INTO daily_logs (user_name, log_date, meal_type, description, calories, protein, carbs, fat)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_name, today, meal_type, desc, calories, protein, carbs, fat))
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

def get_daily_summary(user_name, user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # 查詢飲食
    cursor.execute('''
        SELECT meal_type, description, calories, protein, carbs, fat 
        FROM daily_logs 
        WHERE user_name = ? AND log_date = ?
    ''', (user_name, today))
    food_rows = cursor.fetchall()
    
    # 查詢運動
    cursor.execute('''
        SELECT workout_desc, est_calories 
        FROM workout_logs 
        WHERE user_id = ? AND log_date = ?
    ''', (user_id, today))
    workout_rows = cursor.fetchall()
    
    # 查詢個人身體數據
    cursor.execute('SELECT height, weight, body_fat, goal FROM user_profiles WHERE user_id = ?', (user_id,))
    profile_row = cursor.fetchone()
    conn.close()
    
    if not food_rows and not workout_rows:
        return f"📊 [{user_name}] 今天尚無任何飲食與運動紀錄喔！"
    
    # 計算飲食總計
    total_cal = sum(r[2] for r in food_rows)
    total_p = sum(r[3] for r in food_rows)
    total_c = sum(r[4] for r in food_rows)
    total_f = sum(r[5] for r in food_rows)
    
    # 計算運動消耗總計
    total_workout_cal = sum(r[1] for r in workout_rows)
    
    profile_text = "👤 尚未設定身體數據（可輸入：`設定 哲瑜 身高 175 體重 70 體脂 18 目標 減脂`）"
    tdee = 2000
    if profile_row:
        h, w, bf, goal = profile_row
        bmr = 10 * w + 6.25 * h - 5 * 34 + 5
        tdee = bmr * 1.375
        profile_text = f"👤 身高：{h}cm | 體重：{w}kg | 體脂：{bf}% | 目標：{goal}\n🔥 估算基礎代謝 (BMR)：{int(bmr)} kcal | 總消耗 (TDEE)：{int(tdee)} kcal"
    
    food_detail = ""
    for r in food_rows:
        food_detail += f"🍳 {r[0]}：{r[1]}\n   └ {r[2]} kcal | P:{r[3]}g | C:{r[4]}g | F:{r[5]}g\n"
        
    workout_detail = ""
    for r in workout_rows:
        workout_detail += f"🏃 {r[0]} (消耗約 -{r[1]} kcal)\n"
        
    net_calories = total_cal - total_workout_cal
    
    return (
        f"📊 [{user_name}] {today} 綜合健康與熱量報告\n"
        f"---------------------------------\n"
        f"{profile_text}\n"
        f"---------------------------------\n"
        f"🏋️ **今日運動明細**：\n{workout_detail if workout_detail else '   (今日尚無運動紀錄)\n'}"
        f"🔥 運動總消耗：-{round(total_workout_cal, 1)} kcal\n"
        f"---------------------------------\n"
        f"🍱 **今日飲食明細**：\n{food_detail}"
        f"---------------------------------\n"
        f"🎯 **熱量結算**：\n"
        f"• 攝取總熱量：{round(total_cal, 1)} kcal\n"
        f"• 蛋白質：{round(total_p, 1)}g | 碳水：{round(total_c, 1)}g | 脂肪：{round(total_f, 1)}g\n"
        f"• 淨熱量 (攝取 - 消耗)：{round(net_calories, 1)} kcal\n"
        f"💡 建議對照 TDEE 評估今日熱量赤字/盈餘狀態！"
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

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    if not user_text:
        return

    user_id = event.source.user_id

    # 1. 股市估價指令：估價2330
    if user_text.startswith("估價"):
        ticker = user_text.replace("估價", "").strip()
        reply_text = "請輸入正確代號，例如：`估價2330`" if not ticker else analyze_stock_value(ticker)
        reply_line(event.reply_token, reply_text)
        return

    # 2. 設定個人身體數據指令：設定 哲瑜 身高 175 體重 70 體脂 18 目標 減脂
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

    # 3. 運動紀錄指令：哲瑜運動 重訓 60分鐘
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
        reply_line(event.reply_token, f"🏃 [{user_name}] 運動紀錄已存檔！\n• 項目：{workout_desc}\n• 預估消耗：-{est_cal} kcal")
        return

    # 4. 存入帳本指令：記錄在哲瑜的第一餐 / 記在哲瑜的點心
    record_match = re.search(r"(?:記錄在|記在)\s*([^\s的]+)\s*的\s*([^\s]+)", user_text)
    if record_match:
        user_name = record_match.group(1).strip()
        meal_type = record_match.group(2).strip()
        reply_text = confirm_and_save_log(user_id, user_name, meal_type)
        reply_line(event.reply_token, reply_text)
        return

    # 5. 總結指令：哲瑜今日總結
    summary_match = re.search(r"([^\s]+)\s*(?:今日總結|今天的總結)", user_text)
    if summary_match:
        user_name = summary_match.group(1).replace("查看", "").strip()
        reply_text = get_daily_summary(user_name, user_id)
        reply_line(event.reply_token, reply_text)
        return

    # 6. 飲食微調指令（依據個人暫存）
    pending = get_pending_analysis(user_id)
    modify_keywords = ["半碗", "去皮", "少飯", "微調", "改為", "換成", "不加", "少醬", "備註"]
    is_modify = any(kw in user_text for kw in modify_keywords)

    if pending and is_modify:
        base_desc = pending[0]
        combined_prompt = f"{SYSTEM_PROMPT}\n\n上一筆內容：{base_desc}\n補充備註：{user_text}\n請根據補充備註重新精算熱量與三大營養素。"
        try:
            response = call_gemini_with_retry(combined_prompt)
            reply_text = response.text
            desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
            save_pending_analysis(user_id, f"{base_desc} (備註: {user_text})", cal, p, c, f)
            reply_text += "\n\n👉 已重新精算！確認後可輸入：`記錄在哲瑜的第一餐`"
        except Exception as e:
            reply_text = f"⚠️ 重新精算發生錯誤: {str(e)}"
        reply_line(event.reply_token, reply_text)
        return

    # 7. 食物關鍵字過濾
    food_keywords = ["吃", "喝", "蛋白粉", "蛋白飲", "橘子", "餐", "雞肉", "便當", "飯", "麵", "奶昔", "沙拉", "蛋", "燕麥", "水果", "高培", "乳清", "微克", "卡路里"]
    is_food_related = any(kw in user_text for kw in food_keywords)

    if event.source.type != 'user' and not is_food_related:
        return

    # 8. 計算全新飲食內容
    full_prompt = f"{SYSTEM_PROMPT}\n\n使用者輸入的飲食內容：\n{user_text}"
    try:
        response = call_gemini_with_retry(full_prompt)
        reply_text = response.text
        
        desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
        save_pending_analysis(user_id, desc, cal, p, c, f)
        
        reply_text += (
            f"\n\n---------------------------------\n"
            f"💡 **小提示**：確認這筆內容無誤後，可直接輸入："
            f"`記錄在哲瑜的第一餐` 或 `記在哲瑜的點心` 進行存檔！"
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

# 📸 圖片事件處理（已改為同步 Reply，完全不消耗 Push 額度）
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    message_id = event.message.id
    reply_token = event.reply_token
    user_id = event.source.user_id

    print(f"📥 收到圖片事件，Message ID: {message_id}, User ID: {user_id}")

    try:
        with ApiClient(configuration) as api_client:
            line_bot_blob_api = MessagingApiBlob(api_client)
            image_bytes = line_bot_blob_api.get_message_content(message_id)

        image_part = types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg')

        response = call_gemini_with_retry([SYSTEM_PROMPT, image_part])
        reply_text = response.text

        desc, cal, p, c, f = parse_nutrition_from_text(reply_text)
        save_pending_analysis(user_id, desc, cal, p, c, f)

        reply_text += (
            f"\n\n---------------------------------\n"
            f"💡 **小提示**：如果這餐有特殊吃法（例如：*白飯吃半碗、雞腿去皮*），"
            f"可以直接打字告訴我幫你重新精算！\n"
            f"確認無誤後，可直接輸入：`記錄在哲瑜的第一餐`"
        )

    except Exception as e:
        print("❌ 圖片處理發生錯誤:", traceback.format_exc())
        reply_text = f"⚠️ 圖片分析時發生了一點狀況，請稍後再試一次！"

    reply_line(reply_token, reply_text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
