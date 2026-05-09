# Stock Sentinel — Advisor 数据架构升级说明

## 背景

原始 `advisor.py` 存在以下问题：

1. **API 配额浪费**：每次 Advisor 被唤醒，都实时调用 Finnhub 拉取一年日线数据计算指标，每次消耗 2-3 个 API 请求，高频触发必然打爆免费配额。
2. **指数误触发**：`market_scan.py` 对所有异动标的（包括 SPY/QQQ 等指数）都唤醒 Advisor，但指数没有持仓数据，报告无意义。
3. **新闻视野窄**：Advisor 只有个股定向新闻，缺少宏观背景，无法判断个股异动是自身原因还是大盘联动。

---

## 升级方案

### 新增模块

| 模块 | 职责 |
|---|---|
| `agent/data_store.py` | 数据库管理层，负责四张新表的建表与读写 |
| `agent/data_sync.py` | 数据同步层，每日收盘后定时运行，维护本地数据库 |

### 修改模块

| 模块 | 改动内容 |
|---|---|
| `agent/data_engine.py` | `get_technical_indicators` 和 `get_analyst_estimates` 改为先读缓存，未命中才调 Finnhub |
| `agent/advisor.py` | 新闻输入改为两层：公司层 + 宏观层；修复指数误触发 |
| `agent/market_scan.py` | 触发 Advisor 前过滤，只对 `watch_list` 标的生效 |
| `config/settings.toml` | 新增 `[data_sync]` 配置块 |
| `deploy/vars.yml` | 新增 `data_sync_time` |
| `deploy/roles/app/tasks/schedules.yml` | 新增 `stock-sentinel-data-sync` timer |

### 不需要改动的模块

`news_scanner.py` / `report.py` / `bot.py` / `news_dedup.py` 完全不动。

---

## 数据库表设计

所有新表由 `data_store.py` 统一管理，写入同一个 `sentinel.db`，与 `news_dedup.py` 管理的两张表（`news_registry` / `news_briefs`）平行存在，互不干涉。

### `stock_candles` — 日线数据

```
ticker     TEXT  股票代码
date       TEXT  交易日 YYYY-MM-DD
close      REAL  收盘价
quality    TEXT  ok / suspect
updated_at TEXT  写入时间
PRIMARY KEY (ticker, date)
```

- **永久保留**，不设过期
- 数据量极小：每只股票每年约 25KB
- `quality=suspect` 标记条件：收盘价 <= 0，或单日涨跌幅超过 50%
- 异常数据标记后仍然保留，不丢失

### `market_holidays` — 美股节假日

```
exchange   TEXT  交易所代码，如 US
date       TEXT  节假日日期 YYYY-MM-DD
name       TEXT  节假日名称
updated_at TEXT  写入时间
PRIMARY KEY (exchange, date)
```

- **永久保留**，按年写入
- 每年只从 Finnhub `market_holiday` 接口拉取一次
- 跨年时自动补充新一年数据
- 用于构建真实交易日列表，避免误判节假日为数据缺口

### `api_cache` — 计算结果缓存

```
cache_key  TEXT  唯一键，如 tech_AAPL / estimates_AAPL
data       TEXT  JSON 序列化结果
created_at TEXT  写入时间
expires_at TEXT  过期时间
PRIMARY KEY (cache_key)
```

| cache_key 格式 | 内容 | TTL |
|---|---|---|
| `tech_{TICKER}` | SMA50 / SMA200 / RSI(14) / current_price | 4小时 |
| `estimates_{TICKER}` | targetHigh / targetLow / targetMean / numberAnalysts | 24小时 |

- 过期后由 `data_sync.py` 每日重新计算并刷新
- `data_engine.py` 作为兜底：缓存未命中时实时计算/拉取并回写

### `company_news` — 公司定向新闻

```
guid       TEXT  URL MD5，去重主键
ticker     TEXT  关联股票代码
headline   TEXT  标题
summary    TEXT  摘要（最多300字）
url        TEXT  原文链接
source     TEXT  来源
datetime   TEXT  新闻时间戳
created_at TEXT  写入时间
PRIMARY KEY (guid)
```

- 保留 **7天**，定期清理
- 由 `data_sync.py` 每日收盘后按 `watch_list` 逐一拉取
- Advisor 直接读库，零 Finnhub 请求

---

## `data_sync.py` 运行流程

```
启动（每日 17:00 ET，收盘约30分钟后）
 │
 ├─ 1. 节假日检查
 │      └─ 当年数据不存在 → 调 Finnhub market_holiday → 写入 market_holidays 表
 │
 ├─ 2. 构建真实交易日列表
 │      └─ 回溯 lookback_days 自然日 → 排除周末 → 排除节假日表 → 得到交易日列表
 │
 ├─ 对每只 watch_list 股票循环：
 │   ├─ 3. 日线补录
 │   │      ├─ get_missing_dates → 找缺口
 │   │      ├─ 有缺口 → 调 Finnhub stock_candles 补录
 │   │      ├─ close <= 0 → quality=suspect
 │   │      └─ 单日涨跌幅 > 50% → quality=suspect
 │   │
 │   ├─ 4. 技术指标缓存（本地计算）
 │   │      └─ 从 stock_candles 读取近252条
 │   │         → 计算 SMA50 / SMA200 / RSI(14)
 │   │         → set_cache("tech_{TICKER}", ttl=4h)
 │   │
 │   ├─ 5. 分析师目标价缓存
 │   │      └─ 调 Finnhub price_target
 │   │         → set_cache("estimates_{TICKER}", ttl=24h)
 │   │
 │   └─ 6. 公司新闻拉取
 │          └─ 调 Finnhub company_news(近3天)
 │             → 去重写入 company_news 表
 │
 ├─ 7. 清理过期数据
 │      ├─ api_cache：删除 expires_at < now 的记录
 │      └─ company_news：删除 created_at < 7天前 的记录
 │
 └─ 8. 错误汇报
        └─ 有错误 → 发送 Discord 通知到 log 频道
```

---

## Advisor 新闻两层架构

Advisor 被唤醒时，新闻输入由原来的单层改为两层：

```
【近期新闻 — 公司层】
来源：company_news 表
查询：按 ticker + 近3天
内容：该股票的定向新闻，已由 Finnhub 过滤
上限：10条

【近期新闻 — 宏观层】
来源：news_registry 表（news_dedup 管理）
查询：score >= min_score_threshold + 近24小时
内容：高分宏观/市场/行业新闻，已经过 AI 评分过滤
上限：5条
```

两层数据分别组装进 Prompt，让 LLM 在分析时能把个股异动放在宏观背景下判断。

---

## Systemd Timer

新增第6个 timer：

| Timer | 触发时间 | 任务 |
|---|---|---|
| `stock-sentinel-data-sync` | 每日 17:00 ET | 日线补录、指标缓存、新闻拉取 |

---

## 修复问题清单

| 问题 | 修复方式 |
|---|---|
| 指数误触发 Advisor | `market_scan.py` 过滤，只对 `watch_list` 标的调用 Advisor |
| 每次实时拉取一年日线 | `stock_candles` 永久存储，`data_sync` 每日增量补录 |
| 高频 API 调用打爆配额 | `api_cache` 缓存技术指标和目标价，Advisor 运行时零 Finnhub 请求 |
| 新闻缺少宏观背景 | 新增宏观层，读取 `news_registry` 高分新闻 |
| `fetch_company_news` 实时拉取 | 改为读 `company_news` 表，由 `data_sync` 每日维护 |

---

## 配置参数

`config/settings.toml` 新增：

```toml
[data_sync]
lookback_days                 = 14    # 交易日缺口检测回溯自然日数
company_news_retention_days   = 7     # 公司新闻保留天数
company_news_fetch_days       = 3     # 每次拉取公司新闻的天数范围
tech_cache_ttl_hours          = 4     # 技术指标缓存有效期（小时）
estimates_cache_ttl_hours     = 24    # 分析师目标价缓存有效期（小时）
```

`deploy/vars.yml` 新增：

```yaml
data_sync_time: "17:00"
```
