# 闲鱼商品筛选与邮件推送实施计划

本文档记录项目从基础架构到完整业务链路的实施顺序和验证标准。LLM1、LLM2 的正式 Prompt 尚未确定，开发前期使用占位 Prompt 或 Mock Provider 验证整体架构。

## 阶段一：项目基础架构

目标：建立可运行的 FastAPI 项目骨架。

主要任务：

- 创建 `backend/app/` 分层目录；
- 实现 YAML 配置和环境变量读取；
- 使用 Pydantic Settings 校验配置；
- 建立统一日志系统和异常类型；
- 增加健康检查接口；
- 提供本地开发启动命令。

验证标准：应用可以启动；配置错误时启动失败；日志同时输出到控制台和文件。

## 阶段二：数据库和基础模型

目标：建立规则、商品、任务和推送记录。

核心表：

```text
watch_rules
listings
listing_conditions
task_runs
notification_states
notification_logs
llm_call_logs
```

主要约束：

- `listings.xianyu_item_id` 唯一；
- 一条规则支持多个邮箱；
- `notification_states` 保存规则、商品、邮箱的最近成功推送价格；
- `notification_logs` 保存每次实际发送记录；
- `task_runs` 保存每次任务的状态和统计信息；
- 商品、任务、解析和通知历史暂时永久保存。

验证标准：Alembic 可以创建和升级数据库；唯一约束有效；降价商品可以再次发送；同价或涨价商品不会重复发送。

## 阶段三：LLM 抽象层

目标：实现可替换 Provider 和模型的统一调用层。

建议目录：

```text
app/llm/
  base.py
  service.py
  schemas.py
  retry.py
  providers/
    openai.py
```

主要任务：

- 定义 Provider 统一接口；
- 从配置选择 Provider 和模型；
- 首版接入本地 Ollama API，默认模型为 `gemma4:latest`，Base URL 为 `http://127.0.0.1:11434`；
- 使用 Ollama `/api/chat` 接口和 Pydantic JSON Schema，不需要云端 API Key；
- 实现超时控制；
- 单个 LLM 阶段最多重试 2 次；
- 使用 JSON Schema/Pydantic 校验返回结果；
- 支持按配置切换备用 Provider；
- 记录 LLM 调用日志并脱敏。

验证标准：可以切换 Provider；错误 JSON 会重试；重试失败后抛出统一异常；业务代码不直接依赖 Provider SDK。

Ollama 的真实联调需验证本地服务、模型已下载、超时和结构化输出；本地服务不需要 API Key。SiliconFlow 适配器仅作为可选 Provider 保留。

## 阶段四：闲鱼采集模块

目标：将现有 Playwright 脚本封装为后台采集服务。

主要任务：

- 使用一条完整搜索语句，并在结果页逐页采集；
- 默认匿名访问；
- 遇到登录、验证码或安全验证时停止当前任务；
- 标准化商品字段；
- 保存 `raw_data.card_text`；
- 增加 `matched_search_queries`；
- 管理浏览器生命周期；
- 记录页面结构变化和访问错误。

验证标准：单条搜索语句可完成搜索；采集器在每次翻页前后随机等待；商品可以按 `xianyu_item_id` 去重；标题、价格、链接和卡片原文正确获取；访问受限时任务停止并写日志。

## 阶段五：LLM1 用户需求解析

目标：将前端输入转换为用户需求 JSON。

输入：

```text
商品名称
额外条件
预算
多个邮箱
执行频率
```

输出包含：

```text
keyword
search_query
price_range
conditions
original_input
```

验证标准：`search_query` 为一条非空完整搜索语句；`conditions` 为自然语言字符串数组；预算区间格式正确；原始输入完整保存；JSON Schema 校验通过。

## 阶段六：商品解析和后端规则筛选

目标：在进入 LLM2 前准备候选商品。

执行顺序：

```text
采集商品
→ 合并去重
→ 查询通知状态
→ 同价或涨价的已推送商品排除
→ 保留降价商品
→ 排除价格为空商品
→ 执行预算筛选
→ LLM1 解析剩余商品 conditions
```

同一商品按 `xianyu_item_id` 缓存 LLM1 商品解析结果；商品文本没有变化时不重复调用。

验证标准：同价商品不会进入 LLM2；降价商品可以重新进入；价格为空商品被排除；预算判断可重复；商品完整条件成功保存。补充条件中的正向和否定要求都由 LLM 参与比较。

## 阶段七：LLM2 排序和邮件推送

目标：筛选候选商品并自动发送邮件。

LLM2 输入：

```text
用户 conditions
xianyu_item_id
price
title
raw_data.card_text
seller_location
collected_at
商品 conditions
```

执行顺序：

```text
LLM2 返回结果
→ 取最多 5 个商品
→ 少于 5 个则全部发送
→ 合并成一封邮件
→ 每个商品单独记录发送结果
→ 更新最近成功推送价格
```

验证标准：不确定商品允许发送；邮件包含推荐理由和风险；邮件使用真实商品链接；部分商品失败时状态为 `partial_success`；成功发送后才更新通知状态。

## 阶段八：定时任务和任务控制

目标：实现自动运行、重试、停止和进度查询。

任务状态：

```text
pending
running
scraping
parsing
filtering
ranking
sending
success
partial_success
failed
stopped
```

主要规则：

- APScheduler 按规则创建任务；
- 默认每 3 分钟执行；
- 使用 `Asia/Shanghai` 时区；
- 同一规则禁止并行；
- 任务失败最多重试 3 次；
- 用户停止时立即取消当前任务，并阻止下一次调度；
- 记录阶段进度、统计数据和错误信息。

验证标准：前端可以查询任务状态；停止按钮可以取消任务；失败任务自动重试；三次失败后记录最终原因；定时器不会重复创建任务。

## 阶段九：FastAPI 接口和前端页面

后端接口至少包括：

```text
POST   /api/rules
GET    /api/rules
PUT    /api/rules/{rule_id}
POST   /api/rules/{rule_id}/start
POST   /api/rules/{rule_id}/stop
GET    /api/tasks/{task_id}
GET    /api/rules/{rule_id}/history
GET    /api/rules/{rule_id}/notifications
```

前端页面包括规则创建和编辑、多邮箱输入、预算和额外条件输入、规则启停、任务状态、最近执行结果、推送历史以及错误和风险信息。

## 阶段十：集成测试

验证完整链路：

```text
用户输入
→ 创建或编辑规则时由 LLM1 生成并保存需求 JSON
→ 单条搜索分页采集
→ 合并去重
→ 排除已推送商品
→ 预算筛选
→ LLM1 解析商品
→ LLM2 排序
→ 邮件发送
→ 写入通知记录
→ 下一轮正确跳过或识别降价商品
```

正式 Prompt 确定后，替换 Mock Provider 并完成真实 LLM、邮件和定时任务联调。
