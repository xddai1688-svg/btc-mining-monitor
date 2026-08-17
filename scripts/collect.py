#!/usr/bin/env python3
"""
BTC 挖矿行业每日数据采集
- 全网算力/难度: mempool.space API
- BTC 价格: Coinbase spot (fallback: Kraken, CoinGecko)
- Hashprice: 由 近144区块总奖励 × 价格 / 全网算力 计算 (USD/PH/s/day)
- 上市矿企算力: ziven.io (原 bitcoinminingstock.io) 排名页
- 矿企股价: stooq.com 免费 CSV 接口
输出: data/network.csv, data/miners.csv, data/stocks.csv, data/latest.json
幂等: 同一日期重复运行会覆盖当日数据。
"""
import csv
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
DEBUG = os.path.join(BASE, "debug")
os.makedirs(DATA, exist_ok=True)
os.makedirs(DEBUG, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (compatible; btc-mining-monitor/1.0; +https://github.com)"}
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# 股价监控标的 (stooq 代码)
STOCK_TICKERS = [
    "MARA", "CLSK", "RIOT", "IREN", "BTDR", "CORZ", "HIVE",
    "WULF", "CIFR", "HUT", "CANG", "ABTC", "CAN",
]

warnings = []


def get(url, tries=3, timeout=30, **kw):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


# ---------------- network: hashrate / difficulty ----------------

def fetch_network():
    j = get("https://mempool.space/api/v1/mining/hashrate/1w").json()
    hashrate_hs = float(j["currentHashrate"])   # H/s
    difficulty = float(j["currentDifficulty"])
    return hashrate_hs, difficulty


def fetch_price():
    try:
        j = get("https://api.coinbase.com/v2/prices/BTC-USD/spot").json()
        return float(j["data"]["amount"])
    except Exception:  # noqa: BLE001
        pass
    try:
        j = get("https://api.kraken.com/0/public/Ticker?pair=XBTUSD").json()
        k = list(j["result"].keys())[0]
        return float(j["result"][k]["c"][0])
    except Exception:  # noqa: BLE001
        pass
    j = get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd").json()
    return float(j["bitcoin"]["usd"])


def fetch_daily_reward_btc():
    """近 144 个区块 (约1天) 矿工总收入, BTC 计."""
    j = get("https://mempool.space/api/v1/mining/reward-stats/144").json()
    return float(j["totalReward"]) / 1e8


# ---------------- public miners hashrate ----------------

def fetch_miners():
    """解析 ziven.io 上市矿企算力排名. 返回 [(ticker, company, ehs)]."""
    html = get("https://ziven.io/bitcoin-mining/hashrate").text
    with open(os.path.join(DEBUG, "ziven_last.html"), "w") as f:
        f.write(html)

    miners = []
    # 策略0: 页面内嵌 JSON (window.allTickersHashrateData = [...])
    m = re.search(r"window\.allTickersHashrateData\s*=\s*(\[.*?\]);", html, re.S)
    if m:
        try:
            for row in json.loads(m.group(1)):
                t = str(row.get("ticker", "")).strip()
                name = str(row.get("name", "")).strip()
                ehs = row.get("operating_hashrate")
                if t and ehs is not None and 0 < float(ehs) < 500:
                    miners.append((t, name[:60], float(ehs)))
        except Exception as e:  # noqa: BLE001
            warnings.append(f"embedded-json parse miners failed: {e}")
    if miners:
        return _dedupe_miners(miners)

    # 策略1: pandas 解析所有表格
    try:
        import pandas as pd
        for tbl in pd.read_html(io.StringIO(html)):
            cols = [str(c).lower() for c in tbl.columns]
            if not any("eh/s" in c or "hashrate" in c for c in cols):
                continue
            for _, row in tbl.iterrows():
                vals = [str(v) for v in row.tolist()]
                ticker = next((v.strip() for v in vals
                               if re.fullmatch(r"[A-Z]{2,6}", v.strip())), None)
                ehs = None
                for v in vals:
                    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:\s*EH/s)?", v.strip())
                    if m and float(m.group(1)) < 500:
                        ehs = float(m.group(1))
                if ticker and ehs:
                    company = max(vals, key=len).strip()
                    miners.append((ticker, company[:60], ehs))
            if miners:
                break
    except Exception as e:  # noqa: BLE001
        warnings.append(f"pandas parse miners failed: {e}")

    # 策略2: 正则兜底 —— 形如 "Company ... (TICK) ... 70.3 EH/s"
    if not miners:
        text = re.sub(r"<[^>]+>", "|", html)
        for m in re.finditer(
            r"([A-Za-z0-9 .,&'\-]{3,60})\|+\(?([A-Z]{2,6})\)?\|+([0-9]{1,3}(?:\.[0-9]+)?)\|*\s*EH/s",
            text,
        ):
            miners.append((m.group(2), m.group(1).strip(), float(m.group(3))))

    out = _dedupe_miners(miners)
    if not out:
        warnings.append("miners: parse produced 0 rows — check debug/ziven_last.html")
    return out


def _dedupe_miners(miners):
    seen, out = set(), []
    for t, c, e in miners:
        if t not in seen and 0 < e < 500:
            seen.add(t)
            out.append((t, c, e))
    return out


# ---------------- stock prices ----------------

def fetch_stocks():
    out = []
    # 策略1: stooq 批量 CSV
    try:
        syms = ",".join(f"{t.lower()}.us" for t in STOCK_TICKERS)
        r = get(f"https://stooq.com/q/l/?s={syms}&f=sd2t2ohlcv&h&e=csv", tries=2, timeout=20)
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                close = float(row["Close"])
                sym = row["Symbol"].replace(".US", "").upper()
                out.append((sym, close))
            except (ValueError, KeyError):
                continue
    except Exception as e:  # noqa: BLE001
        warnings.append(f"stocks: stooq failed: {e}")

    # 策略2: Yahoo Finance 逐个兜底
    if not out:
        for t in STOCK_TICKERS:
            try:
                j = get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
                    "?range=5d&interval=1d",
                    tries=2, timeout=20,
                ).json()
                meta = j["chart"]["result"][0]["meta"]
                px = meta.get("regularMarketPrice")
                if px:
                    out.append((t, float(px)))
                time.sleep(0.5)
            except Exception:  # noqa: BLE001
                continue
        if out:
            warnings.append("stocks: used yahoo fallback")

    if not out:
        warnings.append("stocks: all sources returned no parsable rows")
    return out


# ---------------- csv helpers ----------------

def upsert_csv(path, header, rows, key_cols):
    """按 key_cols 去重追加 (当日重跑覆盖)."""
    old = []
    if os.path.exists(path):
        with open(path) as f:
            old = list(csv.reader(f))[1:]
    new_keys = {tuple(str(r[i]) for i in key_cols) for r in rows}
    kept = [r for r in old if tuple(str(r[i]) for i in key_cols) not in new_keys]
    allrows = kept + [[str(x) for x in r] for r in rows]
    allrows.sort(key=lambda r: r[0])
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(allrows)


def main():
    ok = {}

    # network + economics
    try:
        hs, diff = fetch_network()
        price = fetch_price()
        reward_btc = fetch_daily_reward_btc()
        ehs = hs / 1e18
        phs = hs / 1e15
        hashprice = reward_btc * price / phs  # USD / PH/s / day
        upsert_csv(
            os.path.join(DATA, "network.csv"),
            ["date", "hashrate_ehs", "difficulty", "btc_price_usd", "hashprice_usd_ph_day"],
            [[TODAY, f"{ehs:.1f}", f"{diff:.0f}", f"{price:.0f}", f"{hashprice:.2f}"]],
            key_cols=[0],
        )
        ok["network"] = True
        print(f"network: {ehs:.0f} EH/s, diff {diff:.3e}, BTC ${price:,.0f}, hashprice ${hashprice:.2f}")
    except Exception as e:  # noqa: BLE001
        ok["network"] = False
        warnings.append(f"network failed: {e}")

    # miners
    try:
        miners = fetch_miners()
        if miners:
            upsert_csv(
                os.path.join(DATA, "miners.csv"),
                ["date", "ticker", "company", "hashrate_ehs"],
                [[TODAY, t, c, e] for t, c, e in miners],
                key_cols=[0, 1],
            )
        ok["miners"] = bool(miners)
        print(f"miners: {len(miners)} companies")
    except Exception as e:  # noqa: BLE001
        ok["miners"] = False
        warnings.append(f"miners failed: {e}")

    # stocks
    try:
        stocks = fetch_stocks()
        if stocks:
            upsert_csv(
                os.path.join(DATA, "stocks.csv"),
                ["date", "ticker", "close_usd"],
                [[TODAY, t, p] for t, p in stocks],
                key_cols=[0, 1],
            )
        ok["stocks"] = bool(stocks)
        print(f"stocks: {len(stocks)} tickers")
    except Exception as e:  # noqa: BLE001
        ok["stocks"] = False
        warnings.append(f"stocks failed: {e}")

    with open(os.path.join(DATA, "latest.json"), "w") as f:
        json.dump(
            {
                "updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "date": TODAY,
                "sources_ok": ok,
                "warnings": warnings,
            },
            f, ensure_ascii=False, indent=1,
        )

    for w in warnings:
        print("WARN:", w, file=sys.stderr)
    # 只要有任一数据源成功即视为成功 (部分失败不阻塞提交)
    if not any(ok.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
