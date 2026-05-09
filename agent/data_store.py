# agent/data_store.py
# v1 - 市场数据数据库管理层
# 负责: stock_candles / market_holidays / api_cache / company_news 四张表
# 使用者: data_sync.py (写), data_engine.py (读写 api_cache), advisor.py (读 company_news)
# 与 news_dedup.py 完全独立，互不干涉

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# [1] 初始化
# ══════════════════════════════════════════════════════════════

def init_db(db_path: str):
    """建立所有四张表，幂等，可反复调用"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:

            # 日线数据：永久保留
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_candles (
                    ticker     TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    close      REAL NOT NULL,
                    quality    TEXT NOT NULL DEFAULT 'ok',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (ticker, date)
                )
            """)

            # 美股节假日：永久保留，按年写入
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_holidays (
                    exchange   TEXT NOT NULL,
                    date       TEXT NOT NULL,
                    name       TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (exchange, date)
                )
            """)

            # 计算结果缓存：有过期时间
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key  TEXT PRIMARY KEY,
                    data       TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)

            # 公司定向新闻：保留7天
            conn.execute("""
                CREATE TABLE IF NOT EXISTS company_news (
                    guid       TEXT PRIMARY KEY,
                    ticker     TEXT NOT NULL,
                    headline   TEXT NOT NULL DEFAULT '',
                    summary    TEXT NOT NULL DEFAULT '',
                    url        TEXT NOT NULL DEFAULT '',
                    source     TEXT NOT NULL DEFAULT '',
                    datetime   TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)

            # 索引：加速 advisor 按 ticker + 时间查询
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_company_news_ticker_dt
                ON company_news (ticker, datetime)
            """)

            # 索引：加速日线按 ticker + date 区间查询
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_stock_candles_ticker_date
                ON stock_candles (ticker, date)
            """)

            # 投顾冷却：记录每只 ticker 最后触发时间
            conn.execute("""
                CREATE TABLE IF NOT EXISTS advisor_cooldown (
                    ticker         TEXT PRIMARY KEY,
                    last_triggered TEXT NOT NULL
                )
            """)

            conn.commit()
            logger.debug("data_store: 所有表初始化完成")
    except Exception as e:
        logger.error("data_store init_db 失败: %s", e)
        raise


# ══════════════════════════════════════════════════════════════
# [2] market_holidays
# ══════════════════════════════════════════════════════════════

def has_holidays_for_year(db_path: str, year: int) -> bool:
    """检查指定年份的节假日数据是否已存在"""
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM market_holidays WHERE date LIKE ?",
                (f"{year}-%",)
            )
            return cursor.fetchone()[0] > 0
    except Exception as e:
        logger.error("has_holidays_for_year 查询失败: %s", e)
        return False


def save_holidays(db_path: str, exchange: str, holidays: list):
    """
    批量写入节假日数据，主键冲突忽略（幂等）
    holidays 格式: [{"date": "2026-01-01", "name": "New Year's Day"}, ...]
    """
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO market_holidays (exchange, date, name, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                [(exchange, h["date"], h.get("name", ""), now) for h in holidays]
            )
            conn.commit()
        logger.info("节假日写入完成：%s 年 %d 条", exchange, len(holidays))
    except Exception as e:
        logger.error("save_holidays 失败: %s", e)


def get_holidays(db_path: str, year: int) -> set:
    """返回指定年份的节假日日期集合 {'2026-01-01', ...}"""
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute(
                "SELECT date FROM market_holidays WHERE date LIKE ?",
                (f"{year}-%",)
            )
            return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        logger.error("get_holidays 查询失败: %s", e)
        return set()


# ══════════════════════════════════════════════════════════════
# [3] stock_candles
# ══════════════════════════════════════════════════════════════

def save_candles(db_path: str, ticker: str, rows: list):
    """
    批量写入日线数据，主键冲突忽略（幂等）
    rows 格式: [{"date": "2026-05-06", "close": 195.2, "quality": "ok"}, ...]
    """
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO stock_candles (ticker, date, close, quality, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [(ticker, r["date"], r["close"], r.get("quality", "ok"), now) for r in rows]
            )
            conn.commit()
        logger.info("日线写入完成：%s %d 条", ticker, len(rows))
    except Exception as e:
        logger.error("save_candles 失败 [%s]: %s", ticker, e)


def get_candles(db_path: str, ticker: str, limit: int = 252) -> list:
    """
    获取最近 limit 条日线收盘价，按日期升序返回
    返回格式: [{"date": "...", "close": 195.2, "quality": "ok"}, ...]
    """
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT date, close, quality FROM stock_candles
                WHERE ticker = ?
                ORDER BY date DESC
                LIMIT ?
                """,
                (ticker, limit)
            )
            rows = cursor.fetchall()
            # 反转为升序（oldest first），方便 pandas 计算指标
            return [dict(r) for r in reversed(rows)]
    except Exception as e:
        logger.error("get_candles 查询失败 [%s]: %s", ticker, e)
        return []


def get_missing_dates(db_path: str, ticker: str, trading_days: list) -> list:
    """
    传入真实交易日列表 ['2026-05-01', '2026-05-02', ...]
    返回数据库中缺失的日期列表
    """
    if not trading_days:
        return []
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            placeholders = ",".join(["?"] * len(trading_days))
            cursor = conn.execute(
                f"SELECT date FROM stock_candles WHERE ticker = ? AND date IN ({placeholders})",
                [ticker] + trading_days
            )
            existing = {row[0] for row in cursor.fetchall()}
            return [d for d in trading_days if d not in existing]
    except Exception as e:
        logger.error("get_missing_dates 查询失败 [%s]: %s", ticker, e)
        return trading_days  # 查询失败时保守返回全部，触发补录


# ══════════════════════════════════════════════════════════════
# [4] api_cache
# ══════════════════════════════════════════════════════════════

def get_cache(db_path: str, key: str) -> dict | None:
    """
    查询未过期的缓存
    命中返回反序列化的 dict，未命中或已过期返回 None
    """
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute(
                "SELECT data FROM api_cache WHERE cache_key = ? AND expires_at > ?",
                (key, now)
            )
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
            return None
    except Exception as e:
        logger.error("get_cache 查询失败 [%s]: %s", key, e)
        return None


def set_cache(db_path: str, key: str, data: dict, ttl_seconds: int):
    """
    写入缓存，同 key 覆盖已有记录
    ttl_seconds: 缓存有效期（秒）
    """
    now = datetime.now()
    expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_cache (cache_key, data, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (key, json.dumps(data, ensure_ascii=False), now.isoformat(), expires_at)
            )
            conn.commit()
    except Exception as e:
        logger.error("set_cache 写入失败 [%s]: %s", key, e)


def cleanup_cache(db_path: str):
    """清理所有已过期的 api_cache 记录"""
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute(
                "DELETE FROM api_cache WHERE expires_at < ?", (now,)
            )
            conn.commit()
            logger.info("api_cache 清理完成，删除 %d 条过期记录", cursor.rowcount)
    except Exception as e:
        logger.error("cleanup_cache 失败: %s", e)


# ══════════════════════════════════════════════════════════════
# [5] company_news
# ══════════════════════════════════════════════════════════════

def save_company_news(db_path: str, ticker: str, news_items: list):
    """
    批量写入公司定向新闻，guid 冲突忽略（幂等）
    news_items 格式:
    [{"guid": "md5", "headline": "...", "summary": "...",
      "url": "...", "source": "...", "datetime": "..."}, ...]
    """
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO company_news
                    (guid, ticker, headline, summary, url, source, datetime, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["guid"],
                        ticker,
                        item.get("headline", ""),
                        item.get("summary", ""),
                        item.get("url", ""),
                        item.get("source", ""),
                        item.get("datetime", ""),
                        now,
                    )
                    for item in news_items
                    if item.get("guid")
                ]
            )
            conn.commit()
        logger.info("company_news 写入完成：%s %d 条", ticker, len(news_items))
    except Exception as e:
        logger.error("save_company_news 失败 [%s]: %s", ticker, e)


def get_company_news(db_path: str, ticker: str, days: int = 3) -> list:
    """
    获取指定 ticker 最近 days 天的公司新闻
    返回格式: [{"headline": "...", "summary": "...", "url": "...", "datetime": "..."}, ...]
    """
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT headline, summary, url, source, datetime
                FROM company_news
                WHERE ticker = ? AND created_at >= ?
                ORDER BY datetime DESC
                LIMIT 10
                """,
                (ticker, cutoff)
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error("get_company_news 查询失败 [%s]: %s", ticker, e)
        return []


def cleanup_company_news(db_path: str, retention_days: int = 7):
    """清理超过 retention_days 天的公司新闻"""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute(
                "DELETE FROM company_news WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
            logger.info("company_news 清理完成，删除 %d 条过期记录", cursor.rowcount)
    except Exception as e:
        logger.error("cleanup_company_news 失败: %s", e)


# ══════════════════════════════════════════════════════════════
# [6] 宏观新闻查询 (读 news_dedup 的 news_registry 表)
# ══════════════════════════════════════════════════════════════

def get_macro_news(db_path: str, min_score: int = 7, hours: int = 24) -> list:
    """
    从 news_registry 表读取近 hours 小时内的高分宏观新闻
    供 advisor 使用，作为宏观背景层
    返回格式: [{"title": "...", "title_zh": "...", "url": "...", "score": 8}, ...]
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT title, title_zh, url, score, created_at
                FROM news_registry
                WHERE score >= ? AND created_at >= ?
                ORDER BY score DESC
                LIMIT 10
                """,
                (min_score, cutoff)
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logger.error("get_macro_news 查询失败: %s", e)
        return []


# ══════════════════════════════════════════════════════════════
# [7] advisor_cooldown
# ══════════════════════════════════════════════════════════════

def check_advisor_cooldown(db_path: str, ticker: str) -> bool:
    """
    检查 ticker 是否在冷却期内（当天已触发过）。
    返回 True 表示还在冷却期（应跳过），False 表示可以触发。
    """
    now = datetime.now()
    # 按照美东时间 (如果是本地时间也可以，取决于部署环境，为了简单，统一按服务器日期判断)
    today_str = now.strftime("%Y-%m-%d")
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute(
                "SELECT last_triggered FROM advisor_cooldown WHERE ticker = ?",
                (ticker,)
            )
            row = cursor.fetchone()
            if row:
                last_triggered = row[0]
                if last_triggered.startswith(today_str):
                    return True
            return False
    except Exception as e:
        logger.error("check_advisor_cooldown 查询失败 [%s]: %s", ticker, e)
        return False


def update_advisor_cooldown(db_path: str, ticker: str):
    """更新 ticker 的最后触发时间为当前时间"""
    now_str = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO advisor_cooldown (ticker, last_triggered)
                VALUES (?, ?)
                """,
                (ticker, now_str)
            )
            conn.commit()
    except Exception as e:
        logger.error("update_advisor_cooldown 失败 [%s]: %s", ticker, e)

