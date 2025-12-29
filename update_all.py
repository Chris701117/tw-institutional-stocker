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
JSON_PATH = "docs/data/top_three_inst_change_5_up.json"
HISTORY_DAYS = 120 

# ==========================================
# 核心函式
# ==========================================

def get_twse_daily_chips():
    print("🚀 開始抓取證交所今日籌碼資料...")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date="
    
    for i in range(5):
        date_obj = datetime.now() - timedelta(days=i)
        date_str = date_obj.strftime("%Y%m%d")
        target_url = url + date_str
        print(f"   正在嘗試下載日期: {date_str} ...")
        
        try:
            df = pd.read_csv(target_url, header=1, encoding='cp950', thousands=',')
            if '證券代號' in df.columns:
                print(f"✅ 成功抓到 {date_str} 的資料！")
                
                df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
                df['name'] = df['證券名稱'].astype(str).str.strip()
                
                def clean_num(x):
                    if isinstance(x, str):
                        return float(x.replace(',', ''))
                    return float(x)

                # 單位：千張 (為了配合原本習慣，先轉成張數方便後續計算)
                df['外資'] = df['外陸資買賣超股數(不含外資自營商)'].apply(clean_num) / 1000
                df['投信'] = df['投信買賣超股數'].apply(clean_num) / 1000
                df['自營商'] = df['自營商買賣超股數'].apply(clean_num) / 1000
                df['總變'] = df['三大法人買賣超股數'].apply(clean_num) / 1000
                
                return df[['code', 'name', '外資', '投信', '總變']]
            
        except Exception as e:
            print(f"   ⚠️ 無資料或下載失敗: {e}")
            time.sleep(1)
            
    print("❌ 錯誤：過去 5 天都抓不到資料。")
    return pd.DataFrame()

def add_price_position_via_yfinance(df_chips):
    print(f"🚀 啟動 yfinance 抓取歷史股價 (共 {len(df_chips)} 檔)...")
    
    stock_list = df_chips[df_chips['code'].str.len() == 4]['code'].tolist()
    yf_tickers = [f"{code}.TW" for code in stock_list]
    
    if not yf_tickers:
        return df_chips

    print("   正在向 Yahoo Finance 請求資料...")
    try:
        data = yf.download(yf_tickers, period="6mo", progress=False, group_by='ticker')
    except Exception as e:
        print(f"❌ yfinance 下載失敗: {e}")
        return df_chips

    print("✅ 下載完成，開始計算技術指標...")
    
    df_chips['vol_ratio'] = 0.0
    df_chips['price_pos'] = 0.5 
    df_chips['conc_ratio'] = 0.0 # 新增：籌碼集中度 (買超/成交量)
    
    for code in stock_list:
        ticker = f"{code}.TW"
        try:
            if ticker not in data.columns.levels[0]:
                continue
                
            df_stock = data[ticker].dropna()
            if len(df_stock) < 10:
                continue

            # 1. 計算量比
            today_vol = df_stock['Volume'].iloc[-1] # 單位：股
            avg_vol_5 = df_stock['Volume'].iloc[-6:-1].mean()
            vol_ratio = today_vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
                
            # 2. 計算位階
            df_hist = df_stock.tail(HISTORY_DAYS)
            highest = df_hist['High'].max()
            lowest = df_hist['Low'].min()
            current = df_stock['Close'].iloc[-1]
            pos = (current - lowest) / (highest - lowest) if highest > lowest else 0.5

            # 3. 🔥 計算籌碼集中度 (總買超張數 / 今日成交張數)
            # df_chips['總變'] 單位是張 (例如 1000 代表 1000張)
            # today_vol 單位是股 (例如 1000000 代表 1000張)
            # 所以要先把 總變 轉成 股
            mask = (df_chips['code'] == code)
            net_buy_shares = df_chips.loc[mask, '總變'].values[0] * 1000 
            
            if today_vol > 0:
                conc = (net_buy_shares / today_vol) * 100 # 變成百分比
            else:
                conc = 0
            
            # 寫入回主表
            df_chips.loc[mask, 'vol_ratio'] = round(vol_ratio, 2)
            df_chips.loc[mask, 'price_pos'] = round(pos, 2)
            df_chips.loc[mask, 'conc_ratio'] = round(conc, 1) # 小數點1位
            
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
            "three_inst_ratio": row.get('conc_ratio', 0), # 這裡改成傳送「集中度」
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
    df = get_twse_daily_chips()
    if df.empty: return

    df = add_price_position_via_yfinance(df)
    export_json(df)

if __name__ == "__main__":
    main()
