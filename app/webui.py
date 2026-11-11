"""页面路由（Jinja2 服务端渲染，无前端构建步骤，NAS 友好）。"""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import COOKIE, current_user, login_user, make_token

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _authed(request: Request) -> bool:
    return current_user(request) is not None


def _ctx(request: Request, **kw) -> dict:
    return {"user": current_user(request), **kw}


@router.get("/")
def home(request: Request):
    return RedirectResponse("/capture" if _authed(request) else "/login", 303)


@router.get("/login")
def login_page(request: Request):
    if _authed(request):
        return RedirectResponse("/capture", 303)
    return templates.TemplateResponse(request, "login.html", _ctx(request))


@router.post("/login")
def login_submit(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else "?"
    user = login_user(ip, username.strip(), password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html", _ctx(request, error="用户名或口令不对（连续失败将临时锁定）"),
            status_code=401)
    resp = RedirectResponse("/capture", 303)
    resp.set_cookie(COOKIE, make_token(user["id"]), httponly=True,
                    samesite="lax", max_age=30 * 86400)
    return resp


@router.get("/capture")
def capture_page(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "capture.html", _ctx(request))


@router.get("/items")
def items_page(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "items.html", _ctx(request))


@router.get("/items/{item_id}")
def item_detail_page(item_id: int, request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "item_detail.html", _ctx(request, item_id=item_id))


@router.get("/items/{item_id}/print")
def item_print_page(item_id: int, request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "item_print.html", _ctx(request, item_id=item_id))


@router.get("/locations")
def locations_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] != "admin":
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "locations.html", _ctx(request))


@router.get("/inventory")
def inventory_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] not in ("admin", "staff"):
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "inventory.html", _ctx(request))


@router.get("/batch")
def batch_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] not in ("admin", "staff"):
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "batch.html", _ctx(request))


@router.get("/checkup")
def checkup_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] != "admin":
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "checkup.html", _ctx(request))


@router.get("/logs")
def logs_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] != "admin":
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "logs.html", _ctx(request))


@router.get("/items/{item_id}/tag")
def item_tag_page(item_id: int, request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "item_tag.html", _ctx(request, item_id=item_id))


@router.get("/pub/item/{code}")
def pub_item_page(code: str, request: Request):
    """游客导览页（免登录，内网可访问），只展示脱敏字段。"""
    return templates.TemplateResponse(request, "pub_item.html", {"code": code})


@router.get("/dict")
def dict_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] != "admin":
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "dict.html", _ctx(request))


@router.get("/users")
def users_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    if user["role"] != "admin":
        return RedirectResponse("/items", 303)
    return templates.TemplateResponse(request, "users.html", _ctx(request))
