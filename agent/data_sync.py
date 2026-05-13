# agent/data_sync.py
# v1 - 市场数据同步层
# 每日 17:00 ET 由 systemd timer 触发，跑完自动退出
# 职责：节假日补充、日线数据缺口补录、技术指标缓存、分析师目标价缓存、公司新闻拉取

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd

from . import config
from .config import settings
from . import data_engine
from . import data_store
from . import discord_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("data_sync")


# ══════════════════════════════════════════════════════════════
# [1] 节假日管理
# ══════════════════════════════════════════════════════════════

def _ensure_holidays(db_path: str, year: int):
    """确保当年节假日数据存在，不存在则从 Finnhub 拉取并写入"""
    if data_store.has_holidays_for_year(db_path, year):
        logger.info("节假日数据已存在：%d 年，跳过拉取", year)
        return

    logger.info("节假日数据不存在，正在从 Finnhub 拉取：%d 年...", year)
    try:
        import finnhub
        api_key = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            logger.error("FINNHUB_API_KEY 未配置，无法拉取节假日数据")
            return

        client = finnhub.Client(api_key=api_key)
        result = client.market_holiday(exchange="US")

        # Finnhub 返回格式: {"data": [{"atDate": "2026-01-01", "eventName": "New Year's Day"}, ...]}
        raw_list = result.get("data", [])
        holidays = [
            {"date": h["atDate"], "name": h.get("eventName", "")}
            for h in raw_list
            if h.get("atDate", "").startswith(str(year))
        ]

        if not holidays:
            logger.warning("Finnhub 未返回 %d 年节假日数据", year)
            return

        data_store.save_holidays(db_path, "US", holidays)
        logger.info("节假日写入完成：%d 年共 %d 条", year, len(holidays))

    except Exception as e:
        logger.error("节假日拉取失败: %s", e)


# ══════════════════════════════════════════════════════════════
# [2] 真实交易日列表构建
# ══════════════════════════════════════════════════════════════

def _build_trading_days(db_path: str, lookback_days: int) -> list:
    """
    向前回溯 lookback_days 个自然日，排除周末和节假日，返回真实交易日列表
    格式: ['2026-05-01', '2026-05-02', ...]
    """
    today = date.today()
    year = today.year

    # 确保当年和去年节假日都在库（跨年回溯时需要）
    _ensure_holidays(db_path, year)
    if lookback_days > 30:
        _ensure_holidays(db_path, year - 1)

    holidays = data_store.get_holidays(db_path, year)
    holidays |= data_store.get_holidays(db_path, year - 1)

    trading_days = []
    for i in range(lookback_days):
        d = today - timedelta(days=i)
        # 排除周末
        if d.weekday() in (5, 6):
            continue
        # 排除节假日
        if d.strftime("%Y-%m-%d") in holidays:
            continue
        trading_days.append(d.strftime("%Y-%m-%d"))

    # 升序返回（oldest first）
    return sorted(trading_days)


# ══════════════════════════════════════════════════════════════
# [3] 日线数据补录
# ══════════════════════════════════════════════════════════════

def _sync_candles(db_path: str, ticker: str, trading_days: list):
    """
    检查缺口并补录日线数据
    只拉取缺失的日期区间，不重复拉取已有数据
    """
    missing = data_store.get_missing_dates(db_path, ticker, trading_days)
    if not missing:
        logger.info("日线数据无缺口：%s", ticker)
        return

    logger.info("发现缺口 %d 天，开始补录：%s %s ~ %s",
                len(missing), ticker, missing[0], missing[-1])

    try:
        import requests
        
        tiingo_api_key = os.environ.get("TIINGO_API_KEY")
        if not tiingo_api_key:
            logger.error("TIINGO_API_KEY 未配置，无法补录日线数据")
            return
            
        start_date = missing[0]
        # 加一天以确保获取到结束当天的数据
        end_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        url = f"https://api.tiingo.com/tiingo/daily/{ticker}/prices"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Token {tiingo_api_key}'
        }
        params = {
            'startDate': start_date,
            'endDate': end_date
        }
        
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        
        if resp.status_code == 404:
            logger.warning("Tiingo 未找到数据：%s", ticker)
            return
        resp.raise_for_status()
        
        data = resp.json()
        if not data:
            logger.warning("Tiingo 返回数据为空：%s", ticker)
            return

        rows = []
        for row in data:
            # Tiingo returns date as "YYYY-MM-DDTHH:MM:SS.000Z"
            date_str = row['date'].split('T')[0]
            if date_str not in missing:
                continue
                
            c = float(row.get('close', 0.0))
            
            # 数据质量校验
            if not c or pd.isna(c) or c <= 0:
                logger.warning("异常收盘价，标记 suspect：%s %s close=%s", ticker, date_str, c)
                rows.append({"date": date_str, "close": c or 0.0, "quality": "suspect"})
                continue

            # 涨跌幅超过50%标记为 suspect
            quality = "ok"
            if rows:
                prev_close = rows[-1]["close"]
                if prev_close > 0:
                    change_pct = abs(c - prev_close) / prev_close * 100
                    if change_pct > 50:
                        logger.warning("涨跌幅异常(%.1f%%)，标记 suspect：%s %s", change_pct, ticker, date_str)
                        quality = "suspect"

            rows.append({"date": date_str, "close": c, "quality": quality})

        if rows:
            data_store.save_candles(db_path, ticker, rows)
            logger.info("补录完成：%s 写入 %d 条", ticker, len(rows))
        else:
            logger.warning("补录结果为空：%s，可能 Tiingo 未包含缺失日期", ticker)

    except Exception as e:
        logger.error("日线补录失败 [%s]: %s", ticker, e)


# ══════════════════════════════════════════════════════════════
# [4] 技术指标计算并缓存
# ══════════════════════════════════════════════════════════════

def _cache_technical_indicators(db_path: str, ticker: str, tech_cache_ttl_hours: int):
    """从本地 stock_candles 计算技术指标并写入 api_cache"""
    cache_key = f"tech_{ticker}"

    rows = data_store.get_candles(db_path, ticker, limit=252)
    if not rows:
        logger.warning("本地日线数据为空，无法计算技术指标：%s", ticker)
        return

    closes = [r["close"] for r in rows if r["quality"] == "ok"]
    if len(closes) < 14:
        logger.warning("有效日线数据不足14条，跳过指标计算：%s", ticker)
        return

    df = pd.Series(closes)

    result = {"current_price": closes[-1]}

    # SMA50
    if len(closes) >= 50:
        result["sma50"] = round(df.rolling(window=50).mean().iloc[-1], 2)

    # SMA200
    if len(closes) >= 200:
        result["sma200"] = round(df.rolling(window=200).mean().iloc[-1], 2)

    # RSI(14) — Wilder 算法
    delta = df.diff()
    gain = delta.clip(lower=0)
    loss = -1 * delta.clip(upper=0)
    ema_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    ema_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()
    rs = ema_gain / ema_loss
    rsi = 100 - (100 / (1 + rs))
    rsi_val = rsi.iloc[-1]
    if not pd.isna(rsi_val):
        result["rsi_14"] = round(float(rsi_val), 2)

    # 数据完整性标记
    result["partial"] = len(closes) < 200

    ttl_seconds = tech_cache_ttl_hours * 3600
    data_store.set_cache(db_path, cache_key, result, ttl_seconds)
    logger.info("技术指标缓存完成：%s %s", ticker, result)


# ══════════════════════════════════════════════════════════════
# [5] 分析师目标价缓存
# ══════════════════════════════════════════════════════════════

def _cache_analyst_estimates(db_path: str, ticker: str, estimates_cache_ttl_hours: int):
    """从 yfinance 拉取分析师目标价并写入 api_cache"""
    cache_key = f"estimates_{ticker}"

    try:
        import yfinance as yf
        
        info = yf.Ticker(ticker).info

        result = {
            "targetHigh": info.get("targetHighPrice"),
            "targetLow": info.get("targetLowPrice"),
            "targetMean": info.get("targetMeanPrice"),
            "numberAnalysts": info.get("numberOfAnalystOpinions"),
        }

        ttl_seconds = estimates_cache_ttl_hours * 3600
        data_store.set_cache(db_path, cache_key, result, ttl_seconds)
        logger.info("分析师目标价缓存完成：%s", ticker)

    except Exception as e:
        logger.error("分析师目标价拉取失败 [%s]: %s", ticker, e)


# ══════════════════════════════════════════════════════════════
# [6] 公司定向新闻拉取
# ══════════════════════════════════════════════════════════════

def _sync_company_news(db_path: str, ticker: str, fetch_days: int):
    """拉取指定 ticker 最近 fetch_days 天的公司新闻写入 company_news 表"""
    try:
        import finnhub
        api_key = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            logger.error("FINNHUB_API_KEY 未配置")
            return

        client = finnhub.Client(api_key=api_key)
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")

        news_raw = client.company_news(ticker, _from=start_date, to=end_date)
        time.sleep(1.2)

        items = []
        for n in news_raw:
            url = n.get("url", "").strip()
            if not url:
                continue
            guid = hashlib.md5(url.encode()).hexdigest()
            items.append({
                "guid": guid,
                "headline": n.get("headline", "").strip(),
                "summary": n.get("summary", "").strip()[:300],
                "url": url,
                "source": n.get("source", ""),
                "datetime": str(n.get("datetime", "")),
            })

        if items:
            data_store.save_company_news(db_path, ticker, items)
            logger.info("公司新闻写入完成：%s %d 条", ticker, len(items))
        else:
            logger.info("公司新闻无新数据：%s", ticker)

    except Exception as e:
        logger.error("公司新闻拉取失败 [%s]: %s", ticker, e)


# ══════════════════════════════════════════════════════════════
# [7] 主流程
# ══════════════════════════════════════════════════════════════

async def main(channel_id: int):
    cfg = config.load()
    db_path = str(settings.DB_PATH)
    sync_cfg = cfg.get("data_sync", {})

    lookback_days          = sync_cfg.get("lookback_days", 14)
    company_news_retention = sync_cfg.get("company_news_retention_days", 7)
    company_news_fetch     = sync_cfg.get("company_news_fetch_days", 3)
    tech_cache_ttl         = sync_cfg.get("tech_cache_ttl_hours", 4)
    estimates_cache_ttl    = sync_cfg.get("estimates_cache_ttl_hours", 24)

    # 获取 watch_list
    watch_list = [i["ticker"] for i in cfg["market"].get("watch_list", [])]
    if not watch_list:
        logger.warning("watch_list 为空，跳过数据同步")
        return

    # 初始化数据库表
    data_store.init_db(db_path)

    logger.info("═" * 50)
    logger.info("开始数据同步，标的：%s", watch_list)
    logger.info("═" * 50)

    # Step 1 & 2：节假日 + 真实交易日列表（所有 ticker 共用）
    trading_days = _build_trading_days(db_path, lookback_days)
    logger.info("本次检查交易日范围：%s ~ %s（%d 天）",
                trading_days[0] if trading_days else "N/A",
                trading_days[-1] if trading_days else "N/A",
                len(trading_days))

    errors = []

    for ticker in watch_list:
        logger.info("─" * 40)
        logger.info("处理标的：%s", ticker)

        # Step 3：日线补录
        try:
            _sync_candles(db_path, ticker, trading_days)
        except Exception as e:
            logger.error("日线补录异常 [%s]: %s", ticker, e)
            errors.append(f"{ticker} 日线补录失败: {e}")

        # Step 4：技术指标缓存
        try:
            _cache_technical_indicators(db_path, ticker, tech_cache_ttl)
        except Exception as e:
            logger.error("技术指标缓存异常 [%s]: %s", ticker, e)
            errors.append(f"{ticker} 技术指标失败: {e}")

        # Step 5：分析师目标价缓存
        try:
            _cache_analyst_estimates(db_path, ticker, estimates_cache_ttl)
        except Exception as e:
            logger.error("分析师目标价缓存异常 [%s]: %s", ticker, e)
            errors.append(f"{ticker} 目标价失败: {e}")

        # Step 6：公司新闻
        try:
            _sync_company_news(db_path, ticker, company_news_fetch)
        except Exception as e:
            logger.error("公司新闻拉取异常 [%s]: %s", ticker, e)
            errors.append(f"{ticker} 公司新闻失败: {e}")

    # Step 7：清理过期数据
    logger.info("─" * 40)
    logger.info("开始清理过期数据...")
    data_store.cleanup_cache(db_path)
    data_store.cleanup_company_news(db_path, company_news_retention)

    # 汇报结果
    logger.info("═" * 50)
    if errors:
        err_msg = "\n".join(errors)
        logger.error("数据同步完成，存在以下错误：\n%s", err_msg)
        try:
            await discord_utils.send_to_channel(
                channel_id,
                f"⚠️ **数据同步完成（有错误）**\n```{err_msg}```"
            )
        except Exception as e:
            logger.error("Discord 通知发送失败: %s", e)
    else:
        logger.info("数据同步全部完成，无错误")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True, help="Discord log channel ID")
    args = parser.parse_args()
    asyncio.run(main(args.channel))
