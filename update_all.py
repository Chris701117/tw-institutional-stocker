import os
import json
import time
import requests
import pandas as pd
import yfinance as yf
import io
from datetime import datetime, timedelta, timezone

# 引入富果 API
from fugle_marketdata import RestClient

# 引入嚴格連線重試機制
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# 設定區
# ==========================================
JSON_PATH = "docs/data/top_three_inst_change_5_up.json"
EXCEL_PATH = "docs/data/stock_report.xlsx"
CSV_PATH = "docs/data/stock_report.csv"

TDCC_HISTORY_PATH = "docs/data/tdcc_history.json"
TDCC_REPORT_PATH = "docs/data/tdcc_report.json"

HISTORY_DAYS = 120 

FORCE_RUN_SATURDAY = False
GAS_URL = "https://script.google.com/macros/s/AKfycbzkOm64edpadEtMUJZGkzGvU_IjYdAPj8Hs2cute5J2BC82SFdflxaA3URszd3zWcnp/exec" 

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

FUGLE_API_KEY = os.getenv("FUGLE_API_KEY", "")

# ==========================================
# 建立強健連線與嚴格重試機制
# ==========================================
session = requests.Session()
retry = Retry(connect=5, backoff_factor=1, status_forcelist=[403, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retry)
session.mount('http://', adapter)
session.mount('https://', adapter)

# ==========================================
# 核心函式：抓取籌碼 (嚴格真實模式)
# ==========================================
def get_twse_chips(date_obj):
    date_str = date_obj.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=csv&selectType=ALL&date={date_str}"
    try:
        time.sleep(3)
        res = session.get(url, headers=HEADERS, timeout=15)
        
        if res.status_code != 200: 
            raise ValueError(f"TWSE 回傳錯誤碼: {res.status_code}")
            
        text = res.text
        # 🔥【修正】若內容行數少於等於2行(只有標題)，代表當天尚未出資料或休市，正常略過
        if len(text.strip().split('\n')) <= 2:
            return pd.DataFrame() 

        df = pd.read_csv(io.StringIO(text), header=1, thousands=',')
        df.columns = [c.strip() for c in df.columns]
        
        if '證券代號' not in df.columns:
            df = pd.read_csv(io.StringIO(text), header=2, thousands=',')
            df.columns = [c.strip() for c in df.columns]

        if '證券代號' in df.columns:
            df['code'] = df['證券代號'].astype(str).str.replace('=', '').str.replace('"', '').str.strip()
            df['name'] = df['證券名稱'].astype(str).str.strip()
            df['market'] = 'TW'
            return df
        else:
            raise ValueError("欄位解析失敗，可能證交所格式已更改")
    except Exception as e:
        print(f" ❌ [錯誤] TWSE 抓取異常: {e}", end="")
        return None

def get_tpex_chips(date_obj):
    minguo_year = date_obj.year - 1911
    date_str = f"{minguo_year}/{date_obj.month:02d}/{date_obj.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result_download.php?l=zh-tw&se=EW&t=D&d={date_str}"
    try:
        time.sleep(3)
        res = session.get(url, headers=HEADERS, timeout=15)
        if res.status_code != 200: 
            raise ValueError(f"TPEX 回傳錯誤碼: {res.status_code}")
            
        text = res.text
        # 🔥【修正】若內容行數少於等於2行，代表當天尚未出資料或休市，正常略過
        if len(text.strip().split('\n')) <= 2:
            return pd.DataFrame()

        df = pd.read_csv(io.StringIO(text), header=1, thousands=',')
        df.columns = [c.strip() for c in df.columns]

        if '代號' not in df.columns:
             df = pd.read_csv(io.StringIO(text), header=2, thousands=',')
             df.columns = [c.strip() for c in df.columns]
             
        if '代號' in df.columns:
            df['code'] = df['代號'].astype(str).str.strip()
            df['name'] = df['名稱'].astype(str).str.strip()
            df['market'] = 'TWO'
            if '三大法人買賣超股數' not in df.columns and '三大法人-買賣超股數' in df.columns:
                df['三大法人買賣超股數'] = df['三大法人-買賣超股數']
            return df
        else:
            raise ValueError("欄位解析失敗")
    except Exception as e:
        print(f" ❌ [錯誤] TPEX 抓取異常: {e}", end="")
        return None

def get_all_chips_data(is_intraday=False):
    print(f"🚀 啟動抓取程序 (嚴格真實模式 | 上市+上櫃)...")
    
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    
    # 🔥【修正】確保時間邏輯正確：
    # 如果是下午 3 點以後，才能抓「今天(0)」的資料；否則（半夜、早上、盤中）都從「昨天(1)」開始推算
    start_delay = 0 if tw_now.hour >= 15 else 1
    
    valid_dfs = [] 
    days_collected = 0
    target_days = 5 
    daily_records = []

    for i in range(start_delay, start_delay + 20):
        if days_collected >= target_days: break
        
        date_obj = tw_now - timedelta(days=i)
        
        if date_obj.weekday() >= 5: continue
        
        print(f"   🔍 嘗試抓取: {date_obj.strftime('%Y-%m-%d')} ...", end="")
        
        df_twse = get_twse_chips(date_obj)
        df_tpex = get_tpex_chips(date_obj)
        
        if df_twse is None or df_tpex is None:
            print("\n🚨 [嚴格模式攔截] 偵測到資料連線失敗或被封鎖。為確保數據 100% 真實，已強制中斷程式，拒絕使用舊資料拼湊！")
            return pd.DataFrame()
        
        day_dfs = []
        if not df_twse.empty: day_dfs.append(df_twse)
        if not df_tpex.empty: day_dfs.append(df_tpex)
        
        if day_dfs:
            print(f" ✅ 成功")
            df_day = pd.concat(day_dfs)
            
            col_foreign = '外陸資買賣超股數(不含外資自營商)'
            col_trust = '投信買賣超股數'
            col_total = '三大法人買賣超股數'
            
            if col_foreign not in df_day.columns and '外資及陸資(不含外資自營商)-買賣超股數' in df_day.columns:
                col_foreign = '外資及陸資(不含外資自營商)-買賣超股數'
            if col_trust not in df_day.columns and '投信-買賣超股數' in df_day.columns:
                col_trust = '投信-買賣超股數'

            def parse_col(df, col_name):
                if col_name in df.columns:
                    return df[col_name].astype(str).str.replace(',', '').apply(pd.to_numeric, errors='coerce').fillna(0)
                return 0

            df_day['外資'] = parse_col(df_day, col_foreign) / 1000
            df_day['投信'] = parse_col(df_day, col_trust) / 1000
            df_day['總變'] = parse_col(df_day, col_total) / 1000
            df_day['date_idx'] = days_collected 
            
            cols = ['code', 'name', 'market', '外資', '投信', '總變', 'date_idx']
            df_clean = df_day[cols].copy()
            
            valid_dfs.append(df_clean)
            daily_records.append(df_clean)
            days_collected += 1
        else:
            print(f" 💤 無資料 (確認為假日休市或盤後資料尚未結算)")

    if not valid_dfs: return pd.DataFrame()
    
    print(f"\n📊 計算連買天數與加總...")
    merged_df = pd.concat(valid_dfs)
    final_df = merged_df.groupby(['code', 'name', 'market'], as_index=False)[['外資', '投信', '總變']].sum()
    
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

def get_tdcc_data():
    print("🚀 啟動週六集保大戶抓取...")
    url = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"
    print("   📥 下載集保 CSV 中...")
    try:
        res = requests.get(url, headers=HEADERS)
        if res.status_code != 200: return False, f"HTTP Error {res.status_code}"
        s = res.content
        df = pd.read_csv(io.StringIO(s.decode('utf-8')), encoding='utf-8', thousands=',')
    except:
        print("   ⚠️ UTF-8 失敗，嘗試 Big5...")
        try:
            res = requests.get(url, headers=HEADERS)
            s = res.content
            df = pd.read_csv(io.StringIO(s.decode('big5')), encoding='big5', thousands=',')
        except Exception as e: return False, str(e)

    try:
        df.columns = [c.strip() for c in df.columns]
        df['證券代號'] = df['證券代號'].astype(str).str.strip()
        df = df[
            (df['證券代號'].str.len() == 4) & 
            (~df['證券代號'].str.startswith('00')) &
            (df['證券代號'].str.isdigit()) 
        ].copy()

        target_tiers = [12, 13, 14, 15] 
        for col in ['持股分級', '人數', '股數', '占集保庫存數比例%']:
            if col in df.columns:
                if df[col].dtype == 'object':
                    df[col] = df[col].astype(str).str.replace(',', '').apply(pd.to_numeric, errors='coerce')
                else:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                
        mask = df['持股分級'].isin(target_tiers)
        df_big = df[mask].copy()
        df_sum = df_big.groupby('證券代號').agg({'人數': 'sum', '占集保庫存數比例%': 'sum'}).reset_index()
    except Exception as e: return False, f"資料清洗失敗: {e}"
    
    last_week_map = {}
    is_first_run = True
    if os.path.exists(TDCC_HISTORY_PATH):
        try:
            with open(TDCC_HISTORY_PATH, 'r', encoding='utf-8') as f:
                old_data = json.load(f)
                if old_data:
                    is_first_run = False
                    for d in old_data: last_week_map[str(d['code'])] = d
        except: pass
    
    name_map = {}
    try:
        if os.path.exists(CSV_PATH):
            df_names = pd.read_csv(CSV_PATH)
            name_map = dict(zip(df_names['代號'].astype(str), df_names['名稱']))
    except: pass

    report_list = []
    history_list = [] 
    for _, row in df_sum.iterrows():
        code = str(row['證券代號'])
        holders = int(row['人數'])
        pct = float(row['占集保庫存數比例%'])
        last = last_week_map.get(code, {'holders': holders, 'pct': pct})
        
        stock_name = name_map.get(code, code)
        item = {
            "code": code,
            "name": stock_name,
            "holders": holders,
            "hold_pct": round(pct, 2),
            "diff_holders": holders - last['holders'],
            "diff_pct": round(pct - last['pct'], 2)
        }
        report_list.append(item)
        history_list.append({"code": code, "holders": holders, "pct": pct})

    os.makedirs(os.path.dirname(TDCC_HISTORY_PATH), exist_ok=True)
    with open(TDCC_HISTORY_PATH, 'w', encoding='utf-8') as f: json.dump(history_list, f, ensure_ascii=False)
    with open(TDCC_REPORT_PATH, 'w', encoding='utf-8') as f: json.dump(report_list, f, ensure_ascii=False)
    print(f"   💾 集保數據處理完成！共 {len(report_list)} 檔")
    return True, "OK"

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
    print(f"🚀 啟抓 yfinance (共 {len(df_chips)} 檔)...")
    df_valid = df_chips[df_chips['code'].str.len() == 4].copy()
    if df_valid.empty: return df_chips

    df_valid['ticker'] = df_valid.apply(lambda x: f"{x['code']}.TW" if x['market'] == 'TW' else f"{x['code']}.TWO", axis=1)
    yf_tickers = df_valid['ticker'].tolist()
    if not yf_tickers: return df_chips
    
    try: 
        data = yf.download(yf_tickers, period="6mo", progress=False, group_by='ticker')
    except Exception as e: 
        print(f"❌ yfinance 下載失敗: {e}")
        return df_chips

    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    market_open = tw_now.replace(hour=9, minute=0, second=0, microsecond=0)
    minutes_elapsed = (tw_now - market_open).total_seconds() / 60
    if minutes_elapsed < 1: minutes_elapsed = 1
    if minutes_elapsed > 270: minutes_elapsed = 270

    for col in ['vol_ratio', 'conc_ratio', 'ma60_gap', 'k_val', 'macd_osc', 'pct_change']:
        df_chips[col] = 0.0
    for col in ['kd_gold_cross', 'bb_low', 'macd_gc', 'spike_high', 'strong_long']:
        df_chips[col] = False
    df_chips['price_pos'] = 0.5

    ticker_map = df_valid.set_index('code')['ticker'].to_dict()

    fugle_client = None
    if FUGLE_API_KEY:
        try:
            fugle_client = RestClient(api_key=FUGLE_API_KEY)
            print("💎 已掛載富果 API 金鑰，啟用【零延遲精準報價】混血模式！")
        except Exception as e:
            print(f"⚠️ 富果初始化失敗 ({e})，降級為純 yfinance 模式")

    for index, row in df_chips.iterrows():
        code = row['code']
        if code not in ticker_map: continue
        ticker = ticker_map[code]
        try:
            if len(yf_tickers) == 1:
                df_stock = data.dropna()
            else:
                if ticker not in data.columns.levels[0]: continue
                df_stock = data[ticker].dropna()
            
            if len(df_stock) < 35: continue

            if fugle_client and is_intraday:
                try:
                    quote = fugle_client.stock.intraday.quote(symbol=code)
                    fugle_price = quote.get('lastPrice', None)
                    fugle_vol = quote.get('total', {}).get('tradeVolume', 0)
                    
                    if fugle_price is not None and fugle_vol > 0:
                        df_stock.iloc[-1, df_stock.columns.get_loc('Close')] = fugle_price
                        df_stock.iloc[-1, df_stock.columns.get_loc('Volume')] = fugle_vol
                        df_stock.iloc[-1, df_stock.columns.get_loc('High')] = max(df_stock['High'].iloc[-1], fugle_price)
                        df_stock.iloc[-1, df_stock.columns.get_loc('Low')] = min(df_stock['Low'].iloc[-1], fugle_price)
                    
                    time.sleep(1.1)
                except Exception as e:
                    print(f"⚠️ 富果抓取 {code} 異常，回退使用 yfinance ({e})")
                    time.sleep(1.1)

            current_vol = df_stock['Volume'].iloc[-1]
            k, d, is_kd_gc, ma60_gap, is_bb_low, is_macd_gc, osc, is_spike_high, is_strong_long, pct = calculate_technical_indicators(df_stock)
            
            sum_vol_5 = df_stock['Volume'].iloc[-5:].sum()
            avg_vol_5 = df_stock['Volume'].iloc[-6:-1].mean()
            est_vol = current_vol * (270 / minutes_elapsed) if (is_intraday and minutes_elapsed < 270) else current_vol
            if is_intraday: sum_vol_5 = df_stock['Volume'].iloc[-5:-1].sum() + est_vol

            if sum_vol_5 < 500000:
                conc = 0
                vol_ratio = 0
            else:
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
        except Exception as e: continue
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
    
    excel_df = df.copy().rename(columns={
        'code': '代號', 'name': '名稱', 'market': '市場', '總變': '5日籌碼',
        '外資': '外資5日', '投信': '投信5日', 'trust_streak': '投信連買',
        'conc_ratio': '5日集中%', 'vol_ratio': '預估量比', 'ma60_gap': '季線乖離',
        'spike_high': '高檔爆量(空)', 'strong_long': '強勢動能(多)', 'pct_change': '漲幅%'
    })
    output_cols = ['代號', '名稱', '市場', '漲幅%', '5日籌碼', '外資5日', '投信5日', '投信連買', '5日集中%', '預估量比']
    final_cols = [c for c in output_cols if c in excel_df.columns]
    
    excel_df[final_cols].to_excel(EXCEL_PATH, index=False)
    print(f"✅ Excel 輸出成功: {EXCEL_PATH}")

    excel_df[final_cols].to_csv(CSV_PATH, index=False, encoding='utf-8-sig')
    print(f"✅ CSV 輸出成功: {CSV_PATH}")

def trigger_gas(action_name="run", error_msg=None):
    print(f"🔔 通知 GAS (Action: {action_name})...")
    payload = {"action": action_name}
    if error_msg: payload["error"] = error_msg
    try:
        response = requests.post(GAS_URL, json=payload)
        print(f"✅ GAS 回應: {response.text}")
    except Exception as e: print(f"❌ GAS 觸發失敗: {e}")

def main():
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    is_saturday = (tw_now.weekday() == 5)

    if FORCE_RUN_SATURDAY:
        print("⚠️ 強制執行週六模式 (測試用)")
        is_saturday = True

    if is_saturday:
        success, msg = get_tdcc_data()
        if not success:
            print(f"❌ 集保抓取失敗: {msg}")
            trigger_gas(action_name="error_report", error_msg=msg) 
    else:
        # ✅ 改回真實時間判斷：早上 9 點到 下午 3 點前為盤中，其餘為盤後
        is_intraday = (9 <= tw_now.hour < 15) 
        
        df = get_all_chips_data(is_intraday)
        if not df.empty:
            df = add_realtime_data(df, is_intraday)
            export_data(df)
            print("✅ 資料處理完畢，等待 GitHub Actions 執行 Push 與通知 GAS...")
        else: print("❌ 無法取得任何籌碼資料，程式結束。")

if __name__ == "__main__": main()
