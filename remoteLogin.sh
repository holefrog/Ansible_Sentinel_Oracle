#!/bin/bash
set -euo pipefail

# ==========================================================
# 1. 配置常量
# ==========================================================
HOST="myoracle.dynamic-dns.net"
PORT="22"
USER="ubuntu"
# 你的相对路径
RELATIVE_KEY="./deploy/ubuntu.key"
SSH_PASS=""

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# ==========================================================
# 2. 路径转换与权限检查
# ==========================================================
# 切换到脚本所在目录，确保相对路径基准正确
cd "$(dirname "$0")"

if [[ -n "$RELATIVE_KEY" ]]; then
    # 转换为绝对路径
    ABS_KEY="$(pwd)/${RELATIVE_KEY#./}"
    
    if [[ ! -f "$ABS_KEY" ]]; then
        echo -e "${RED}[错误] 未找到密钥文件: $ABS_KEY${NC}"
        exit 1
    fi
    
    # 按照你的习惯，强制修正权限
    chmod 400 "$ABS_KEY"
    SSH_CMD="ssh -i $ABS_KEY"
else
    SSH_CMD="ssh"
fi

SSH_OPTS="-p $PORT -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o IdentitiesOnly=yes"
REMOTE="$USER@$HOST"

# ==========================================================
# 3. 清理函数 (适配 Kitty)
# ==========================================================
cleanup() {
    # 恢复 Kitty 默认配色
    # OSC 110/111 在某些 Kitty 配置下不敏感，改用显式重置
    echo -ne "\033[0m"         # 重置文本格式
    echo -ne "\e]110\a"        # 重置前景
    echo -ne "\e]111\a"        # 重置背景
    
    # 如果仍然不生效，Kitty 可以通过发送特定的键盘协议复位
    # 或者直接调用终端重置
    if command -v reset >/dev/null; then
        reset
    else
        clear
    fi

    echo -e "${NC}>>> 已断开连接，本地环境已重置${NC}"
}

trap cleanup EXIT INT TERM

# ==========================================================
# 4. 执行登录
# ==========================================================
echo -e "${GREEN}>>> 正在连接 $HOST (端口: $PORT)...${NC}"

# 清理旧条目
ssh-keygen -f "$HOME/.ssh/known_hosts" -R "$HOST" >/dev/null 2>&1 || true

# 设置 Kitty 配色 (黑底绿字)
echo -ne "\e]10;#39FF14\a" 
echo -ne "\e]11;#000000\a" 
clear 

# 正式进入
TERM=xterm-256color $SSH_CMD $SSH_OPTS "$REMOTE"
