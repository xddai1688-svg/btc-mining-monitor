# BTC 挖矿行业监控

自动化监控系统:每日采集 BTC 全网算力、挖矿经济指标与上市矿企算力/股价,保存历史数据并发布仪表盘。

**仪表盘**: GitHub Pages 首页 (`index.html`)
**更新频率**: 每日北京时间 08:30 (GitHub Actions 自动运行)

## 数据

| 文件 | 内容 | 来源 |
|---|---|---|
| `data/network.csv` | 全网算力 (EH/s)、难度、BTC 价格、hashprice | mempool.space / Coinbase |
| `data/miners.csv` | 各上市矿企算力 (EH/s) | ziven.io |
| `data/stocks.csv` | 矿企股价 (USD) | stooq.com |
| `data/latest.json` | 最近一次采集状态 | — |

hashprice 由本系统计算: 近 144 区块矿工总收入 (BTC) × BTC 价格 ÷ 全网算力 (PH/s),单位 USD/PH/s/天。

## 维护

- 修改股价监控标的: 编辑 `scripts/collect.py` 中 `STOCK_TICKERS`
- 手动触发采集: Actions → Daily data collection → Run workflow
- 矿企算力解析失败时,查看 `debug/ziven_last.html` 排查页面结构变化
