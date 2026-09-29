# 闲鱼商品监控与邮件推送

这是一个在本机运行的闲鱼商品监控工具。用户在网页中配置商品、补充条件、预算、排除词和收件邮箱后，系统会定时检索闲鱼列表页，筛选未推送过的候选商品，并把推荐结果发送到邮箱。

项目使用 FastAPI、MySQL、Playwright、Ollama 和 Next.js。商品采集应遵守闲鱼的平台规则；出现登录、验证或非法访问时，任务会记录失败原因并停止。

详细的技术设计、数据结构和筛选流程见 [PROJECT_DESIGN.md](PROJECT_DESIGN.md)。

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

## 使用

1. 在前端填写商品名称、补充条件、预算、排除词和一个或多个收件邮箱。
2. 创建并启用规则；系统按 10 至 30 分钟的随机间隔自动执行。
3. 在运行记录中查看每次任务的采集、候选和推送数量；失败任务可点击“查看原因”。
