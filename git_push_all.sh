#!/bin/bash

# === 配置 ===
REPO_URL="https://github.com/holefrog/Ansible_Sentinel_Oracle.git"
BRANCH="main"

# === 初始化仓库（如果还没初始化）===
git init

# === 清除缓存
git rm -r --cached .

# === 添加所有文件 ===
git add .

# === 提交 ===
git commit -m "覆盖远程仓库"

# === 绑定远程（如果已存在会报错，可忽略）===
git remote remove origin 2>/dev/null
git remote add origin $REPO_URL

# === 强制推送 ===
git branch -M $BRANCH
git push -f origin $BRANCH

echo "✅ 已强制覆盖远程仓库"
