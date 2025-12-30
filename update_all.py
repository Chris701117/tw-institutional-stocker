import os
import json
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta, timezone

# ==========================================
# 設定區
# ==========================================
JSON_PATH = "docs/data/top_three_inst_change_5_up.json"
HISTORY_DAYS = 120 

# ==========================================
# 核心函式
# ==========================================

def get_twse_daily_chips(is_intraday=False):
    """
    抓取「累計 5 天」的籌碼資料。
    """
    print(f"🚀 啟動抓取程序 (模式: 累計 5 日籌碼)...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date="
    
    start_delay = 1 if is_intraday else 0
    valid_dfs = [] 
    days_collected = 0
    target_days = 5 

    for i in range(start_delay, start_delay + 14):
        if days_collected >= target_days:
            break

        tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
        date_obj = tw_now - timedelta(days=i)
        date_str = date_obj.strftime("%Y%m%d")
        
        target_url = url + date_str
        print(f"   [{days_collected+1}/{target_days}] 正在下載: {date_str} ...")
        
        try:
            df = pd.read_csv(target_url, header=1, encoding='cp950', thousands=',')
            if '證券代號' in df.columns:
                print(f"   ✅ 成功取得: {date_str}")
                df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
                df['name'] = df['證券名稱'].astype(str).str.strip()
                
                def clean_num(x):
                    if isinstance(x, str): return float(x.replace(',', ''))
                    return float(x)

                df['外資'] = df['外陸資買賣超股數(不含外資自營商)'].apply(clean_num) / 1000
                df['投信'] = df['投信買賣超股數'].apply(clean_num) / 1000
                df['總變'] = df['三大法人買賣超股數'].apply(clean_num) / 1000
                
                valid_dfs.append(df[['code', 'name', '外資', '投信', '總變']])
                days_collected += 1
            else:
                print(f"   ⚠️ 無資料 (可能是假日): {date_str}")
                
        except Exception:
            print(f"   ⚠️ 下載失敗 (可能是假日): {date_str}")
            time.sleep(1) 
            continue
            
    if not valid_dfs:
        print("❌ 錯誤：無法取得任何籌碼資料。")
        return pd.DataFrame()

    print(f"📊 正在合併 {len(valid_dfs)} 天的籌碼資料...")
    merged_df = pd.concat(valid_dfs)
    final_df = merged_df.groupby(['code', 'name'], as_index=False).sum()
    print(f"✅ 累計籌碼計算完成！(共 {len(final_df)} 檔)")
    return final_df

def calculate_technical_indicators(df):
    """
    計算所有技術指標：KD, MA60, Bollinger Bands, MACD
    回傳多個訊號旗標
    """
    # 資料長度不足 35 天無法計算準確的 MACD/Bollinger
    if len(df) < 35: 
        return 50, 50, False, 0, False, False, 0

    # =========================================
    # 1. 計算 KD (9,3,3)
    # =========================================
    low_min = df['Low'].rolling(window=9).min()
    high_max = df['High'].rolling(window=9).max()
    rsv = (df['Close'] - low_min) / (high_max - low_min) * 100
    rsv = rsv.fillna(50)
    
    k_values = [50]; d_values = [50]
    rsv_list = rsv.tolist()
    for i in range(1, len(rsv_list)):
        k = (2/3) * k_values[-1] + (1/3) * rsv_list[i]
        d = (2/3) * d_values[-1] + (1/3) * k
        k_values.append(k)
        d_values.append(d)
    
    curr_k = k_values[-1]; curr_d = d_values[-1]
    prev_k = k_values[-2]; prev_d = d_values[-2]
    
    # KD 金叉條件：K < 50 (中低檔) 且 K 向上穿過 D
    is_kd_gc = (prev_k < prev_d) and (curr_k > curr_d) and (curr_k < 50)

    # =========================================
    # 2. 計算 MA60 (季線) 與 乖離率
    # =========================================
    ma60_gap = 0
    if len(df) >= 60:
        ma60 = df['Close'].rolling(window=60).mean().iloc[-1]
        if ma60 > 0:
            ma60_gap = ((df['Close'].iloc[-1] - ma60) / ma60) * 100

    # =========================================
    # 3. 計算布林通道 (Bollinger Bands)
    # =========================================
    # 中軌 = 20MA, 標準差 = 20日
    ma20 = df['Close'].rolling(window=20).mean()
    std20 = df['Close'].rolling(window=20).std()
    lower = ma20 - (2 * std20)
    
    current_price = df['Close'].iloc[-1]
    curr_lower = lower.iloc[-1]
    
    # 布林抄底條件：股價 <= 下軌 * 1.015 (給予 1.5% 緩衝區)
    is_bb_low = current_price <= (curr_lower * 1.015)

    # =========================================
    # 4. 🔥 計算 MACD (12, 26, 9)
    # =========================================
    # 使用 pandas ewm 計算指數移動平均
    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dem = dif.ewm(span=9, adjust=False).mean() # 訊號線 (Signal Line)
    osc = dif - dem # 柱狀圖 (Histogram)

    curr_dif = dif.iloc[-1]; curr_dem = dem.iloc[-1]
    prev_dif = dif.iloc[-2]; prev_dem = dem.iloc[-2]
    curr_osc = osc.iloc[-1]; prev_osc = osc.iloc[-2]

    # MACD 翻紅/金叉條件：
    # 條件 A: DIF 向上穿過 DEM (黃金交叉)
    # 條件 B: 柱狀圖 (OSC) 由負轉正 (零軸翻紅)
    # 這裡我們採用標準的「黃金交叉」作為訊號，且柱狀圖必須是紅的(>0)
    is_macd_gc = (prev_dif < prev_dem) and (curr_dif > curr_dem)

    return curr_k, curr_d, is_kd_gc, ma60_gap, is_bb_low, is_macd_gc, curr_osc

def add_realtime_data(df_chips, is_intraday):
    """
    使用 yfinance 抓取股價並計算所有指標
    """
    print(f"🚀 啟動 yfinance 抓取 (共 {len(df_chips)} 檔)...")
    
    stock_list = df_chips[df_chips['code'].str.len() == 4]['code'].tolist()
    yf_tickers = [f"{code}.TW" for code in stock_list]
    
    if not yf_tickers:
        return df_chips

    print("   正在向 Yahoo Finance 請求數據...")
    try:
        # 維持 6mo 以計算所有中長期指標
        data = yf.download(yf_tickers, period="6mo", progress=False, group_by='ticker')
    except Exception as e:
        print(f"❌ yfinance 下載失敗: {e}")
        return df_chips

    print("✅ 下載完成，開始計算技術指標 (MA60, KD, BB, MACD)...")
    
    # 取得台灣時間計算盤中經過分鐘數
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    market_open = tw_now.replace(hour=9, minute=0, second=0, microsecond=0)
    minutes_elapsed = (tw_now - market_open).total_seconds() / 60
    
    if minutes_elapsed < 1: minutes_elapsed = 1
    if minutes_elapsed > 270: minutes_elapsed = 270

    # 初始化新欄位
    df_chips['vol_ratio'] = 0.0
    df_chips['price_pos'] = 0.5 
    df_chips['conc_ratio'] = 0.0
    df_chips['ma60_gap'] = 0.0  
    df_chips['kd_gold_cross'] = False 
    df_chips['k_val'] = 0.0 
    df_chips['bb_low'] = False      # 布林下軌
    df_chips['macd_gc'] = False     # 🔥 MACD 金叉
    df_chips['macd_osc'] = 0.0      # MACD 柱狀圖數值
    
    for code in stock_list:
        ticker = f"{code}.TW"
        try:
            if ticker not in data.columns.levels[0]:
                continue
                
            df_stock = data[ticker].dropna()
            if len(df_stock) < 35: # 指標需要足夠長的歷史資料
                continue

            current_close = df_stock['Close'].iloc[-1]
            current_vol = df_stock['Volume'].iloc[-1]
            
            # 🔥 計算所有技術指標 (Unpack 7 個回傳值)
            k, d, is_kd_gc, ma60_gap, is_bb_low, is_macd_gc, osc = calculate_technical_indicators(df_stock)
            
            # --- 計算量比 (爆量預估) ---
            avg_vol_5 = df_stock['Volume'].iloc[-6:-1].mean()
            
            if is_intraday:
                est_vol = current_vol * (270 / minutes_elapsed)
                if minutes_elapsed >= 270: est_vol = current_vol
            else:
                est_vol = current_vol

            if avg_vol_5 > 0:
                vol_ratio = est_vol / avg_vol_5
            else:
                vol_ratio = 1.0

            # --- 計算位階 ---
            check_len = min(len(df_stock), 120)
            highest = df_stock['High'].iloc[-check_len:].max()
            lowest = df_stock['Low'].iloc[-check_len:].min()
            
            if highest > lowest:
                pos = (current_close - lowest) / (highest - lowest)
            else:
                pos = 0.5

            # --- 計算集中度 ---
            mask = (df_chips['code'] == code)
            net_buy_shares = df_chips.loc[mask, '總變'].values[0] * 1000
            
            if est_vol > 0:
                conc = (net_buy_shares / est_vol) * 100
            else:
                conc = 0

            # 寫入 DataFrame
            df_chips.loc[mask, 'vol_ratio'] = round(vol_ratio, 2)
            df_chips.loc[mask, 'price_pos'] = round(pos, 2)
            df_chips.loc[mask, 'conc_ratio'] = round(conc, 1)
            df_chips.loc[mask, 'ma60_gap'] = round(ma60_gap, 2)
            df_chips.loc[mask, 'kd_gold_cross'] = bool(is_kd_gc)
            df_chips.loc[mask, 'k_val'] = round(k, 1)
            df_chips.loc[mask, 'bb_low'] = bool(is_bb_low) # 布林
            df_chips.loc[mask, 'macd_gc'] = bool(is_macd_gc) # MACD
            df_chips.loc[mask, 'macd_osc'] = round(osc, 2)
            
        except Exception:
            continue

    return df_chips

def export_json(df):
    print("💾 正在輸出 JSON 檔案...")
    df = df.fillna(0)
    output_list = []
    for _, row in df.iterrows():
        record = {
            "code": row['code'],
            "name": row['name'],
            "change": row['總變'],
            "three_inst_ratio": row.get('conc_ratio', 0),
            "foreign_ratio_diff": row['外資'], 
            "trust_ratio_diff": row['投信'],    
            "vol_ratio": row.get('vol_ratio', 0),
            "price_pos": row.get('price_pos', 0.5),
            "ma60_gap": row.get('ma60_gap', 0),
            "kd_gold_cross": row.get('kd_gold_cross', False),
            "k_val": row.get('k_val', 0),
            "bb_low": row.get('bb_low', False),     # 輸出布林訊號
            "macd_gc": row.get('macd_gc', False)    # 輸出 MACD 訊號
        }
        output_list.append(record)
        
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(output_list, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(f"✅ JSON 輸出成功！共 {len(output_list)} 筆資料。")

def main():
    # 判斷時間
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    hour = tw_now.hour
    
    # 簡單判斷：9點~14點 算盤中
    is_intraday = (9 <= hour < 14)
    
    # 1. 抓籌碼 (盤中抓昨日, 盤後抓今日)
    df = get_twse_daily_chips(is_intraday)
    
    if df.empty:
        print("⚠️ 無法取得籌碼資料，程式結束。")
        return

    # 2. 補上即時行情 (盤中即時抓, 盤後抓收盤)
    df = add_realtime_data(df, is_intraday)
    
    # 3. 輸出
    export_json(df)

if __name__ == "__main__":
    main()
