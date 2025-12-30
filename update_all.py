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
    抓取籌碼資料。
    - 盤中模式：抓「最新已公佈」的資料 (通常是昨天)，作為觀察名單。
    - 盤後模式：抓「今天」的資料 (如果公佈了)。
    """
    print(f"🚀 啟動抓取程序 (盤中模式: {is_intraday})...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date="
    
    # 如果是盤中，直接從昨天開始往前找 (因為今天絕對還沒出)
    # 如果是盤後，從今天開始找
    start_delay = 1 if is_intraday else 0
    
    for i in range(start_delay, start_delay + 5):
        # 調整時區確保日期正確 (GitHub Server 是 UTC，要+8)
        tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
        date_obj = tw_now - timedelta(days=i)
        date_str = date_obj.strftime("%Y%m%d")
        
        target_url = url + date_str
        print(f"   正在嘗試下載籌碼日期: {date_str} ...")
        
        try:
            df = pd.read_csv(target_url, header=1, encoding='cp950', thousands=',')
            if '證券代號' in df.columns:
                print(f"✅ 成功鎖定籌碼日期: {date_str}")
                
                df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
                df['name'] = df['證券名稱'].astype(str).str.strip()
                
                def clean_num(x):
                    if isinstance(x, str):
                        return float(x.replace(',', ''))
                    return float(x)

                df['外資'] = df['外陸資買賣超股數(不含外資自營商)'].apply(clean_num) / 1000
                df['投信'] = df['投信買賣超股數'].apply(clean_num) / 1000
                df['總變'] = df['三大法人買賣超股數'].apply(clean_num) / 1000
                
                return df[['code', 'name', '外資', '投信', '總變']]
            
        except Exception:
            time.sleep(1)
            continue
            
    print("❌ 錯誤：無法取得任何籌碼資料。")
    return pd.DataFrame()

def add_realtime_data(df_chips, is_intraday):
    """
    使用 yfinance 抓取股價。
    - 盤中：抓取即時報價，計算「預估量」。
    - 盤後：抓取收盤價。
    """
    print(f"🚀 啟動 yfinance 抓取 (共 {len(df_chips)} 檔)...")
    
    stock_list = df_chips[df_chips['code'].str.len() == 4]['code'].tolist()
    yf_tickers = [f"{code}.TW" for code in stock_list]
    
    if not yf_tickers:
        return df_chips

    print("   正在向 Yahoo Finance 請求數據...")
    try:
        # 盤中需要即時，盤後只要日線。這裡統一抓最近 5 天的 1m (分鐘線) 資料太慢，改抓 1d
        # yfinance 的 1d 資料在盤中會包含「當下最新一筆」
        data = yf.download(yf_tickers, period="5d", progress=False, group_by='ticker')
    except Exception as e:
        print(f"❌ yfinance 下載失敗: {e}")
        return df_chips

    print("✅ 下載完成，開始計算技術指標...")
    
    # 取得台灣時間計算盤中經過分鐘數
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    market_open = tw_now.replace(hour=9, minute=0, second=0, microsecond=0)
    minutes_elapsed = (tw_now - market_open).total_seconds() / 60
    
    # 防呆：如果還沒開盤或剛開盤，避免除以 0
    if minutes_elapsed < 1: minutes_elapsed = 1
    # 最多就是 270 分鐘 (4.5小時)
    if minutes_elapsed > 270: minutes_elapsed = 270

    df_chips['vol_ratio'] = 0.0
    df_chips['price_pos'] = 0.5 
    df_chips['conc_ratio'] = 0.0
    
    for code in stock_list:
        ticker = f"{code}.TW"
        try:
            if ticker not in data.columns.levels[0]:
                continue
                
            df_stock = data[ticker].dropna()
            if len(df_stock) < 2: # 至少要有昨天跟今天
                continue

            # --- 關鍵：取得最新一筆 (可能是盤中，也可能是盤後) ---
            current_close = df_stock['Close'].iloc[-1]
            current_vol = df_stock['Volume'].iloc[-1] # 當下累積量
            
            # --- 計算量比 (爆量預估) ---
            # 前5日均量 (不含今天)
            avg_vol_5 = df_stock['Volume'].iloc[-6:-1].mean()
            
            if is_intraday:
                # 盤中預估量 = 當前量 * (270 / 經過分鐘)
                est_vol = current_vol * (270 / minutes_elapsed)
                # 校正：如果已經收盤 (超過13:30)，預估量 = 當前量
                if minutes_elapsed >= 270: est_vol = current_vol
            else:
                # 盤後直接用當天量
                est_vol = current_vol

            if avg_vol_5 > 0:
                vol_ratio = est_vol / avg_vol_5
            else:
                vol_ratio = 1.0

            # --- 計算位階 (過去 120 日) ---
            # 這裡簡單用 recent high/low
            highest = df_stock['High'].max()
            lowest = df_stock['Low'].min()
            if highest > lowest:
                pos = (current_close - lowest) / (highest - lowest)
            else:
                pos = 0.5

            # --- 計算集中度 ---
            mask = (df_chips['code'] == code)
            net_buy_shares = df_chips.loc[mask, '總變'].values[0] * 1000
            
            # 如果是盤中，用預估量來算集中度會比較準一點，或是用昨天的量
            # 這裡我們先用「昨天的籌碼 / 今天的量」做參考，數值僅供參考
            if est_vol > 0:
                conc = (net_buy_shares / est_vol) * 100
            else:
                conc = 0

            # 寫入
            df_chips.loc[mask, 'vol_ratio'] = round(vol_ratio, 2)
            df_chips.loc[mask, 'price_pos'] = round(pos, 2)
            df_chips.loc[mask, 'conc_ratio'] = round(conc, 1)
            
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
            "price_pos": row.get('price_pos', 0.5)
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
