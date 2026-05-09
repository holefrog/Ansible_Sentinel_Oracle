# StockSentinel 部署手册

## 架构概览

```
deploy/
├── bootstrap.yml            # 首次部署 playbook（ubuntu 用户执行）
├── site.yml                 # 日常部署 playbook（sentinel 用户执行）
├── vars.yml                 # 共享变量（域名、路径、应用用户等）
├── secrets.yml              # 敏感密钥（不入 git，从 secrets.yml.example 复制）
├── inventory_bootstrap.yml  # bootstrap 主机清单（ubuntu 连接）
├── inventory.yml            # 日常部署主机清单（sentinel 连接）
├── ansible.cfg              # Ansible 全局配置
├── apply.sh                 # 统一部署入口脚本
├── ubuntu.key               # ubuntu 用户 SSH 私钥（不入 git）
├── sentinel.key             # sentinel 用户 SSH 私钥（bootstrap 自动生成，不入 git）
└── roles/
    ├── infrastructure/      # 系统基础：swap、包安装、防火墙、DDNS
    ├── web/                 # Nginx + SSL 证书
    ├── app_user/            # 创建应用运行用户 sentinel，配置 SSH key 和 sudo
    ├── app/                 # 业务代码部署：Python agent、systemd 服务和 timer
    └── hugo/                # Hugo 静态站点构建与部署
```

## 两阶段部署模型

### 阶段一：Bootstrap（仅首次执行）

**执行身份**：ubuntu 用户（Oracle Cloud 默认管理员）

**完成内容**：
- 系统初始化（swap、基础包、防火墙、DDNS）
- Nginx + Let's Encrypt SSL 证书
- 创建 sentinel 服务账户
- 在本地生成 `sentinel.key` / `sentinel.key.pub` 密钥对
- 将 sentinel 公钥写入 VPS，配置 sudo 权限

### 阶段二：Apply（日常部署）

**执行身份**：sentinel 用户（应用专属账户）

**完成内容**：
- 上传 agent 业务代码和配置文件
- 安装/更新 Python 依赖
- 生成 `.env` 环境变量文件
- 部署 systemd 服务和 timer
- 部署 Hugo 站点

## 快速开始

### 前置准备

1. 将 Oracle Cloud 下载的 SSH 私钥改名并放入 `deploy/` 目录：
   ```bash
   mv ssh-key-xxxx.key deploy/ubuntu.key
   ```

2. 复制并填写密钥配置：
   ```bash
   cp deploy/secrets.yml.example deploy/secrets.yml
   # 编辑 deploy/secrets.yml，填入所有 API Key
   ```

3. 确认 `deploy/vars.yml` 里的域名和路径正确。

### 执行部署

```bash
cd deploy
./apply.sh
# 选择 1 执行 Bootstrap（首次）
# 选择 2 执行 Apply（日常）
```

## Role 职责说明

| Role | 执行阶段 | 职责 |
|------|----------|------|
| `infrastructure` | Bootstrap | swap、apt 包、ufw 防火墙、DDNS |
| `web` | Bootstrap | Nginx 安装配置、Let's Encrypt 证书申请与续期 |
| `app_user` | Bootstrap | 创建 sentinel 用户、生成并配置 SSH 密钥、配置 sudoers |
| `app` | Apply | 代码上传、venv 安装、.env 生成、systemd 服务和 timer |
| `hugo` | Apply | 目录创建、主题同步、站点配置、build 脚本部署 |

## 变量说明

### `vars.yml`（公开配置）

| 变量 | 说明 |
|------|------|
| `domain` | VPS 域名 |
| `web_base` | Nginx 站点根目录基础路径 |
| `sentinel_prefix` | 站点 URL 前缀（空字符串表示无前缀） |
| `hugo_path` | Hugo 可执行文件路径 |
| `service_name` | systemd 服务名 |
| `app_user_name` | 应用运行用户名 |
| `app_user_home` | 应用用户 home 目录 |
| `app_user_shell` | 应用用户 shell |

### `secrets.yml`（敏感配置，不入 git）

| 变量 | 说明 |
|------|------|
| `discord_bot_token` | Discord Bot Token |
| `finnhub_api_key` | Finnhub API Key |
| `deepl_api_key` | DeepL API Key |
| `fred_api_key` | FRED API Key |
| `changeip_username/password` | DDNS 账户 |
| `ai_engines.api_key_gemeni` | Gemini API Key |
| `ai_engines.api_key_nvidia` | NVIDIA API Key |
| `ai_engines.api_key_claude` | Claude API Key |
| `ai_engines.api_key_openai` | OpenAI API Key |
| `ai_engines.api_key_grop` | Groq/Grop API Key |
| `ai_engines.api_key_mistral`| Mistral API Key |

## 注意事项

- `ubuntu.key` 和 `sentinel.key` 已加入 `.gitignore`，绝不入 git
- `secrets.yml` 已加入 `.gitignore`，绝不入 git
- Bootstrap 只需执行一次，之后日常更新只用 Apply
- `sentinel.key` 由 Bootstrap 阶段自动生成在 `deploy/` 目录下
