import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router
from .config import get_settings
from .database import ensure_database_schema
from .scheduler import RuleScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="闲鱼商品筛选与邮件推送 API")
app.add_middleware(
    CORSMiddleware,
    # 本地 Next.js 管理界面使用浏览器直接请求 FastAPI。
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
scheduler = RuleScheduler()


@app.on_event("startup")
def startup() -> None:
    ensure_database_schema()
    scheduler.start()


@app.on_event("shutdown")
def shutdown() -> None:
    scheduler.shutdown()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": get_settings().app_name}
