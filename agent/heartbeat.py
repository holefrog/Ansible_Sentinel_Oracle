# v2.1 - 修复绝对路径导入
"""
heartbeat.py — 死人开关，每日固定时间向预警频道发送心跳消息。
由 cron 每天 19:00 ET 触发，跑完自动退出。

无论当天早晚报是否正常，心跳都必须发出。
"""

import argparse
import asyncio
import logging
import os
import sys
import re
from datetime import datetime, timedelta

from . import config
from . import discord_utils

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("heartbeat")


def _read_log_summary() -> dict:
    """
    智能读取日志摘要：
    1. 提取最后一次运行的日期戳。
    2. 如果最后运行日期太久远，报警“停摆”。
    3. 检查最后运行日期的日志中是否包含 [ERROR]。
    """
    summary = {
        "morning": "❓ 未知",
        "evening": "❓ 未知",
        "scan":    "❓ 未知",
    }

    # 使用 settings 单例获取日志目录
    log_dir = config.settings.LOG_DIR

    checks = {
        "morning": log_dir / "morning.log",
        "evening": log_dir / "evening.log",
        "scan":    log_dir / "scan.log",
    }

    # 用于匹配日志开头的日期如 2026-04-21
    date_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2})")
    today_date = datetime.now().date()
    # 允许昨天的日志（兼容时区跨日情况）
    yesterday_date = today_date - timedelta(days=1)

    for key, log_path in checks.items():
        if not os.path.exists(log_path):
            summary[key] = "❓ 日志不存在"
            continue
        try:
            # 读最后 10KB
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 10240))
                tail = f.read().decode("utf-8", errors="ignore")

            lines = [l.strip() for l in tail.splitlines() if l.strip()]
            if not lines:
                summary[key] = "⚠️ 日志为空"
                continue

            # 寻找文件末尾最后一次运行的时间戳
            last_date_str = None
            for line in reversed(lines):
                match = date_pattern.match(line)
                if match:
                    last_date_str = match.group(1)
                    break

            if not last_date_str:
                summary[key] = "⚠️ 无法解析时间戳"
                continue

            # 检查是否已停摆
            try:
                log_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                if log_date < yesterday_date:
                    summary[key] = f"🛑 已停摆 (最后运行 {last_date_str})"
                    continue
            except ValueError:
                pass

            # 提取最后一天产生的所有日志进行错误检测
            latest_lines = [l for l in lines if l.startswith(last_date_str)]
            errors = [l for l in latest_lines if "[ERROR]" in l]

            if errors:
                summary[key] = f"❌ 有错误（{len(errors)} 条）"
            else:
                summary[key] = "✅ 正常"

        except Exception as e:
            summary[key] = f"❓ 读取失败（{e}）"

    return summary


async def main(channel_id: int):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    # 读取今日运行摘要 (不再需要 app_dir)
    summary = _read_log_summary()

    lines = [
        f"🫀 **哨兵心跳** | {now_str}",
        "",
        "**今日运行状态**",
        f"早报：{summary['morning']}",
        f"晚报：{summary['evening']}",
        f"扫描：{summary['scan']}",
        "",
        "*若超过 24 小时未收到心跳，请检查系统。*",
    ]

    msg = "\n".join(lines)

    try:
        await discord_utils.send_to_channel(channel_id, msg)
        logger.info("心跳已发送")
    except Exception as e:
        logger.error("心跳发送失败：%s", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.channel))
