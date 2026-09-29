# 闲鱼商品实时筛选与邮件推送

## 项目目标

定时获取闲鱼商品数据，根据用户配置的关键词、价格区间和排除词筛选商品；对新商品去重后，通过邮件推送筛选结果。

数据获取应遵守闲鱼平台规则，优先使用已获授权的数据接口或用户可合法访问的数据源。

## 已确定技术栈

| 模块 | 技术选型 | 职责 |
| --- | --- | --- |
| 前端 | Next.js + TypeScript + Tailwind CSS | 配置筛选规则，查看商品与推送记录。 |
| 后端 | Python 3.11+ + FastAPI | 提供 REST API，处理规则管理、商品筛选和邮件触发。 |
| 数据库 | 本地 MySQL 8.x | 保存筛选规则、商品快照和通知记录。 |
| ORM 与迁移 | SQLAlchemy 2.x + Alembic | 访问 MySQL 并维护数据库结构变更。 |
| 数据校验 | Pydantic v2 | 定义 FastAPI 的请求、响应和配置模型。 |
| 数据采集 | Playwright Python | 获取动态页面数据；具体采集方式需遵守平台规则。 |
| 定时调度 | APScheduler | 在 FastAPI 服务中按固定频率执行采集与筛选任务。 |
| LLM | 本地 Ollama + Gemma 4 | 通过 `gemma4:latest` 执行用户需求解析、商品属性提取和候选比较。 |
| 邮件推送 | SMTP 或 Resend API | 向目标邮箱发送新商品通知。 |
| 前后端通信 | REST API + OpenAPI | 前端调用后端接口；FastAPI 自动提供 `/docs` 调试文档。 |

## 运行约束

- 开发阶段直接在本地运行前端与 FastAPI 服务，暂不使用 Docker Compose。
- 数据库使用本机已安装的 MySQL 实例，不在项目中启动 MySQL 容器。
- 首版按单用户设计，支持配置多个收件邮箱；调度器每分钟检查，规则实际每 10～30 分钟随机执行一次。

## 当前后端运行

前端暂不实现。准备本地 MySQL 数据库后，在项目根目录执行：

```bash
uv venv --allow-existing .venv
uv pip install --python .venv/bin/python -r backend/requirements.txt
export DATABASE_URL='mysql+pymysql://用户名:密码@127.0.0.1:3306/xianyu_filter'
export LLM_PROVIDER=ollama
export OLLAMA_BASE_URL='http://127.0.0.1:11434'
export OLLAMA_MODEL='gemma4:latest'
# 如果 Playwright 自动下载浏览器不可用，可指定本机 Chromium 可执行文件
# export PLAYWRIGHT_EXECUTABLE_PATH='/path/to/chromium'
.venv/bin/python -m uvicorn app.main:app --app-dir backend --reload
```

启动后可访问 `GET /health` 和 FastAPI 的 `/docs`。规则通过 `POST /api/rules` 创建，调用 `POST /api/rules/{rule_id}/run` 立即执行；定时任务会自动执行已启用规则。首次建库或升级使用 Alembic：

```bash
.venv/bin/alembic -c alembic.ini upgrade head
```

默认使用本地 Ollama 的 `gemma4:latest`，不需要 API Key。Ollama 服务地址为 `http://127.0.0.1:11434`。本地无模型时先执行 `ollama pull gemma4:latest`。

本地无模型或只想快速测试时可将 `LLM_PROVIDER=mock`，使用内置 Mock Provider 验证 JSON 契约和任务编排。SiliconFlow 适配器仍保留为可选 Provider，但不再是默认方案。

离线运行基础测试：

```bash
.venv/bin/python -m pytest -q backend/tests
```

## 建议后端依赖

```text
fastapi
uvicorn[standard]
sqlalchemy
pymysql
alembic
pydantic-settings
apscheduler
playwright
httpx
openai
resend
```

当邮件使用 SMTP 时，可不安装 `resend`。

## 建议目录

```text
backend/
  app/
    main.py           # FastAPI 应用入口
    api/              # 商品、规则、通知接口
    models/           # SQLAlchemy 模型
    schemas/          # Pydantic 请求与响应模型
    services/         # 筛选、邮件和采集逻辑
    tasks/            # APScheduler 定时任务
    database.py       # MySQL 连接与 Session
  alembic/
  requirements.txt
  .env
```

## 解耦与配置约束

项目采用分层和模块化设计。业务流程不得直接依赖具体 LLM Provider、模型 SDK、邮件 SDK 或 Playwright 页面选择器；每个外部能力通过独立服务接口封装，便于替换实现和单元测试。

### LLM 层

LLM 相关代码统一放在 `app/llm/`，至少包含以下边界：

```text
app/llm/
  base.py              # 统一 LLM 客户端接口和请求结果模型
  service.py           # LLM1/LLM2 业务无关的调用编排
  schemas.py           # JSON Schema/Pydantic 输出模型
  retry.py             # 超时、重试、递增等待和错误分类
  providers/
    openai.py          # OpenAI 兼容 Provider 适配器
    ...                 # 其他 Provider 适配器
```

业务层只依赖统一接口，例如“生成结构化结果”或“比较候选商品”，不能直接导入某个 Provider 的 SDK。LLM 层内部负责：

- 根据配置选择 Provider 和模型；
- 统一请求参数、超时和 Token 限制；
- 对临时网络错误、限流和服务端错误执行重试；
- 对模型返回内容执行 JSON Schema/Pydantic 校验；
- 格式错误时按配置重新请求，必要时切换备用 Provider 或模型；
- 记录调用耗时、重试次数、模型名称和错误类型，但不记录 API Key、Cookie 或完整敏感输入。

LLM1 和 LLM2 只提供不同的业务输入输出 Schema 与 Prompt 配置，不在业务代码中实现 Provider 分支判断。

首版默认使用本地 Ollama 的 Chat API：

- API Base URL：`http://127.0.0.1:11434`；
- 默认模型：`gemma4:latest`；
- 请求接口：`POST /api/chat`；
- 结构化输出：传入 Pydantic 生成的 JSON Schema，最终仍经过本地校验；
- 不需要云端 API Key。

示例调用：

```python
import httpx

response = httpx.post(
    "http://127.0.0.1:11434/api/chat",
    json={
        "model": "gemma4:latest",
        "messages": [{"role": "user", "content": "..."}],
        "stream": False,
        "format": "json",
    },
)
content = response.json()["message"]["content"]
```

### 配置文件

项目通过配置文件统一管理运行参数，代码中不硬编码 Provider、模型、重试次数、任务频率或邮件配置。建议使用 YAML 配置非敏感参数，并使用环境变量覆盖密钥和部署环境参数：

```yaml
app:
  timezone: Asia/Shanghai

llm:
  default_provider: ollama
  retry_count: 2
  timeout_seconds: 60
  tasks:
    llm1_user_parse:
      provider: ollama
      model: gemma4:latest
    llm1_product_parse:
      provider: ollama
      model: gemma4:latest
    llm2_rank:
      provider: ollama
      model: gemma4:latest
  providers:
    ollama:
      base_url: http://127.0.0.1:11434
      model: gemma4:latest

scheduler:
  interval_minutes: 15
  max_task_retries: 3

mail:
  provider: smtp
```

邮箱密码等秘密只通过环境变量或本地秘密配置注入，不提交到 Git。配置读取后使用 Pydantic Settings 校验；配置错误应在应用启动时立即报错，而不是等到定时任务运行时才发现。

## 首版数据边界

首版至少包含以下数据实体：

- `watch_rules`：关键词、价格区间、排除词和启用状态。
- `listings`：采集到的商品快照；`xianyu_item_id` 应建立唯一索引，避免重复推送。
- `notifications`：已发送的邮件通知及其对应商品。

后续可在首版稳定后补充多规则合并与更细的推送频率限制。

## 监控规则输入

前端将一次筛选保存为一条监控规则。首版提供以下通用字段：

| 字段 | 是否必填 | 说明 |
| --- | --- | --- |
| 商品名称 | 是 | 主要搜索词，例如 `MacBook M1 Pro`。 |
| 额外条件 | 否 | 自由文本，例如 `16寸 32G+512G`、`未拆封` 或 `M码 黑色`。 |
| 预算 | 否 | 支持价格区间、最低价或最高价限制。 |
| 排除词 | 否 | 命中商品标题、描述或可取得属性时排除，例如 `维修`、`配件`。 |
| 邮件推送 | 否 | 启用后，对未推送过的匹配商品发送邮件。 |

预算的输入与含义如下：

| 输入 | 含义 |
| --- | --- |
| `6000-8000` | `6000 <= 价格 <= 8000`。 |
| `6000-` | `价格 >= 6000`。 |
| `-8000` | `价格 <= 8000`。 |
| 留空 | 不限制价格。 |

所有符合规则的商品均按价格从低到高排序；预算留空时，最低价商品优先展示。

## 跨品类条件解析与匹配

不同品类的属性不同，首版不为电脑、玩偶或衣服等品类预设固定数据库列。规则保留用户原始输入，并由小型 LLM 将用户要求整理为自然语言 `conditions` 字符串数组；商品解析也使用相同字段保存商品的全部可用信息。

例如，电脑用户条件可以表示为：

```json
[
  "屏幕尺寸16寸",
  "内存至少32GB",
  "存储至少512GB",
  "M1 Pro芯片",
  "外观成色较好",
  "无拆修"
]
```

其中 `32G+512G` 应整理为“内存至少32GB”和“存储至少512GB”。玩偶和衣服等品类仍使用同一字符串数组，可表达品牌、系列、尺寸、颜色、尺码和成色等信息。

用户只需在前端填写商品名称、额外条件、预算、邮箱等信息，不需要确认或修改模型解析结果。系统保留用户原始输入和模型输出，便于后续排查和调整。

## 完整搜索与自动推送流程

一条监控规则的执行流程如下：

```text
用户创建或编辑规则
  -> LLM1 解析用户输入
  -> 输出结构化筛选条件 + 3 条搜索语句
  -> 在同一个浏览器上下文中串行打开 3 个闲鱼搜索页，搜索之间随机等待
  -> 标准化商品数据，按 xianyu_item_id 合并去重并排除已成功推送商品
  -> LLM1 解析去重后的商品列表属性
  -> 后端去重、排除已推送商品并检查预算
  -> LLM2 对通过规则的候选商品筛选、比较并排序
  -> 取排序后的 Top 5
  -> 自动将 Top 5 商品链接发送到用户邮箱
  -> 记录发送结果，防止后续重复推送
```

### LLM1 输出

LLM1 对用户输入生成两个结果：

1. 自然语言 `conditions` 条件列表，例如“内存至少32GB”“存储至少512GB”“外观成色较好”。
2. 三条语义相近但表达不同的闲鱼搜索语句，用于覆盖不同卖家的标题写法。

三条搜索语句在同一个浏览器上下文中串行执行，默认每条之间随机等待 5～15 秒；所有结果合并后仅保留唯一的 `xianyu_item_id`。去重后，后端根据通知记录排除当前规则和邮箱已经成功推送过的商品，再把剩余商品交给 LLM1，避免对已推送商品重复解析和重复发送。随后仍由 LLM1 使用统一的属性命名，从每个商品的 `title` 和 `raw_data.card_text` 中提取商品属性。LLM1 的两次调用都只负责生成或提取结构化信息，不直接发送邮件。

## 三个模型与后端的接口边界

具体 Prompt 另行确定；当前先固定输入输出职责和数据结构。

### LLM1 解析结果

LLM1 第一次解析用户输入，输出：

```json
{
  "category": "computer",
  "search_queries": ["MacBook M1 Pro 16寸 32G 512G", "MacBook Pro M1 Pro 16GB 512GB", "苹果 M1 Pro 16英寸 32GB 512固态"],
  "conditions": [
    "屏幕尺寸16寸",
    "内存至少32GB",
    "存储至少512GB",
    "M1 Pro芯片",
    "外观成色较好",
    "无拆修"
  ],
  "exclude_keywords": [],
  "price_range": {
    "min": 6000,
    "max": 8000
  },
  "original_input": {
    "product": "MacBook M1 Pro",
    "extra_conditions": "16寸 32G+512G",
    "budget": "6000-8000"
  }
}
```

`search_queries` 必须恰好包含三条可直接输入闲鱼搜索框的语句。`conditions` 是自然语言字符串数组，表达用户关心的商品属性和要求；`exclude_keywords` 保存明确排除词。用户原始输入也必须保留，不能只保存模型结果。

LLM1 第二次解析商品列表，为每个未推送商品输出从标题和原始卡片文本中提取的全部可用商品信息：

```json
{
  "xianyu_item_id": "1066277990223",
  "title": "特价 2020款M1 13寸 高配 MacBook Pro Apple 二手苹果笔记本电脑 A2338 16+256 银色 企业管理机",
  "price": 2988.0,
  "url": "https://www.goofish.com/item?id=1066277990223&categoryId=126854525",
  "image_url": "https://img.alicdn.com/imgextra/example.jpg",
  "seller_name": null,
  "seller_location": "广东",
  "description": null,
  "source_keyword": "MacBook M1 Pro",
  "collected_at": "2026-09-28T09:28:22.560612+00:00",
  "raw_data": {
    "card_text": "特价 2020款M1 13寸 高配 MacBook Pro Apple 二手苹果笔记本电脑 A2338 16+256 银色 企业管理机...\\n¥\\n2988\\n10人想要\\n\\n广东\\n\\n回复超快",
    "price_text": "¥\\n2988\\n10人想要"
  },
  "conditions": [
    "Apple品牌",
    "MacBook Pro型号",
    "M1芯片",
    "13寸屏幕",
    "内存16GB",
    "存储256GB",
    "银色",
    "企业管理机",
    "全原无修",
    "外观成色一般"
  ],
  "extraction_status": "complete"
}
```

商品解析结果必须保留爬虫返回的全部原始标准字段：

- `xianyu_item_id`：商品唯一 ID，用于去重和通知记录关联；
- `title`：商品标题及列表页展示的主要描述；
- `price`：代码从页面价格节点提取的当前价格；
- `url`：商品详情页链接；
- `image_url`：列表页商品图片链接；
- `seller_name`：当前列表页未稳定提供时为 `null`；
- `seller_location`：卖家地区；
- `description`：当前列表页未提供独立描述时为 `null`；
- `source_keyword`：本次搜索使用的商品关键词；
- `collected_at`：采集时间；
- `raw_data`：原始卡片文本和原始价格文本。

LLM1 在这些原始字段之上新增 `conditions`，表示从 `title` 和 `raw_data.card_text` 中提取的全部可用商品信息，而不是只提取用户当前关心的字段。无法确认的信息不得臆造；无法归类但有价值的原文可以作为独立字符串保留。`extraction_status` 用于表示解析是否完整。

### 后端确定性筛选规则

后端先执行可重复、可审计的规则，只有通过规则的商品才会交给 LLM2：

- `xianyu_item_id` 有效，且同一轮三条搜索结果只保留一条；
- 当前规则和目标邮箱已经成功推送过、且当前价格没有低于上次成功推送价格的商品直接排除；
- 价格满足 `price_range.min` 和 `price_range.max`，价格缺失时不能通过预算筛选；
- 标题或 `raw_data.card_text` 命中排除词时排除；
- 商品数据不完整、已下架或链接无效时排除；
- 对代码可直接判断的条件进行筛选；无法仅靠代码确认的条件交给 LLM2 判断。

无法从商品文本确认的条件，不由后端猜测；保留给 LLM2 判断，并要求 LLM2 标记不确定性。后端不根据 LLM2 的自由文本重新推导预算或修改用户条件。

### LLM2 筛选输入信息

LLM2 不需要接收商品全部原始字段。每个候选商品只传递以下信息：

- 用户需求 JSON 中的 `conditions`；
- `xianyu_item_id`：用于将 LLM2 结果映射回原始商品；
- `price`：商品价格；
- `title`：商品标题；
- `raw_data.card_text`：用于核对 LLM1 是否遗漏信息；
- `seller_location`：卖家地区；
- `collected_at`：商品采集时间；
- 商品解析后的 `conditions`：商品全部可用信息。

单个候选商品传给 LLM2 的数据结构如下：

```json
{
  "xianyu_item_id": "1066277990223",
  "price": 2988.0,
  "title": "xxx",
  "raw_data": {
    "card_text": "xxxx"
  },
  "seller_location": "广东",
  "collected_at": "2026-09-28T09:28:22.560612+00:00",
  "conditions": [
    "Apple品牌",
    "MacBook Pro型号",
    "M1芯片",
    "13寸屏幕",
    "内存16GB",
    "存储256GB",
    "企业管理机",
    "全原无修",
    "外观成色一般"
  ]
}
```

`url`、`image_url` 等字段保留在后端商品记录中，不需要发送给 LLM2。LLM2 返回 `xianyu_item_id`、排序、匹配理由和风险后，后端根据 ID 取回原始商品链接并发送邮件。

LLM2 输出每个候选商品的是否推荐、匹配分数、排序、理由和风险，并返回最多 5 个推荐商品。LLM2 不接收或修改数据库推送状态，也不负责发送邮件；发送仍由后端完成。

### LLM2 筛选与推送

后端先执行商品去重、排除已推送商品和预算范围检查，再将通过规则的商品与用户 `conditions`、商品 `conditions` 交给 LLM2。LLM2 负责品类属性匹配、成色和描述风险比较、综合排序，并返回最多 5 个商品及推荐理由。

LLM2 完成排序后不等待用户勾选，系统直接把 Top 5 未推送商品链接发送到规则配置的邮箱。邮件发送成功后，才将对应的规则、商品和邮箱写入通知记录；同价或涨价商品不得重复发送，降价商品允许再次发送。排除已推送商品发生在 LLM1 商品属性解析之前。

### 定时后台任务

每条规则包含启用状态和执行频率。APScheduler 定时触发启用规则，后台任务执行上述完整流程；任务不能因为一次采集或邮件失败而被标记为成功。

前端提供：

- 规则的开启和停止按钮；
- 执行频率配置；
- 当前任务状态：等待、运行中、成功、失败；
- 最近一次执行时间、下一次执行时间和错误信息；
- 最近一次候选商品与推送结果。

建议将每次执行保存为一条任务记录，至少包含 `rule_id`、任务状态、开始和结束时间、三个搜索语句、采集数量、去重数量、候选数量、推送数量和错误信息。前端通过 FastAPI 查询任务状态，不需要长时间保持一个搜索请求。

## 已确认的运行规则

- 支持为同一条规则配置多个邮箱；同一商品可以分别发送到多个邮箱。
- 新建规则第一次执行时，推送当前搜索结果中符合条件的商品，不限制商品必须在规则创建之后发布。
- 商品降价后允许再次推送。同一规则、商品和邮箱需要保存最近一次成功推送价格；只有当前价格低于最近成功推送价格时才再次发送，价格相同或上涨不重复发送。每次实际发送仍单独保留通知记录。
- 当前价格缺失的商品不进入预算筛选和邮件推送。
- 一个定时任务对应一次用户输入规则；同一规则不允许并行执行。
- 任务失败后自动重试，默认最多 3 次；3 次全部失败后记录最终失败原因，并将任务状态设为 `failed`。
- LLM1 解析商品前，必须先完成商品合并去重和已成功推送商品排除；不对已经排除的商品调用 LLM1。
- 候选商品少于 5 个时全部发送；LLM2 标记为不确定的商品也允许发送，最终由用户判断。邮件中包含推荐理由和风险提示。
- 首版使用匿名闲鱼访问。若页面要求登录、验证码或安全验证，记录原因并停止当前任务，不尝试绕过限制。
- 首版只使用搜索列表页字段，不访问商品详情页。
- 商品、解析结果、任务记录、日志和通知记录暂时永久保存，供个人使用和问题排查。
- 默认使用 `Asia/Shanghai` 时区；调度器每分钟轮询，规则每次执行后随机安排 10～30 分钟后的下一次执行；用户停止任务时立即取消当前执行，并阻止下一次调度。
- 多邮箱使用邮箱数组配置；每个邮箱独立保存发送状态和最近成功推送价格。
- 三条搜索语句的来源保存在商品的 `matched_search_queries` 中；同一商品的 LLM1 解析结果按 `xianyu_item_id` 缓存复用。
- 候选不足 5 个时全部发送；没有候选商品时不发送空邮件，只记录本轮任务成功且候选数为 0。

## 重试、校验与日志

所有任务、采集、LLM 调用、筛选和邮件发送过程都写入统一日志，至少记录 `rule_id`、任务 ID、阶段、重试次数、耗时、结果和错误原因；日志中不得写入邮箱密码、登录 Cookie 或 LLM API 密钥。

LLM1 和 LLM2 的返回值必须先经过 JSON Schema/Pydantic 校验，再进入后续流程。JSON 格式错误、必需字段缺失或枚举值非法时，只重试当前 LLM 阶段最多 2 次；仍然失败则视为本次任务失败，并交由任务级最多 3 次重试处理。模型返回的原始内容和校验错误摘要写入任务日志，便于定位 Prompt 或模型问题。

任务重试应使用递增等待时间，并为同一规则保留任务锁，防止重试与下一次定时触发同时执行。邮件发送按商品分别记录成功或失败状态，但 Top 5 仍合并为一封邮件发送；任务最终状态可以是 `success`、`partial_success` 或 `failed`。

## 采集验证脚本

`backend/scripts/search_xianyu.py` 用于在接入筛选、去重和邮件流程前，验证闲鱼搜索页面实际可提供的字段。脚本启动后询问商品搜索词，向网页搜索框输入该词，滚动加载当前可见的商品结果，并将商品数组输出到标准输出。

```bash
python3.11 -m pip install -r backend/requirements.txt
python3.11 -m playwright install chromium
python3.11 backend/scripts/search_xianyu.py
```

也可以直接传入搜索词：

```bash
python3.11 backend/scripts/search_xianyu.py "MacBook M1 Pro"
```

输出中的 `xianyu_item_id`、`title`、`price`、`url`、`image_url`、卖家信息、采集时间和 `raw_data.card_text` 均来自当前页面实际可读数据；页面没有提供的字段输出 `null`。脚本不会绕过登录、验证码或访问限制，检测到限制时会以非零状态码报告真实原因，而不会将空数组误报为成功结果。
