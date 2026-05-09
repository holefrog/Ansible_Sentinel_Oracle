# agent/data_engine.py
# v5.5 - 修复成交量数据缺失，支持实时与均量抓取

import logging
import os
import time
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


# ── 自定义 API 引擎 ────────────────────────────────────────────────────────────

def _fetch_fear_greed() -> Optional[dict]:
    """通过 Alternative.me 抓取恐慌与贪婪指数"""
    url = "https://api.alternative.me/fng/"
    try:
        import requests
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        val = int(data['data'][0]['value'])
        status = data['data'][0]['value_classification']
        
        return {
            "name":       "恐惧与贪婪指数",
            "price":      val,
            "change_pct": 0.0,
            "volume":     0,
            "avg_volume": 0,
            "note":       status
        }
    except Exception as e:
        logger.warning("恐惧与贪婪指数抓取失败: %s", e)
        return None

def _fetch_fred(series_id: str, name: str) -> Optional[dict]:
    """通过官方 FRED API 抓取宏观数据"""
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        logger.warning("FRED_API_KEY 未设置，跳过 %s 抓取", name)
        return None
        
    url = f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}&api_key={api_key}&file_type=json&sort_order=desc&limit=2"
    try:
        import requests
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("observations", [])
        
        if len(data) < 2:
            return None
            
        valid_vals = [float(d["value"]) for d in data if d["value"] != "."][:2]
        if len(valid_vals) < 2:
            return None
            
        val_today = valid_vals[0]
        val_prev = valid_vals[1]
        change_pct = (val_today - val_prev) / val_prev * 100
        
        return {
            "name":       name,
            "price":      val_today,
            "change_pct": change_pct,
            "volume":     0,
            "avg_volume": 0,
            "note":       ""
        }
    except Exception as e:
        logger.warning("FRED 抓取失败 [%s]: %s", series_id, e)
        return None


# ── 公共接口：市场状态 ─────────────────────────────────────────────────────────

def market_status() -> dict:
    """查询当前市场状态。"""
    weekday = date.today().weekday()
    if weekday == 5:
        return {"is_trading_day": False, "is_open": False,
                "note": "⚠️ 今日周六，数据为上一交易日收盘"}
    if weekday == 6:
        return {"is_trading_day": False, "is_open": False,
                "note": "⚠️ 今日周日，数据为上一交易日收盘"}

    api_key = os.environ.get("FINNHUB_API_KEY")
    if api_key:
        try:
            import finnhub
            client = finnhub.Client(api_key=api_key)
            status = client.market_status(exchange="US")
            holiday = status.get("holiday")
            is_open = status.get("isOpen", False)

            if holiday:
                return {
                    "is_trading_day": False,
                    "is_open":        False,
                    "note":           f"⚠️ 今日休市（{holiday}），数据为上一交易日收盘",
                }
            return {"is_trading_day": True, "is_open": is_open, "note": ""}
        except Exception as e:
            logger.warning("Finnhub market_status 查询失败：%s", e)

    # 兜底默认设为休市，防止 API 失败导致无效扫描
    return {"is_trading_day": True, "is_open": False, "note": "接口查询异常"}


# ── 公共接口：股票数据 ─────────────────────────────────────────────────────────

def fetch(tickers: list) -> dict:
    """统一数据出口。"""
    results = {}
    finnhub_tickers = []
    
    for sym, name in tickers:
        if sym == "FNG":
            results[sym] = _fetch_fear_greed()
        elif sym == "DGS10":
            results[sym] = _fetch_fred(sym, name)
        else:
            finnhub_tickers.append((sym, name))
            
    if finnhub_tickers:
        results.update(_from_finnhub(finnhub_tickers))
        
    return results


# ── Finnhub 引擎 ───────────────────────────────────────────────────────────

def _from_finnhub(tickers: list) -> dict:
    """
    [修复版] 从 Finnhub 获取实时报价、当日成交量及历史均量。
    """
    import finnhub

    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        logger.error("FINNHUB_API_KEY 未设置！")
        return {sym: None for sym, _ in tickers}

    client  = finnhub.Client(api_key=api_key)
    results = {}

    for sym, name in tickers:
        try:
            # 1. 获取实时报价与当日成交量 (v 字段)
            res = client.quote(sym)
            if not res or res.get("c") in (0, None):
                results[sym] = None
                continue

            close_today = res["c"]
            close_prev  = res["pc"]
            current_vol = res.get("v", 0)  # 获取当日成交量
            change_pct  = (close_today - close_prev) / close_prev * 100 if close_prev else 0.0

            # 2. 获取财务数据以提取“10日平均成交量” (用于计算量比)
            avg_vol = 0
            try:
                # 针对指数（如 SPY/QQQ）和个股通用的指标接口
                financials = client.company_basic_financials(sym, 'all')
                avg_vol = financials.get('metric', {}).get('10DayAverageTradingVolume', 0)
            except Exception as fe:
                logger.debug("无法获取 %s 的均量指标: %s", sym, fe)

            results[sym] = {
                "name":       name,
                "price":      close_today,
                "change_pct": change_pct,
                "volume":     current_vol,
                "avg_volume": avg_vol,
                "note":       ""
            }
            # Finnhub 免费版限速保护 (每次请求后 sleep)
            time.sleep(1.2)
        except Exception as e:
            logger.warning("Finnhub: %s 请求失败：%s", sym, e)
            results[sym] = None

    return results


# ── 量化指标引擎 (Agent Tools) ───────────────────────────────────────────────

# ── 量化指标引擎 (Agent Tools) ───────────────────────────────────────────────

def get_technical_indicators(ticker: str, db_path: str) -> dict:
    """
    获取个股技术指标 (SMA50 / SMA200 / RSI)
    优先读 api_cache，未命中则从本地 stock_candles 计算后写缓存
    不再直接调用 Finnhub stock_candles 接口
    """
    from . import data_store

    cache_key = f"tech_{ticker}"
    cached = data_store.get_cache(db_path, cache_key)
    if cached:
        logger.debug("技术指标命中缓存：%s", ticker)
        return cached

    logger.info("技术指标缓存未命中，从本地日线计算：%s", ticker)
    rows = data_store.get_candles(db_path, ticker, limit=252)
    if not rows:
        logger.warning("本地日线数据为空，返回空指标：%s", ticker)
        return {}

    closes = [r["close"] for r in rows if r["quality"] == "ok"]
    if len(closes) < 14:
        logger.warning("有效日线数据不足14条，返回空指标：%s", ticker)
        return {"current_price": closes[-1] if closes else None}

    import pandas as pd
    df = pd.Series(closes)
    result = {"current_price": closes[-1]}

    if len(closes) >= 50:
        result["sma50"] = round(df.rolling(window=50).mean().iloc[-1], 2)

    if len(closes) >= 200:
        result["sma200"] = round(df.rolling(window=200).mean().iloc[-1], 2)

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

    result["partial"] = len(closes) < 200

    # 写入缓存，TTL 4小时（data_sync 每日收盘后刷新，此处作为盘中保护）
    data_store.set_cache(db_path, cache_key, result, ttl_seconds=4 * 3600)
    return result


def get_analyst_estimates(ticker: str, db_path: str) -> dict:
    """
    获取华尔街分析师一致预期
    优先读 api_cache，未命中则从 Finnhub 拉取后写缓存
    """
    from . import data_store

    cache_key = f"estimates_{ticker}"
    cached = data_store.get_cache(db_path, cache_key)
    if cached:
        logger.debug("分析师目标价命中缓存：%s", ticker)
        return cached

    logger.info("分析师目标价缓存未命中，从 Finnhub 拉取：%s", ticker)
    try:
        import finnhub
        api_key = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            logger.error("FINNHUB_API_KEY 未配置")
            return {}

        client = finnhub.Client(api_key=api_key)
        res = client.price_target(ticker)

        result = {
            "targetHigh": res.get("targetHigh"),
            "targetLow": res.get("targetLow"),
            "targetMean": res.get("targetMean"),
            "numberAnalysts": res.get("numberAnalysts"),
        }

        # 写入缓存，TTL 24小时
        data_store.set_cache(db_path, cache_key, result, ttl_seconds=24 * 3600)
        return result

    except Exception as e:
        logger.warning("分析师目标价拉取失败 [%s]: %s", ticker, e)
        return {}
