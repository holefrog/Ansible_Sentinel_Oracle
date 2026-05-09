# agent/brief.py
import os
import requests
import logging
import time
import random
import re

try:
    from curl_cffi import requests as cffi_requests
    import trafilatura
    HAS_LOCAL_SCRAPER = True
except ImportError:
    HAS_LOCAL_SCRAPER = False

logger = logging.getLogger(__name__)

# 常见的高匿指纹，用于绕过 Cloudflare/Datadome
IMPERSONATES = ["chrome110", "chrome116", "chrome120", "safari15_5"]

def extract_yahoo_fallback(html: str) -> str:
    """如果 trafilatura 失败，针对 Yahoo 财经页面的专用回退提取逻辑"""
    match = re.search(r'<div class="caas-body">(.*?)</div>', html, re.DOTALL)
    if not match:
        return ""
    content = match.group(1)
    # 移除 HTML 标签
    text = re.sub(r'<[^>]+>', ' ', content)
    # 移除多余空白
    return re.sub(r'\s+', ' ', text).strip()

def fetch_content(url: str, timeout: int = 20) -> str:
    """
    抓取网页正文，采用三级降级策略绕过反爬：
    1. 优先：本地伪装 Chrome 指纹直连抓取 + Trafilatura 提取正文（绕过 Cloudflare/Datadome）
    2. 降级：Jina.ai 免费提取接口
    3. 兜底：Jina.ai API Key 提取接口（解决 429 限流问题）
    """
    if not url:
        return ""

    # -------------------------------------------------------------
    # 第一级：本地 curl_cffi 伪装指纹 + trafilatura 提取
    # -------------------------------------------------------------
    if HAS_LOCAL_SCRAPER:
        try:
            impersonate = random.choice(IMPERSONATES)
            logger.info(f"尝试本地高匿伪装抓取 ({impersonate}): {url}")
            # 使用随机指纹绕过多数 TLS 拦截
            resp = cffi_requests.get(url, impersonate=impersonate, timeout=timeout)
            resp.raise_for_status()
            
            # 使用 trafilatura 智能提取正文（去除广告/导航栏）
            # include_comments=False 避免抓到无关评论
            text = trafilatura.extract(resp.text, include_comments=False)
            
            # 如果 trafilatura 对 Yahoo 提取失败，尝试专用逻辑
            if (not text or len(text.strip()) <= 100) and "yahoo.com" in url:
                logger.info("Trafilatura 提取 Yahoo 失败，尝试专用解析规则...")
                text = extract_yahoo_fallback(resp.text)

            if text and len(text.strip()) > 100:
                logger.info("✅ 本地抓取并提取成功！")
                return text.strip()
            else:
                logger.warning("⚠️ 本地提取到的正文过短或为空，降级至 Jina")
        except Exception as e:
            logger.warning(f"⚠️ 本地抓取异常: {e}，降级至 Jina")
    else:
        logger.warning("未安装 curl_cffi 或 trafilatura，直接使用 Jina。建议安装以增强反爬能力。")

    # -------------------------------------------------------------
    # 第二级：Jina.ai 免费模式
    # -------------------------------------------------------------
    jina_url = f"https://r.jina.ai/{url}"
    headers = {
        "X-Return-Format": "markdown",
        "User-Agent": "Mozilla/5.0 StockSentinel/1.0"
    }
    
    api_key = os.environ.get("JINA_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        time.sleep(1) # 基础限速保护
        mode = "API Key 模式" if api_key else "免费模式"
        logger.info(f"正在通过 Jina 抓取 ({mode}): {url}")
        resp = requests.get(jina_url, headers=headers, timeout=timeout)

        # -------------------------------------------------------------
        # 第三级：Jina.ai 重试机制 (应对偶发服务端错误或限速)
        # -------------------------------------------------------------
        if resp.status_code in (429, 500, 502, 503):
            logger.warning(f"⚠️ Jina 接口异常 ({resp.status_code})，1秒后重试: {url}")
            time.sleep(1)
            resp = requests.get(jina_url, headers=headers, timeout=timeout)
            
        if resp.status_code == 451:
            logger.warning(f"❌ Jina 被目标网站 ({url}) 的 WAF 拦截 (451)。无法提取。")
            return ""

        resp.raise_for_status()
        return resp.text.strip()

    except requests.exceptions.RequestException as e:
        logger.warning(f"❌ 正文最终抓取失败: {url}, 错误: {e}")
        return ""
