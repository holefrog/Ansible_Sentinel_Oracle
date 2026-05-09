# agent/market_scan.py
# v5.3 - 统一使用显式相对导入

import argparse
import asyncio
import logging
import sys
from datetime import datetime

from . import config
from . import data_engine
from . import discord_utils
from . import advisor
from . import data_store
from . import llm_gateway

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("market_scan")


# ── 异动检测 ───────────────────────────────────────────────────────────────────

def detect_anomalies(data: dict, alert_pct: float, spike_x: float) -> list:
    """
    [Agent 升级版] 检测价格和成交量异动。
    返回结构化的异动列表，供下游 Advisor Agent 调用。
    """
    alerts = []
    for sym, d in data.items():
        if d is None:
            continue

        item_alerts = []
        is_anomalous = False
        
        # 1. 价格异动判断
        if abs(d["change_pct"]) >= alert_pct:
            direction = "📈" if d["change_pct"] > 0 else "📉"
            item_alerts.append(f"{direction} 价格 {d['change_pct']:+.2f}%")
            is_anomalous = True

        # 2. 成交量异动判断 (必须有均量数据且满足倍率)
        ratio = 0.0
        if (d.get("avg_volume") and d["avg_volume"] > 0):
            ratio = d["volume"] / d["avg_volume"]
            if ratio >= spike_x:
                item_alerts.append(f"📊 放量 {ratio:.1f}x")
                is_anomalous = True

        # 如果有任何一项触发，组合成结构化数据
        if is_anomalous:
            alert_str = " | ".join(item_alerts)
            alerts.append({
                "ticker": sym,
                "name": d["name"],
                "price": d["price"],
                "change_pct": d["change_pct"],
                "spike_ratio": ratio,
                "display_str": f"**{d['name']} ({sym})** {d['price']:.2f} ({alert_str})"
            })

    return alerts


# ── 主流程 ─────────────────────────────────────────────────────────────────────

async def main(channel_id: int, force: bool = False):
    cfg         = config.load()
    
    # 获取扫描配置参数
    alert_pct   = cfg["market"]["alert_threshold_pct"]
    spike_x     = cfg["market"]["volume_spike_threshold"]

    # 获取专属的投顾频道
    advisor_channel_id = cfg.get("discord", {}).get("advisor_channel_id")

    # 合并监控名单
    watch_list   = [(i["ticker"], i["name"]) for i in cfg["market"].get("watch_list", [])]
    indices_list = [(i["ticker"], i["name"]) for i in cfg["market"].get("indices", [])]
    scan_targets = watch_list + indices_list

    # 0. 市场状态检查
    if not force:
        mkt = data_engine.market_status()
        if not mkt["is_open"]:
            logger.info("市场未开盘，跳过扫描 (%s)", mkt.get("note", "休市"))
            return

    if not scan_targets:
        logger.info("监控标的为空，跳过扫描")
        return

    # 1. 抓取数据
    logger.info("开始市场异动扫描...")
    try:
        market_data = data_engine.fetch(scan_targets)
    except Exception as e:
        logger.error("扫描数据抓取失败：%s", e)
        return

    # 2. 检测异动
    anomalies = detect_anomalies(market_data, alert_pct, spike_x)

    if not anomalies:
        logger.info("扫描完成，无显著异动")
        return

    # 3. 常规异动通知 (发送到原本的频道)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    display_lines = [item["display_str"] for item in anomalies]
    lines   = [f"🔍 **实时市场异动监控** | {now_str}", ""] + display_lines
    msg     = "\n".join(lines)

    try:
        await discord_utils.send_to_channel(channel_id, msg)
        logger.info("基础异动通知已成功发送")
    except Exception as e:
        logger.error("Discord 发送失败：%s", e)

    # 4. 唤醒私人投顾 Agent (并行处理多只异动股票)
    if advisor_channel_id:
        db_path = str(config.settings.DB_PATH)
        data_store.init_db(db_path)
        
        watch_tickers = {i["ticker"].upper() for i in cfg["market"].get("watch_list", [])}
        advisor_anomalies = []
        for a in anomalies:
            ticker = a["ticker"].upper()
            if ticker in watch_tickers:
                if not data_store.check_advisor_cooldown(db_path, ticker):
                    advisor_anomalies.append(a)
                else:
                    logger.info(f"[{ticker}] 今天已触发过投顾报告，跳过冷却期间的异动。")

        if not advisor_anomalies:
            logger.info("异动标的均为指数/宏观，或均已在冷却期内，跳过 Advisor Agent。")
            return
        logger.info(f"正在唤醒 Advisor Agent 处理 {len(advisor_anomalies)} 只异动股票...")
        
        # 将分析包装为协程任务
        async def analyze_and_send(anomaly):
            # 将同步的 LLM 调用包装在执行器中运行，避免阻塞主事件循环
            loop = asyncio.get_running_loop()
            analysis_report = await loop.run_in_executor(
                None, 
                advisor.analyze_ticker, 
                anomaly["ticker"], 
                anomaly["price"], 
                anomaly["change_pct"], 
                anomaly["spike_ratio"]
            )
            
            # 分割超长消息并发送到投顾专属频道
            try:
                for chunk in discord_utils._split(analysis_report):
                    await discord_utils.send_to_channel(int(advisor_channel_id), chunk)
                # 发送成功后更新冷却时间
                data_store.update_advisor_cooldown(db_path, anomaly["ticker"].upper())
            except llm_gateway.AllEnginesFailedError as e:
                logger.error(f"Advisor LLM 彻底崩溃 [{anomaly['ticker']}]: {e}")
                await discord_utils.send_emergency_alert(cfg, f"**Advisor Agent 罢工** ({anomaly['ticker']})\n```{e}```")
            except Exception as e:
                logger.error(f"投顾报告发送失败 [{anomaly['ticker']}]: {e}")

        # 并发执行所有的分析任务
        await asyncio.gather(*(analyze_and_send(a) for a in advisor_anomalies))
        logger.info("所有投顾 Agent 任务已执行完毕。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", type=int, required=True)
    parser.add_argument("--force", action="store_true", help="Bypass market open check")
    args = parser.parse_args()
    asyncio.run(main(args.channel, args.force))
