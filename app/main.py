"""FastAPI 应用：时刻表上传、站点检索、换乘查询。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .db import get_active_dataset, get_conn, init_db
from .importer import GTFSError, import_zip
from .planner import PlanError, plan

app = FastAPI(title="公交班次换乘查询台")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/status")
def status():
    conn = get_conn()
    try:
        ds = get_active_dataset(conn)
        if ds is None:
            return {"imported": False}
        info = conn.execute(
            "SELECT id, imported_at FROM datasets WHERE id=?", (ds,)).fetchone()
        stats = {}
        for table in ("stops", "routes", "trips", "stop_times", "transfers", "agency"):
            stats[table] = conn.execute(
                f"SELECT COUNT(*) c FROM {table} WHERE dataset_id=?", (ds,)
            ).fetchone()["c"]
        tz = conn.execute(
            "SELECT agency_timezone FROM agency WHERE dataset_id=?"
            " ORDER BY rowid LIMIT 1", (ds,)).fetchone()
        agencies = [dict(r) for r in conn.execute(
            "SELECT agency_id, agency_name, agency_timezone"
            " FROM agency WHERE dataset_id=?", (ds,))]
        return {"imported": True, "dataset_id": info["id"],
                "imported_at": info["imported_at"],
                "timezone": tz["agency_timezone"] if tz else None,
                "agencies": agencies, "stats": stats}
    finally:
        conn.close()


@app.post("/api/import")
async def import_gtfs(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        result = import_zip(data)
    except GTFSError as exc:
        # 整包拒绝：事务已回滚，旧数据仍可查询
        raise HTTPException(status_code=400, detail=f"GTFS 校验失败，整包拒绝：{exc}")
    except Exception as exc:  # 兜底：任何异常都不能破坏旧版本
        raise HTTPException(status_code=400, detail=f"导入失败，已保留旧数据：{exc}")
    return {"ok": True, **result}


@app.get("/api/stops")
def search_stops(q: str = "", limit: int = 50):
    conn = get_conn()
    try:
        ds = get_active_dataset(conn)
        if ds is None:
            return {"stops": []}
        like = f"%{q}%"
        rows = conn.execute(
            "SELECT stop_id, stop_name FROM stops WHERE dataset_id=?"
            " AND (stop_id LIKE ? OR stop_name LIKE ?)"
            " ORDER BY stop_name LIMIT ?", (ds, like, like, max(1, min(limit, 100)))
        ).fetchall()
        return {"stops": [{"stop_id": r["stop_id"], "stop_name": r["stop_name"]}
                          for r in rows]}
    finally:
        conn.close()


@app.get("/api/plan")
def plan_trip(origin: str, destination: str, date: str, time: str,
              max_transfers: int = 3):
    if not origin or not destination:
        raise HTTPException(status_code=400, detail="起终站不能为空")
    try:
        return plan(origin, destination, date, time, max_transfers)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
