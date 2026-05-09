# agent/news_engine.py
# v8.8 - 统一使用显式相对导入
import hashlib
import logging
import os
import re
import time
import json
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from random import uniform
from typing import Optional, List
from urllib.parse import urlparse, urlunparse

import requests
from . import llm_gateway
from .config import settings
import time

logger = logging.getLogger(__name__)

# [新增] 动态 UA 池，模拟现代浏览器特征，降低被 WAF 拦截风险
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/124.0.0.0"
]

def _get_headers(is_sec: bool = False) -> dict:
    """生成伪装浏览器请求头"""
    ua = random.choice(USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0"
    }
    if is_sec:
        # SEC.gov 强制要求包含特定格式的联系信息
        headers["User-Agent"] = f"StockSentinel (Contact: bot@personal.use) {ua}"
    return headers

def _clean_url(url: str) -> str:
    """[修复] 移除 URL 中的所有追踪参数，确保去重 GUID 在链接变动时保持稳定"""
    try:
        parsed = urlparse(url)
        # 剥离 query (查询参数) 和 fragment (锚点)，只保留核心路径
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
    except Exception:
        return url

def _extract_tickers(text: str, cfg: dict) -> list:
    """[修复] 结合自选股名单进行精准匹配，彻底解决 THE/CEO/NEW 等单词误识别问题"""
    # 获取自选股列表并去重
    watch_list = list(set([item["ticker"].upper() for item in cfg["market"]["watch_list"]]))
    if not watch_list:
        return []
    
    text_upper = text.upper()
    found = []
    for ticker in watch_list:
        # 使用单词边界 \b 匹配，确保不会匹配到单词中间的字母
        if re.search(r'\b' + re.escape(ticker) + r'\b', text_upper):
            found.append(ticker)
    return found

def ai_score_items(items: list, cfg: dict) -> list:
    """调用 AI 对新闻条目进行批量评估打分"""
    if not items:
        return items

    db_path = str(settings.DB_PATH)
    watch_list_items = cfg["market"]["watch_list"]
    ticker_str = ", ".join([i["ticker"] for i in watch_list_items]) or "美股科技股"
    raw_prompt = cfg["news"]["filter_prompt"]["system"]
    sys_prompt = raw_prompt.replace("{watch_list}", ticker_str)

    final_scored = []
    needs_ai = []
    
    for item in items:
        # 过滤 Google News 元数据链接
        if "googleusercontent.com/immersive_entry_chip" in item.get("url", ""):
            continue
            
        needs_ai.append(item)

    if not needs_ai:
        return final_scored

    batch_size = 10
    total_batches = (len(needs_ai) + batch_size - 1) // batch_size
    
    for i in range(0, len(needs_ai), batch_size):
        current_batch_num = (i // batch_size) + 1
        logger.info(f"进度: 正在处理第 {current_batch_num}/{total_batches} 批 AI 评分...")
        batch = needs_ai[i : i + batch_size]
        news_payload = [{"guid": item["guid"], "title": item["title"], "summary": (item.get("summary") or "")[:200]} for item in batch]
    
        try:
            answer, audit_log, used_engine = llm_gateway.query(sys_prompt, json.dumps(news_payload, ensure_ascii=False), db_path, task_role="scoring")
            
            # 提取 JSON 数组部分
            match = re.search(r'\[.*\]', answer, re.DOTALL)
            if not match:
                logger.warning("AI 返回的数据无法解析为 JSON 数组，本批次将留待下次重试。")
                continue
            
            clean_json = match.group(0).strip()
            for m in ["```json", "```"]: 
                clean_json = clean_json.replace(m, "")
            
            scored_data = json.loads(clean_json.strip())
            
            # --- 核心修复：提取 AI 生成的中文标题 ---
            score_map = {str(d["guid"]): d.get("score", 0) for d in scored_data if "guid" in d}
            reason_map = {str(d["guid"]): d.get("reason", "") for d in scored_data if "guid" in d}
            
            for item in batch:
                item["ai_score"] = score_map.get(str(item["guid"]), 0)
                item["reason"] = reason_map.get(str(item["guid"]), "")
                item["title_zh"] = ""  # 修复之前被硬编码为字面量的问题，实际标题翻译交给 scanner 环节
                item["ai_engine"] = used_engine
                final_scored.append(item)
        except llm_gateway.AllEnginesFailedError as e:
            # 致命异常，直接向上抛出给业务层处理
            raise e
        except Exception as e:
            logger.error(f"AI 评估批次失败: {e}，本批次将留待下次重试。")
                
        # 强行限制请求速率，防止API被BAN
        time.sleep(5)
        
    return final_scored


# agent/news_engine.py

def fetch_realtime(cfg: dict) -> list:
    """合并抓取所有实时数据源"""
    sources = cfg["news"]["sources"]
    delay = (cfg["news"]["request_delay_min"],
             cfg["news"]["request_delay_max"])
    
    items = []
    for source in sources:
        if not source.get("enabled", False): 
            continue
        try:
            raw = _fetch_source(source, cfg, delay)
            
            # --- 核心修改：透传配置开关 ---
            scrape_cfg = source.get("scrape_full_text", True)
            for item in raw:
                item["scrape_full_text"] = scrape_cfg
            # --------------------------
                
            items.extend(raw)
            logger.info(f"新闻源 [{source['name']}] 成功抓取 {len(raw)} 条")
        except Exception as e:
            logger.warning(f"新闻源 [{source['name']}] 抓取失败: {e}")
            
    return items
def _fetch_source(source: dict, cfg: dict, delay: tuple) -> list:
    """根据类型路由到不同的抓取器"""
    src_type = source.get("type", "rss")
    if src_type == "rss": 
        return _fetch_rss(source, cfg, delay)
    if src_type == "finnhub": 
        return _fetch_finnhub_news(source, cfg)
    return []

def _fetch_rss(source: dict, cfg: dict, delay: tuple) -> list:
    """[增强版] 抓取并解析 RSS 信源，支持动态 UA 和自动脱敏"""
    # 模拟真实用户行为，随机延迟
    time.sleep(uniform(*delay))
    
    url = source.get("url", "")
    is_sec = "sec.gov" in url.lower()
    
    # 使用增强的 Headers 和动态 UA
    resp = requests.get(url, headers=_get_headers(is_sec), timeout=15)
    resp.raise_for_status()
    
    root = ET.fromstring(resp.content)
    items = []
    
    # 识别 Atom 协议或标准 RSS 协议
    is_atom = root.tag.startswith("{http://www.w3.org/2005/Atom}")
    entries = (root.findall("{http://www.w3.org/2005/Atom}entry") if is_atom else root.findall(".//item"))
    
    for entry in entries:
        item = _parse_entry(entry, source["name"], is_atom, cfg)
        if item: 
            items.append(item)
    return items

def _fetch_finnhub_news(source: dict, cfg: dict) -> list:
    """[重构] 升级为全市场消息抓取模式，通过 Finnhub General 接口获取全局视野"""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key: 
        logger.error("未配置 FINNHUB_API_KEY，跳过抓取")
        return []
        
    import finnhub
    client = finnhub.Client(api_key=api_key)
    
    try:
        # --- 核心修复：方法名修正为 general_news ---
        news = client.general_news('general', min_id=0)
        items = []
        
        for n in news[:40]:  # 保持较大的抓取窗口
            raw_url = n.get("url", "")
            if not raw_url: 
                continue
                
            # [核心修复] 剥离 URL 追踪参数并生成稳定 GUID
            clean_url = _clean_url(raw_url)
            guid = hashlib.md5(clean_url.encode()).hexdigest()
            
            # 合并 Finnhub 原生标记和全文精准提取的 Ticker
            related = n.get("related", "").upper()
            found_tickers = [related] if (related and len(related) <= 5) else []
            found_tickers.extend(_extract_tickers(f"{n.get('headline')} {n.get('summary')}", cfg))
            
            items.append({
                "title": n.get("headline", "").strip(), 
                "summary": n.get("summary", "").strip()[:300], 
                "url": raw_url.strip(), 
                "source": f"Finnhub/{n.get('source', 'Market')}", 
                "published": _ts_to_et(n.get("datetime", 0)), 
                "tickers": list(set(found_tickers)), 
                "guid": guid
            })
        return items
    except Exception as e:
        logger.warning(f"Finnhub 全市场抓取异常: {e}")
        return []

def _parse_entry(entry, source_name: str, is_atom: bool, cfg: dict) -> Optional[dict]:
    """[增强版] 解析 RSS 条目，内置 URL 清洗与 Ticker 精准提取"""
    try:
        if is_atom:
            title = _text(entry, "{http://www.w3.org/2005/Atom}title")
            url_node = entry.find("{http://www.w3.org/2005/Atom}link")
            url = url_node.get("href", "") if url_node is not None else ""
            summary = _text(entry, "{http://www.w3.org/2005/Atom}summary")
            pub = _text(entry, "{http://www.w3.org/2005/Atom}updated")
        else:
            title = _text(entry, "title")
            url = _text(entry, "link")
            summary = _text(entry, "description")
            pub = _text(entry, "pubDate")
        
        if not title or not url: 
            return None

        # [核心修复] 彻底剥离 Yahoo 等信源的追踪参数，确保去重 GUID 的绝对稳定性
        clean_url = _clean_url(url.strip())
        guid = hashlib.md5(clean_url.encode()).hexdigest()

        return {
            "title": title.strip(), 
            "summary": (summary or "").strip()[:300], 
            "url": url.strip(), 
            "source": source_name, 
            "published": pub or "", 
            # 使用精准匹配逻辑提取 Ticker
            "tickers": _extract_tickers(f"{title} {summary}", cfg), 
            "guid": guid
        }
    except Exception: 
        return None

def _text(el, tag):
    n = el.find(tag)
    return (n.text or "").strip() if n is not None else ""

def _ts_to_et(ts):
    """时间戳转美东时间字符串"""
    try:
        import pytz
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    except Exception: 
        return str(ts)

def fetch_calendar(cfg: dict) -> dict:
    """抓取宏观经济日历与自选股财报日历"""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key: 
        return {"earnings": [], "economics": []}
    try:
        import finnhub
        client = finnhub.Client(api_key=api_key)
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 只关注自选股的财报
        watch_list = [item["ticker"].upper() for item in cfg["market"]["watch_list"]]
        tickers = set(watch_list)
        
        earnings_raw = client.earnings_calendar(_from=today, to=today, symbol="", international=False)
        earnings = [
            {
                "symbol": e.get("symbol", ""), 
                "date": e.get("date", ""), 
                "estimate": e.get("epsEstimate"), 
                "time": e.get("hour", "")
            }
            for e in earnings_raw.get("earningsCalendar", []) if e.get("symbol") in tickers
        ]
        
        # 获取美股高影响力经济事件
        econ_raw = client.economic_calendar()
        economics = [
            {
                "event": e.get("event", ""), 
                "date": e.get("time", "")[:10], 
                "country": e.get("country", ""), 
                "impact": e.get("impact", "")
            }
            for e in econ_raw.get("economicCalendar", []) 
            if e.get("country") == "US" and e.get("impact") in ("high", "medium")
        ]
        return {"earnings": earnings, "economics": economics}
    except Exception as e:
        logger.warning(f"日历抓取失败: {e}")
        return {"earnings": [], "economics": []}


def fetch_company_news(ticker: str, cfg: dict = None) -> list:
    """[Event-Driven Agent] 抓取特定公司的最新新闻用于异动分析"""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key: 
        logger.error("未配置 FINNHUB_API_KEY，无法抓取公司新闻")
        return []
    try:
        from . import config
        cfg = config.load()
        fetch_days = cfg.get("data_sync", {}).get("company_news_fetch_days", 3)
        
        import finnhub
        from datetime import datetime, timedelta
        client = finnhub.Client(api_key=api_key)
        
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=fetch_days)).strftime("%Y-%m-%d")
        
        news_raw = client.company_news(ticker, _from=start_date, to=end_date)
        items = []
        for n in news_raw[:10]:  # 获取最近 10 条
            items.append({
                "headline": n.get("headline", ""),
                "summary": n.get("summary", ""),
                "url": n.get("url", ""),
                "datetime": n.get("datetime", 0)
            })
        return items
    except Exception as e:
        logger.warning(f"定向抓取 {ticker} 新闻失败: {e}")
        return []
