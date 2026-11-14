"""FastAPI web application: upload, per-file review, publish, download."""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import db
from .parser import parse_csv
from .publishing import (
    find_conflicts,
    is_publishable,
    unresolved_ambiguous,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="港口潮位 UTC 复核发布系统", lifespan=lifespan)


def _context(request: Request, **extra) -> dict:
    latest_pub = db.latest_publication()
    ctx = {
        "request": request,
        "latest_pub": latest_pub,
        "latest_session_id": db.latest_session_id(),
    }
    ctx.update(extra)
    return ctx


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _context(request, msg=request.query_params.get("msg")),
    )


@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少上传文件")
    if not content.strip():
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            _context(request, msg="上传文件为空，请选择有效的 CSV 文件"),
            status_code=400,
        )

    result = parse_csv(content, filename=file.filename)
    session_id = secrets.token_urlsafe(18)
    view = db.create_session(session_id, file.filename, result)
    return RedirectResponse(url=f"/review/{view.session_id}", status_code=303)


def _review_context(request: Request, session_id: str, notice: str | None = None):
    view = db.get_session(session_id)
    if view is None:
        raise HTTPException(status_code=404, detail="复核会话不存在（可能因服务重启已失效）")
    result = view.as_parse_result()
    conflicts = find_conflicts(result)
    publishable, reasons = is_publishable(result)
    return _context(
        request,
        view=view,
        result=result,
        conflicts=conflicts,
        unresolved=unresolved_ambiguous(result),
        publishable=publishable,
        block_reasons=reasons,
        notice=notice or request.query_params.get("msg"),
        published_id=request.query_params.get("published"),
    )


@app.get("/review/{session_id}", response_class=HTMLResponse)
def review(request: Request, session_id: str):
    return TEMPLATES.TemplateResponse(
        request, "review.html", _review_context(request, session_id)
    )


@app.post("/review/{session_id}/choice")
async def choose(
    request: Request,
    session_id: str,
    line_no: int = Form(...),
    choice: str = Form(...),
):
    try:
        db.set_fold_choice(session_id, line_no, choice)
    except db.StaleSession:
        return _stale_response(request, session_id)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(
        url=f"/review/{session_id}?msg=已保存第 {line_no} 行的时段选择",
        status_code=303,
    )


@app.post("/review/{session_id}/publish")
def publish(request: Request, session_id: str):
    # All decision-making happens inside one BEGIN IMMEDIATE transaction
    # (see db.publish_session): the rows are re-read under the write lock
    # and re-validated, so a choice saved by another window while this
    # request was in flight cannot be validated against a stale snapshot.
    try:
        meta = db.publish_session(session_id)
    except db.StaleSession:
        return _stale_response(request, session_id)
    except db.PublishConflict as exc:
        return TEMPLATES.TemplateResponse(
            request,
            "review.html",
            _review_context(request, session_id,
                            notice="发布被阻止：" + "；".join(exc.reasons)),
            status_code=422,
        )
    return RedirectResponse(
        url=f"/review/{session_id}?published={meta['id']}", status_code=303
    )


@app.get("/api/session/{session_id}/status")
def session_status(session_id: str):
    """Polled by open review pages so stale tabs disable themselves."""
    view = db.get_session(session_id)
    if view is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": session_id,
        "active": view.is_active,
        "status": view.status,
        "latest_session_id": db.latest_session_id(),
    }


@app.get("/download/latest")
def download_latest():
    pub = db.latest_publication()
    if pub is None:
        raise HTTPException(status_code=404, detail="尚无已发布的完整结果")
    return Response(
        content=pub["content"],
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="utc_tide_published_{pub["id"]}.csv"'
        },
    )


@app.get("/download/publication/{pub_id}")
def download_publication(pub_id: int):
    pub = db.publication_by_id(pub_id)
    if pub is None:
        raise HTTPException(status_code=404, detail="发布版本不存在")
    return Response(
        content=pub["content"],
        media_type="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="utc_tide_published_{pub_id}.csv"'
        },
    )


def _stale_response(request: Request, session_id: str):
    view = db.get_session(session_id)
    return TEMPLATES.TemplateResponse(
        request,
        "stale.html",
        _context(request, view=view, session_id=session_id),
        status_code=409,
    )
