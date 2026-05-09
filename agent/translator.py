# agent/translator.py
# v2.1 - 修复绝对路径导入
import os
import logging
from . import llm_gateway

logger = logging.getLogger(__name__)

def translate_title(text: str, cfg: dict) -> str:
    """调用路由网关进行翻译 (根据 priority 自动分配 DeepL / LLM)。"""
    if not text:
        return ""
    
    from .config import settings
    db_path = str(settings.DB_PATH)
    sys_prompt = cfg["news"]["title_translation_prompt"]["system"]
    
    try:
        forced = cfg["news"].get("preferred_engine")
        if forced and forced.lower() == "auto": forced = None
        translated_text, _, _ = llm_gateway.query(sys_prompt, text[:500], db_path, task_role="translation", forced_engine=forced)
        return translated_text.strip().strip('"').strip("'")
    except Exception as e:
        logger.error(f"❌ LLM 标题兜底翻译失败: {e}")
        return "暂无法翻译标题"
    
    
def translate_full_report(content: str, cfg: dict) -> tuple:
    """使用 LLM 对抓取的正文进行中文深度翻译与要点总结"""
    if not content:
        return "", ""
    
    # 使用单例 AppConfig 获取数据库路径
    from .config import settings
    db_path = str(settings.DB_PATH)
    
    # 从统一配置加载 AI Prompt (此时 config.py 已确保其存在)
    sys_prompt = cfg["news"]["full_translation_prompt"]["system"]
    
    # 限制输入长度，防止 Token 溢出，取精华部分
    max_len = cfg["news"]["max_summary_input_len"]
    payload = content[:max_len] 
    
    try:
        # 强制使用 AI 引擎进行翻译
        forced = cfg["news"].get("preferred_engine")
        if forced and forced.lower() == "auto": forced = None
        translated_text, _, used_engine = llm_gateway.query(sys_prompt, payload, db_path, task_role="translation", forced_engine=forced)
        return translated_text, used_engine
    except Exception as e:
        logger.error(f"全文翻译失败: {e}") 
        return "", ""
