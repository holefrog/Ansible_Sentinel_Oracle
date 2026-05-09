# Stock Sentinel - AI 驱动的市场情报哨兵

Stock Sentinel 是一个高度自动化、AI 驱动的美股市场情报监控与分析平台。它 7x24 小时不间断地聚合、分析、过滤和分发来自全球的金融新闻和市场数据，旨在为投资者提供**及时、精准、可行动的决策支持**。

基于“Vibe Coding”和“配置驱动”理念设计，系统将繁杂的爬虫、API 对接、提示词工程与事件分发收敛于高度模块化的 Python Agent 中。借助 Ansible 自动化部署，它能轻量化部署在任何 VPS 节点上，实现双重渠道（Discord 和 Hugo 静态站）的投研简报触达。

---

## 🏗️ 核心系统架构 (System Architecture)

Stock Sentinel 的架构设计可划分为以下四个主要子系统：

### 1. 数据采集引擎 (Data Ingestion Layer)
系统不依赖单一数据源，通过多个专门的引擎模块实现多模态数据的抓取：
- **`data_engine.py`**: 对接 Finnhub API 获取实时行情、个股日线级别均量（10-Day Volume）和华尔街一致预期（目标价）。对接 FRED 获取宏观指标，以及 Alternative.me 获取恐慌与贪婪指数。
- **`news_engine.py` / `brief.py`**: 支持 RSS 源解析以及原网页的深度抓取。对于无法直接拉取全文的网页，会自动降级使用 `Jina API` 提取正文内容。

### 2. 状态机与缓存层 (Storage & Caching Layer)
- **本地 SQLite (`data_store.py` / `news_dedup.py`)**: 采用 SQLite 数据库存储历史新闻、去重缓存，并建立轻量级的持久化状态，避免重复处理相同新闻。同时用于统计大模型 (LLM) 调用的 token 数和成本。
- **内存级别缓存 (`config.py`)**: 高频读取的配置文件在加载后采用 LRU 缓存，保障读取性能；支持线程安全地动态修改自选股池。

### 3. AI 分析与网关层 (LLM Intelligence Layer)
- **`llm_gateway.py`**: 所有大语言模型请求的统一路由网关。支持动态 API 鉴权，并在 `settings.toml` 中定义多模型备选队列（Gemini, Claude, Groq, NVIDIA 等）。当某个引擎限流或失败时，网关会自动降级无缝切换到下一个引擎。
- **`advisor.py` (Event-Driven Agent)**: 事件驱动的私人量化投顾模块。根据个股异动情况动态组装提示词，包含仓位上下文、技术指标与公司新闻，并向大语言模型请求即时诊断。
- **多语言处理 (`translator.py`)**: 利用 DeepL 或底层 LLM 提供的翻译功能，将英文原始金融资讯转译为结构清晰的中文总结。

### 4. 交付与渲染引擎 (Delivery & Rendering Layer)
- **Discord Bot (`bot.py` / `discord_utils.py`)**: 作为控制中枢和第一交付出口，提供斜杠 (`/`) 指令交互功能。
- **Markdown / Hugo 生成器 (`renderer.py`)**: 将投研报告或新闻详述渲染为标准 Markdown 格式并持久化到文件系统，供 Hugo 构建静态页面使用。

---

## ⚙️ 主要算法与处理流 (Algorithms & Pipelines)

### 1. 资讯提纯与去重算法 (News Deduplication & Filtering)
- **多维度指纹去重**: 基于新闻的 `GUID` 和 `URL` 生成唯一特征值存入 SQLite。每次扫描只抓取未进入数据库的增量条目。
- **AI 相关性打分**: 对新采集的资讯，`news_scanner` 将调用大模型针对当前《自选股监控池》对其做 1-10 分的相关性与重要性打分。只有达到设定阈值（例如 7 分以上）的新闻，才会触发翻译和长文解析。
- **正文降级解析策略**: 若源站开启了反爬策略导致直抓取失败，算法会自动采用 Jina Reader 接口解析长文；若仍然获取受阻，则回落（Fallback）至抓取新闻自带的简短 Summary。

### 2. 异动监控与信号生成 (Market Anomaly Detection)
- 在 `market_scan.py` 中，系统定时（如每 5 分钟）比对个股**当前价格与昨收价**的偏差值（涨跌幅）以及**当前成交量与历史 10 日均量**的比值（量比）。
- 设定了双重过滤条件：
  1. 价格涨跌幅超过阈值（如 `alert_threshold_pct = 2.5%`）。
  2. 成交量出现峰值（如 `volume_spike_threshold = 1.5x`）。
- 捕捉到上述指标后，会触发 `advisor.py` 开启量化综合分析，进行事件驱动预警。

### 3. LLM 多引擎轮询与降级 (Fallback & Failover Mechanism)
- 网关采用带有优先级（Priority）排序的模型队列。通过 `get_today_usage` 限制单模型每日请求额度（Quota）。当首选引擎发生超时、鉴权失败或配额耗尽时，算法捕获异常并抛弃当前调用，沿着优先级数组顺延调用备用模型，确保系统 7x24 小时绝对高可用。

---

## 💡 设计意图与工程哲学 (Design Intentions)

- **无侵入与低依赖的 "Vibe Coding"**:
  所有核心组件尽量使用 Python 原生库（如 SQLite）或轻量级三方库实现。不在目标机器上安装笨重的 Redis 或 MySQL，确保环境初始化开销极低。
- **配置与代码绝对隔离**:
  将所有容易变更的逻辑（如 Prompt、监控阈值、分发频道和 API Keys）完全抽离至 `settings.toml` 和 `watch_list.toml` 中，保证主流程代码在不修改源码的情况下可任意适配新资产、新场景。
- **Agent 的上下文感知 (Context-Awareness)**:
  `advisor.py` 故意被设计为带“记忆”的分析师。它会查阅 `watch_list.toml` 中定义的**用户平均持仓成本和数量**，从而在报告中能够给出“止损”、“加仓”或“套现”等确切结论，而非虚无缥缈的泛泛之谈。

---

## 🛠️ 实现方法 (Implementation Details)

### Systemd 替代传统常驻进程
为了保障资源的高效利用，避免内存泄露以及应对断网重连等情况，系统在实现上将长线任务拆解：
- `bot.py` 仅作为 Discord 的命令接收器与心跳服务运行（常驻）。
- 各类扫描（新闻 `news_scanner`、市场 `market_scan`）和报告生成（早报/晚报）均依托 Linux **Systemd Timers** 触发，作为一次性脚本执行。执行完毕进程立刻销毁，释放内存。

### Ansible 声明式部署
在 `deploy/` 目录中：
- 将环境初始化分为 `Bootstrap`（预装 Nginx, 防火墙等基建并创建非 root 用户）和 `Apply`（代码同步、依赖安装与 Systemd 服务注册）。
- 利用 Handlers 固化 Iptables 规则，不使用 UFW，避开了云环境（特别是 Oracle Cloud）下常见的多重防火墙冲突。

### SQLite 并发考量
在各个脚本同时被 Timer 唤醒时（如 7:30 既在扫描新闻又在生成早报），为规避同时对本地 `sentinel.db` 读写造成的问题，系统刻意在每个流程开启时实行按需短连接（`connect` 后即时 `close`），保证在多子进程形态下的数据库原子性。

---

## 📁 项目结构

```text
.
├── agent/                    # 核心 Python Agent
│   ├── bot.py                # Discord Bot 控制台
│   ├── report.py             # 早晚报聚合与发布
│   ├── news_scanner.py       # 实时新闻抓取与打分
│   ├── market_scan.py        # 实时市场价格与成交量监控
│   ├── advisor.py            # Event-driven 私人量化投顾
│   ├── config.py             # LRU 配置加载器
│   ├── data_engine.py        # 市场数据源对接（Finnhub, FRED）
│   ├── llm_gateway.py        # 多模型聚合降级网关
│   ├── renderer.py           # Markdown/Embed 渲染器
│   └── ...                   # 其他工具与存储模块
├── config/                   # 业务配置存放地
│   ├── settings.toml         # 全局不可见核心设定（API、定时、模型优先度）
│   └── watch_list.toml       # 自选股池与持仓账本
├── deploy/                   # Ansible 运维配置
│   ├── apply.sh              # 一键部署交互入口
│   ├── bootstrap.yml         # 初始化 Playbook
│   ├── site.yml              # 日常发布 Playbook
│   └── roles/                # 各类基建定义 (Nginx, Systemd, VPN 等)
└── hugo/                     # 静态报告托管目录
```

## 🚀 部署与使用

1. 在 `deploy` 目录配置好 `inventory.yml` 与 `secrets.yml`。
2. 运行 `bash apply.sh` 按照提示执行 Bootstrap，再执行 Apply。
3. 进入 Discord，输入 `/watch add ticker=NVDA` 即可将股票添加入监控池，等待下一次 Systemd Timer 唤醒即可自动化享受到 AI 研报推送服务！