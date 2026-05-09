#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "--------------------------------------------------------"
echo "🚀 StockSentinel 部署系统"
echo "--------------------------------------------------------"
echo "1) Bootstrap  - 首次执行，初始化服务器"
echo "2) Apply      - 日常部署，更新应用"
echo "--------------------------------------------------------"
read -rp "请选择 [1/2，其他退出]: " choice

case "$choice" in
    1)
        MODE="bootstrap"
        INV="inventory_bootstrap.yml"
        PB="bootstrap.yml"
        KEY="ubuntu.key"
        # BECOME_ARGS="--ask-become-pass"
        BECOME_ARGS=""
        ;;
    2)
        MODE="apply"
        INV="inventory.yml"
        PB="site.yml"
        KEY="sentinel.key"
        BECOME_ARGS=""
        ;;
    *)
        echo "已退出。"
        exit 0
        ;;
esac

echo ">>> 已选择: ${MODE}"
echo ""

# 检查是否开启 verbose
VERBOSE=""
read -rp "是否开启详细输出 verbose？[y/N]: " v
if [[ "$v" =~ ^[Yy]$ ]]; then
    VERBOSE="-v"
fi
echo ""

# 检查密钥
if [[ ! -f "$KEY" ]]; then
    echo "❌ 未找到 ${KEY}"
    [[ "$MODE" == "bootstrap" ]] && echo "💡 请先将 ssh-key-2026-04-30.key 改名为 ubuntu.key 放入 deploy/ 目录"
    [[ "$MODE" == "apply" ]] && echo "💡 sentinel.key 由 bootstrap 阶段自动生成，请先选择 1) Bootstrap 完成初始化"
    exit 1
fi
chmod 400 "$KEY"

# 语法检查（仅执行一次，同时重定向 stdout 和 stderr）
echo ">>> [1/2] 正在执行语法校验..."
if ! ansible-playbook -i "$INV" "$PB" --syntax-check >/dev/null 2>&1; then
    echo "❌ 语法错误"
    exit 1
fi
echo "✅ 语法正常"

echo "--------------------------------------------------------"
echo "🛠️  正在执行 ${MODE}..."
echo "--------------------------------------------------------"

if [[ "$MODE" == "bootstrap" ]]; then
    echo "💡 提示：需要输入服务器 sudo 密码。"
    echo "   如果 ubuntu 用户已配置 NOPASSWD sudo，直接回车即可。"
    echo ""
fi

ansible-playbook -i "$INV" "$PB" $BECOME_ARGS $VERBOSE "$@"

echo ""
echo "🎉 ${MODE} 完成！"