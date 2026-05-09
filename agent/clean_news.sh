#!/bin/bash

# 检查是否以 root 权限运行，如果不是则使用 sudo 提权并重新执行脚本
if [ "$EUID" -ne 0 ]; then
    echo "🔑 需要 root 权限执行此操作，正在请求 sudo 提权..."
    exec sudo -E "$0" "$@"
fi

CLEAN_DB=false
CLEAN_MD=false

read -r -p "是否要清空数据库 [y/n] 默认是n: " response_db
case "$response_db" in
    [yY][eE][sS]|[yY]) 
        CLEAN_DB=true
        ;;
    *)
        ;;
esac

read -r -p "是否要清空 Markdown 静态新闻 [y/n] 默认是y: " response_md
case "$response_md" in
    [nN][oO]|[nN]) 
        CLEAN_MD=false
        ;;
    *)
        CLEAN_MD=true
        ;;
esac

# 动态获取脚本所在目录，并推导其上一级作为项目根目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DEFAULT_ROOT="$(dirname "$SCRIPT_DIR")"
SENTINEL_ROOT="${SENTINEL_ROOT:-$DEFAULT_ROOT}"

DB_PATH="$SENTINEL_ROOT/data/sentinel.db"
NEWS_DIR="$SENTINEL_ROOT/hugo/content/news"

if [ "$CLEAN_DB" = true ] || [ "$CLEAN_MD" = true ]; then
    echo "🧹 开始执行清理任务..."
else
    echo "🚀 开始执行站点重建任务 (默认跳过清理数据)..."
fi

# 1. 清理数据库
if [ "$CLEAN_DB" = true ]; then
    if [ -f "$DB_PATH" ]; then
        echo "📦 正在清理数据库: $DB_PATH"
        sqlite3 "$DB_PATH" "DELETE FROM news_registry; DELETE FROM news_briefs;"
        echo "  ✅ 数据库表 news_registry 和 news_briefs 已清空。"
    else
        echo "  ⚠️ 数据库文件不存在，跳过。"
    fi
else
    echo "⏭️  跳过清理数据库。"
fi

# 2. 清理 Hugo 生成的 Markdown 文件
if [ "$CLEAN_MD" = true ]; then
    if [ -d "$NEWS_DIR" ]; then
        echo "📁 正在清理 Hugo Markdown 源文件: $NEWS_DIR"
        find "$NEWS_DIR" -maxdepth 1 -name "*.md" ! -name "_index.md" -delete
        echo "  ✅ Markdown 文件已彻底删除（保留 _index.md）。"
    fi
else
    echo "⏭️  跳过清理 Markdown 文件。"
fi

echo "🎉 准备工作执行完毕！"

# 3. 重新编译 Hugo 站点
if [ -x "$SENTINEL_ROOT/hugo/build_hugo.sh" ]; then
    echo "🏗️ 正在使用 build_hugo.sh 重新编译并部署 Hugo 静态站点..."
    SENTINEL_USER=$(stat -c '%U' "$SENTINEL_ROOT")
    
    sudo -u "$SENTINEL_USER" "$SENTINEL_ROOT/hugo/build_hugo.sh"
    
    echo "  ✅ Hugo 站点编译并部署完成！"
elif command -v hugo &> /dev/null; then
    echo "🏗️ 正在重新编译 Hugo 静态站点..."
    if [ -d "$SENTINEL_ROOT/hugo" ]; then
        cd "$SENTINEL_ROOT/hugo" && hugo
        echo "  ✅ Hugo 站点编译完成！"
    else
        echo "  ⚠️ 找不到 Hugo 目录: $SENTINEL_ROOT/hugo，请检查路径是否正确。"
    fi
else
    echo "  ⚠️ 未找到 hugo 命令，请手动前往 $SENTINEL_ROOT/hugo 执行编译。"
fi
