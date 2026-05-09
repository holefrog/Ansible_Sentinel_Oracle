# agent/discord_utils.py
# v8.2 - 统一使用显式相对导入

import os
import sys
import subprocess
import logging
import asyncio
from typing import Optional, List
from datetime import datetime, timezone
import discord
from .config import settings, load, resolve_channel
import requests
import json

logger = logging.getLogger(__name__)

# 基础常量获取
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

_cfg = load()
DISCORD_API_BASE = _cfg["discord"]["api_base_url"]
USER_AGENT = _cfg["discord"]["user_agent"]

MAX_LENGTH = 1900


def _get_rest_headers():
    """构造统一的 REST 请求头"""
    return {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT
    }


def get_status_color(change_val: float) -> int:
    if change_val > 0: return 0x57F287
    elif change_val < 0: return 0xED4245
    else: return 0x95A5A6

def create_embed(title: str = "", description: str = "", color_val: float = 0.0, url: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=get_status_color(color_val),
        timestamp=datetime.now(timezone.utc),
        url=url or None  # Discord 要求 url 不能是空字符串
    )
    embed.set_footer(text="StockSentinel Project • Market Data")
    return embed

async def send_to_channel(channel_id: int, text: str):
    """使用 REST API 发送纯文本消息"""
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = _get_rest_headers()
    
    for chunk in _split(text):
        payload = {"content": chunk}
        try:
            # 将同步的 requests 放入独立线程运行，防止阻塞 asyncio 事件循环
            resp = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"REST 发送文本失败: {e}")

async def send_embeds(channel_id: int, embeds: List[discord.Embed]):
    """使用 REST API 发送 Embeds"""
    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    headers = _get_rest_headers()
    
    # 转换为 Discord API 接受的字典格式
    raw_embeds = [e.to_dict() for e in embeds]
    
    # Discord 限制单次发送最多 10 个 Embed
    for i in range(0, len(raw_embeds), 10):
        payload = {"embeds": raw_embeds[i:i+10]}
        try:
            # 将同步的 requests 放入独立线程运行，防止阻塞 asyncio 事件循环
            resp = await asyncio.to_thread(requests.post, url, headers=headers, json=payload, timeout=10)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"REST 发送 Embed 失败: {e}")

async def send_emergency_alert(cfg: dict, msg: str):
    """发送红色紧急预警至 Discord Log 频道"""
    try:
        channel_id = resolve_channel(cfg, "log")
        if channel_id:
            embed = create_embed(title="🚨 致命故障：AI 引擎全军覆没", description=msg, color_val=-1.0)
            await send_embeds(channel_id, [embed])
    except Exception as e:
        logger.error(f"发送紧急警告至 Discord 失败: {e}")

def _split(text: str):
    if len(text) <= MAX_LENGTH: return [text]
    chunks = []
    while text:
        if len(text) <= MAX_LENGTH:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, MAX_LENGTH)
        if split_at == -1: split_at = MAX_LENGTH
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    return chunks

def save_markdown(content_dir: str, filename: str, front_matter: str, body: str) -> str:
    os.makedirs(content_dir, exist_ok=True)
    filepath = os.path.join(content_dir, filename)
    full_content = f"""---
{front_matter}
---

{body}
"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full_content)
    logger.info("Markdown 已保存：%s", filepath)
    return filepath

async def send_error(channel_id: int, task: str, step: str, reason: str):
    reason_short = str(reason)[:200]
    text = f"⚠️ **{task}失败** | 步骤：{step}\n```{reason_short}```"
    try: await send_to_channel(channel_id, text)
    except Exception as e: logger.error("send_error 失败：%s", e)

async def publish_report(channel_id: int, task_name: str, embeds: list, 
                         filename: str, front_matter: str, body: str, cfg: dict) -> bool:
    """
    统一的报告发布流水线：发送 Embed -> 保存 Markdown（由 systemd watcher 自动接管 Hugo 构建）。
    """
    try:
        if embeds:
            await send_embeds(channel_id, embeds)
            logger.info("%s Embed 已发送", task_name)
    except Exception as e:
        logger.error("Discord 发送失败：%s", e)
        await send_error(channel_id, task_name, "Discord 发送", str(e))
        return False

    try:
        save_markdown(str(settings.REPORTS_DIR), filename, front_matter, body)
    except Exception as e:
        logger.error("Markdown 保存失败：%s", e)
        await send_error(channel_id, task_name, "Markdown 保存", str(e))
        return False

    return True
