# agent/news_scanner.py
# v20.2 - 统一配置加载

import argparse
import asyncio
import logging
import sys
import os
from datetime import datetime

from . import config
from .config import settings
from . import discord_utils
from . import news_dedup
from . import llm_gateway
from . import news_engine
from . import translator
from . import brief
from . import renderer

logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s", 
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("news_scanner")

def _format_item(item: dict) -> str:
    tickers = " ".join(f"`{t}`" for t in item["tickers"][:3]) if item.get("tickers") else ""
    pub = item.get("published", "")[:16]
    lines = [f"**{item['source']}** {tickers} | {pub}"]
    
    if "ai_score" in item:
        fire = "🔥" if item["ai_score"] >= 8 else "🤖"
        engine_name = item.get("ai_engine", "AI")
        score_val = f"{int(item['ai_score']):02d}"
        title_zh = item.get("title_zh") or item.get("title")
        title_en = item.get("title", "")
        page_url = item.get("page_url")
        original_url = item.get("url", "")

        if page_url:
            lines.append(f"📰 **[{fire} {engine_name} {score_val}]** [{title_zh}]({page_url})")
        else:
            lines.append(f"📰 **[{fire} {engine_name} {score_val}]** [{title_zh}]({original_url})")

        if title_zh != title_en and title_en:
            lines.append(f"└ 🇺🇸 *[{title_en}]({original_url})*")

    return "\n".join(lines)

async def main(default_channel_id: int):
    # 1. 配置加载
    cfg = config.load()
    news_cfg = cfg["news"]
    summary_min_len = news_cfg["summary_min_length"]
    global_threshold = news_cfg["relevance_threshold"]

    db_path = str(settings.DB_PATH)
    hugo_news_dir = settings.NEWS_DIR
    hugo_news_dir.mkdir(parents=True, exist_ok=True)

    # 2. 频道与信源配置
    source_configs = {}
    all_src_list = news_cfg["sources"]
    for s in all_src_list:
        if s.get("enabled"):
            ch_key = s.get("channel", s.get("channel_id"))
            actual_ch_id = config.resolve_channel(cfg, ch_key) if ch_key else default_channel_id
            source_configs[s["name"]] = {
                "channel": int(actual_ch_id),
                "min_score": s.get("min_score", global_threshold)
            }

    # 3. 数据库维护
    news_dedup.init_db(db_path)
    news_dedup.cleanup(db_path, news_cfg["retention_days"])

    # 4. 数据抓取与去重
    logger.info("📡 正在从各信源拉取最新实时数据...")
    items = news_engine.fetch_realtime(cfg)
    if not items: 
        logger.info("未发现任何实时更新，扫描结束。")
        return
        
    unprocessed = news_dedup.filter_unprocessed(db_path, items)
    if not unprocessed: 
        logger.info(f"本轮抓取的 {len(items)} 条内容均已存在，跳过 AI 评估。")
        return
    logger.info(f"去重完成：{len(items)} -> {len(unprocessed)} 条待处理。")

    # 5. AI 评分与深度处理
    logger.info(f"正在调用 AI 引擎对 {len(unprocessed)} 条条目进行打分...")
    try:
        scored_items = news_engine.ai_score_items(unprocessed, cfg)
    except llm_gateway.AllEnginesFailedError as e:
        logger.error(f"AI 评分发生致命异常: {e}")
        await discord_utils.send_emergency_alert(cfg, f"**打分任务终止**\n```{e}```")
        scored_items = []
    valid_items = []

    for item in scored_items:
        score, reason = item.get("ai_score", 0), item.get("reason", "")
        logger.info(f"评估详情: [{item['source']}] 分数: {score}, 原因: {reason} | 标题: {item['title'][:40]}...")
        
        guid, url = item.get("guid", item.get("url")), item.get("url", "")
        has_brief = False
        
        base_src = item["source"].split('/')[0]
        src_conf = source_configs.get(item["source"], source_configs.get(base_src, {"min_score": global_threshold}))
        current_threshold = src_conf["min_score"]

        if score >= current_threshold:
            logger.info(f"✨ 发现达标新闻 ({score}分): {item['title'][:40]}...")
            item["title_zh"] = translator.translate_title(item["title"], cfg)
            
            full_text, trans_content = "", ""
            if item.get("scrape_full_text", True):
                full_text = brief.fetch_content(url)
            
            if not full_text:
                summary_text = item.get("summary", "").strip()
                if len(summary_text) < summary_min_len:
                    trans_content = "⚠️ [该信源禁止原文抓取，且摘要过短不予解析]"
                else:
                    full_text = summary_text

            if full_text:
                trans_content, trans_engine = translator.translate_full_report(full_text, cfg)
                item["trans_engine"] = trans_engine
                if not item.get("scrape_full_text", True):
                    trans_content = f"⚠️ [正文抓取受阻，仅提供摘要解析]\n{trans_content}"
                
                news_dedup.save_brief(db_path, guid, url, full_text, trans_content)
                has_brief = True
            
            item["content_zh"] = trans_content
            
            front, body, fname = renderer.build_news_markdown(item, cfg)
            try:
                target_file = hugo_news_dir / fname
                with open(target_file, "w", encoding="utf-8") as f:
                    f.write(f"---\n{front}\n---\n\n{body}")
                logger.info(f"✅ Hugo 文章已生成: {fname}")
                
                page_slug = fname.replace(".md", "")
                domain = os.getenv("SENTINEL_DOMAIN", "").strip("/")
                sentinel_prefix = os.getenv("SENTINEL_PREFIX", "sentinel").strip("/")
                prefix_path = f"/{sentinel_prefix}" if sentinel_prefix else ""
                item["page_url"] = f"https://{domain}{prefix_path}/news/{page_slug}/"
                valid_items.append(item)
            except Exception as e:
                logger.error(f"Hugo 写入失败: {e}")

        news_dedup.mark_processed(
            db_path, guid, url, score, has_brief, reason, 
            item["title"], item.get("title_zh", ""), item.get("ai_engine", "AI")
        )

    # 6. 频道分发
    if valid_items:
        logger.info(f"🚀 准备对 {len(valid_items)} 条预警进行频道分发...")
        channel_batches = {}
        for item in valid_items:
            base_src = item["source"].split('/')[0]
            conf = source_configs.get(item["source"], source_configs.get(base_src, {"channel": default_channel_id}))
            ch_id = conf["channel"]
            if ch_id not in channel_batches: channel_batches[ch_id] = []
            channel_batches[ch_id].append(_format_item(item))

        for ch_id, msg_lines in channel_batches.items():
            header = f"🚨 **实时预警** | {datetime.now().strftime('%H:%M ET')}（{len(msg_lines)} 条重要公告）\n\n"
            full_msg = header + "\n\n---\n\n".join(msg_lines)
            for chunk in discord_utils._split(full_msg): 
                await discord_utils.send_to_channel(ch_id, chunk)
            logger.info(f"已推送 {len(msg_lines)} 条新闻至频道 {ch_id}")
    else:
        logger.info("本轮扫描无达标新闻。")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True, help="Default Discord Channel ID")
    try:
        asyncio.run(main(parser.parse_args().channel))
    except Exception as e:
        logger.critical(f"扫描器异常崩溃: {e}")
        sys.exit(1)
