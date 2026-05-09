# agent/report.py
# v8.1 - 晚报增强版：支持从数据库提取具体时间并展示
import argparse
import asyncio
import logging
import os
import sys
import json
import re
import sqlite3
from datetime import datetime, timedelta

from . import config
from .config import settings  # 引入全局单例配置对象
from . import data_engine
from . import discord_utils
from . import news_dedup
from . import news_engine
from . import translator
from . import llm_gateway
from . import brief
from . import renderer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("report")

async def distribute_news(cfg: dict, news_items: list):
    """
    [完整保留] 按照 news.toml 的配置，将新闻分发到不同的 Discord 频道。
    已适配新的 content_zh (中文深度解析) 显示。
    """
    source_map = {}
    for s in cfg["news"]["sources"]:
        if s.get("enabled"):
            ch_key = s.get("channel", s.get("channel_id"))
            actual_ch_id = config.resolve_channel(cfg, ch_key) if ch_key else "0"
            source_map[s["name"]] = {
                "channel": int(actual_ch_id) if str(actual_ch_id).isdigit() else None,
                "min_score": s.get("min_score", 0)
            }

    channel_batches = {}
    for item in news_items:
        src_info = source_map.get(item["source"]) or source_map.get(item["source"].split('/')[0])
        if not src_info or not src_info["channel"]: continue
        if item.get("ai_score", 0) < src_info["min_score"]: continue
        
        ch_id = src_info["channel"]
        if ch_id not in channel_batches: channel_batches[ch_id] = []
        
        tickers = " ".join(f"`{t}`" for t in item["tickers"][:3]) if item.get("tickers") else ""
        
        # 修复：格式化分数为两位数，并移除“分”字
        score_val = f"{int(item['ai_score']):02d}"
        ai_badge = f"**[{'🔥' if item['ai_score']>=8 else '🤖'} {item.get('ai_engine', 'AI')} {score_val}]** "
        title_display = f"[{item.get('title_zh', item['title'])}]({item['url']})"
        
        
        # 【微调】增加时间戳显示
        time_tag = f" `[{item.get('published', '')}]`" if item.get('published') else ""
        msg = f"**{item['source']}** {tickers}{time_tag}\n📰 {ai_badge}{title_display}"
        
        # 仅显示短摘要，避免 Discord 消息过长
        if item.get("summary"):
            msg += f"\n> {item['summary'][:150]}..."
            
        channel_batches[ch_id].append(msg)

    for ch_id, msgs in channel_batches.items():
        full_msg = f"🚨 **报告分发预警** | {len(msgs)} 条重要资讯\n\n" + "\n\n".join(msgs)
        chunks = discord_utils._split(full_msg)
        for chunk in chunks:
            await discord_utils.send_to_channel(ch_id, chunk)

async def main(channel_id: int, report_type: str):
    cfg = config.load()
    task_name = "早报" if report_type == "morning" else "晚报"
    
    # 完美解耦：直接从 settings 获取绝对路径并转为字符串供 SQLite 使用
    db_path = str(settings.DB_PATH)

    # 1. 获取基础配置与交易日状态
    market_context, watch_list = config.get_targets(cfg)
    mkt = data_engine.market_status()
    
    # [新增] 休市 Discord 提示
    if not mkt.get("is_trading_day", True):
        logger.warning(f"🕒 当前非交易日，取消{task_name}生成。")
        try:
            msg = f"🛏️ **休市提示** | 当前时间为非交易日（或美股休市），今日无{task_name}生成。好好休息！"
            await discord_utils.send_to_channel(channel_id, msg)
        except Exception as e:
            logger.error(f"发送休市提示失败: {e}")
        return

    calendar, news_items, ai_summary = {}, [], None

    # 2. 数据准备逻辑
    news_dedup.init_db(db_path)
    rpt_cfg = cfg["report"]
    min_score = rpt_cfg["min_score_threshold"]
    max_items = rpt_cfg.get("max_news_items", 15)

    if report_type == "morning":
        calendar = news_engine.fetch_calendar(cfg)
        cutoff_iso = (datetime.now() - timedelta(hours=15)).isoformat()
        
        try:
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                cursor = conn.execute("""
                    SELECT r.guid, r.url, r.score, r.title, r.title_zh, r.reason,
                           b.translated_content, b.full_content, r.created_at, r.ai_engine
                    FROM news_registry r
                    LEFT JOIN news_briefs b ON r.guid = b.guid
                    WHERE r.created_at >= ? AND r.score >= ?
                    ORDER BY r.score DESC LIMIT ?
                """, (cutoff_iso, min_score, max_items))
                
                rows = cursor.fetchall()
                for row in rows:
                    guid,url,score,title, title_zh,reason,trans_content,full_content,created_at,ai_engine = row                    
                    try:
                        dt_obj = datetime.fromisoformat(created_at)
                        pub_time_str = dt_obj.strftime(" %H:%M ET")
                    except:
                        pub_time_str = ""

                    news_items.append({
                        "guid": guid, "url": url, "ai_score": score,
                        "title": title or "", "title_zh": title_zh or title or "",
                        "reason": reason or "", "content_zh": trans_content or "",
                        "full_content": full_content or "", "source": "隔夜资讯", 
                        "published": pub_time_str, "tickers": [],
                        "ai_engine": ai_engine or "AI" 
                    })
            logger.info(f"早报：从数据库盘点到 {len(news_items)} 条隔夜重要预警（包含时间戳）。")
        except Exception as e:
            logger.error(f"早报提取隔夜新闻数据失败: {e}")
            
    else:
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        try:
            with sqlite3.connect(db_path, timeout=10.0) as conn:
                cursor = conn.execute("""
                    SELECT r.guid, r.url, r.score, r.title, r.title_zh, r.reason,
                           b.translated_content, b.full_content, r.created_at, r.ai_engine
                    FROM news_registry r
                    LEFT JOIN news_briefs b ON r.guid = b.guid
                    WHERE r.created_at LIKE ? AND r.score >= ?
                     ORDER BY r.score DESC LIMIT ?
            """, (f"{today_str}%", min_score, max_items))
                
                rows = cursor.fetchall()
                for row in rows:
                    guid,url,score,title, title_zh,reason,trans_content,full_content,created_at,ai_engine = row                    
                    # 【新增】转换 ISO 时间为 HH:MM ET 格式
                    try:
                        # created_at 格式为 2026-04-25T22:00:14.xxx
                        dt_obj = datetime.fromisoformat(created_at)
                        pub_time_str = dt_obj.strftime(" %H:%M ET")
                    except:
                        pub_time_str = ""

                    news_items.append({
                        "guid": guid,
                        "url": url,
                        "ai_score": score,
                        "title": title or "",
                        "title_zh": title_zh or title or "",
                        "reason": reason or "",
                        "content_zh": trans_content or "",
                        "full_content": full_content or "",
                        "source": "今日焦点", 
                        "published": pub_time_str,
                        "tickers": [],
                        "ai_engine": ai_engine or "AI" 
                    })
            logger.info(f"晚报：从数据库盘点到 {len(news_items)} 条今日重要预警（包含时间戳）。")
        except Exception as e:
            logger.error(f"提取今日新闻数据失败: {e}")

    # 3. 统一生成 AI 深度总结 (基于本地已提纯的数据，早晚报共享逻辑)
    if news_items:
        payload_data = []
        for n in news_items:
            raw_content = n.get("full_content", "")
            payload_data.append({
                "title": n["title_zh"] or n["title"], 
                "content": raw_content[:2500] if raw_content else n.get("content_zh", "")[:500]
            })
        user_payload = json.dumps(payload_data, ensure_ascii=False)
        
        summary_sys = rpt_cfg.get("summary_prompt", {}).get("system", "")
        anomaly_sys = rpt_cfg.get("anomaly_prompt", {}).get("system", "")
        summary_text, anomaly_text = "", ""
        
        # 视角一：生成宏观基本面解读
        try:
            if summary_sys:
                summary_text, _, used_engine1 = llm_gateway.query(summary_sys, user_payload, db_path, task_role="summary")
                summary_text = re.sub(r"```[a-zA-Z]*\n?", "", summary_text).strip()
                summary_text = f"**[{used_engine1}]**\n" + summary_text
        except llm_gateway.AllEnginesFailedError as e:
            logger.error(f"生成 AI 总结致命异常: {e}")
            await discord_utils.send_emergency_alert(cfg, f"**AI 宏观总结生成失败（报告将照常发布）**\n```{e}```")
        except Exception as e:
            logger.error(f"生成 AI 总结失败: {e}")
            
        # 视角二：生成交易员异动分析
        try:
            if anomaly_sys:
                anomaly_text, _, used_engine2 = llm_gateway.query(anomaly_sys, user_payload, db_path, task_role="anomaly")
                anomaly_text = re.sub(r"```[a-zA-Z]*\n?", "", anomaly_text).strip()
                anomaly_text = f"**[{used_engine2}]**\n" + anomaly_text
        except llm_gateway.AllEnginesFailedError as e:
            logger.error(f"生成交易员视角致命异常: {e}")
            await discord_utils.send_emergency_alert(cfg, f"**AI 异动分析生成失败（报告将照常发布）**\n```{e}```")
        except Exception as e:
            logger.error(f"生成交易员视角失败: {e}")
            
        # 拼接双视角内容
        parts = []
        if summary_text: parts.append("### 📊 宏观与基本面总结\n" + summary_text)
        if anomaly_text: parts.append("### ⚡ 交易员异动视角\n" + anomaly_text)
        
        ai_summary = "\n\n".join(parts) if parts else "⚠️ *AI 市场解读生成失败，请稍后检查系统日志或 LLM API 状态。*"

    # 4. 获取最终行情
    mc_data = data_engine.fetch(market_context)
    watch_data = data_engine.fetch(watch_list)

    # 5. 渲染视觉组件 (调用工厂)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    date_str = datetime.now().strftime("%Y-%m-%d")
    
    DOMAIN = os.getenv("SENTINEL_DOMAIN", "").strip("/")
    SENTINEL_PREFIX = os.getenv("SENTINEL_PREFIX", "sentinel").strip("/")
    prefix_path = f"/{SENTINEL_PREFIX}" if SENTINEL_PREFIX else ""
    report_url = f"https://{DOMAIN}{prefix_path}/reports/" if DOMAIN else ""

    embeds = renderer.build_report_embeds(
        report_type, market_context, watch_list, mc_data, watch_data,
        now_str, mkt["note"], calendar, news_items, ai_summary,
        report_url=report_url  # 直接传进去
    )

    front, body, fname = renderer.build_report_markdown(
        report_type, market_context, watch_list, mc_data, watch_data, 
        now_str, date_str, mkt["note"], calendar, news_items, ai_summary
    )

    # 6. 发布
    await discord_utils.publish_report(channel_id, task_name, embeds, fname, front, body, cfg)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--type", type=str, required=True, choices=["morning", "evening"])
    args = parser.parse_args()
    asyncio.run(main(args.channel, args.type))
