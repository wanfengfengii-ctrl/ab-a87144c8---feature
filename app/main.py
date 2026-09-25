"""FastAPI 入口：事故导排网络检修审计业务 API 与静态页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .flow import NetworkValidationError, audit_network, review_time_window

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="化工园区事故导排网络检修审计",
    version=__version__,
    description=(
        "录入泄压源、安全焚烧端、汇合节点与带方向/容量/检修标记/输送时长的管段，"
        "在正常网络及每条可检修管段临时失效后的残余网络上独立求最大流，"
        "并可按分钟展开时间层复核事故初期持续泄压流量能否在限时窗口内送达焚烧端。"
    ),
)


@app.exception_handler(NetworkValidationError)
async def _on_validation_error(_: Request, exc: NetworkValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": exc.message, "field": exc.field},
    )


@app.get("/health")
async def health() -> dict:
    """容器健康检查端点。"""
    return {"status": "ok", "service": "flare-audit", "version": __version__}


@app.post("/api/audit")
async def audit(request: Request) -> dict:
    """对一份导排网络草稿执行检修审计。

    正常网络与每个单管段移除情景**独立**计算最大流；全部达标才放行。
    方向、容量、节点引用等业务输入无效时返回 400。
    """
    payload = await _json_object(request)
    if isinstance(payload, JSONResponse):
        return payload

    result = audit_network(
        source=payload.get("source"),
        sink=payload.get("sink"),
        nodes=payload.get("nodes", []),
        edges=payload.get("edges", []),
        required_flow=payload.get("required_flow"),
    )
    result["service"] = "flare-audit"
    result["version"] = __version__
    return result


@app.post("/api/time-window-review")
async def time_window_review(request: Request):
    """限时送达复核：先重新审计，再按分钟展开时间层求实际送达量。

    请求体在检修审计字段之外增加：

    * ``window_minutes``：事故初期限时窗口长度（正整数分钟）；
    * 每条管段增加 ``duration``：正整数输送时长（分钟）。

    审计未通过时返回 ``stage="audit_failed"`` 且不展开时间层；
    审计通过后返回截止时刻前实际送达量、逐分钟送达量与管段—时段流量，
    仅当全部应排量按时到达时 ``passed=true``；否则附带时间层最小割证据。
    """
    payload = await _json_object(request)
    if isinstance(payload, JSONResponse):
        return payload

    result = review_time_window(
        source=payload.get("source"),
        sink=payload.get("sink"),
        nodes=payload.get("nodes", []),
        edges=payload.get("edges", []),
        required_flow=payload.get("required_flow"),
        window_minutes=payload.get("window_minutes"),
    )
    result["service"] = "flare-audit"
    result["version"] = __version__
    return result


async def _json_object(request: Request):
    """解析 JSON 对象请求体；失败时返回可直接响应的 400 JSONResponse。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是合法 JSON", "field": None})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON 对象", "field": None})
    return payload


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
