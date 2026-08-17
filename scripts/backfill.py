#!/usr/bin/env python3
"""一次性回补近 N 天历史 (默认30天,BACKFILL_DAYS 可调).
- 全网算力: mempool.space hashrate/6m (日均值)
- 难度: mempool.space 难度调整记录 (阶梯展开到每日)
- BTC 价格: Coinbase 日K (fallback: CoinGecko)
- Hashprice: blockchain.info 矿工日收入(USD) ÷ 当日算力(PH/s)
- 矿企股价: Yahoo Finance 2 个月日线
矿企算力无免费历史日频数据,从部署日起每日累积。
不覆盖已有日期的数据。
"""
import csv
import os
import sys
import time
from datetime import datetime, timezone

from collect import DATA, STOCK_TICKERS, get, upsert_csv

DAYS = int(os.environ.get("BACKFILL_DAYS", "30"))


def day(ts):
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def existing_dates(path):
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return {r[0] for r in list(csv.reader(f))[1:] if r}


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1) 算力日均值 + 难度调整
    j = get("https://mempool.space/api/v1/mining/hashrate/6m").json()
    hr = {}
    for p in j.get("hashrates", []):
        ts = p.get("timestamp") or p.get("time")
        if ts is not None and p.get("avgHashrate"):
            hr[day(ts)] = float(p["avgHashrate"])
    diffs = []
    for d in j.get("difficulty", []):
        ts = d.get("timestamp") or d.get("time")
        if ts is not None and d.get("difficulty"):
            diffs.append((day(ts), float(d["difficulty"])))
    diffs.sort()

    # 2) BTC 日收盘价
    price = {}
    try:
        end = int(time.time())
        start = end - (DAYS + 3) * 86400
        r = get(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles"
            f"?granularity=86400&start={start}&end={end}"
        )
        for row in r.json():
            price[day(row[0])] = float(row[4])
    except Exception as e:  # noqa: BLE001
        print("coinbase candles failed, trying coingecko:", e, file=sys.stderr)
        j2 = get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
            f"?vs_currency=usd&days={DAYS + 3}&interval=daily"
        ).json()
        for ts, v in j2.get("prices", []):
            price[day(ts / 1000)] = float(v)

    # 3) 矿工日收入 (USD) → hashprice
    rev = {}
    try:
        j3 = get(
            f"https://api.blockchain.info/charts/miners-revenue?timespan={DAYS + 7}days&format=json"
        ).json()
        for p in j3.get("values", []):
            rev[day(p["x"])] = float(p["y"])
    except Exception as e:  # noqa: BLE001
        print("miners-revenue failed, hashprice will be blank:", e, file=sys.stderr)

    def difficulty_for(d):
        vals = [v for dd, v in diffs if dd <= d]
        return vals[-1] if vals else None

    have = existing_dates(os.path.join(DATA, "network.csv"))
    rows = []
    for d in sorted(hr.keys())[-(DAYS + 1):]:
        if d in have or d >= today:
            continue
        h = hr[d]
        p = price.get(d)
        dv = difficulty_for(d)
        hp = (rev[d] / (h / 1e15)) if (d in rev and h) else None
        rows.append([
            d, f"{h / 1e18:.1f}",
            f"{dv:.0f}" if dv else "",
            f"{p:.0f}" if p else "",
            f"{hp:.2f}" if hp else "",
        ])
    if rows:
        upsert_csv(
            os.path.join(DATA, "network.csv"),
            ["date", "hashrate_ehs", "difficulty", "btc_price_usd", "hashprice_usd_ph_day"],
            rows, key_cols=[0],
        )
    print(f"network: backfilled {len(rows)} days")

    # 4) 股价历史 (2 个月日线)
    srows = []
    for t in STOCK_TICKERS:
        try:
            j4 = get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=2mo&interval=1d",
                tries=2, timeout=20,
            ).json()
            res = j4["chart"]["result"][0]
            closes = res["indicators"]["quote"][0]["close"]
            for tt, c in zip(res["timestamp"], closes):
                if c is None:
                    continue
                d = day(tt)
                if d >= today:
                    continue
                srows.append([d, t, f"{float(c):.2f}"])
            time.sleep(0.4)
        except Exception as e:  # noqa: BLE001
            print("stock backfill failed:", t, e, file=sys.stderr)
    if srows:
        upsert_csv(
            os.path.join(DATA, "stocks.csv"),
            ["date", "ticker", "close_usd"],
            srows, key_cols=[0, 1],
        )
    print(f"stocks: backfilled {len(srows)} rows")


if __name__ == "__main__":
    main()
