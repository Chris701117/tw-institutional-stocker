import os
import json
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

# ==========================================
# 設定區
# ==========================================
# JSON 儲存路徑
JSON_PATH = "docs/data/top_three_inst_change_5_up.json"

# 設定要抓取歷史天數來計算位階 (例如 120 天)
HISTORY_DAYS = 120 

# ==========================================
# 核心函式
# ==========================================

def get_twse_daily_chips():
    """
    從證交所抓取『今天』的三大法人買賣超日報
    只抓一天，速度快且不會被鎖。
    """
    print("🚀 開始抓取證交所今日籌碼資料...")
    
    # 證交所三大法人網址 (直接抓 CSV)
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date="
    
    # 嘗試抓取今天 (如果今天是假日，往前推直到抓到資料)
    # 限制往前找 5 天，避免無限迴圈
    for i in range(5):
        date_obj = datetime.now() - timedelta(days=i)
        date_str = date_obj.strftime("%Y%m%d") # 格式 20251230
        
        target_url = url + date_str
        print(f"   正在嘗試下載日期: {date_str} ...")
        
        try:
            # 使用 pandas 直接讀取 CSV (跳過前 1 行標題)
            # 台灣證交所 CSV 通常是 Big5 編碼，若失敗改用 utf-8
            df = pd.read_csv(target_url, header=1, encoding='cp950', thousands=',')
            
            # 檢查欄位是否正確 (簡單檢查是否有 '證券代號')
            if '證券代號' in df.columns:
                print(f"✅ 成功抓到 {date_str} 的資料！")
                
                # 清理資料：代號去除特殊符號
                df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
                df['name'] = df['證券名稱'].astype(str).str.strip()
                
                # 轉換數值 (三大法人買賣超股數 -> 張數，除以 1000)
                # 注意：有些欄位可能是文字，要轉 float
                def clean_num(x):
                    if isinstance(x, str):
                        return float(x.replace(',', ''))
                    return float(x)

                df['外資'] = df['外陸資買賣超股數(不含外資自營商)'].apply(clean_num) / 1000
                df['投信'] = df['投信買賣超股數'].apply(clean_num) / 1000
                df['自營商'] = df['自營商買賣超股數'].apply(clean_num) / 1000
                df['總變'] = df['三大法人買賣超股數'].apply(clean_num) / 1000
                
                # 我們主要需要：代號、名稱、外資、投信、總變
                return df[['code', 'name', '外資', '投信', '總變']]
            
        except Exception as e:
            print(f"   ⚠️ 無資料或下載失敗 (可能是假日): {e}")
            time.sleep(1) # 休息一下
            
    print("❌ 錯誤：過去 5 天都抓不到證交所資料，請檢查網路或是否為長假。")
    return pd.DataFrame()

def add_price_position_via_yfinance(df_chips):
    """
    使用 yfinance 批量抓取股價，計算『位階』與『量比』
    """
    print(f"🚀 啟動 yfinance 抓取歷史股價 (共 {len(df_chips)} 檔)...")
    
    # 1. 準備股票代碼清單 (加上 .TW 後綴)
    # 過濾掉權證或太長的代碼，只留 4 碼的一般股票
    stock_list = df_chips[df_chips['code'].str.len() == 4]['code'].tolist()
    yf_tickers = [f"{code}.TW" for code in stock_list]
    
    if not yf_tickers:
        return df_chips

    # 2. 批量下載 (一次抓半年的資料，足以計算 120 日位階)
    # group_by='ticker' 讓資料結構更好處理
    print("   正在向 Yahoo Finance 請求資料 (這可能需要 30 秒)...")
    try:
        data = yf.download(yf_tickers, period="6mo", progress=False, group_by='ticker')
    except Exception as e:
        print(f"❌ yfinance 下載失敗: {e}")
        return df_chips

    print("✅ 下載完成，開始計算技術指標...")
    
    # 3. 計算指標並填回 df_chips
    # 預設值
    df_chips['vol_ratio'] = 0.0
    df_chips['price_pos'] = 0.5 # 預設 50%
    
    for code in stock_list:
        ticker = f"{code}.TW"
        try:
            # 取得該檔股票的 DataFrame
            # yfinance 格式有時是 MultiIndex，需注意
            if ticker not in data.columns.levels[0]:
                continue
                
            df_stock = data[ticker].dropna()
            
            if len(df_stock) < 10:
                continue

            # --- A. 計算量比 (成交量 / 5日均量) ---
            # 取得最後一天的量 (今日)
            today_vol = df_stock['Volume'].iloc[-1]
            # 取得前 5 天的平均量 (不含今天)
            avg_vol_5 = df_stock['Volume'].iloc[-6:-1].mean()
            
            if avg_vol_5 > 0:
                vol_ratio = today_vol / avg_vol_5
            else:
                vol_ratio = 1.0
                
            # --- B. 計算位階 (120日) ---
            # 取最後 120 天 (或可用 HISTORY_DAYS)
            df_hist = df_stock.tail(HISTORY_DAYS)
            
            highest = df_hist['High'].max()
            lowest = df_hist['Low'].min()
            current = df_stock['Close'].iloc[-1]
            
            if highest > lowest:
                pos = (current - lowest) / (highest - lowest)
            else:
                pos = 0.5
            
            # 寫入回主表
            mask = (df_chips['code'] == code)
            df_chips.loc[mask, 'vol_ratio'] = round(vol_ratio, 2)
            df_chips.loc[mask, 'price_pos'] = round(pos, 2)
            
        except Exception as e:
            # 個別股票計算錯誤就跳過
            continue

    return df_chips

def export_json(df):
    """
    輸出成 Google Apps Script 需要的 JSON 格式
    """
    print("💾 正在輸出 JSON 檔案...")
    
    # 為了節省流量，我們只輸出有意義的股票
    # 例如：法人有買 OR 爆量 OR 漲幅大
    # 這裡簡單設定：只要三大法人合計 > 0 或 量比 > 1.5 
    
    # 格式化欄位名稱以符合您的 GAS 讀取習慣
    # code, name, change(總變), three_inst_ratio(這裡暫時用佔比代替?), foreign_ratio_diff...
    
    # 您的 GAS 讀取這幾個欄位：
    # code, name, change (總變), three_inst_ratio (籌碼集中度), 
    # foreign_ratio_diff (外資), trust_ratio_diff (投信), 
    # vol_ratio (量比), price_pos (位階)
    
    output_list = []
    
    for _, row in df.iterrows():
        # 簡單計算一個虛擬的 "籌碼集中度" (總變張數 / 股本) 
        # 這裡因為沒抓股本，我們暫時用 "總買賣超張數" 當作參考
        # 或者直接回傳 0，因為您的 GAS 主要看 外資/投信/量比
        
        record = {
            "code": row['code'],
            "name": row['name'],
            "change": row['總變'],          # 總變 (張數)
            "three_inst_ratio": 0,         # 佔比 (需股本資料，暫略)
            "foreign_ratio_diff": row['外資'], # 外資 (張數)
            "trust_ratio_diff": row['投信'],   # 投信 (張數)
            "vol_ratio": row.get('vol_ratio', 0),
            "price_pos": row.get('price_pos', 0.5)
        }
        output_list.append(record)
        
    # 確保資料夾存在
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    
    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(output_list, f, ensure_ascii=False, indent=2)
        
    print(f"✅ JSON 輸出成功！共 {len(output_list)} 筆資料。")

# ==========================================
# 主程式
# ==========================================
def main():
    # 1. 抓籌碼 (來源：證交所)
    df = get_twse_daily_chips()
    
    if df.empty:
        print("⚠️ 無法取得籌碼資料，程式結束。")
        return

    # 2. 補上技術面資料 (來源：Yahoo Finance)
    # 這一步會幫 df 加上 'vol_ratio' 和 'price_pos'
    df = add_price_position_via_yfinance(df)
    
    # 3. 輸出 JSON
    export_json(df)

if __name__ == "__main__":
    main()
