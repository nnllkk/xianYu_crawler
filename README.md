# 闲鱼商品监控与邮件推送

这是一个在本机运行的闲鱼商品监控工具。用户在网页中配置商品、补充条件、预算和收件邮箱后，系统会定时检索闲鱼列表页，筛选未推送过的候选商品，并把推荐结果发送到邮箱。

项目使用 FastAPI、MySQL、Playwright、Ollama 和 Next.js。商品采集应遵守闲鱼的平台规则；出现登录、验证或非法访问时，任务会记录失败原因并停止。

详细的技术设计、数据结构和筛选流程见 [PROJECT_DESIGN.md](PROJECT_DESIGN.md)。

## 闲鱼登录状态与多账号

采集使用 Playwright `storage_state` JSON，不使用浏览器 profile，也不保存闲鱼密码。在前端“闲鱼账号”标签页点击“添加账号”即可打开可见登录窗口；完成登录后，系统会自动检测并保存状态。默认优先启动本机 Chrome，无法启动时回退到 Playwright Chromium。每次添加账号会自动生成账号名称。状态文件保存在 `backend/data/xianyu_states/`，已被 Git 忽略。

执行数据库迁移后，账号会自动登记为可用状态；也可以通过 `/api/xianyu-accounts` 查看和启停账号。任务只使用存在状态文件且处于 `active` 的账号。若当前会话触发登录、验证码或非法访问，账号会进入冷却，任务尝试下一个已授权账号；所有账号不可用时任务停止并记录原因。账号切换用于管理多个已获授权的登录会话，不用于规避平台验证。

迁移命令（需先确认 `.env` 中的 `DATABASE_URL` 可连接）：

```bash
.venv/bin/alembic upgrade head
```

## 首次准备

需要已安装并运行：MySQL、Ollama、Python 3.11+、Node.js 与 npm。

1. 在 MySQL 中创建数据库 `xianyu_filter`。
2. 基于 `.env.example` 创建本地 `.env`，填写 MySQL 和 SMTP 配置。
3. 安装后端与前端依赖，并安装 Playwright 浏览器：

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/playwright install chromium
(cd frontend && npm install)
```

4. 确保本地模型已就绪：

```bash
ollama pull gemma4:latest
```

## LLM 配置

LLM 配置位于 `backend/config.yaml` 的 `llm` 节点。`default_provider` 和 `default_model` 是全部任务的默认值；任务没有指定 `provider` 或 `model` 时会继承它们。

```yaml
llm:
  default_provider: ollama
  default_model: gemma4:latest
  tasks:
    user_requirement:
      context_length: 4096
    item_assessment:
      context_length: 4096
    candidate_ranking:
      context_length: 16384
  providers:
    ollama:
      type: ollama
      base_url: http://127.0.0.1:11434
```

支持将不同任务路由到不同 Provider。例如，把候选排序交给 SiliconFlow，而其他高频任务继续使用本地 Ollama：

```yaml
llm:
  default_provider: ollama
  default_model: gemma4:latest
  tasks:
    candidate_ranking:
      provider: siliconflow
      model: Qwen/Qwen3-32B
      context_length: 16384
  providers:
    ollama:
      type: ollama
      base_url: http://127.0.0.1:11434
    siliconflow:
      type: siliconflow
      base_url: https://api.siliconflow.cn/v1
      api_key_env: SILICONFLOW_API_KEY
```

`providers` 中的名称由任务的 `provider` 字段引用，`type` 当前支持 `ollama`、`siliconflow` 和测试用的 `mock`。可为同一类型定义多个不同名称的实例，以连接不同地址。SiliconFlow 的密钥不写入 YAML，而是写入 `.env`：

```dotenv
SILICONFLOW_API_KEY=your-api-key
```

`retry_count` 是首次请求之外的重试次数，默认 `3`；`retry_delay_seconds` 默认 `5`，请求失败后的等待依次为 `5`、`10`、`15` 秒。修改 `backend/config.yaml` 后需要重启后端服务。

## 启动

首次为脚本增加执行权限：

```bash
chmod +x start.sh
```

之后在项目根目录一键启动：

```bash
./start.sh
```

脚本会自动执行数据库迁移，再启动前端和后端。启动成功后访问：

- 前端工作台：[http://127.0.0.1:3000](http://127.0.0.1:3000)
- 后端接口文档：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

按 `Ctrl+C` 会同时停止两个服务。运行日志保存在 `.run/backend.log` 与 `.run/frontend.log`。

## 登录状态诊断

当任务提示没有可用登录状态时，可单独验证状态 JSON 是否在采集器的 Playwright 环境中被风控拦截。脚本不会输出 Cookie 内容，也不会修改账号、数据库或状态文件：

```bash
.venv/bin/python backend/scripts/verify_xianyu_login_state.py account-1 --query "MacBook M1 Pro"
```

后台默认使用可见浏览器；诊断脚本默认复现无头模式，需要验证后台实际行为时增加 `--headed`。

## 使用

1. 在前端填写商品名称、补充条件、预算、采集页数和一个或多个收件邮箱；不接受的情况也直接写进补充条件，例如“不要维修机”。
2. 创建并启用规则；系统按 10 至 30 分钟的随机间隔自动执行。
3. 在运行记录中查看每次任务的采集、候选和推送数量；失败任务可点击“查看原因”。
