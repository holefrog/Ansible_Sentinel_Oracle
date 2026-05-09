# agent/llm_gateway.py
# v5.2 - 统一配置加载

import os
import json
import sqlite3
import logging
import requests
import re
from datetime import datetime

# 从 agent.config 导入 load 函数，用于加载统一的配置
from .config import load as load_config, settings as app_settings

logger = logging.getLogger(__name__)

class AllEnginesFailedError(Exception):
    """当所有配置的 LLM 引擎均不可用或调用失败时抛出此核心异常"""
    pass

def init_db(db_path: str):
    """初始化数据库表结构（幂等）"""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute('CREATE TABLE IF NOT EXISTS usage_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, engine TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL)')
    conn.commit()
    conn.close()

def log_usage(db_path, engine, model, prompt_t, completion_t, cost):
    conn = sqlite3.connect(db_path, timeout=10.0)
    c = conn.cursor()
    c.execute("INSERT INTO usage_logs (timestamp, engine, model, prompt_tokens, completion_tokens, cost) VALUES (?, ?, ?, ?, ?, ?)",
              (datetime.now().isoformat(), engine, model, prompt_t, completion_t, cost))
    conn.commit()
    conn.close()

def get_today_usage(db_path, engine):
    init_db(db_path)
    conn = sqlite3.connect(db_path, timeout=10.0)
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute("SELECT COUNT(*) FROM usage_logs WHERE engine=? AND timestamp LIKE ?", (engine, f"{today}%"))
    count = c.fetchone()[0]
    conn.close()
    return count

def get_usage_stats(db_path):
    init_db(db_path) 
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    today = datetime.now().strftime('%Y-%m-%d')
    query = """SELECT engine, COUNT(*) as total_calls, SUM(cost) as total_cost, 
               SUM(CASE WHEN timestamp LIKE ? THEN 1 ELSE 0 END) as today_calls 
               FROM usage_logs GROUP BY engine"""
    try:
        c.execute(query, (f"{today}%",))
        return [dict(r) for r in c.fetchall()]
    except Exception as e:
        logger.error(f"查询失败: {e}")
        return []
    finally:
        conn.close()

def get_engine_list() -> list:
    """动态获取 settings.toml 中定义的所有引擎 ID"""
    config = load_config()
    # llm 配置现在在 settings.toml 的 llm.engines 列表下
    engines_list = config.get("llm", {}).get("engines", [])
    return [e.get("name") for e in engines_list if e.get("name")]

def _execute_llm_call(conf, api_key, sys_prompt, user_prompt):
    safe_sys = json.dumps(sys_prompt, ensure_ascii=False)[1:-1]
    safe_user = json.dumps(user_prompt, ensure_ascii=False)[1:-1]

    raw_payload = conf["payload_template"]
    raw_payload = raw_payload.replace("{sys}", safe_sys).replace("{user}", safe_user)
    raw_payload = raw_payload.replace("{model}", conf.get("model", ""))
    raw_payload = raw_payload.replace("{max_tokens}", str(conf.get("max_tokens", 1024)))

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as e:
        logger.error(f"Payload 依然解析失败。原始文本预览: {raw_payload[:200]}...")
        raise e

    headers = {"Content-Type": "application/json"}
    request_url = conf["url"]

    # 1. 注入用户自定义额外请求头 (如 anthropic-version)
    extra_headers = conf.get("extra_headers", {})
    if isinstance(extra_headers, dict):
        headers.update(extra_headers)

    # 2. 动态组装鉴权参数
    auth_loc = conf.get("api_key_location", "header").lower()
    if api_key and auth_loc != "none":
        auth_name = conf.get("api_key_name", "Authorization")
        auth_format = conf.get("api_key_format", "Bearer {key}")
        
        if auth_loc == "header":
            headers[auth_name] = auth_format.replace("{key}", api_key)
        elif auth_loc == "query":
            connector = "&" if "?" in request_url else "?"
            request_url = f"{request_url}{connector}{auth_name}={api_key}"

    try:
        timeout_sec = conf.get("timeout", 60)
        resp = requests.post(request_url, headers=headers, json=payload, timeout=timeout_sec)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        err_msg = str(e)
        if e.response is not None:
            err_msg += f" | 响应详情: {e.response.text}"
        if api_key:
            err_msg = re.sub(re.escape(api_key), "********", err_msg, flags=re.I)
        err_msg = re.sub(r'key=[a-zA-Z0-9_-]+', 'key=********', err_msg, flags=re.I)
        raise ValueError(f"API 请求失败: {err_msg}")
    except Exception as e:
        raise ValueError(f"网络异常: {str(e)}")

    data = resp.json()
    resp_format = conf.get("response_format", "openai").lower()

    try:
        if resp_format == "gemini":
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata") or {}
            in_t = usage.get("promptTokenCount", 0)
            out_t = usage.get("candidatesTokenCount", 0)
            cost = 0.0
        elif resp_format == "claude":
            text = data["content"][0]["text"]
            usage = data.get("usage") or {}
            in_t = usage.get("input_tokens", 0)
            out_t = usage.get("output_tokens", 0)
            cost = (in_t / 1e6 * conf.get('cost_per_1m_in', 3.0)) + (out_t / 1e6 * conf.get('cost_per_1m_out', 15.0))
        elif resp_format == "deepl":
            text = data["translations"][0]["text"]
            in_t, out_t, cost = len(user_prompt), 0, 0.0
        else:
            text = data["choices"][0]["message"]["content"]
            in_t, out_t, cost = data.get("usage", {}).get("prompt_tokens", 0), data.get("usage", {}).get("completion_tokens", 0), 0.0
        return text, conf['model'], in_t, out_t, cost
    except KeyError as e:
        raise ValueError(f"响应解析失败: {e}")

def query(sys_prompt: str, user_prompt: str, db_path: str, task_role: str = None, forced_engine: str = None) -> tuple:
    init_db(db_path)
    config = load_config()
    engines_list = config.get("llm", {}).get("engines", [])
    
    available = []
    for s in engines_list:
        eid = s.get("name", "")
        if not eid: continue
        key = os.environ.get(f"{eid.upper()}_API_KEY", "").strip()
        
        if "dummy" in key.lower():
            key = ""
            
        auth_loc = s.get("api_key_location", "header").lower()
        is_local = "localhost" in s.get("url", "") or "127.0.0.1" in s.get("url", "")
        
        if key or auth_loc == "none" or is_local: 
            conf = s.copy()
            conf['api_key'] = key
            available.append(conf)
        else:
            logger.warning(f"⏭️ 引擎 {eid} 已跳过：未配置环境变量，且未声明为免密(none)引擎")

    if forced_engine and forced_engine.lower() != "auto":
        engines = [e for e in available if e['name'] == forced_engine.lower()]
    else:
        if task_role:
            engines = [e for e in available if task_role in e.get("roles", [])]
        else:
            engines = available
        engines = sorted(engines, key=lambda x: x.get('priority', 99))

    if not engines: 
        role_str = f" (角色: {task_role})" if task_role else ""
        err_msg = f"未检测到有效 AI 引擎配置{role_str}。请检查 `settings.toml` 中的 `roles` 标签或环境变量 API Key。"
        raise AllEnginesFailedError(err_msg)

    last_error = "所有配置的引擎均已达到今日 Quota 配额保护限制被跳过，没有发起任何请求。"
    for eng in engines:
        try:
            if get_today_usage(db_path, eng['name']) >= eng.get('quota', 9999): 
                last_error = f"引擎 {eng['name']} 触发配额保护限制。"
                continue
            text, model, in_t, out_t, cost = _execute_llm_call(eng, eng['api_key'], sys_prompt, user_prompt)
            log_usage(db_path, eng['name'], model, in_t, out_t, cost)
            audit = f"🧠 **AI 审计** | 引擎: `{eng['name'].upper()}` (`{model}`)\n└ 任务: `{task_role or '通用'}` | 消耗: `{in_t + out_t}` | 计费: `${cost:.4f}`"
            return text, audit, eng['name'].upper()
        except Exception as e:
            last_error = str(e)
            logger.warning(f"⚠️ 引擎 {eng['name']} 调用失败: {e}，准备切换备用引擎...")
            continue
            
    raise AllEnginesFailedError(f"AI 调用完全失败。最后报错: {last_error}")

def test_all_engines(sys_prompt: str, user_prompt: str, db_path: str, task_role: str = None) -> list:
    """遍历所有已配置 Key 的引擎（可选按角色过滤）进行连通性测试。"""
    init_db(db_path)
    config = load_config()
    engines_list = config.get("llm", {}).get("engines", [])
    
    results = []
    for s in engines_list:
        eid = s.get("name", "")
        if not eid: continue
        
        # 如果指定了测试特定角色，过滤掉不符合该角色的引擎
        if task_role and task_role not in s.get("roles", []):
            continue
            
        key = os.environ.get(f"{eid.upper()}_API_KEY", "").strip()
        if "dummy" in key.lower():
            key = ""
            
        auth_loc = s.get("api_key_location", "header").lower()
        is_local = "localhost" in s.get("url", "") or "127.0.0.1" in s.get("url", "")
            
        if not key and auth_loc != "none" and not is_local:
            logger.warning(f"⏭️ 引擎 {eid} 已跳过：未配置有效 API Key，且非免密请求")
            results.append({"name": eid, "status": "skipped", "reason": "鉴权缺失跳过"})
            continue
            
        conf = s.copy()
        try:
            text, model, in_t, out_t, cost = _execute_llm_call(conf, key, sys_prompt, user_prompt)
            log_usage(db_path, eid, model, in_t, out_t, cost)
            results.append({
                "name": eid, 
                "status": "success", 
                "model": model, 
                "answer": text, 
                "audit": f"Tokens: {in_t+out_t} | Cost: ${cost:.4f}"
            })
        except Exception as e:
            results.append({"name": eid, "status": "error", "reason": str(e)})
            logger.warning(f"⚠️ 引擎 {eid} 测试失败: {e}")
    return results
