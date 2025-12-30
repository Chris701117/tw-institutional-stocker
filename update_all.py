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
    邏輯：
    1. 迴圈嘗試抓取過去 14 天內的資料。
    2. 只要成功抓到「5天」有效的交易日資料，就停止。
    3. 將這 5 天的「外資」、「投信」、「總變」加總，計算波段籌碼。
    """
    print(f"🚀 啟動抓取程序 (模式: 累計 5 日籌碼)...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date="
    
    # 盤中從昨天開始找，盤後從今天開始找
    start_delay = 1 if is_intraday else 0
    
    valid_dfs = [] # 存放成功下載的資料表
    days_collected = 0
    target_days = 5 # 🔥 設定為累計 5 天

    # 往回找 14 天，確保能湊滿 5 個交易日 (避開週末假日)
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
                
                # 清洗資料
                df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
                df['name'] = df['證券名稱'].astype(str).str.strip()
                
                def clean_num(x):
                    if isinstance(x, str):
                        return float(x.replace(',', ''))
                    return float(x)

                df['外資'] = df['外陸資買賣超股數(不含外資自營商)'].apply(clean_num) / 1000
                df['投信'] = df['投信買賣超股數'].apply(clean_num) / 1000
                df['總變'] = df['三大法人買賣超股數'].apply(clean_num) / 1000
                
                # 只保留需要的欄位
                valid_dfs.append(df[['code', 'name', '外資', '投信', '總變']])
                days_collected += 1
            else:
                print(f"   ⚠️ 無資料 (可能是假日): {date_str}")
                
        except Exception:
            print(f"   ⚠️ 下載失敗 (可能是假日): {date_str}")
            time.sleep(1) # 休息一下避免被封 IP
            continue
            
    if not valid_dfs:
        print("❌ 錯誤：無法取得任何籌碼資料。")
        return pd.DataFrame()

    print(f"📊 正在合併 {len(valid_dfs)} 天的籌碼資料...")
    
    # --- 核心邏輯：將多天資料合併並加總 ---
    # 1. 合併所有 DataFrame
    merged_df = pd.concat(valid_dfs)
    
    # 2. 針對 'code' 和 'name' 進行群組，並將數值欄位 'sum' (加總)
    final_df = merged_df.groupby(['code', 'name'], as_index=False).sum()
    
    print(f"✅ 累計籌碼計算完成！(共 {len(final_df)} 檔)")
    return final_df

def calculate_kd(df, n=9):
    """
    計算 KD 值 (9,3,3)
    回傳最後一筆的 K, D 值以及是否金叉
    """
    # 至少要有 9 天資料才能算 RSV
    if len(df) < 9:
        return 50, 50, False

    # 計算 RSV
    low_min = df['Low'].rolling(window=n).min()
    high_max = df['High'].rolling(window=n).max()
    rsv = (df['Close'] - low_min) / (high_max - low_min) * 100
    rsv = rsv.fillna(50) # 補值防止錯誤

    # 遞迴計算 K 與 D (標準公式: K = 2/3*前K + 1/3*RSV)
    k_values = [50] # 初始值
    d_values = [50]
    
    rsv_list = rsv.tolist()
    
    for i in range(1, len(rsv_list)):
        k = (2/3) * k_values[-1] + (1/3) * rsv_list[i]
        d = (2/3) * d_values[-1] + (1/3) * k
        k_values.append(k)
        d_values.append(d)
        
    curr_k = k_values[-1]
    curr_d = d_values[-1]
    prev_k = k_values[-2]
    prev_d = d_values[-2]

    # 判斷低檔黃金交叉
    # 條件1: K < 30 (低檔超賣區)
    # 條件2: K 向上突破 D (昨天 K<D, 今天 K>D)
    is_low_level = curr_k < 30
    is_gold_cross = (prev_k < prev_d) and (curr_k > curr_d)
    
    return curr_k, curr_d, (is_low_level and is_gold_cross)

def add_realtime_data(df_chips, is_intraday):
    """
    使用 yfinance 抓取股價。
    - 盤中：抓取即時報價，計算「預估量」、「MA60」、「KD指標」。
    - 盤後：抓取收盤價。
    """
    print(f"🚀 啟動 yfinance 抓取 (共 {len(df_chips)} 檔)...")
    
    stock_list = df_chips[df_chips['code'].str.len() == 4]['code'].tolist()
    yf_tickers = [f"{code}.TW" for code in stock_list]
    
    if not yf_tickers:
        return df_chips

    print("   正在向 Yahoo Finance 請求數據...")
    try:
        # 維持 6mo 以計算 MA60 和 KD
        data = yf.download(yf_tickers, period="6mo", progress=False, group_by='ticker')
    except Exception as e:
        print(f"❌ yfinance 下載失敗: {e}")
        return df_chips

    print("✅ 下載完成，開始計算技術指標 (MA60, KD, 預估量)...")
    
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
    df_chips['kd_gold_cross'] = False # 🔥 新增 KD 金叉訊號
    df_chips['k_val'] = 0.0 # (選填) 方便觀察數值
    
    for code in stock_list:
        ticker = f"{code}.TW"
        try:
            if ticker not in data.columns.levels[0]:
                continue
                
            df_stock = data[ticker].dropna()
            if len(df_stock) < 9: # KD 需要至少 9 天
                continue

            # --- 基本數據 ---
            current_close = df_stock['Close'].iloc[-1]
            current_vol = df_stock['Volume'].iloc[-1]
            
            # 🔥 1. 計算 KD 指標 (低檔金叉)
            k, d, is_gc = calculate_kd(df_stock)
            
            # 🔥 2. 計算 MA60 (季線) 與 乖離率
            ma60_gap = 0
            if len(df_stock) >= 60:
                ma60 = df_stock['Close'].rolling(window=60).mean().iloc[-1]
                if ma60 > 0:
                    ma60_gap = ((current_close - ma60) / ma60) * 100
            
            # --- 3. 計算量比 (爆量預估) ---
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

            # --- 4. 計算位階 ---
            check_len = min(len(df_stock), 120)
            highest = df_stock['High'].iloc[-check_len:].max()
            lowest = df_stock['Low'].iloc[-check_len:].min()
            
            if highest > lowest:
                pos = (current_close - lowest) / (highest - lowest)
            else:
                pos = 0.5

            # --- 5. 計算集中度 ---
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
            df_chips.loc[mask, 'kd_gold_cross'] = bool(is_gc)
            df_chips.loc[mask, 'k_val'] = round(k, 1)
            
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
            "k_val": row.get('k_val', 0)
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
