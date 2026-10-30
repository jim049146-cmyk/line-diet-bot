def analyze_stock_value(ticker_symbol: str) -> str:
    clean_ticker = ticker_symbol.strip().upper()
    if not (clean_ticker.endswith(".TW") or clean_ticker.endswith(".TWO")):
        full_ticker = f"{clean_ticker}.TW"
    else:
        full_ticker = clean_ticker

    try:
        stock = yf.Ticker(full_ticker)
        price_history = stock.history(period="1d")
        if price_history.empty:
            full_ticker = f"{clean_ticker}.TWO"
            stock = yf.Ticker(full_ticker)
            price_history = stock.history(period="1d")
            if price_history.empty:
                return f"⚠️ 找不到股票代號 [{clean_ticker}] 的數據，請確認台股代號是否正確。"
        
        current_price = round(price_history['Close'].iloc[-1], 2)
        
        info = stock.info
        est_annual_eps = None
        
        # 1. 先嘗試從 info 抓取 trailingEps 或 forwardEps
        trailing_eps = info.get('trailingEps')
        forward_eps = info.get('forwardEps')
        
        if trailing_eps and trailing_eps > 0:
            est_annual_eps = round(trailing_eps, 2)
        elif forward_eps and forward_eps > 0:
            est_annual_eps = round(forward_eps, 2)
        
        # 2. 備援機制：如果 info 抓不到，改從季報 (quarterly_financials) 計算近四季 TTM EPS
        if not est_annual_eps or est_annual_eps <= 0:
            try:
                q_fin = stock.quarterly_financials
                if not q_fin.empty:
                    # 尋找淨利欄位
                    net_income_row = None
                    for name in ['Net Income', 'Net Income Common Stockholders', 'Net Income From Continuing Operation']:
                        if name in q_fin.index:
                            net_income_row = q_fin.loc[name].dropna()
                            break
                    
                    if net_income_row is not None and len(net_income_row) >= 4:
                        # 加總最近 4 季淨利
                        ttm_net_income = net_income_row.iloc[:4].sum()
                        shares = info.get('sharesOutstanding')
                        if shares and shares > 0:
                            est_annual_eps = round(ttm_net_income / shares, 2)
            except Exception:
                pass

        # 若依然無法取得有效 EPS，則回報異常
        if not est_annual_eps or est_annual_eps <= 0:
            return f"⚠️ [{clean_ticker}] 最近期 EPS 數據異常或無法取得，不適用本益比估價法。"

        # 3. 設定歷史本益比基準乘數 (可依需求調整：低 15x / 均 22x / 高 28x)
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
            f"📊 [{clean_ticker}] 智慧價值評估報告\n"
            f"---------------------------------\n"
            f"💵 目前盤中/收盤價：{current_price} 元\n"
            f"📈 預估/近四季 EPS：{est_annual_eps} 元\n\n"
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
        return f"❌ 計算過程發生錯誤：{str(e)}"
