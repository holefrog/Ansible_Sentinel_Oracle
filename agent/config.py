# agent/config.py
# v9.2 - 统一配置加载

import os
import logging
import copy
import threading
from pathlib import Path
from functools import lru_cache

try:
    import tomllib
except ImportError:
    import tomli as tomllib
import tomli_w

logger = logging.getLogger("config")

# 文件读写锁
_watch_list_lock = threading.Lock()

# ══════════════════════════════════════════════════════════════
# [1] 路径自举单例 AppConfig
# ══════════════════════════════════════════════════════════════

class AppConfig:
    def __init__(self):
        self._validate()
        
        self.APP_DIR     = Path(os.getenv("SENTINEL_ROOT"))
        self.HUGO_ROOT   = self.APP_DIR / "hugo"
        self.DB_PATH     = self.APP_DIR / "data" / "sentinel.db"
        self.LOG_DIR     = self.APP_DIR / "logs"
        self.CONTENT_DIR = self.HUGO_ROOT / "content"
        self.REPORTS_DIR = self.CONTENT_DIR / "reports"
        self.NEWS_DIR    = self.CONTENT_DIR / "news"
        self.CONFIG_DIR  = self.APP_DIR / "config"

        env_web_base  = os.getenv("SENTINEL_WEB_BASE")
        env_domain    = os.getenv("SENTINEL_DOMAIN")
        self.WEB_ROOT = Path(env_web_base) / env_domain
        self.HUGO_EXE = os.getenv("HUGO_PATH")

        self._bootstrap()

    def _validate(self):
        required = [
            "SENTINEL_ROOT", "SENTINEL_WEB_BASE", "SENTINEL_DOMAIN",
            "DISCORD_BOT_TOKEN", "FINNHUB_API_KEY", "HUGO_PATH"
        ]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise RuntimeError(f"启动失败，缺少必要环境变量: {missing}")

    def _bootstrap(self):
        for d in [self.DB_PATH.parent, self.LOG_DIR, self.REPORTS_DIR, self.NEWS_DIR]:
            d.mkdir(parents=True, exist_ok=True)


settings = AppConfig()


# ══════════════════════════════════════════════════════════════
# [2] 频道短键映射
# ══════════════════════════════════════════════════════════════

_CHANNEL_MAP = {
    "report": "report_channel_id",
    "scan":   "scan_channel_id",
    "alert":  "alert_channel_id",
    "notice": "notice_channel_id",
    "log":    "log_channel_id",
}

def resolve_channel(cfg: dict, key: str) -> int:
    full_key = _CHANNEL_MAP.get(key, key)
    discord_cfg = cfg.get("discord", {})
    val = discord_cfg.get(full_key)
    if not val:
        raise KeyError(f"discord.{full_key} 未在 settings.toml 中配置")
    return int(val)


# ══════════════════════════════════════════════════════════════
# [3] 配置加载
# ══════════════════════════════════════════════════════════════

def _load_toml(filename: str) -> dict:
    path = settings.CONFIG_DIR / filename
    if not path.exists():
        logger.warning("配置文件不存在，返回空字典: %s", path)
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


@lru_cache(maxsize=1)
def _cached_load() -> dict:
    """
    内部只读缓存层.
    v9.2: 统一从 settings.toml 加载，watch_list.toml 动态合并.
    """
    cfg = _load_toml("settings.toml")
    watch = _load_toml("watch_list.toml")

    # 合并 watch_list 到 market.watch_list 和 market.indices
    if "market" not in cfg:
        cfg["market"] = {}
    cfg["market"]["watch_list"] = watch.get("watch_list", [])
    cfg["market"]["indices"]    = watch.get("indices", [])

    # 注入 AI Prompt 默认值兜底
    if "discord" not in cfg:
        cfg["discord"] = {}
    if "bot_prompt" not in cfg["discord"]:
        cfg["discord"]["bot_prompt"] = {}
    if "system" not in cfg["discord"]["bot_prompt"] or not cfg["discord"]["bot_prompt"]["system"]:
        cfg["discord"]["bot_prompt"]["system"] = "你是一个专业的金融与技术助手，请根据用户的指令提供准确的回答。"

    if "news" not in cfg:
        cfg["news"] = {}
        
    if "filter_prompt" not in cfg["news"]:
        cfg["news"]["filter_prompt"] = {}
    if "system" not in cfg["news"]["filter_prompt"] or not cfg["news"]["filter_prompt"]["system"]:
        cfg["news"]["filter_prompt"]["system"] = (
            "你是一个华尔街极客量化分析师。你的任务是评估新闻对我们的核心关注标的（{watch_list}）以及美国宏观经济、地缘政治的实质性影响。\n"
            "请对提供的每条新闻进行 0-10 分打分。\n"
            "- 8-10分：直接影响关注标的的财报/基本面、重大宏观数据（加息/非农/CPI）、突发重大地缘政治或黑天鹅事件（大选/战争/灾难）。\n"
            "- 5-7分：相关行业的整体趋势、竞品动态、普通的产品或技术发布。\n"
            "- 0-4分：公关稿、人事八卦、蹭热度的无效资讯、无关的股市花边。\n\n"
            "严格输出合法 JSON 数组，不含任何说明文字和 markdown 代码块标记：\n"
            '[{"guid": "传入的guid", "score": 8, "reason": "打分理由简述"}]'
        )
        
    if "title_translation_prompt" not in cfg["news"]:
        cfg["news"]["title_translation_prompt"] = {}
    if "system" not in cfg["news"]["title_translation_prompt"] or not cfg["news"]["title_translation_prompt"]["system"]:
        cfg["news"]["title_translation_prompt"]["system"] = "你是一个专业的华尔街财经翻译助手。请将以下英文新闻标题极其精准、流畅地翻译为中文。绝对不要输出拼音，不要加引号，只返回最终翻译结果本身。"

    if "full_translation_prompt" not in cfg["news"]:
        cfg["news"]["full_translation_prompt"] = {}
    if "system" not in cfg["news"]["full_translation_prompt"] or not cfg["news"]["full_translation_prompt"]["system"]:
        cfg["news"]["full_translation_prompt"]["system"] = (
            "你是一个极其严谨的金融分析师。请将以下输入的美股新闻文本翻译成中文并提取核心要点。\n"
            "【绝对红线】：\n"
            "1. 严禁使用任何外部知识进行扩写、脑补或推理。你的输出必须 100% 来源于我提供的文本。\n"
            "2. 如果输入文本极短（例如只有一两句话的标题或摘要），请直接提供精准翻译即可，**绝对不要**强行拆分多条要点、不要生成“深度解析”等废话。\n"
            "3. 保持金融术语准确，条理清晰。"
        )

    if "advisor" not in cfg:
        cfg["advisor"] = {}
    if "system_prompt" not in cfg["advisor"] or not cfg["advisor"]["system_prompt"]:
        cfg["advisor"]["system_prompt"] = (
            "你是一个顶级的华尔街对冲基金经理和量化分析师，专注于美股交易。\n"
            "现在有一只股票发生了市场异动，我将为你提供该股票的实时量化指标、华尔街一致预期、近期新闻催化剂，以及用户的真实持仓成本和策略。\n\n"
            "请你综合这些信息，给出一份定量、客观、冷血的交易建议报告。\n\n"
            "你的输出必须遵循以下 Markdown 格式结构：\n"
            "🚨 **私人投顾 Agent | 深度交易建议**\n"
            "**[股票名称] ([股票代码])** 当前价格: **$[当前价格]**\n\n"
            "📰 **催化剂分析 (定性):** \n"
            "[一句话总结引发异动的核心新闻及其性质（利好/利空/情绪错杀），不要大段复述新闻。如果没新闻，就说\"未发现明显消息面催化剂\"。]\n\n"
            "📊 **量化与技术面 (定量):**\n"
            "* **趋势:** [结合提供的 SMA50 和 SMA200 判断，例如\"跌破50日均线支撑\"、\"在200日均线处企稳\"等。]\n"
            "* **RSI (14):** [结合提供的 RSI 值判断，例如\"30(极度超卖)\"、\"70(面临回调风险)\"等。]\n"
            "* **华尔街共识:** [列出提供的平均目标价和最高/低目标价，对比当前价格，指出潜在上涨/下跌空间。]\n\n"
            "💼 **您的持仓诊断:**\n"
            "* **当前持仓:** [填入提供的数据] 股\n"
            "* **持仓成本:** $[填入提供的数据]\n"
            "* **未结盈亏:** [计算并填入：+百分比 (具体美元金额)]\n"
            "* **策略定位:** [填入提供的数据，如\"长线\"]\n"
            "* **[注意：如果输入数据中显示无持仓，这里请直接说明“当前无持仓记录”，不需要硬套盈亏模板]**\n\n"
            "🎯 **操作建议 (Action Plan):**\n"
            "【[给出明确的行动标签，如：逢低建仓 / 止盈减仓 / 严格止损 / 持有观望]】\n"
            "1. **机会:** [一句话分析潜在上行机会]\n"
            "2. **风险:** [一句话分析潜在下行风险]\n"
            "3. **具体操作:** [给出带具体价格数字的操作建议。如果是减仓，说出减仓比例；如果是加仓，说出入场区间；如果是止损，必须给出一个硬性的数字跌破止损位。]\n\n"
            "严禁废话，严禁模糊的\"可能上涨也可能下跌\"的废话，像一个真正的基金经理一样做出决断。"
        )

    # 注入全局非 Prompt 默认值
    cfg["market"].setdefault("alert_threshold_pct", 2.5)
    cfg["market"].setdefault("volume_spike_threshold", 1.5)
    
    cfg["news"].setdefault("summary_min_length", 40)
    cfg["news"].setdefault("relevance_threshold", 6)
    cfg["news"].setdefault("retention_days", 30)
    cfg["news"].setdefault("request_delay_min", 1)
    cfg["news"].setdefault("request_delay_max", 3)
    cfg["news"].setdefault("max_summary_input_len", 3500)
    cfg["news"].setdefault("preferred_engine", "auto")
    cfg["news"].setdefault("sources", [])

    if "report" not in cfg:
        cfg["report"] = {}
    cfg["report"].setdefault("min_score_threshold", 7)
    if "summary_prompt" not in cfg["report"]:
        cfg["report"]["summary_prompt"] = {}
    cfg["report"]["summary_prompt"].setdefault("system", "")

    if "advisor_prompt" not in cfg["report"]:
        cfg["report"]["advisor_prompt"] = {}
    if "system" not in cfg["report"]["advisor_prompt"] or not cfg["report"]["advisor_prompt"]["system"]:
        cfg["report"]["advisor_prompt"]["system"] = (
            "你是一个顶级的华尔街对冲基金经理和量化分析师，专注于美股交易。\n"
            "现在有一只股票发生了市场异动，我将为你提供该股票的实时量化指标、华尔街一致预期、近期新闻催化剂，以及用户的真实持仓成本和策略。\n\n"
            "请你综合这些信息，给出一份定量、客观、冷血的交易建议报告。\n\n"
            "你的输出必须遵循以下 Markdown 格式结构：\n"
            "🚨 **私人投顾 Agent | 深度交易建议**\n"
            "**[股票名称] ([股票代码])** 当前价格: **$[当前价格]**\n\n"
            "📰 **催化剂分析 (定性):** \n"
            "[一句话总结引发异动的核心新闻及其性质（利好/利空/情绪错杀），不要大段复述新闻。如果没新闻，就说\"未发现明显消息面催化剂\"。]\n\n"
            "📊 **量化与技术面 (定量):**\n"
            "* **趋势:** [结合提供的 SMA50 和 SMA200 判断，例如\"跌破50日均线支撑\"、\"在200日均线处企稳\"等。]\n"
            "* **RSI (14):** [结合提供的 RSI 值判断，例如\"30(极度超卖)\"、\"70(面临回调风险)\"等。]\n"
            "* **华尔街共识:** [列出提供的平均目标价和最高/低目标价，对比当前价格，指出潜在上涨/下跌空间。]\n\n"
            "💼 **您的持仓诊断:**\n"
            "【如果有持仓数据，请按以下格式输出：】\n"
            "* **当前持仓:** [填入提供的数据] 股\n"
            "* **持仓成本:** $[填入提供的数据]\n"
            "* **未结盈亏:** [计算并填入：+百分比 (具体美元金额)]\n"
            "* **策略定位:** [填入提供的数据，如\"长线\"]\n"
            "【如果没有持仓，请输出：】\n"
            "* **当前持仓:** 无\n"
            "* **分析视角:** 作为潜在新建仓位进行评估\n\n"
            "🎯 **操作建议 (Action Plan):**\n"
            "【[给出明确的行动标签，如：逢低加仓 / 止盈减仓 / 严格止损 / 持有观望]】\n"
            "1. **机会:** [一句话分析潜在上行机会]\n"
            "2. **风险:** [一句话分析潜在下行风险]\n"
            "3. **具体操作:** [这是最重要的部分。给出带具体价格数字的操作建议。如果是减仓，说出减仓比例；如果是加仓，说出入场区间；**如果是止损，必须给出一个硬性的数字跌破止损位**。]\n\n"
            "严禁废话，严禁模糊的\"可能上涨也可能下跌\"的废话，像一个真正的基金经理一样做出决断。"
        )

    if "services" not in cfg:
        cfg["services"] = {}
    if "translator" not in cfg["services"]:
        cfg["services"]["translator"] = {}
    cfg["services"]["translator"].setdefault("url_free", "https://api-free.deepl.com/v2/translate")
    cfg["services"]["translator"].setdefault("url_pro", "https://api.deepl.com/v2/translate")

    cfg["discord"].setdefault("api_base_url", "https://discord.com/api/v10")
    cfg["discord"].setdefault("user_agent", "StockSentinel/1.0")

    return cfg


def load() -> dict:
    """
    公开调用接口, 返回配置的深拷贝.
    """
    return copy.deepcopy(_cached_load())


def invalidate_cache():
    """使内部缓存失效"""
    _cached_load.cache_clear()


# ══════════════════════════════════════════════════════════════
# [4] 业务辅助函数
# ══════════════════════════════════════════════════════════════

def get_targets(cfg: dict) -> tuple:
    """返回 (市场宏观列表, 自选股列表)，供 data_engine.fetch() 使用"""
    allowed_types = ("index", "future", "macro", "sentiment")
    
    market_cfg = cfg.get("market", {})
    
    market_context = [
        (i["ticker"], i["name"])
        for i in market_cfg.get("indices", [])
        if i.get("type") in allowed_types
    ]
    watch_list = [
        (i["ticker"], i["name"])
        for i in market_cfg.get("watch_list", [])
    ]
    
    if not market_context:
        market_context = [("SPY", "S&P 500 ETF"), ("QQQ", "Nasdaq 100 ETF")]
    if not watch_list:
        watch_list = [("AAPL", "Apple"), ("MSFT", "Microsoft")]
    return market_context, watch_list


def update_watch_list_file(action: str, ticker: str,
                           name: str = None, note: str = None,
                           shares: int = 0, avg_cost: float = 0.0, strategy: str = None) -> tuple:
    """动态增删自选股，写入 watch_list.toml 并清除配置缓存"""
    ticker = ticker.upper()
    path   = settings.CONFIG_DIR / "watch_list.toml"
    
    with _watch_list_lock:
        try:
            data = _load_toml("watch_list.toml") if path.exists() else {
                "watch_list": [], "indices": []
            }
            watch_list = data.get("watch_list", [])

            if action == "add":
                if any(item["ticker"] == ticker for item in watch_list):
                    return False, "该股票已在列表中"
                watch_list.append({
                    "ticker": ticker,
                    "name":   name or ticker,
                    "note":   note or "",
                    "shares": shares,
                    "avg_cost": avg_cost,
                    "strategy": strategy or "未设定"
                })
            elif action == "remove":
                watch_list = [i for i in watch_list if i["ticker"] != ticker]
            else:
                return False, f"未知操作: {action}"

            data["watch_list"] = watch_list
            with open(path, "wb") as f:
                tomli_w.dump(data, f)

            invalidate_cache()
            return True, "操作成功"
        except Exception as e:
            logger.error("更新 watch_list.toml 失败: %s", e)
            return False, f"文件操作失败: {e}"
