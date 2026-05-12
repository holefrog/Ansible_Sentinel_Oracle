# agent/renderer.py
# v8.8 - 统一使用显式相对导入

from . import discord_utils
import logging
from datetime import datetime
from datetime import datetime, timezone
import re
import json

logger = logging.getLogger("renderer")

# 成交量异动阈值
VOLUME_SPIKE_X = 1.5

def build_report_embeds(report_type: str, market_context: list, watch_list: list, 
                        mc_data: dict, watch_data: dict, now_str: str, note: str = "",
                        calendar: dict = None, news_items: list = None, ai_summary: str = None, report_url: str = "") -> list:
    """构建 Discord Embeds"""
    embeds = []
    is_morning = (report_type == "morning")
    title_icon = "🌅" if is_morning else "🌆"
    title_text = "美股早报" if is_morning else "收盘晚报"
    watch_title = "**🔭 自选股监控（昨收）**" if is_morning else "**🔭 自选股监控**"

    # 1. 大盘与宏观卡片
    mc_lines = []
    total_mc = 0.0
    for sym, name in market_context:
        d = mc_data.get(sym)
        if d:
            total_mc += d["change_pct"]
            icon = "📈" if d["change_pct"] > 0 else "📉" if d["change_pct"] < 0 else "📊"
            extra_note = f" [{d['note']}]" if d.get("note") else ""
            mc_lines.append(f"{icon} **{name}**: {d['price']:.2f} ({d['change_pct']:+.2f}%){extra_note}")
        else:
            mc_lines.append(f"⚠️ **{sym}**: 接口数据异常")

    e1 = discord_utils.create_embed(
        title=f"{title_icon} {title_text} | {now_str}" + (f"  {note}" if note else ""),
        description="**📊 大盘与宏观表现**\n" + "\n".join(mc_lines),
        color_val=total_mc,
        url=report_url
    )
    embeds.append(e1)

    # 2. 自选股监控卡片
    winners, losers, spikes = [], [], []
    net_watch = 0.0
    for sym, name in watch_list:
        d = watch_data.get(sym)
        if not d:
            losers.append((0.0, f"`{sym:5}` ⚠️ 接口数据异常"))
            continue
        net_watch += d["change_pct"]
        line = f"`{sym:5}` {d['price']:7.2f} ({d['change_pct']:+6.2f}%)"
        if d["change_pct"] >= 0:
            winners.append((d["change_pct"], line))
        else:
            losers.append((d["change_pct"], line))
            
        if not is_morning and d.get("avg_volume"):
            spike = d["volume"] / d["avg_volume"]
            if spike >= VOLUME_SPIKE_X:
                spikes.append(f"**{sym}** 量比 {spike:.1f}x")

    all_lines = (
        [l for _, l in sorted(winners, key=lambda x: -x[0])] +
        [l for _, l in sorted(losers,  key=lambda x:  x[0])]
    )
    desc2 = f"{watch_title}\n" + "\n".join(all_lines)
    if spikes:
        desc2 += "\n\n**📈 成交量异动**\n" + "\n".join(spikes)

    e2 = discord_utils.create_embed(description=desc2, color_val=net_watch)
    embeds.append(e2)

    # 3. 动态扩展卡片
    if is_morning and calendar:
        cal_lines = []
        for e in calendar.get("earnings", []):
            timing = "盘前" if e.get("time") == "bmo" else "盘后" if e.get("time") == "amc" else ""
            cal_lines.append(f"📋 **{e['symbol']}** 财报 {timing}")
        for e in calendar.get("economics", []):
            impact = "🔴" if e.get("impact") == "high" else "🟡"
            cal_lines.append(f"{impact} {e['event']}")
        if cal_lines:
            embeds.append(discord_utils.create_embed(
                description="**📅 今日重要事件**\n" + "\n".join(cal_lines), color_val=0.0
            ))

    # 第3/4部分：重要公告列表（取消仅晚报的限制，早晚报均可展示）
    header = "**📰 隔夜重要资讯**\n" if is_morning else "**📰 今日重要公告**\n"
    if news_items:
        news_lines = []
        for item in news_items[:8]:
            title_zh = item.get("title_zh", "")
            time_part = f"[{item.get('published', '')[-8:]}] " if item.get('published') else ""
            display_title = title_zh if title_zh else item['title'][:60]
            
            if item.get("content_zh"):
                display_line = f"• {time_part}**[{display_title}]({item['url']})** ✨"
            else:
                display_line = f"• {time_part}[{display_title}]({item['url']})"
                
            news_lines.append(display_line)
        if news_lines:
            embeds.append(discord_utils.create_embed(
                description=header + "\n".join(news_lines), color_val=0.0
            ))
        else:
            embeds.append(discord_utils.create_embed(
                description=header + "今日暂无重要新闻。", color_val=0.0
            ))
    else:
        embeds.append(discord_utils.create_embed(
            description=header + "今日暂无重要新闻。", color_val=0.0
        ))
            
    # 第4/5部分：AI 市场解读
    if ai_summary:
        display_summary = f"✨ AI 深度市场解读与异动分析已生成。\n\n🔗 **[请点击此处前往网页端阅读完整投研报告及资讯细节]({report_url})**"
        embeds.append(discord_utils.create_embed(
            description="**📰 AI 市场解读**\n" + display_summary, color_val=0.0
        ))
    elif not news_items:
        embeds.append(discord_utils.create_embed(
            description="**📰 AI 市场解读**\n今日暂无重要新闻，跳过 AI 分析。", color_val=0.0
        ))
    else:
        embeds.append(discord_utils.create_embed(
            description="**📰 AI 市场解读**\nAI 失败，无法生成分析和总结。", color_val=0.0
        ))

    return embeds

def build_report_markdown(report_type: str, market_context: list, watch_list: list, mc_data: dict, watch_data: dict,
                          now_str: str, date_str: str, note: str = "",
                          calendar: dict = None, news_items: list = None, ai_summary: str = None) -> tuple:
    """构建 Hugo Markdown 投研报告"""
    is_morning = (report_type == "morning")
    title_text = "美股早报" if is_morning else "美股晚报"
    title_icon = "🌅" if is_morning else "🌆"
    time_str   = "07:30:00" if is_morning else "18:30:00"

    safe_title = f"{title_text} {date_str}".replace('"', '\\"')

    # 动态获取 VPS 本地时区偏移（自动适配美东夏令时 EDT -04:00 与冬令时 EST -05:00）
    tz_offset = datetime.now().astimezone().strftime("%z")
    tz_str = f"{tz_offset[:3]}:{tz_offset[3:]}" if tz_offset else "-05:00"

    front_matter = "\n".join([
        f'title: "{safe_title}"',
        f'date: "{date_str}T{time_str}{tz_str}"',
        'type: "report"',
        f'report_type: "{report_type}"',
    ])

    lines = [
        "**[⬅️ 返回上一级](../)**",
        "",
        f"# {title_icon} {title_text} — {now_str}", 
        ""
    ]
    if note: lines += [f"> {note}", ""]

    lines += ["## 📊 大盘与宏观表现", "", "| 标的 | 收盘价 | 涨跌幅 | 备注 |", "|------|--------|--------|------|"]
    for sym, name in market_context:
        d = mc_data.get(sym)
        if d:
            arrow = "▲" if d["change_pct"] > 0 else "▼" if d["change_pct"] < 0 else "—"
            lines.append(f"| {name} ({sym}) | {d['price']:.2f} | {arrow} {d['change_pct']:+.2f}% | {d.get('note', '')} |")

    lines += ["", "## 🔭 自选股监控", "", "| 股票 | 收盘价 | 涨跌幅 | 量比 |", "|------|--------|--------|------|"]
    for sym, name in watch_list:
        d = watch_data.get(sym)
        if d:
            spike_str = f"{d['volume']/d['avg_volume']:.1f}x" if not is_morning and d.get("avg_volume") else "—"
            lines.append(f"| {name} ({sym}) | {d['price']:.2f} | {'▲' if d['change_pct']>=0 else '▼'} {d['change_pct']:+.2f}% | {spike_str} |")

    if is_morning and calendar:
        lines += ["", "## 📅 今日重要事件", ""]
        for e in calendar.get("earnings", []): lines.append(f"- **{e['symbol']}** 财报")
        for e in calendar.get("economics", []): lines.append(f"- {e['event']}")

    header_title = "## 📰 隔夜重要资讯" if is_morning else "## 📰 今日重要公告"
    lines += ["", header_title, ""]
    if news_items:
        for item in news_items:
            time_tag = f"**[{item.get('published', '')[-8:]}]** " if item.get('published') else ""
            display_title = item.get('title_zh', item['title'])
            
            has_detail = bool(item.get("content_zh"))
            if has_detail:
                lines.append(f"- {time_tag}**[{display_title}]({item['url']})** ✨")
                safe_content = item['content_zh'].replace('\n', '\n  > ')
                trans_engine = item.get('trans_engine', item.get('ai_engine', 'AI'))
                lines.append(f"  > **AI 深度解析 [{trans_engine}]：**\n  > {safe_content}\n")
            else:
                lines.append(f"- {time_tag}[{display_title}]({item['url']})")
    else:
        lines.append("今日暂无重要新闻。")
                
    lines += ["", "## 📰 AI 市场解读", ""]
    if ai_summary:
        lines.append(ai_summary)
    elif not news_items:
        lines.append("今日暂无重要新闻，跳过 AI 分析。")
    else:
        lines.append("AI 失败，无法生成分析和总结。")

    lines += ["", "---", "", "**[⬅️ 返回上一级](../)**"]

    filename = f"{date_str}-{report_type}.md"
    return front_matter, "\n".join(lines), filename

def build_news_markdown(item: dict, cfg: dict) -> tuple:
    """
    [全量版] 为单条高分新闻构建 Hugo Markdown。
    标题采用 [中文] English 格式，增强网页预览效果。
    """
    now = datetime.now().astimezone()
    date_str = now.strftime("%Y-%m-%d")
    iso_time = now.isoformat(timespec="seconds")
    
    # --- 标题逻辑修复：中英文双显 ---
    zh = item.get("title_zh", "")
    en = item.get("title", "")
    display_title = f"[{zh}] {en}" if zh else en
    
    # Slug 建议保留英文以确保 URL 链接稳定性
    slug = re.sub(r'[^\w\s-]', '', en).strip().lower().replace(' ', '-')[:50]
    filename = f"{date_str}-{slug}.md"

    # 对所有可能包含引号的字段应用 JSON 转义
    title_val = json.dumps(display_title, ensure_ascii=False)
    source_val = json.dumps(item["source"], ensure_ascii=False)
    url_val = json.dumps(item["url"], ensure_ascii=False)
    tickers_val = json.dumps(item.get("tickers", []), ensure_ascii=False)
    ai_engine_val = json.dumps(item.get("ai_engine", "AI"), ensure_ascii=False)

    front_matter = "\n".join([
        f'title: {title_val}',
        f'date: "{iso_time}"',
        f'type: "news"',
        f'source: {source_val}',
        f'tickers: {tickers_val}',
        f'ai_score: {item.get("ai_score", 0)}',
        f'ai_engine: {ai_engine_val}', 
        f'original_url: {url_val}'
    ])

    content_lines = [
        "**[⬅️ 返回上一级](../)**",
        "",
        f"# {display_title}",
        "",
        f"**原文标题**: {en}",
        f"**来源**: {item['source']} | **时间**: {item.get('published', '未知')} | **评分引擎**: {item.get('ai_engine', 'AI')} ({item.get('ai_score', 0)}分) | **分析引擎**: {item.get('trans_engine', item.get('ai_engine', 'AI'))}",
        "",
        f"## 💡 AI 深度解析 [{item.get('trans_engine', item.get('ai_engine', 'AI'))}]",
        "",
        item.get("content_zh", "（暂无深度解析内容）"),
        "",
        "---",
        f"[阅读原文]({item['url']})",
        "",
        "**[⬅️ 返回上一级](../)**"
    ]

    return front_matter, "\n".join(content_lines), filename
