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
EXCEL_PATH = "docs/data/stock_report.xlsx"
CSV_PATH = "docs/data/stock_report.csv"
HISTORY_DAYS = 120 

# ⚠️ 填入您的 GAS 部署網址 (用於主動觸發)
GAS_URL = "https://script.google.com/macros/s/AKfycbzkOm64edpadEtMUJZGkzGvU_IjYdAPj8Hs2cute5J2BC82SFdflxaA3URszd3zWcnp/exec" 

# ==========================================
# 核心函式：抓取籌碼 (上市+上櫃)
# ==========================================
def get_twse_chips(date_obj):
    date_str = date_obj.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date={date_str}"
    try:
        df = pd.read_csv(url, header=1, encoding='cp950', thousands=',')
        if '證券代號' not in df.columns:
             df = pd.read_csv(url, header=2, encoding='cp950', thousands=',')
        if '證券代號' in df.columns:
            df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
            df['name'] = df['證券名稱'].astype(str).str.strip()
            df['market'] = 'TW'
            return df
    except: pass
    return None

def get_tpex_chips(date_obj):
    minguo_year = date_obj.year - 1911
    date_str = f"{minguo_year}/{date_obj.month:02d}/{date_obj.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result_download.php?l=zh-tw&se=EW&t=D&d={date_str}"
    try:
        df = pd.read_csv(url, header=1, encoding='cp950', thousands=',')
        if '代號' not in df.columns:
             df = pd.read_csv(url, header=2, encoding='cp950', thousands=',')
        if '代號' in df.columns:
            df['code'] = df['代號'].astype(str).str.strip()
            df['name'] = df['名稱'].astype(str).str.strip()
            df['證券代號'] = df['code']
            df['market'] = 'TWO'
            df['外陸資買賣超股數(不含外資自營商)'] = df['外資及陸資(不含外資自營商)-買賣超股數']
            df['投信買賣超股數'] = df['投信-買賣超股數']
            df['三大法人買賣超股數'] = df['三大法人-買賣超股數']
            return df
    except: pass
    return None

def get_all_chips_data(is_intraday=False):
    print(f"🚀 啟動抓取程序 (模式: 累計 5 日 | 上市+上櫃)...")
    start_delay = 1 if is_intraday else 0
    valid_dfs = [] 
    days_collected = 0
    target_days = 5 
    daily_records = []

    for i in range(start_delay, start_delay + 14):
        if days_collected >= target_days: break
        tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
        date_obj = tw_now - timedelta(days=i)
        
        df_twse = get_twse_chips(date_obj)
        df_tpex = get_tpex_chips(date_obj)
        
        day_dfs = []
        if df_twse is not None: day_dfs.append(df_twse)
        if df_tpex is not None: day_dfs.append(df_tpex)
        
        if day_dfs:
            print(f"   ✅ 取得資料: {date_obj.strftime('%Y-%m-%d')}")
            df_day = pd.concat(day_dfs)
            def clean_num(x):
                if isinstance(x, str): return float(x.replace(',', ''))
                return float(x)
            
            df_day['外資'] = df_day['外陸資買賣超股數(不含外資自營商)'].apply(clean_num) / 1000
            df_day['投信'] = df_day['投信買賣超股數'].apply(clean_num) / 1000
            df_day['總變'] = df_day['三大法人買賣超股數'].apply(clean_num) / 1000
            df_day['date_idx'] = days_collected 
            
            cols = ['code', 'name', 'market', '外資', '投信', '總變', 'date_idx']
            valid_dfs.append(df_day[cols])
            daily_records.append(df_day[cols])
            days_collected += 1
        else:
            print(f"   ⚠️ 無資料: {date_obj.strftime('%Y-%m-%d')}")
            time.sleep(1)

    if not valid_dfs: return pd.DataFrame()
    
    print(f"📊 計算連買天數與加總...")
    merged_df = pd.concat(valid_dfs)
    final_df = merged_df.groupby(['code', 'name', 'market'], as_index=False)[['外資', '投信', '總變']].sum()
    
    # 計算連買天數
    all_daily = pd.concat(daily_records)
    streak_map = {}
    for code, group in all_daily.groupby('code'):
        group = group.sort_values('date_idx') 
        streak = 0
        for val in group['投信']:
            if val > 0: streak += 1
            else: break 
        streak_map[code] = streak

    final_df['trust_streak'] = final_df['code'].map(streak_map).fillna(0).astype(int)
    return final_df

# ==========================================
# 技術指標與即時運算
# ==========================================
def calculate_technical_indicators(df):
    if len(df) < 35: return 50, 50, False, 0, False, False, 0, False, False, 0.0
    low_min = df['Low'].rolling(window=9).min(); high_max = df['High'].rolling(window=9).max()
    rsv = (df['Close'] - low_min) / (high_max - low_min) * 100; rsv = rsv.fillna(50)
    k_values = [50]; d_values = [50]; rsv_list = rsv.tolist()
    for i in range(1, len(rsv_list)):
        k = (2/3) * k_values[-1] + (1/3) * rsv_list[i]; d = (2/3) * d_values[-1] + (1/3) * k
        k_values.append(k); d_values.append(d)
    curr_k = k_values[-1]; curr_d = d_values[-1]; prev_k = k_values[-2]; prev_d = d_values[-2]
    is_kd_gc = (prev_k < prev_d) and (curr_k > curr_d) and (curr_k < 50)
    
    ma60_gap = 0
    if len(df) >= 60:
        ma60 = df['Close'].rolling(window=60).mean().iloc[-1]
        if ma60 > 0: ma60_gap = ((df['Close'].iloc[-1] - ma60) / ma60) * 100
    
    ma20 = df['Close'].rolling(window=20).mean(); std20 = df['Close'].rolling(window=20).std()
    upper = ma20 + (2 * std20); lower = ma20 - (2 * std20)
    is_bb_low = df['Close'].iloc[-1] <= (lower.iloc[-1] * 1.015)
    
    ema12 = df['Close'].ewm(span=12, adjust=False).mean(); ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26; dem = dif.ewm(span=9, adjust=False).mean(); osc = dif - dem
    is_macd_gc = (dif.iloc[-2] < dem.iloc[-2]) and (dif.iloc[-1] > dem.iloc[-1])
    
    pct_change = ((df['Close'].iloc[-1] - df['Close'].iloc[-2]) / df['Close'].iloc[-2]) * 100
    is_spike_high = (df['Close'].iloc[-1] >= upper.iloc[-1]) and (pct_change > 2) and (pct_change < 9.5)
    is_strong_long = (df['Close'].iloc[-1] > ma20.iloc[-1]) and (pct_change > 1.5) and (df['Close'].iloc[-1] < upper.iloc[-1])
    return curr_k, curr_d, is_kd_gc, ma60_gap, is_bb_low, is_macd_gc, osc.iloc[-1], is_spike_high, is_strong_long, pct_change

def add_realtime_data(df_chips, is_intraday):
    print(f"🚀 啟動 yfinance 抓取 (共 {len(df_chips)} 檔)...")
    df_valid = df_chips[df_chips['code'].str.len() == 4].copy()
    df_valid['ticker'] = df_valid.apply(lambda x: f"{x['code']}.TW" if x['market'] == 'TW' else f"{x['code']}.TWO", axis=1)
    yf_tickers = df_valid['ticker'].tolist()
    if not yf_tickers: return df_chips
    
    try: data = yf.download(yf_tickers, period="6mo", progress=False, group_by='ticker')
    except: return df_chips

    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    market_open = tw_now.replace(hour=9, minute=0, second=0, microsecond=0)
    minutes_elapsed = (tw_now - market_open).total_seconds() / 60
    if minutes_elapsed < 1: minutes_elapsed = 1
    if minutes_elapsed > 270: minutes_elapsed = 270

    df_chips['vol_ratio'] = 0.0; df_chips['price_pos'] = 0.5; df_chips['conc_ratio'] = 0.0
    df_chips['ma60_gap'] = 0.0; df_chips['kd_gold_cross'] = False; df_chips['k_val'] = 0.0
    df_chips['bb_low'] = False; df_chips['macd_gc'] = False; df_chips['macd_osc'] = 0.0
    df_chips['spike_high'] = False; df_chips['strong_long'] = False; df_chips['pct_change'] = 0.0

    ticker_map = df_valid.set_index('code')['ticker'].to_dict()

    for index, row in df_chips.iterrows():
        code = row['code']
        if code not in ticker_map: continue
        ticker = ticker_map[code]
        try:
            if ticker not in data.columns.levels[0]: continue
            df_stock = data[ticker].dropna()
            if len(df_stock) < 35: continue

            current_vol = df_stock['Volume'].iloc[-1]
            k, d, is_kd_gc, ma60_gap, is_bb_low, is_macd_gc, osc, is_spike_high, is_strong_long, pct = calculate_technical_indicators(df_stock)
            
            sum_vol_5 = df_stock['Volume'].iloc[-5:].sum()
            avg_vol_5 = df_stock['Volume'].iloc[-6:-1].mean()
            est_vol = current_vol * (270 / minutes_elapsed) if (is_intraday and minutes_elapsed < 270) else current_vol
            if is_intraday: sum_vol_5 = df_stock['Volume'].iloc[-5:-1].sum() + est_vol

            vol_ratio = est_vol / avg_vol_5 if avg_vol_5 > 0 else 1.0
            net_buy_shares = row['總變'] * 1000
            conc = (net_buy_shares / sum_vol_5) * 100 if sum_vol_5 > 0 else 0

            df_chips.at[index, 'vol_ratio'] = round(vol_ratio, 2)
            df_chips.at[index, 'conc_ratio'] = round(conc, 1)
            df_chips.at[index, 'ma60_gap'] = round(ma60_gap, 2)
            df_chips.at[index, 'kd_gold_cross'] = bool(is_kd_gc)
            df_chips.at[index, 'k_val'] = round(k, 1)
            df_chips.at[index, 'bb_low'] = bool(is_bb_low)
            df_chips.at[index, 'macd_gc'] = bool(is_macd_gc)
            df_chips.at[index, 'macd_osc'] = round(osc, 2)
            df_chips.at[index, 'spike_high'] = bool(is_spike_high and vol_ratio > 2.0)
            df_chips.at[index, 'strong_long'] = bool(is_strong_long and vol_ratio > 1.2)
            df_chips.at[index, 'pct_change'] = round(pct, 2)
        except: continue
    return df_chips

def export_data(df):
    print("💾 輸出資料中...")
    df = df.fillna(0)
    output_list = []
    for _, row in df.iterrows():
        record = {
            "code": row['code'], "name": row['name'], "change": row['總變'],
            "three_inst_ratio": row.get('conc_ratio', 0),
            "foreign_ratio_diff": row['外資'], "trust_ratio_diff": row['投信'], 
            "trust_streak": row.get('trust_streak', 0), 
            "vol_ratio": row.get('vol_ratio', 0), "price_pos": row.get('price_pos', 0.5),
            "ma60_gap": row.get('ma60_gap', 0), "kd_gold_cross": row.get('kd_gold_cross', False),
            "k_val": row.get('k_val', 0), "bb_low": row.get('bb_low', False),     
            "macd_gc": row.get('macd_gc', False), "spike_high": row.get('spike_high', False),
            "strong_long": row.get('strong_long', False), "pct_change": row.get('pct_change', 0)
        }
        output_list.append(record)
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    with open(JSON_PATH, 'w', encoding='utf-8') as f: json.dump(output_list, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON 輸出成功！共 {len(output_list)} 筆")
    
    # 準備報表 DataFrame
    excel_df = df.copy().rename(columns={
        'code': '代號', 'name': '名稱', 'market': '市場', '總變': '5日籌碼',
        '外資': '外資5日', '投信': '投信5日', 'trust_streak': '投信連買',
        'conc_ratio': '5日集中%', 'vol_ratio': '預估量比', 'ma60_gap': '季線乖離',
        'spike_high': '高檔爆量(空)', 'strong_long': '強勢動能(多)', 'pct_change': '漲幅%'
    })
    output_cols = ['代號', '名稱', '市場', '漲幅%', '5日籌碼', '外資5日', '投信5日', '投信連買', '5日集中%', '預估量比']
    final_cols = [c for c in output_cols if c in excel_df.columns]
    
    # Excel 輸出
    excel_df[final_cols].to_excel(EXCEL_PATH, index=False)
    print(f"✅ Excel 輸出成功: {EXCEL_PATH}")

    # CSV 輸出 (含 BOM 解決中文亂碼)
    excel_df[final_cols].to_csv(CSV_PATH, index=False, encoding='utf-8-sig')
    print(f"✅ CSV 輸出成功: {CSV_PATH}")

def trigger_gas():
    print(f"🔔 通知 GAS 立即發送戰報...")
    try:
        response = requests.post(GAS_URL, json={"action": "run"})
        print(f"✅ GAS 回應: {response.text}")
    except Exception as e:
        print(f"❌ GAS 觸發失敗: {e}")

def main():
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    is_intraday = (9 <= tw_now.hour < 14)
    df = get_all_chips_data(is_intraday)
    if not df.empty:
        df = add_realtime_data(df, is_intraday)
        export_data(df)
        
        # 🔥 跑完資料後，直接踢 GAS 一腳，叫它起床工作
        trigger_gas()

if __name__ == "__main__": main()
