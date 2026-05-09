# agent/news_dedup.py
import sqlite3
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

def init_db(db_path: str):
    """初始化数据库，增加理由和标题字段"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            # 登记表：记录流水与打分
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_registry (
                    guid TEXT PRIMARY KEY,
                    url TEXT,
                    ai_engine TEXT,
                    score INTEGER,
                    reason TEXT,
                    has_brief INTEGER,
                    title TEXT,
                    title_zh TEXT,
                    created_at TEXT
                )
            """)

            # 正文表：存储抓取的内容和翻译
            conn.execute("""
                CREATE TABLE IF NOT EXISTS news_briefs (
                    guid TEXT PRIMARY KEY,
                    url TEXT,
                    full_content TEXT,
                    translated_content TEXT, 
                    created_at TEXT
                )
            """)
            
    except Exception as e:
        logger.error("初始化数据库失败: %s", e)

def filter_unprocessed(db_path: str, news_items: list) -> list:
    if not news_items: return []
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            guids = [item.get("guid", item.get("url")) for item in news_items]
            placeholders = ",".join(["?"] * len(guids))
            cursor = conn.execute(f"SELECT guid FROM news_registry WHERE guid IN ({placeholders})", tuple(guids))
            seen = {row[0] for row in cursor.fetchall()}
            return [it for it in news_items if it.get("guid", it.get("url")) not in seen]
    except Exception as e:
        logger.error("去重查询失败: %s", e)
        return news_items

def mark_processed(db_path: str, guid: str, url: str, score: int, has_brief: bool, reason: str = "", title: str = "", title_zh: str = "", ai_engine: str = "AI"):
    """记录已处理状态及元数据"""
    if not guid: return
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute("""
                INSERT OR IGNORE INTO news_registry (guid, url, score, reason, has_brief, title, title_zh, created_at, ai_engine)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (guid, url, score, reason, 1 if has_brief else 0, title, title_zh, now, ai_engine))
            conn.commit()
    except Exception as e:
        logger.error("标记已处理失败: %s", e)

def save_brief(db_path: str, guid: str, url: str, content: str, translated_content: str = None):
    if not guid or not content: return
    now = datetime.now().isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO news_briefs (guid, url, full_content, translated_content, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (guid, url, content, translated_content, now))
            conn.commit()
    except Exception as e:
        logger.error("保存正文内容失败: %s", e)

def get_brief(db_path: str, guid: str) -> tuple:
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            cursor = conn.execute("SELECT full_content, translated_content FROM news_briefs WHERE guid = ?", (guid,))
            row = cursor.fetchone()
            return (row[0] or "", row[1] or "") if row else ("", "")
    except Exception: return ("", "")

def cleanup(db_path: str, retention_days: int = 30):
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
    try:
        with sqlite3.connect(db_path, timeout=10.0) as conn:
            conn.execute("DELETE FROM news_registry WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM news_briefs WHERE created_at < ?", (cutoff,))
            conn.commit()
    except Exception as e:
        logger.error("清理旧数据失败: %s", e)
