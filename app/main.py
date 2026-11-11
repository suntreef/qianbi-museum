"""FastAPI 入口：API 路由 + 页面路由 + 静态资源 + 照片访问。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import models
from .api import router as api_router
from .auth import ensure_default_admin
from .batch import start_worker
from .config import LLM_API_KEY, PHOTO_DIR, STATIC_DIR
from .seeds.dict_seed import load_seeds
from .services import ocr
from .webui import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    models.init_db()
    load_seeds()
    ensure_default_admin()
    start_worker()
    yield


app = FastAPI(title="钱币博物馆藏品入库系统", lifespan=lifespan)
app.include_router(web_router)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media", StaticFiles(directory=str(PHOTO_DIR)), name="media")


@app.get("/api/health")
def health():
    """部署后先访问这个确认服务正常、API Key 已配置（可选）、OCR 可用。"""
    return {
        "status": "ok",
        "llm_configured": bool(LLM_API_KEY),
        "ocr_available": ocr.available(),
    }
