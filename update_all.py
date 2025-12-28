# -*- coding: utf-8 -*-
"""Update & export Taiwan institutional (三大法人) holdings data.

Enhanced Version:
- Includes differentiated daily ratio changes for Foreign and Trust funds.
- Outputs detailed diff metrics in JSON for advanced Google Apps Script filtering.
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
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, usecols=["date"])
    if df.empty:
        return None
    return pd.to_datetime(df["date"]).dt.date.max()

def iter_trading_days(start: date, end: date):
    cur = start
    while cur <= end:
        if not is_weekend(cur):
            yield cur
        cur += timedelta(days=1)

def numeric_series(series: pd.Series, to_float: bool = False) -> pd.Series:
    s = series.astype(str)
    s = s.str.replace(",", "", regex=False)
    s = (
        s.str.replace("\u2212", "-", regex=False)
         .str.replace("\uFF0D", "-", regex=False)
         .str.replace("\uFF0B", "+", regex=False)
         .str.strip()
    )
    mask_paren = s.str.match(r"^\([\d\.]+\)$")
    s.loc[mask_paren] = "-" + s.loc[mask_paren].str.strip("()")
    missing_tokens = {"", "nan", "NaN", "None", "--"}
    s = s.where(~s.isin(missing_tokens), "0")
    if to_float:
        return pd.to_numeric(s, errors="coerce").fillna(0.0)
    return pd.to_numeric(s, errors="coerce").fillna(0).astype("Int64")

# ---------- TWSE & TPEX Fetchers (Keep existing logic) ----------

def fetch_twse_t86(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/fund/T86"
    params = {"response": "csv", "date": datestr, "selectType": "ALLBUT0999"}
    resp = requests.get(url, params=params, timeout=20)
    csv_text = resp.content.decode("cp950", errors="ignore")
    df = pd.read_csv(StringIO(csv_text), header=1)
    df = normalize_columns(df.dropna(how="all", axis=0).dropna(how="all", axis=1))
    if df.empty: return pd.DataFrame()
    code_col = find_col_any(df, ["證券代號"])
    name_col = find_col_any(df, ["證券名稱"])
    col_f_ex = find_col_any(df, ["外陸資買賣超股數(不含外資自營商)","外資及陸資(不含外資自營商)買賣超股數","外資及陸資買賣超股數(不含外資自營商)"])
    col_f_self = find_col_any(df, ["外資自營商買賣超股數"])
    col_t = find_col_any(df, ["投信買賣超股數"])
    col_d = find_col_any(df, ["自營商買賣超股數合計","自營商買賣超股數"])
    df["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
    out = pd.DataFrame({
        "date": trade_date, "code": df["code"], "name": df[name_col].astype(str).str.strip(),
        "foreign_net": (numeric_series(df[col_f_ex]) + numeric_series(df[col_f_self])),
        "trust_net": numeric_series(df[col_t]), "dealer_net": numeric_series(df[col_d]), "market": "TWSE"
    })
    return out[out["code"].str.match(r"^\d{4,5}[A-Z]*$")]

def fetch_twse_mi_qfiis(trade_date: date) -> pd.DataFrame:
    datestr = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/fund/MI_QFIIS"
    params = {"response": "csv", "date": datestr, "selectType": "ALLBUT0999"}
    resp = requests.get(url, params=params, timeout=20)
    csv_text = resp.content.decode("cp950", errors="ignore")
    try:
        df = pd.read_csv(StringIO(csv_text), header=1)
        df = normalize_columns(df.dropna(how="all", axis=0).dropna(how="all", axis=1))
    except: return pd.DataFrame()
    if df.empty: return pd.DataFrame()
    code_col = find_col_any(df, ["證券代號"])
    issued_col = find_col_any(df, ["發行股數"])
    ratio_col = find_col_any(df, ["全體外資及陸資持股比率"])
    out = pd.DataFrame()
    out["code"] = df[code_col].astype(str).str.replace("=", "").str.replace('"', "").str.strip().str.zfill(4)
    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask].copy()
    out["total_shares"] = numeric_series(df.loc[mask, issued_col])
    out["foreign_ratio"] = numeric_series(df.loc[mask, ratio_col], to_float=True)
    out["date"], out["market"] = trade_date, "TWSE"
    return out

def fetch_tpex_flows(trade_date: date) -> pd.DataFrame:
    y = trade_date.year - 1911
    roc = f"{y:03d}/{trade_date.month:02d}/{trade_date.day:02d}"
    url = "https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php"
    params = {"d": roc, "l": "zh-tw", "o": "htm", "s": "0", "se": "EW", "t": "D"}
    resp = requests.get(url, params=params, timeout=20)
    try:
        df = pd.read_html(StringIO(resp.text))[0]
        df = normalize_columns(df)
    except: return pd.DataFrame()
    col_f_ex = find_col_any(df, ["外資及陸資(不含外資自營商)買賣超股數","外資及陸資買賣超股數(不含外資自營商)","外資及陸資買賣超股數"])
    col_f_self = find_col_any(df, ["外資自營商買賣超股數"])
    df["code"] = df[find_col_any(df, ["代號"])].astype(str).str.strip().str.zfill(4)
    out = pd.DataFrame({
        "date": trade_date, "code": df["code"], "name": df[find_col_any(df, ["名稱"])].astype(str).str.strip(),
        "foreign_net": (numeric_series(df[col_f_ex]) + numeric_series(df[col_f_self])),
        "trust_net": numeric_series(df[find_col_any(df, ["投信買賣超股數"])]),
        "dealer_net": numeric_series(df[find_col_any(df, ["自營商買賣超股數合計","自營商買賣超股數"])]), "market": "TPEX"
    })
    return out[out["code"].str.match(r"^\d{4,5}[A-Z]*$")]

def fetch_tpex_qfii(trade_date: date) -> pd.DataFrame:
    url = "https://www.tpex.org.tw/web/stock/3insti/qfii/qfii_result.php"
    resp = requests.get(url, params={"l": "zh-tw", "o": "data"}, timeout=20)
    df = normalize_columns(pd.read_csv(StringIO(resp.text)).dropna(how="all"))
    if df.empty: return pd.DataFrame()
    out = pd.DataFrame()
    out["code"] = df[find_col_any(df, ["代號"])].astype(str).str.strip().str.zfill(4)
    mask = out["code"].str.match(r"^\d{4,5}[A-Z]*$")
    out = out[mask].copy()
    out["total_shares"] = numeric_series(df.loc[mask, find_col_any(df, ["發行股數"])])
    out["foreign_ratio"] = numeric_series(df.loc[mask, find_col_any(df, ["僑外資及陸資持股比率"])], to_float=True)
    out["date"], out["market"] = trade_date, "TPEX"
    return out

# ---------- model: holdings estimation (Enhanced) ----------

def build_foreign_master(twse, tpex):
    all_df = pd.concat([twse, tpex], ignore_index=True)
    if all_df.empty: return all_df
    return all_df.sort_values(["code", "date"]).set_index(["code", "date"]).sort_index().groupby(level=0).ffill().reset_index()

def build_estimated_holdings(flows, foreign_master, baseline=None):
    flows["date"] = pd.to_datetime(flows["date"]).dt.date
    foreign_master["date"] = pd.to_datetime(foreign_master["date"]).dt.date
    merged = flows.merge(foreign_master[["date", "code", "market", "total_shares", "foreign_ratio"]], on=["date", "code", "market"], how="left")
    
    # Baseline Logic
    if baseline is not None and not baseline.empty:
        base = baseline.copy()
        base["date"] = pd.to_datetime(base["date"]).dt.date
        merged = merged.merge(base[["date", "code", "trust_shares_base", "dealer_shares_base"]], on=["date", "code"], how="left")

    merged = merged.sort_values(["code", "date"])
    merged["total_shares"] = pd.to_numeric(merged["total_shares"], errors="coerce").fillna(0.0)

    def accumulate(g):
        g = g.copy()
        g["t_cum"] = g["trust_net"].astype(float).cumsum()
        g["d_cum"] = g["dealer_net"].astype(float).cumsum()
        bt = pd.to_numeric(g["trust_shares_base"], errors="coerce").ffill().fillna(0.0)
        bd = pd.to_numeric(g["dealer_shares_base"], errors="coerce").ffill().fillna(0.0)
        tc_at_b = g["t_cum"].where(g["trust_shares_base"].notna()).ffill().fillna(0.0)
        dc_at_b = g["d_cum"].where(g["dealer_shares_base"].notna()).ffill().fillna(0.0)
        g["trust_shares_est"] = bt + (g["t_cum"] - tc_at_b)
        g["dealer_shares_est"] = bd + (g["d_cum"] - dc_at_b)
        if bt.sum() == 0 and bd.sum() == 0:
            g["trust_shares_est"], g["dealer_shares_est"] = g["t_cum"], g["d_cum"]
        return g

    merged = merged.groupby("code", group_keys=False).apply(accumulate)
    denom = merged["total_shares"].astype(float)
    valid = denom > 0
    merged["trust_ratio_est"] = 0.0
    merged["dealer_ratio_est"] = 0.0
    merged.loc[valid, "trust_ratio_est"] = (merged.loc[valid, "trust_shares_est"] / denom[valid] * 100.0)
    merged.loc[valid, "dealer_ratio_est"] = (merged.loc[valid, "dealer_shares_est"] / denom[valid] * 100.0)
    merged["foreign_ratio"] = merged["foreign_ratio"].fillna(0.0)
    merged["three_inst_ratio_est"] = merged["foreign_ratio"] + merged["trust_ratio_est"] + merged["dealer_ratio_est"]

    # --- NEW: Calculate Daily Diffs ---
    merged = merged.sort_values(["code", "date"])
    merged["foreign_ratio_diff"] = merged.groupby("code")["foreign_ratio"].diff().fillna(0.0)
    merged["trust_ratio_diff"] = merged.groupby("code")["trust_ratio_est"].diff().fillna(0.0)
    merged["dealer_ratio_diff"] = merged.groupby("code")["dealer_ratio_est"].diff().fillna(0.0)
    
    return merged

def add_change_metrics(merged, windows):
    merged = merged.sort_values(["code", "date"])
    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        merged[col] = merged.groupby("code")["three_inst_ratio_est"].diff(periods=w)
    return merged

# ---------- export JSON (Enhanced) ----------

def export_change_rankings(merged, windows, out_dir=DOCS_DIR):
    latest_date = merged["date"].max()
    latest = merged[merged["date"] == latest_date].copy()
    os.makedirs(out_dir, exist_ok=True)
    for w in windows:
        col = f"three_inst_ratio_change_{w}"
        if col not in latest.columns: continue
        tmp = latest[latest[col].notna()].copy()
        if tmp.empty: continue
        up = tmp.sort_values(col, ascending=False).head(200)
        down = tmp.sort_values(col, ascending=True).head(200)

        def to_dict_list(df):
            return [{
                "code": r["code"], "name": r["name"], "market": r["market"],
                "three_inst_ratio": float(r["three_inst_ratio_est"]),
                "change": float(r[col]),
                "foreign_ratio_diff": float(r["foreign_ratio_diff"]),
                "trust_ratio_diff": float(r["trust_ratio_diff"]),
                "dealer_ratio_diff": float(r["dealer_ratio_diff"])
            } for _, r in df.iterrows()]

        with open(os.path.join(out_dir, f"top_three_inst_change_{w}_up.json"), "w", encoding="utf-8") as f:
            json.dump(to_dict_list(up), f, ensure_ascii=False, indent=2)
        with open(os.path.join(out_dir, f"top_three_inst_change_{w}_down.json"), "w", encoding="utf-8") as f:
            json.dump(to_dict_list(down), f, ensure_ascii=False, indent=2)

def clean_float(val, default=0.0):
    try:
        f = float(val)
        return f if not (math.isnan(f) or math.isinf(f)) else default
    except: return default

def export_timeseries_by_code(merged, out_root=TIMESERIES_DIR, primary_window=20):
    os.makedirs(out_root, exist_ok=True)
    col_change = f"three_inst_ratio_change_{primary_window}"
    for code, g in merged.groupby("code"):
        records = [{
            "date": r["date"].strftime("%Y-%m-%d") if not isinstance(r["date"], str) else r["date"],
            "code": code, "name": r["name"], "market": r["market"],
            "foreign_ratio": clean_float(r["foreign_ratio"]),
            "trust_ratio": clean_float(r["trust_ratio_est"]),
            "dealer_ratio": clean_float(r["dealer_ratio_est"]),
            "three_inst_ratio": clean_float(r["three_inst_ratio_est"]),
            col_change: clean_float(r.get(col_change, 0.0))
        } for _, r in g.iterrows()]
        with open(os.path.join(out_root, f"{code}.json"), "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

# ---------- main orchestration (Simplified calls) ----------

def append_history(df_new, path, key_cols):
    if os.path.exists(path):
        df_old = pd.read_csv(path); df_old["date"] = pd.to_datetime(df_old["date"]).dt.date
        df_new["date"] = pd.to_datetime(df_new["date"]).dt.date
        df_all = pd.concat([df_old, df_new], ignore_index=True)
    else: df_all = df_new.copy(); df_all["date"] = pd.to_datetime(df_all["date"]).dt.date
    df_all = df_all.drop_duplicates(subset=key_cols).sort_values(["date", "code"])
    df_all.to_csv(path, index=False, date_format="%Y-%m-%d")
    return df_all

def main():
    ensure_dirs()
    target_date = get_target_trade_date()
    print(f"[INFO] Target: {target_date}")
    
    paths = {
        "twse_f": os.path.join(DATA_DIR, "twse_flows.csv"), "tpex_f": os.path.join(DATA_DIR, "tpex_flows.csv"),
        "twse_h": os.path.join(DATA_DIR, "twse_foreign.csv"), "tpex_h": os.path.join(DATA_DIR, "tpex_foreign.csv")
    }

    # Data Fetching & Appending
    flows_list, foreign_list = [], []
    # Dummy logic to determine start dates based on existing CSVs
    start_date = target_date - timedelta(days=7) # Fast update for 1 week
    
    for d in iter_trading_days(start_date, target_date):
        print(f"Processing {d}...")
        flows_list.extend([fetch_twse_t86(d), fetch_tpex_flows(d)])
        foreign_list.extend([fetch_twse_mi_qfiis(d), fetch_tpex_qfii(d)])
    
    f_all = pd.concat([x for x in flows_list if not x.empty], ignore_index=True)
    h_all = pd.concat([x for x in foreign_list if not x.empty], ignore_index=True)

    if not f_all.empty:
        twse_flows_all = append_history(f_all[f_all["market"]=="TWSE"], paths["twse_f"], ["date", "code"])
        tpex_flows_all = append_history(f_all[f_all["market"]=="TPEX"], paths["tpex_f"], ["date", "code"])
        twse_h_all = append_history(h_all[h_all["market"]=="TWSE"], paths["twse_h"], ["date", "code"])
        tpex_h_all = append_history(h_all[h_all["market"]=="TPEX"], paths["tpex_h"], ["date", "code"])

        # Model processing
        f_master = build_foreign_master(twse_h_all, tpex_h_all)
        flows_combined = pd.concat([twse_flows_all, tpex_flows_all], ignore_index=True)
        
        baseline_df = pd.read_csv(INST_BASELINE_PATH, comment="#") if os.path.exists(INST_BASELINE_PATH) else None
        merged = build_estimated_holdings(flows_combined, f_master, baseline=baseline_df)
        merged = add_change_metrics(merged, windows=WINDOWS)

        export_change_rankings(merged, windows=WINDOWS)
        export_timeseries_by_code(merged)
        print("Update complete.")

if __name__ == "__main__":
    main()
