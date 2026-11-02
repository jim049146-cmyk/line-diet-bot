import re
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

# 處理 LINE 文字訊息的總入口
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    raw_text = event.message.text.strip()
    
    # ----------------------------------------------------
    # 1. 系統指令：刪除紀錄（支援動態餐別與點心刪除）
    # ----------------------------------------------------
    if "刪除" in raw_text and not raw_text.startswith("更正"):
        target_meal = None
        
        # 使用正則表達式動態抓取「第X餐」（支援無限擴充至第4、5、6餐...）
        match = re.search(r'第([一二三四五六七八九十0-9]+)餐', raw_text)
        
        if match:
            meal_num = match.group(1)
            target_meal = f"第{meal_num}餐"
        elif "點心" in raw_text or "宵夜" in raw_text:
            target_meal = "點心" if "點心" in raw_text else "宵夜"
        elif "早餐" in raw_text or "第一餐" in raw_text:
            target_meal = "第一餐"
        elif "午餐" in raw_text or "第二餐" in raw_text:
            target_meal = "第二餐"
        elif "晚餐" in raw_text or "第三餐" in raw_text:
            target_meal = "第三餐"

        # 執行資料庫刪除動作
        if target_meal:
            success = delete_user_meal_by_category(user_id, target_meal)
            if success:
                reply_msg = f"🗑️ 已經成功幫你刪除今日【{target_meal}】的紀錄囉！"
            else:
                reply_msg = f"⚠️ 找不到今日【{target_meal}】的紀錄，請確認名稱是否正確。"
        else:
            # 備援：若沒指定特定餐別，則刪除最後一筆紀錄
            success = delete_last_user_log(user_id)
            reply_msg = "🗑️ 已經幫你刪除最後一筆飲食紀錄了！"
            
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        return

    # ----------------------------------------------------
    # 2. 系統指令：確認存檔 (例如: 記錄在哲瑜的第一餐 / 記在哲瑜的點心)
    # ----------------------------------------------------
    elif "記錄在" in raw_text or "記在" in raw_text:
        # 你的寫入資料庫/帳本存檔邏輯...
        # confirm_and_save_log(user_id, raw_text)
        return

    # ----------------------------------------------------
    # 3. 系統指令：設定 / 運動 / 今日總結 等其他功能 (略)
    # ----------------------------------------------------
    elif raw_text.startswith("設定") or "運動" in raw_text or "今日總結" in raw_text:
        # 你的其他功能邏輯...
        return

    # ----------------------------------------------------
    # 4. 核心功能 A：【更正】直接覆蓋當前暫存內容
    # ----------------------------------------------------
    elif raw_text.startswith("更正"):
        correct_text = raw_text.replace("更正", "").strip()
        try:
            ai_data = analyze_text_with_gemini(correct_text)
            
            # 覆蓋暫存區
            save_to_pending_logs(
                user_id=user_id,
                description=ai_data['description'],
                calories=ai_data['calories'],
                protein=ai_data['protein'],
                carbs=ai_data['carbs'],
                fat=ai_data['fat']
            )
            
            reply_msg = (
                f"🔄 已為你重新修正並計算！\n"
                f"• 內容：{ai_data['description']}\n"
                f"• 熱量：約 {ai_data['calories']} kcal\n"
                f"• 蛋白質：{ai_data['protein']}g | 碳水：{ai_data['carbs']}g | 脂肪：{ai_data['fat']}g\n\n"
                f"💡 確認無誤請輸入：`記錄在[名字]的第幾餐` 進行存檔！"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 更正解析失敗，請確認格式是否正確。"))
        return

    # ----------------------------------------------------
    # 5. 核心功能 B：【@機器人】純文字直接觸發熱量計算
    # ----------------------------------------------------
    elif "@HabitEats" in raw_text or "@bot" in raw_text:
        food_text = raw_text.replace("@HabitEats", "").replace("@bot", "").strip()
        try:
            ai_data = analyze_text_with_gemini(food_text)
            
            # 寫入暫存區
            save_to_pending_logs(
                user_id=user_id,
                description=ai_data['description'],
                calories=ai_data['calories'],
                protein=ai_data['protein'],
                carbs=ai_data['carbs'],
                fat=ai_data['fat']
            )
            
            reply_msg = (
                f"🍱 純文字飲食估算：\n"
                f"• 內容：{ai_data['description']}\n"
                f"• 熱量：約 {ai_data['calories']} kcal\n"
                f"• 蛋白質：{ai_data['protein']}g | 碳水：{ai_data['carbs']}g | 脂肪：{ai_data['fat']}g\n\n"
                f"💡 確認無誤請輸入：`記錄在[名字]的第幾餐` 進行存檔！"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 無法辨識該食物內容，請重新輸入。"))
        return

    # ----------------------------------------------------
    # 6. 其他一般日常訊息：完全靜默不處理，避免群組洗板
    # ----------------------------------------------------
    else:
        return
