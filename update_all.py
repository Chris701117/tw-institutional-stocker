# -*- coding: utf-8 -*-
"""Update & export Taiwan institutional (三大法人) holdings data.

功能重點：
- 自動抓 TWSE/TPEX 三大法人日交易 + 外資持股；
- 抓取 TWSE/TPEX 每日收盤行情與成交量；
- 以 inst_baseline.csv 為基準點，校正投信 / 自營商持股；
- 計算低檔爆量指標 (量比 > 2.5 且 股價位階 < 25%)；
- 支援盤中時間加權預估全天成交量；
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
PRICE_VOL_PATH = os.path.join(DATA_DIR, "price_vol_history.csv")

WINDOWS = [5, 20, 60, 120]

# ---------- generic helpers ----------

def ensure_dirs():
    for p in (DATA_DIR, DOCS_DIR, TIMESERIES_DIR):
        os.makedirs(p, exist_ok=True)

def get_taipei_today() -> date:
    tz = ZoneInfo("Asia/Taipei")
    return datetime.now(tz).date()

def is_weekend(d: date) -> bool:
    return d.weekday() >= 5

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

# ---------- 抓取成交量與價格 (含盤中防錯) ----------

def fetch_twse_price_vol(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={datestr}&type=ALLBUT0999&response=csv"
    try:
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
        df["vol"] = numeric_series(df["成交股數"], to_float=True) / 1000 
        return df[["code", "close", "vol"]]
    except: return pd.DataFrame()

def fetch_tpex_price_vol(trade_date: date) -> pd.DataFrame:
    roc = f"{trade_date.year - 1911}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_quot_result.php?l=zh-tw&d={roc}&o=data"
    try:
        resp = requests.get(url, timeout=20)
        # 增加 JSON 解析防錯
        data = resp.json().get('aaData', [])
    except Exception as e:
        print(f"[INFO] TPEX Price data not available yet for {trade_date}")
        return pd.DataFrame()
    
    if not data: return pd.DataFrame()
    df = pd.DataFrame(data)
    df["code"] = df[0].str.strip().str.zfill(4)
    df["close"] = numeric_series(df[2], to_float=True)
    df["vol"] = numeric_series(df[7], to_float=True) / 1000 
    return df[["code", "close", "vol"]]

# ---------- 三大法人買賣超 (TWSE/TPEX) ----------

def fetch_twse_t86(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/fund/T86"
    params = {"response": "csv", "date": datestr, "selectType": "ALLBUT0999"}
    try:
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
    except: return pd.DataFrame()

def fetch_tpex_flows(trade_date: date) -> pd.DataFrame:
    """上櫃股票三大法人買賣明細 (修正 MultiIndex 與模糊欄位搜尋)."""
    roc = f"{trade_date.year - 1911}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {"d": roc, "l": "zh-tw", "o": "htm"}
    try:
        resp = requests.get(url, params=params, timeout=20)
        tables = pd.read_html(StringIO(resp.text))
        if not tables: return pd.DataFrame()
        df = tables[0]
        
        # 處理 MultiIndex 壓平問題
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = ['_'.join([str(c) for c in col if 'Unnamed' not in str(c)]) for col in df.columns.values]
        else:
            df.columns = df.columns.astype(str)

        cols = df.columns.tolist()
        def find_tpex_col(keywords):
            for c in cols:
                if any(k in c for k in keywords): return c
            return None

        code_col = find_tpex_col(["代號"])
        name_col = find_tpex_col(["名稱"])
        f_net_col = find_tpex_col(["外資及陸資買賣超股數", "外資及陸資(不含外資自營商)買賣超股數"])
        t_net_col = find_tpex_col(["投信買賣超股數"])
        s_net_col = find_tpex_col(["自營商買賣超股數"]) 

        if not all([code_col, name_col, f_net_col, t_net_col, s_net_col]): return pd.DataFrame()

        out = pd.DataFrame({
            "date": trade_date,
            "code": df[code_col].astype(str).str.strip().str.zfill(4),
            "name": df[name_col].astype(str).str.strip(),
            "foreign_net": numeric_series(df[f_net_col]),
            "trust_net": numeric_series(df[t_net_col]),
            "dealer_net": numeric_series(df[s_net_col]),
            "market": "TPEX"
        })
        return out[out["code"].str.match(r"^\d{4,5}[A-Z]*$")]
    except: return pd.DataFrame()

# ---------- 外資持股與持股計算 ----------

def fetch_twse_mi_qfiis(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = f"https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS?response=csv&date={datestr}&selectType=ALLBUT0999"
    try:
        resp = requests.get(url, timeout=20)
        csv_text = resp.content.decode("cp950", errors="ignore")
        df = pd.read_csv(StringIO(csv_text), header=1)
        df = normalize_columns(df.dropna(how="all", axis=0))
        out = pd.DataFrame()
        out["code"] = df[find_col_any(df, ["證券代號"])].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
        out["total_shares"] = numeric_series(df[find_col_any(df, ["發行股數"])])
        out["foreign_ratio"] = numeric_series(df[find_col_any(df, ["全體外資及陸資持股比率"])], to_float=True)
        out["date"], out["market"] = trade_date, "TWSE"
        return out
    except: return pd.DataFrame()

def fetch_tpex_qfii(trade_date: date) -> pd.DataFrame:
    url = "https://www.tpex.org.tw/web/stock/3insti/qfii/qfii_result.php?l=zh-tw&o=data"
    try:
        resp = requests.get(url, timeout=20)
        df = pd.read_csv(StringIO(resp.text))
        df = normalize_columns(df.dropna(how="all", axis=0))
        out = pd.DataFrame()
        out["code"] = df[find_col_any(df, ["代號"])].astype(str).str.strip().str.zfill(4)
        out["total_shares"] = numeric_series(df[find_col_any(df, ["發行股數"])])
        out["foreign_ratio"] = numeric_series(df[find_col_any(df, ["僑外資及陸資持股比率"])], to_float=True)
        out["date"], out["market"] = trade_date, "TPEX"
        return out
    except: return pd.DataFrame()

def build_estimated_holdings(flows, foreign_master):
    merged = flows.merge(foreign_master[["date", "code", "total_shares", "foreign_ratio"]], on=["date", "code"], how="left")
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

def add_change_metrics(merged, windows):
    merged = merged.sort_values(["code", "date"])
    for w in windows:
        merged[f"three_inst_ratio_change_{w}"] = merged.groupby("code")["three_inst_ratio_est"].diff(periods=w)
    return merged

# ---------- 盤中爆量預估與輸出 ----------

def calculate_low_heavy_vol(merged, price_vol_history):
    df = merged.merge(price_vol_history, on=["date", "code"], how="left")
    df = df.sort_values(["code", "date"])
    
    tz = ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    
    # 盤中時間加權邏輯
    if 9 <= now.hour < 14:
        minutes_passed = max(1, min(270, (now.hour - 9) * 60 + now.minute))
        time_weight = 270 / minutes_passed
    else:
        time_weight = 1.0

    def calc_metrics(g):
        g["est_vol"] = g["vol"] * time_weight
        g["vol_ma5"] = g["vol"].shift(1).rolling(5).mean() # 用前5日均量
        g["vol_ratio"] = (g["est_vol"] / g["vol_ma5"]).fillna(0.0)
        low_60, high_60 = g["close"].rolling(60).min(), g["close"].rolling(60).max()
        g["price_pos"] = ((g["close"] - low_60) / (high_60 - low_60)).fillna(0.5)
        return g

    df = df.groupby("code", group_keys=False).apply(calc_metrics)
    return df

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
        up = latest[latest["code"].str.len() == 4].sort_values(col, ascending=False).head(200)
        records = [{
            "code": r["code"], "name": r["name"], "market": r.get("market", ""),
            "three_inst_ratio": float(r["three_inst_ratio_est"]),
            "change": float(r.get(col, 0)),
            "foreign_ratio_diff": float(r["f_diff"]),
            "trust_ratio_diff": float(r["t_diff"]),
            "dealer_ratio_diff": float(r["s_diff"]),
            "vol_ratio": float(r.get("vol_ratio", 0)),
            "price_pos": float(r.get("price_pos", 0.5))
        } for _, r in up.iterrows()]

        with open(os.path.join(out_dir, f"top_three_inst_change_{w}_up.json"), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

def main():
    ensure_dirs()
    target_date = get_target_trade_date()
    flows_list, foreign_list, pv_list = [], [], []
    
    for d in iter_trading_days(target_date - timedelta(days=10), target_date):
        print(f"[INFO] Processing {d}...")
        flows_list.extend([fetch_twse_t86(d), fetch_tpex_flows(d)])
        foreign_list.extend([fetch_twse_mi_qfiis(d), fetch_tpex_qfii(d)])
        p_twse, p_tpex = fetch_twse_price_vol(d), fetch_tpex_price_vol(d)
        if not p_twse.empty: p_twse["date"] = d; pv_list.append(p_twse)
        if not p_tpex.empty: p_tpex["date"] = d; pv_list.append(p_tpex)

    flows_all = pd.concat([df for df in flows_list if not df.empty])
    foreign_master = pd.concat([df for df in foreign_list if not df.empty])
    pv_all = pd.concat(pv_list).drop_duplicates(subset=["date", "code"])
    pv_all.to_csv(PRICE_VOL_PATH, index=False)

    merged = build_estimated_holdings(flows_all, foreign_master)
    merged = add_change_metrics(merged, WINDOWS)
    merged = calculate_low_heavy_vol(merged, pv_all)
    export_change_rankings(merged, WINDOWS)

if __name__ == "__main__":
    main()
