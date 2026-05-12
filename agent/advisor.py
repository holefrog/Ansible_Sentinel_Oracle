# agent/advisor.py
# v2 - 升级数据架构：本地缓存 + 新闻两层查询

import json
import logging
from . import config
from . import data_engine
from . import data_store
from . import llm_gateway
from .config import settings

logger = logging.getLogger("advisor")


def _calculate_pnl(current_price: float, avg_cost: float, shares: int) -> str:
    """计算未结盈亏（不变）"""
    if shares == 0 or avg_cost == 0:
        return "无持仓"

    total_cost = avg_cost * shares
    current_value = current_price * shares
    pnl_dollar = current_value - total_cost
    pnl_pct = (current_price - avg_cost) / avg_cost * 100

    sign = "+" if pnl_dollar > 0 else ""
    return f"{sign}{pnl_pct:.2f}% ({sign}${pnl_dollar:.2f})"


def analyze_ticker(ticker: str, current_price: float, change_pct: float, spike_ratio: float = 0.0) -> str:
    """
    Agent 主分析流：
    1. 获取用户持仓
    2. 从缓存获取量化指标（不再实时调用 Finnhub）
    3. 从缓存获取华尔街预期（不再实时调用 Finnhub）
    4. 从 company_news 表读取公司层新闻
    5. 从 news_registry 表读取宏观层新闻
    6. 调用 LLM 生成报告
    """
    logger.info("🧠 Advisor Agent 已唤醒，正在调查 %s 异动...", ticker)
    cfg = config.load()
    db_path = str(settings.DB_PATH)

    # 初始化 data_store 表（幂等）
    data_store.init_db(db_path)

    # 1. 获取持仓配置
    watch_list = cfg.get("market", {}).get("watch_list", [])
    user_position = next((item for item in watch_list if item["ticker"].upper() == ticker.upper()), None)

    if not user_position:
        shares, avg_cost, strategy, name = 0, 0.0, "无", ticker
    else:
        shares   = user_position.get("shares", 0)
        avg_cost = user_position.get("avg_cost", 0.0)
        strategy = user_position.get("strategy", "未设定")
        name     = user_position.get("name", ticker)

    # 2. 量化指标（优先读缓存，未命中才计算）
    tech_data = data_engine.get_technical_indicators(ticker, db_path)
    actual_price = tech_data.get("current_price", current_price)

    # 3. 华尔街预期（优先读缓存，未命中才调 Finnhub）
    estimates = data_engine.get_analyst_estimates(ticker, db_path)

    # 4. 公司层新闻（读 company_news 表，零 Finnhub 请求）
    company_news = data_store.get_company_news(
        db_path,
        ticker,
        days=cfg.get("data_sync", {}).get("company_news_fetch_days", 3)
    )

    # 5. 宏观层新闻（读 news_registry 表，取高分宏观新闻）
    min_score = cfg.get("report", {}).get("min_score_threshold", 7)
    macro_news = data_store.get_macro_news(db_path, min_score=min_score, hours=24)

    # 6. 组装 Prompt
    pnl_str = _calculate_pnl(actual_price, avg_cost, shares)

    has_position = shares > 0 and avg_cost > 0
    position_block = f"""
    【您的持仓数据】
    持仓数量: {shares} 股
    平均成本: ${avg_cost:.2f}
    当前盈亏: {pnl_str}
    交易策略: {strategy}
    """ if has_position else "【您的持仓数据】\n    无持仓（新建仓位分析）"

    partial_note = "\n    ⚠️ 注意：本地日线数据不足200条，SMA200 暂不可用。" if tech_data.get("partial") else ""

    trend = "上涨" if change_pct > 0 else "下跌" if change_pct < 0 else "平盘"
    user_prompt = f"""
    【异动信号】
    股票: {name} ({ticker})
    当前价格: ${actual_price:.2f}
    异动情况: 日内变动 {change_pct:+.2f}% (当前趋势: {trend})，成交量放大倍数: {spike_ratio:.1f}x

    {position_block}

    【量化指标】{partial_note}
    SMA50  (50日均线):  {tech_data.get('sma50', 'N/A')}
    SMA200 (200日均线): {tech_data.get('sma200', 'N/A')}
    RSI(14):            {tech_data.get('rsi_14', 'N/A')}

    【华尔街一致预期】
    平均目标价:   {estimates.get('targetMean', 'N/A')}
    最高目标价:   {estimates.get('targetHigh', 'N/A')}
    最低目标价:   {estimates.get('targetLow', 'N/A')}
    覆盖分析师数: {estimates.get('numberAnalysts', 'N/A')}

    【近期公司新闻（公司层，最多10条）】
    """

    if not company_news:
        user_prompt += "    未发现近期公司定向新闻。\n"
    else:
        for idx, news in enumerate(company_news):
            user_prompt += f"    [{idx+1}] {news['headline']}\n"
            if news.get('summary'):
                user_prompt += f"    摘要: {news['summary']}\n\n"

    user_prompt += "\n    【宏观与市场背景（宏观层，最多5条高分新闻）】\n"

    if not macro_news:
        user_prompt += "    近24小时内无高分宏观新闻。\n"
    else:
        for idx, news in enumerate(macro_news):
            title = news.get('title_zh') or news.get('title', '')
            user_prompt += f"    [{idx+1}] [{news.get('score', 0)}分] {title}\n"

    # 7. 请求 LLM 分析
    advisor_prompt = cfg.get("report", {}).get("advisor_prompt", {}).get("system", "")
    
    try:
        answer, audit_log, used_engine = llm_gateway.query(
            advisor_prompt,
            user_prompt,
            db_path,
            task_role="anomaly"
        )
        return answer
    except llm_gateway.AllEnginesFailedError as e:
        logger.error("Agent 分析过程发生致命异常: %s", e)
        from . import discord_utils
        import asyncio
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                loop.create_task(discord_utils.send_emergency_alert(cfg, f"**投顾 Agent 致命异常**\n```{e}```"))
            else:
                asyncio.run(discord_utils.send_emergency_alert(cfg, f"**投顾 Agent 致命异常**\n```{e}```"))
        except Exception as async_err:
            logger.error("发送投顾警报失败: %s", async_err)
        return f"🚨 **私人投顾 Agent 运行异常**\n在分析 {ticker} 时遇到了问题: 所有 AI 引擎均已不可用。"
    except Exception as e:
        logger.error("Agent 分析过程发生异常: %s", e)
        return f"🚨 **私人投顾 Agent 运行异常**\n在分析 {ticker} 时遇到了问题: `{e}`"
