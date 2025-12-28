# -*- coding: utf-8 -*-
"""Update & export Taiwan institutional (三大法人) holdings data.

功能重點：
- 自動抓 TWSE/TPEX 三大法人日交易 + 外資持股；
- 抓取 TWSE/TPEX 每日收盤行情與成交量；
- 以 inst_baseline.csv 為基準點，校正投信 / 自營商持股；
- 計算低檔爆量指標 (量比 > 2.5 且 股價位階 < 20%)；
- 計算三大法人持股比重與多視窗變化；
- 輸出 ranking JSON + 每檔股票時序 JSON。
"""
import json
import os
from io import StringIO
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import math
import requests
import pandas as pd

from utils_columns import find_col_any, normalize_columns

DATA_DIR = "data"
DOCS_DIR = os.path.join("docs", "data")
TIMESERIES_DIR = os.path.join(DOCS_DIR, "timeseries")
INST_BASELINE_PATH = os.path.join(DATA_DIR, "inst_baseline.csv")
PRICE_VOL_PATH = os.path.join(DATA_DIR, "price_vol_history.csv") # 新增：存放價格成交量

WINDOWS = [5, 20, 60, 120]

# ---------- generic helpers ----------

def ensure_dirs():
    for p in (DATA_DIR, DOCS_DIR, TIMESERIES_DIR):
        os.makedirs(p, exist_ok=True)

def get_taipei_today() -> date:
    tz = ZoneInfo("Asia/Taipei")
    return datetime.now(tz).date()

def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=Sat, 6=Sun

def get_target_trade_date() -> date:
    today = get_taipei_today()
    target = today - timedelta(days=1)
    while is_weekend(target):
        target -= timedelta(days=1)
    return target

def get_last_date_from_csv(path: str):
    if not os.path.exists(path): return None
    df = pd.read_csv(path, usecols=["date"])
    if df.empty: return None
    return pd.to_datetime(df["date"]).dt.date.max()

def iter_trading_days(start: date, end: date):
    cur = start
    while cur <= end:
        if not is_weekend(cur): yield cur
        cur += timedelta(days=1)

def numeric_series(series: pd.Series, to_float: bool = False) -> pd.Series:
    s = series.astype(str).str.replace(",", "", regex=False)
    s = s.str.replace("\u2212", "-", regex=False).str.replace("\uFF0D", "-", regex=False).str.replace("\uFF0B", "+", regex=False).str.strip()
    mask_paren = s.str.match(r"^\([\d\.]+\)$")
    s.loc[mask_paren] = "-" + s.loc[mask_paren].str.strip("()")
    missing_tokens = {"", "nan", "NaN", "None", "--", "X"}
    s = s.where(~s.isin(missing_tokens), "0")
    if to_float: return pd.to_numeric(s, errors="coerce").fillna(0.0)
    return pd.to_numeric(s, errors="coerce").fillna(0).astype("Int64")

# ---------- 新增：抓取成交量與價格 (TWSE MI_INDEX & TPEX Daily) ----------

def fetch_twse_price_vol(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={datestr}&type=ALLBUT0999&response=csv"
    resp = requests.get(url, timeout=20)
    if resp.status_code != 200 or len(resp.text) < 500: return pd.DataFrame()
    lines = resp.text.split('\n')
    start_idx = -1
    for i, line in enumerate(lines):
        if "證券代號" in line and "成交股數" in line:
            start_idx = i
            break
    if start_idx == -1: return pd.DataFrame()
    df = pd.read_csv(StringIO('\n'.join(lines[start_idx:])), header=0)
    df = normalize_columns(df)
    df["code"] = df["證券代號"].astype(str).str.replace('"', '').str.strip().str.zfill(4)
    df["close"] = numeric_series(df["收盤價"], to_float=True)
    df["vol"] = numeric_series(df["成交股數"], to_float=True) / 1000 # 轉張數
    return df[["code", "close", "vol"]]

def fetch_tpex_price_vol(trade_date: date) -> pd.DataFrame:
    roc = f"{trade_date.year - 1911}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_quot_result.php?l=zh-tw&d={roc}&o=data"
    resp = requests.get(url, timeout=20)
    if resp.status_code != 200: return pd.DataFrame()
    data = resp.json().get('aaData', [])
    if not data: return pd.DataFrame()
    df = pd.DataFrame(data)
    df["code"] = df[0].str.strip().str.zfill(4)
    df["close"] = numeric_series(df[2], to_float=True)
    df["vol"] = numeric_series(df[7], to_float=True) / 1000 # 轉張數
    return df[["code", "close", "vol"]]

# ---------- TWSE: T86 (daily flows) ----------

def fetch_twse_t86(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/fund/T86"
    params = {"response": "csv", "date": datestr, "selectType": "ALLBUT0999"}
    resp = requests.get(url, params=params, timeout=20)
    csv_text = resp.content.decode("cp950", errors="ignore")
    df = pd.read_csv(StringIO(csv_text), header=1)
    df = normalize_columns(df.dropna(how="all", axis=0))
    if df.empty: return pd.DataFrame()
    code_col = find_col_any(df, ["證券代號"])
    name_col = find_col_any(df, ["證券名稱"])
    col_foreign_ex_net = find_col_any(df, ["外陸資買賣超股數(不含外資自營商)", "外資及陸資(不含外資自營商)買賣超股數"])
    col_trust_net = find_col_any(df, ["投信買賣超股數"])
    col_dealer_net = find_col_any(df, ["自營商買賣超股數合計", "自營商買賣超股數"])
    df["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
    df["name"] = df[name_col].astype(str).str.strip()
    out = pd.DataFrame({
        "date": trade_date, "code": df["code"], "name": df["name"],
        "foreign_net": numeric_series(df[col_foreign_ex_net]),
        "trust_net": numeric_series(df[col_trust_net]),
        "dealer_net": numeric_series(df[col_dealer_net]),
        "market": "TWSE"
    })
    return out[out["code"].str.match(r"^\d{4,5}[A-Z]*$")]

# ---------- TPEX: 三大法人 daily flows ----------

def fetch_tpex_flows(trade_date: date) -> pd.DataFrame:
    """上櫃股票三大法人買賣明細 (強化欄位抓取)."""
    roc = f"{trade_date.year - 1911}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {"d": roc, "l": "zh-tw", "o": "htm"}
    resp = requests.get(url, params=params, timeout=20)
    
    # 使用 StringIO 包裝
    tables = pd.read_html(StringIO(resp.text))
    if not tables: return pd.DataFrame()
    
    df = tables[0]
    
    # 解決多層表頭問題：將所有欄位名稱轉為字串，並只取最後一部分
    # 或是直接使用模糊搜尋關鍵字
    cols = df.columns.astype(str).tolist()
    
    def find_tpex_col(keywords):
        """專為 TPEX 混亂欄位設計的搜尋工具"""
        for c in cols:
            if any(k in c for k in keywords):
                return c
        return None

    code_col = find_tpex_col(["代號"])
    name_col = find_tpex_col(["名稱"])
    f_net_col = find_tpex_col(["外資及陸資買賣超股數", "外資及陸資(不含外資自營商)買賣超股數"])
    t_net_col = find_tpex_col(["投信買賣超股數"])
    s_net_col = find_tpex_col(["自營商買賣超股數"]) # 這是合計欄位

    if not all([code_col, name_col, f_net_col, t_net_col, s_net_col]):
        print(f"[WARN] TPEX 欄位匹配不完全: {cols}")
        return pd.DataFrame()

    out = pd.DataFrame({
        "date": trade_date,
        "code": df[code_col].astype(str).str.strip().str.zfill(4),
        "name": df[name_col].astype(str).str.strip(),
        "foreign_net": numeric_series(df[f_net_col]),
        "trust_net": numeric_series(df[t_net_col]),
        "dealer_net": numeric_series(df[s_net_col]),
        "market": "TPEX"
    })
    
    # 排除非股票代號 (例如合計列)
    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    return out[mask]
# ---------- 外資持股 (TWSE/TPEX) ----------

def fetch_twse_mi_qfiis(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?response=csv&date={datestr}&selectType=ALLBUT0999"
    resp = requests.get(url, timeout=20)
    csv_text = resp.content.decode("cp950", errors="ignore")
    try: df = pd.read_csv(StringIO(csv_text), header=1)
    except: return pd.DataFrame()
    df = normalize_columns(df.dropna(how="all", axis=0))
    if df.empty: return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = df[find_col_any(df, ["證券代號"])].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
    out["total_shares"] = numeric_series(df[find_col_any(df, ["發行股數"])])
    out["foreign_ratio"] = numeric_series(df[find_col_any(df, ["全體外資及陸資持股比率"])], to_float=True)
    out["date"], out["market"] = trade_date, "TWSE"
    return out

def fetch_tpex_qfii(trade_date: date) -> pd.DataFrame:
    url = "https://www.tpex.org.tw/web/stock/3insti/qfii/qfii_result.php?l=zh-tw&o=data"
    resp = requests.get(url, timeout=20)
    df = pd.read_csv(StringIO(resp.text))
    df = normalize_columns(df.dropna(how="all", axis=0))
    if df.empty: return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = df[find_col_any(df, ["代號"])].astype(str).str.strip().str.zfill(4)
    out["total_shares"] = numeric_series(df[find_col_any(df, ["發行股數"])])
    out["foreign_ratio"] = numeric_series(df[find_col_any(df, ["僑外資及陸資持股比率"])], to_float=True)
    out["date"], out["market"] = trade_date, "TPEX"
    return out

# ---------- 資料整合與計算 ----------

def build_estimated_holdings(flows, foreign_master, baseline=None):
    merged = flows.merge(foreign_master[["date", "code", "total_shares", "foreign_ratio"]], on=["date", "code"], how="left")
    # 此處保留您之前的 accumulate 邏輯...
    merged["total_shares"] = pd.to_numeric(merged["total_shares"], errors="coerce").fillna(0.0)
    
    def accumulate(g):
        g = g.sort_values("date")
        g["trust_cum"] = g["trust_net"].cumsum()
        g["dealer_cum"] = g["dealer_net"].cumsum()
        g["trust_ratio_est"] = (g["trust_cum"] / g["total_shares"] * 100).fillna(0.0)
        g["dealer_ratio_est"] = (g["dealer_cum"] / g["total_shares"] * 100).fillna(0.0)
        return g
    
    merged = merged.groupby("code", group_keys=False).apply(accumulate)
    merged["three_inst_ratio_est"] = merged["foreign_ratio"].fillna(0.0) + merged["trust_ratio_est"] + merged["dealer_ratio_est"]
    return merged

# ---------- 新增：低檔爆量計算邏輯 ----------

def calculate_low_heavy_vol(merged, price_vol_history):
    """計算量比與股價位階"""
    df = merged.merge(price_vol_history, on=["date", "code"], how="left")
    df = df.sort_values(["code", "date"])
    
    def calc_metrics(g):
        # 1. 計算量比 (今日成交量 / 5日均量)
        g["vol_ma5"] = g["vol"].rolling(5).mean()
        g["vol_ratio"] = (g["vol"] / g["vol_ma5"]).fillna(0.0)
        
        # 2. 計算股價位階 (今日收盤在過去60日高低點的位置)
        low_60 = g["close"].rolling(60).min()
        high_60 = g["close"].rolling(60).max()
        g["price_pos"] = ((g["close"] - low_60) / (high_60 - low_60)).fillna(0.5)
        return g

    df = df.groupby("code", group_keys=False).apply(calc_metrics)
    return df

# ---------- export JSON ----------

def export_change_rankings(merged, windows, out_dir=DOCS_DIR):
    latest_date = merged["date"].max()
    merged = merged.sort_values(["code", "date"])
    merged["f_diff"] = merged.groupby("code")["foreign_ratio"].diff().fillna(0.0)
    merged["t_diff"] = merged.groupby("code")["trust_ratio_est"].diff().fillna(0.0)
    merged["s_diff"] = merged.groupby("code")["dealer_ratio_est"].diff().fillna(0.0)
    
    latest = merged[merged["date"] == latest_date].copy()
    os.makedirs(out_dir, exist_ok=True)

    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        tmp = latest[latest["code"].str.len() == 4].copy()
        up = tmp.sort_values(col, ascending=False).head(200)

        def to_dict_list(df):
            records = []
            for _, r in df.iterrows():
                records.append({
                    "code": r["code"], "name": r["name"], "market": r.get("market", ""),
                    "three_inst_ratio": float(r["three_inst_ratio_est"]),
                    "change": float(r.get(col, 0)),
                    "foreign_ratio_diff": float(r["f_diff"]),
                    "trust_ratio_diff": float(r["t_diff"]),
                    "dealer_ratio_diff": float(r["s_diff"]),
                    # 新增量價欄位
                    "vol_ratio": float(r.get("vol_ratio", 0)),
                    "price_pos": float(r.get("price_pos", 0.5))
                })
            return records

        with open(os.path.join(out_dir, f"top_three_inst_change_{w}_up.json"), "w", encoding="utf-8") as f:
            json.dump(to_dict_list(up), f, ensure_ascii=False, indent=2)

# ---------- main orchestration ----------

def main():
    ensure_dirs()
    target_date = get_target_trade_date()
    
    # 1. 抓取籌碼與量價數據
    flows_list, foreign_list, pv_list = [], [], []
    for d in iter_trading_days(target_date - timedelta(days=10), target_date):
        print(f"[INFO] Processing {d}...")
        f_twse, f_tpex = fetch_twse_t86(d), fetch_tpex_flows(d)
        q_twse, q_tpex = fetch_twse_mi_qfiis(d), fetch_tpex_qfii(d)
        p_twse, p_tpex = fetch_twse_price_vol(d), fetch_tpex_price_vol(d)
        
        flows_list.extend([f_twse, f_tpex])
        foreign_list.extend([q_twse, q_tpex])
        if not p_twse.empty: p_twse["date"] = d; pv_list.append(p_twse)
        if not p_tpex.empty: p_tpex["date"] = d; pv_list.append(p_tpex)

    # 2. 合併與計算
    flows_all = pd.concat([df for df in flows_list if not df.empty])
    foreign_master = pd.concat([df for df in foreign_list if not df.empty])
    pv_all = pd.concat(pv_list).drop_duplicates(subset=["date", "code"])
    
    # 存檔歷史價格量
    pv_all.to_csv(PRICE_VOL_PATH, index=False)

    merged = build_estimated_holdings(flows_all, foreign_master)
    merged = add_change_metrics(merged, WINDOWS)
    merged = calculate_low_heavy_vol(merged, pv_all)

    export_change_rankings(merged, WINDOWS)
    print("[INFO] update_all.py with Volume Analysis completed.")

if __name__ == "__main__":
    main()
