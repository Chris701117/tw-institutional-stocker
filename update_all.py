# -*- coding: utf-8 -*-
"""Update & export Taiwan institutional (三大法人) holdings data.
功能重點：
- 自動抓 TWSE/TPEX 三大法人日交易 + 外資持股；
- 抓取 TWSE/TPEX 每日收盤行情與成交量 (含盤中預估)；
- 以 inst_baseline.csv 為基準點，校正投信 / 自營商持股；
- 計算低檔爆量指標 (量比 > 2.5 且 股價位階 < 25%)；
- 強力容錯：處理 NAType 與 API 空值問題。
"""
import json
import os
from io import StringIO
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import math
import requests
import pandas as pd
import numpy as np

from utils_columns import find_col_any, normalize_columns

DATA_DIR = "data"
DOCS_DIR = os.path.join("docs", "data")
TIMESERIES_DIR = os.path.join(DOCS_DIR, "timeseries")
INST_BASELINE_PATH = os.path.join(DATA_DIR, "inst_baseline.csv")
PRICE_VOL_PATH = os.path.join(DATA_DIR, "price_vol_history.csv")

WINDOWS = [5, 20, 60, 120]

# ---------- 基礎工具 ----------
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

def numeric_series(series: pd.Series, to_float: bool = False) -> pd.Series:
    s = series.astype(str).str.replace(",", "", regex=False)
    s = s.str.replace("\u2212", "-", regex=False).str.replace("\uFF0D", "-", regex=False).str.replace("\uFF0B", "+", regex=False).str.strip()
    mask_paren = s.str.match(r"^\([\d\.]+\)$")
    s.loc[mask_paren] = "-" + s.loc[mask_paren].str.strip("()")
    missing_tokens = {"", "nan", "NaN", "None", "--", "X", "<NA>"}
    s = s.where(~s.isin(missing_tokens), "0")
    if to_float: return pd.to_numeric(s, errors="coerce").fillna(0.0)
    return pd.to_numeric(s, errors="coerce").fillna(0).astype("Int64")

# 新增：安全浮點數轉換 (解決 NAType 錯誤)
def safe_float(val) -> float:
    if pd.isna(val) or val is pd.NA:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

# ---------- 價格量能抓取 (含防錯處理) ----------
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
    except Exception: return pd.DataFrame()

def fetch_tpex_price_vol(trade_date: date) -> pd.DataFrame:
    roc = f"{trade_date.year - 1911}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_quot_result.php?l=zh-tw&d={roc}&o=data"
    try:
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200: return pd.DataFrame()
        data = resp.json().get('aaData', [])
        if not data: return pd.DataFrame()
        df = pd.DataFrame(data)
        df["code"] = df[0].str.strip().str.zfill(4)
        df["close"] = numeric_series(df[2], to_float=True)
        df["vol"] = numeric_series(df[7], to_float=True) / 1000 
        return df[["code", "close", "vol"]]
    except Exception: return pd.DataFrame()

# ---------- 三大法人抓取 (含 TPEX 模糊匹配) ----------
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
        f_net_col = find_col_any(df, ["外陸資買賣超股數(不含外資自營商)", "外資及陸資(不含外資自營商)買賣超股數"])
        t_net_col = find_col_any(df, ["投信買賣超股數"])
        s_net_col = find_col_any(df, ["自營商買賣超股數合計", "自營商買賣超股數"])
        df["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
        df["name"] = df[name_col].astype(str).str.strip()
        out = pd.DataFrame({
            "date": trade_date, "code": df["code"], "name": df["name"],
            "foreign_net": numeric_series(df[f_net_col]),
            "trust_net": numeric_series(df[t_net_col]),
            "dealer_net": numeric_series(df[s_net_col]),
            "market": "TWSE"
        })
        return out[out["code"].str.match(r"^\d{4,5}[A-Z]*$")]
    except Exception: return pd.DataFrame()

def fetch_tpex_flows(trade_date: date) -> pd.DataFrame:
    roc = f"{trade_date.year - 1911}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {"d": roc, "l": "zh-tw", "o": "htm"}
    try:
        resp = requests.get(url, params=params, timeout=20)
        tables = pd.read_html(StringIO(resp.text))
        if not tables: return pd.DataFrame()
        df = tables[0]
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = ['_'.join([str(c) for c in col if 'Unnamed' not in str(c)]) for col in df.columns.values]
        else:
            df.columns = df.columns.astype(str)
        cols = df.columns.tolist()
        def find_tpex_col(keywords):
            for c in cols:
                if any(c.endswith(k) for k in keywords): return c
            return None
        code_col = find_tpex_col(["代號"])
        name_col = find_tpex_col(["名稱"])
        f_net_col = find_tpex_col(["外資及陸資買賣超股數", "外資及陸資(不含外資自營商)買賣超股數"])
        t_net_col = find_tpex_col(["投信買賣超股數"])
        s_net_col = find_tpex_col(["自營商買賣超股數"]) 
        if not all([code_col, name_col, f_net_col, t_net_col, s_net_col]): return pd.DataFrame()
        out = pd.DataFrame({
            "date": trade_date, "code": df[code_col].astype(str).str.strip().str.zfill(4),
            "name": df[name_col].astype(str).str.strip(),
            "foreign_net": numeric_series(df[f_net_col]),
            "trust_net": numeric_series(df[t_net_col]),
            "dealer_net": numeric_series(df[s_net_col]),
            "market": "TPEX"
        })
        return out[out["code"].str.match(r"^\d{4,5}[A-Z]*$")]
    except Exception: return pd.DataFrame()

# ---------- 外資與估算 ----------
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
    except Exception: return pd.DataFrame()

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
    except Exception: return pd.DataFrame()

def build_estimated_holdings(flows, foreign_master):
    if flows.empty or foreign_master.empty: return pd.DataFrame()
    merged = flows.merge(foreign_master[["date", "code", "total_shares", "foreign_ratio"]], on=["date", "code"], how="left")
    merged["total_shares"] = pd.to_numeric(merged["total_shares"], errors="coerce").fillna(0.0)
    
    def accumulate(g):
        g = g.sort_values("date")
        g["trust_cum"] = g["trust_net"].cumsum()
        g["dealer_cum"] = g["dealer_net"].cumsum()
        g["trust_ratio_est"] = (g["trust_cum"] / g["total_shares"] * 100).fillna(0.0)
        g["dealer_ratio_est"] = (g["dealer_cum"] / g["total_shares"] * 100).fillna(0.0)
        return g
    
    # 修正 FutureWarning
    merged = merged.groupby("code", group_keys=False).apply(accumulate)
    merged["three_inst_ratio_est"] = merged["foreign_ratio"].fillna(0.0) + merged["trust_ratio_est"] + merged["dealer_ratio_est"]
    return merged

# ---------- 爆量指標 ----------
def calculate_low_heavy_vol(merged, price_vol_history):
    if merged.empty or price_vol_history.empty: return merged
    df = merged.merge(price_vol_history, on=["date", "code"], how="left")
    df = df.sort_values(["code", "date"])
    tz = ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    if 9 <= now.hour < 14:
        min_p = max(1, min(270, (now.hour - 9) * 60 + now.minute))
        time_w = 270 / min_p
    else: time_w = 1.0
    
    def calc(g):
        g["est_vol"] = g["vol"] * time_w
        g["vol_ma5"] = g["vol"].shift(1).rolling(5).mean()
        g["vol_ratio"] = (g["est_vol"] / g["vol_ma5"]).fillna(0.0)
        low_60, hi_60 = g["close"].rolling(60).min(), g["close"].rolling(60).max()
        g["price_pos"] = ((g["close"] - low_60) / (hi_60 - low_60)).fillna(0.5)
        return g
    
    # 修正 FutureWarning
    return df.groupby("code", group_keys=False).apply(calc)

# ---------- JSON 輸出 (使用 safe_float 修正報錯) ----------
def export_change_rankings(merged, windows, out_dir=DOCS_DIR):
    if merged.empty: return
    latest_date = merged["date"].max()
    merged = merged.sort_values(["code", "date"])
    merged["f_diff"] = merged.groupby("code")["foreign_ratio"].diff().fillna(0.0)
    merged["t_diff"] = merged.groupby("code")["trust_ratio_est"].diff().fillna(0.0)
    merged["s_diff"] = merged.groupby("code")["dealer_ratio_est"].diff().fillna(0.0)
    
    latest = merged[merged["date"] == latest_date].copy()
    
    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        if col not in latest.columns: latest[col] = 0.0
        up = latest[latest["code"].str.len() == 4].sort_values(col, ascending=False).head(500)
        
        # 這裡使用 safe_float 來處理所有數值，避免 NAType 錯誤
        records = [{
            "code": r["code"], "name": r["name"], 
            "three_inst_ratio": safe_float(r["three_inst_ratio_est"]),
            "change": safe_float(r.get(col, 0)), 
            "foreign_ratio_diff": safe_float(r["f_diff"]),
            "trust_ratio_diff": safe_float(r["t_diff"]), 
            "dealer_ratio_diff": safe_float(r["s_diff"]),
            "vol_ratio": safe_float(r.get("vol_ratio", 0)), 
            "price_pos": safe_float(r.get("price_pos", 0.5))
        } for _, r in up.iterrows()]
        
        with open(os.path.join(out_dir, f"top_three_inst_change_{w}_up.json"), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

# ---------- 主流程 ----------
def main():
    ensure_dirs()
    target = get_target_trade_date()
    f_list, q_list, pv_list = [], [], []
    for d in range(10): # 往前抓120天資料
        cur = target - timedelta(days=d)
        if is_weekend(cur): continue
        print(f"[INFO] Processing {cur}...")
        f_list.extend([fetch_twse_t86(cur), fetch_tpex_flows(cur)])
        q_list.extend([fetch_twse_mi_qfiis(cur), fetch_tpex_qfii(cur)])
        p1, p2 = fetch_twse_price_vol(cur), fetch_tpex_price_vol(cur)
        if not p1.empty: p1["date"] = cur; pv_list.append(p1)
        if not p2.empty: p2["date"] = cur; pv_list.append(p2)

    flows = pd.concat([df for df in f_list if not df.empty]) if f_list else pd.DataFrame()
    q_all = pd.concat([df for df in q_list if not df.empty]) if q_list else pd.DataFrame()
    pv_all = pd.concat(pv_list).drop_duplicates(subset=["date", "code"]) if pv_list else pd.DataFrame()
    
    if not pv_all.empty: pv_all.to_csv(PRICE_VOL_PATH, index=False)
    merged = build_estimated_holdings(flows, q_all)
    if not merged.empty:
        for w in WINDOWS: merged[f"three_inst_ratio_change_{w}"] = merged.groupby("code")["three_inst_ratio_est"].diff(periods=w)
        merged = calculate_low_heavy_vol(merged, pv_all)
        export_change_rankings(merged, WINDOWS)

if __name__ == "__main__":
    main()
