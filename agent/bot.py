# v8.2 - StockSentinel 核心守护进程 (统一配置版)
# 功能：负责 Discord 交互、指令调度、系统状态监控及 AI 网关入口

import logging
import os
import subprocess
import sys
import platform
import asyncio
from datetime import datetime, timezone
from typing import Union, Literal

import discord
import psutil
from discord import app_commands
from discord.ext import commands

# 统一从 agent.config 导入
from . import config
from .config import settings, resolve_channel
from . import discord_utils
from . import llm_gateway

# ── 日志系统配置 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot")

# ── 全局配置与变量初始化 ──────────────────────────────────────────────────────
cfg = config.load()
GUILD_ID = int(cfg["discord"]["guild_id"])

# 使用 resolve_channel 安全解析
REPORT_CH_ID = resolve_channel(cfg, "report")
SCAN_CH_ID   = resolve_channel(cfg, "scan")
LOG_CH_ID    = resolve_channel(cfg, "log")

APP_DIR = str(settings.APP_DIR)
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") 
STARTED_AT = datetime.now(timezone.utc)

# ── Discord Bot 基础设置 ──────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True 

bot = commands.Bot(command_prefix="!", intents=intents)
guild_obj = discord.Object(id=GUILD_ID)

# ── 基础辅助工具函数 ──────────────────────────────────────────────────────────

def _uptime_str() -> str:
    delta = datetime.now(timezone.utc) - STARTED_AT
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"

def _bar(pct: float, width: int = 10) -> str:
    filled = round(pct / 100 * width)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct:.1f}%"

def _run_task(script: str, channel_id: int, extra_args: list = None):
    venv_python = os.path.join(APP_DIR, "venv", "bin", "python")
    module_name = f"agent.{script.replace('.py', '')}"
    
    cmd = [venv_python, "-m", module_name, "--channel", str(channel_id)]
    if extra_args:
        cmd.extend(extra_args)
        
    # 智能匹配日志文件，与 heartbeat.py 读取逻辑对齐
    log_filename = "misc.log"
    if script == "report.py" and extra_args:
        if "morning" in extra_args: log_filename = "morning.log"
        elif "evening" in extra_args: log_filename = "evening.log"
    elif script == "market_scan.py":
        log_filename = "scan.log"
        
    log_path = os.path.join(APP_DIR, "logs", log_filename)
    out_file = open(log_path, "a", encoding="utf-8")
    
    subprocess.Popen(cmd, env={**os.environ}, stdout=out_file, stderr=subprocess.STDOUT)
    out_file.close() # 父进程关闭引用，子进程继续持有并写入文件
    logger.info("已成功唤起独立任务模块：%s，日志已路由至 %s", module_name, log_filename)

def get_ai_choices():
    choices = [
        app_commands.Choice(name="自动轮询 (Auto)", value="Auto"),
        app_commands.Choice(name="全部连通性测试 (All)", value="All"),
        app_commands.Choice(name="角色批量测试: 翻译 (Translation)", value="Role_translation"),
        app_commands.Choice(name="角色批量测试: 打分 (Scoring)", value="Role_scoring"),
        app_commands.Choice(name="角色批量测试: 总结 (Summary)", value="Role_summary")
    ]
    engines = llm_gateway.get_engine_list()
    for eng in engines:
        if len(choices) < 25: # Discord Slash 命令下拉菜单最多支持 25 个选项
            choices.append(app_commands.Choice(name=f"单引擎测试: {eng.upper()}", value=eng))
    return choices

# ── Discord 事件监听 ──────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("StockSentinel 已上线：Logged in as %s", bot.user)
    try:
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        logger.info("Slash 指令树同步成功")
    except Exception as e:
        logger.error("Slash 指令同步失败：%s", e)

    channel = bot.get_channel(LOG_CH_ID)
    if channel:
        desc = (
            f"**主机**: `{platform.node()}`\n"
            f"**环境**: `Python {platform.python_version()}`\n"
            f"**路径**: `{APP_DIR}`\n\n"
            f"💡 *输入 `/` 即可查看所有可用投研指令*"
        )
        embed = discord_utils.create_embed(title="✅ 投研监控节点已就绪", description=desc, color_val=1.0)
        await channel.send(embed=embed)

# ── 基础监控指令 ──────────────────────────────────────────────────────────────

@bot.tree.command(name="ping", description="检查 Bot 与 Discord 的连接延迟", guild=guild_obj)
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong！延迟 **{round(bot.latency * 1000)} ms**", ephemeral=True)

@bot.tree.command(name="status", description="查看 VPS 硬件负载与运行状态", guild=guild_obj)
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    worst = max(cpu, mem.percent, disk.percent)
    color_val = -1.0 if worst >= 85 else (0.0 if worst >= 60 else 1.0)
    
    desc = (
        f"**主机名**: `{platform.node()}`\n"
        f"**运行时长**: `{_uptime_str()}`\n"
        f"**延迟**: `{round(bot.latency * 1000)} ms`\n\n"
        f"**CPU 使用率**: `{_bar(cpu)}`\n"
        f"**内存使用**: `{_bar(mem.percent)}`\n"
        f"**磁盘使用**: `{_bar(disk.percent)}`"
    )
    embed = discord_utils.create_embed(title="📊 StockSentinel 系统负载报告", description=desc, color_val=color_val)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── /report 投研报告指令组 ────────────────────────────────────────────────────
report_group = app_commands.Group(name="report", description="手动触发投研报告生成任务")

@report_group.command(name="morning", description="立即生成并发送美股早报")
async def slash_report_morning(interaction: discord.Interaction):
    await interaction.response.send_message("🌅 正在后台执行早报脚本...", ephemeral=True)
    _run_task("report.py", REPORT_CH_ID, ["--type", "morning"])

@report_group.command(name="evening", description="立即生成并发送收盘晚报")
async def slash_report_evening(interaction: discord.Interaction):
    await interaction.response.send_message("🌆 正在后台执行晚报脚本...", ephemeral=True)
    _run_task("report.py", REPORT_CH_ID, ["--type", "evening"])

bot.tree.add_command(report_group, guild=guild_obj)

# ── 市场扫描指令 ──────────────────────────────────────────────────────────────

@bot.tree.command(name="scan", description="立即触发全市场异动量价扫描", guild=guild_obj)
async def slash_scan(interaction: discord.Interaction):
    await interaction.response.send_message("🔍 正在启动扫描引擎...", ephemeral=True)
    _run_task("market_scan.py", SCAN_CH_ID)

# ── 系统日志指令 ──────────────────────────────────────────────────────────────

@bot.tree.command(name="logs", description="提取系统最新的运行或报错日志", guild=guild_obj)
@app_commands.describe(lines="需要查看的日志行数 (默认 30)")
async def slash_logs(interaction: discord.Interaction, lines: int = 30):
    await interaction.response.defer(ephemeral=True)
    try:
        cmd = ["journalctl", "-u", "stock-sentinel-bot", "-n", str(lines), "--no-pager", "--quiet"]
        result = subprocess.run(cmd, capture_output=True, text=True)
        output = result.stdout.strip() or "暂无相关日志。"
        if len(output) > 1900: output = "...\n" + output[-1880:]
        await interaction.followup.send(f"```log\n{output}\n```", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"日志提取失败: {e}", ephemeral=True)


# ── /clear 批量清除当前频道的消息 ──────────────────────────────────────────────────────
@bot.tree.command(name="clear", description="批量清除当前频道的消息", guild=guild_obj)
@app_commands.describe(amount="需要清除的消息数量 (默认 10)")
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_clear(interaction: discord.Interaction, amount: int = 10):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"✅ 已成功清理 `{len(deleted)}` 条消息。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 清理失败: `{str(e)}`", ephemeral=True)
        
# ── /reboot 进程重启指令 ──────────────────────────────────────────────────────

class RebootView(discord.ui.View):
    def __init__(self, requester: Union[discord.User, discord.Member]):
        super().__init__(timeout=30)
        self.requester = requester

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message("⛔ 您不是该指令的发起者", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="确认重启", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="♻️ **正在终止进程并尝试由 Systemd 自动拉起...**", view=None)
        self.stop()
        await asyncio.sleep(1)
        await bot.close()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="操作已取消。", view=None)
        self.stop()

@bot.tree.command(name="reboot", description="重启 Bot 服务进程", guild=guild_obj)
async def slash_reboot(interaction: discord.Interaction):
    view = RebootView(requester=interaction.user)
    await interaction.response.send_message("⚠️ **警告：这会导致 Bot 暂时下线，确定要重启吗？**", view=view, ephemeral=True)

# ── /watch 自选股管理指令 ────────────────────────────────────────────────────

@bot.tree.command(name="watch", description="管理自选股池或查看当前监控名单", guild=guild_obj)
@app_commands.describe(action="add, remove, list", ticker="股票代码", name="名称", note="备注", shares="持仓数量 (用于 Advisor)", avg_cost="持仓均价 (用于 Advisor)", strategy="策略计划 (用于 Advisor)")
async def slash_watch(interaction: discord.Interaction, action: Literal["add", "remove", "list"], 
                      ticker: str = None, name: str = None, note: str = None,
                      shares: int = 0, avg_cost: float = 0.0, strategy: str = None):
    await interaction.response.defer(ephemeral=True)
    if action == "list":
        current_cfg = config.load()
        watch_list = current_cfg["market"].get("watch_list", [])
        msg = "\n".join([f"• **{i['ticker']}** | {i['name']} (*{i.get('note', '无备注')}*) | 持仓: {i.get('shares', 0)} 成本: {i.get('avg_cost', 0.0)} | 策略: {i.get('strategy', '未设定')}" for i in watch_list]) if watch_list else "监控列表为空。"
        embed = discord_utils.create_embed(title="🔭 实时监控池名单", description=msg, color_val=0.0)
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    if not ticker:
        await interaction.followup.send("❌ 必须提供代码 (ticker)。", ephemeral=True)
        return

    success, message = config.update_watch_list_file(action, ticker, name, note, shares, avg_cost, strategy)
    if success:
        global cfg
        cfg = config.load() # 重新加载配置
    embed = discord_utils.create_embed(title="✅ 更新成功" if success else "❌ 更新失败", 
                                      description=f"动作: `{action}`\n代码: `{ticker.upper()}`\n详情: {message}",
                                      color_val=1.0 if success else -1.0)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ── /ai 模型管理指令组 ────────────────────────────────────────────────────────
ai_group = app_commands.Group(name="ai", description="AI 分析引擎管理与调试")

@ai_group.command(name="stats", description="查看 AI 模型 API 调用统计与计费报告")
async def slash_ai_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        db_path = str(settings.DB_PATH)
        stats = llm_gateway.get_usage_stats(db_path)

        if not stats:
            embed = discord_utils.create_embed(title="🧠 AI 审计", description="暂无历史调用数据。", color_val=0.0)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        lines = []
        total_cost = 0.0
        for s in stats:
            lines.append(f"**{s['engine']}**")
            lines.append(f"└ 今日调用: `{s['today_calls']}` 次 | 累计: `{s['total_calls']}` 次")
            lines.append(f"└ 累计计费: `${s['total_cost']:.4f}`\n")
            total_cost += s['total_cost']

        desc = "\n".join(lines) + f"**💰 总审计计费**: `${total_cost:.4f}`"
        embed = discord_utils.create_embed(title="🧠 AI 分析引擎审计", description=desc, color_val=1.0)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"AI 统计获取失败: {e}")
        await interaction.followup.send(f"❌ 统计读取失败: `{str(e)}`", ephemeral=True)

@ai_group.command(name="test", description="测试特定 AI 引擎的连通性与降级机制")
@app_commands.describe(prompt="测试内容", engine="选择测试模式或特定引擎 (默认: 全部连通性测试 All)")
@app_commands.choices(engine=get_ai_choices())
async def slash_ai_test(interaction: discord.Interaction, 
                        prompt: str = "测试连通性。请说出你是哪个AI大模型，以及你能做什么。", 
                        engine: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    engine_val = engine.value if engine else "All"
    current_cfg = config.load()
    sys_prompt = current_cfg["discord"]["bot_prompt"]["system"]
    db_path = str(settings.DB_PATH)

    try:
        if engine_val == "All" or engine_val.startswith("Role_"):
            task_role = engine_val.split("_")[1] if engine_val.startswith("Role_") else None
            results = llm_gateway.test_all_engines(sys_prompt, prompt, db_path, task_role=task_role)
            
            role_msg = f"角色: **{task_role.title()}**" if task_role else "全部引擎"
            await interaction.followup.send(f"🔍 **启动批量连通性测试** ({role_msg}，共匹配 {len(results)} 个)", ephemeral=True)

            for res in results:
                status_icon = "✅" if res["status"] == "success" else "❌" if res["status"] == "error" else "⚪"
                color_val = 1.0 if res["status"] == "success" else (-1.0 if res["status"] == "error" else 0.0)
                
                embed = discord_utils.create_embed(title=f"{status_icon} 引擎测试: {res['name'].upper()}", color_val=color_val)
                
                if res["status"] == "success":
                    content = res['answer'] if len(res['answer']) < 3000 else res['answer'][:3000] + "..."
                    val = f"**模型**: `{res['model']}`\n\n**📤 AI 回复**:\n{content}\n\n{res['audit']}"
                elif res["status"] == "skipped":
                    val = f"**状态**: `跳过`\n**原因**: {res['reason']}"
                else:
                    val = f"**状态**: `失败`\n**错误信息**: ```{res['reason']}```"
                
                embed.description = val
                await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            forced = None if engine_val == "Auto" else engine_val
            answer, audit, _ = llm_gateway.query(sys_prompt, prompt, db_path, forced_engine=forced)
            desc = f"**模式**: `{engine_val}`\n**输入**: {prompt}\n\n**📤 AI 回复**:\n{answer}\n\n{audit}"
            embed = discord_utils.create_embed(title="✅ AI 通信正常", description=desc, color_val=1.0)
            await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(embed=discord_utils.create_embed(title="❌ AI 测试失败", description=f"```{e}```", color_val=-1.0), ephemeral=True)

bot.tree.add_command(ai_group, guild=guild_obj)

if __name__ == "__main__":
    if not BOT_TOKEN:
        logger.error("环境变量中缺少 DISCORD_BOT_TOKEN")
        sys.exit(1)
    bot.run(BOT_TOKEN)
