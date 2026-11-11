"""页面路由（Jinja2 服务端渲染，无前端构建步骤，NAS 友好）。"""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import COOKIE, make_token, verify_token
from .config import APP_PASSWORD

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _authed(request: Request) -> bool:
    return verify_token(request.cookies.get(COOKIE))


@router.get("/")
def home(request: Request):
    return RedirectResponse("/capture" if _authed(request) else "/login", 303)


@router.get("/login")
def login_page(request: Request):
    if _authed(request):
        return RedirectResponse("/capture", 303)
    return templates.TemplateResponse(request, "login.html", {})


@router.post("/login")
def login_submit(request: Request, password: str = Form("")):
    if password != APP_PASSWORD:
        return templates.TemplateResponse(
            request, "login.html", {"error": "口令不对"}, status_code=401)
    resp = RedirectResponse("/capture", 303)
    resp.set_cookie(COOKIE, make_token(), httponly=True, samesite="lax",
                    max_age=30 * 86400)
    return resp


@router.get("/capture")
def capture_page(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "capture.html", {})


@router.get("/items")
def items_page(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "items.html", {})


@router.get("/items/{item_id}")
def item_detail_page(item_id: int, request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "item_detail.html", {"item_id": item_id})


@router.get("/dict")
def dict_page(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "dict.html", {})
