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
