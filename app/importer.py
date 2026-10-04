"""GTFS Schedule ZIP 解析与整包校验导入。

校验规则（任一不满足则整包拒绝，事务回滚，旧版本仍可查询）：
- 必需文件齐全：agency/routes/stops/trips/stop_times + calendar/calendar_dates 至少一个
- 外键引用完整：routes.agency_id、trips.route_id/service_id、
  stop_times.trip_id/stop_id、transfers.from/to_stop_id
- 同一 trip 内 stop_sequence 严格递增、时间不倒序（站时倒序即拒绝）
- 关键字段类型与取值合法（transfer_type、pickup_type、时区、日期等）

时间统一转成"自服务日午夜起的秒数"整数，允许 >= 86400（24:00 后站时）。
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import threading
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import get_conn

import_lock = threading.Lock()

REQUIRED_FILES = ("agency.txt", "routes.txt", "stops.txt", "trips.txt", "stop_times.txt")

_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_TIME_RE = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})$")


class GTFSError(ValueError):
    """GTFS 数据校验失败。"""


def _parse_hms(value: str) -> int:
    m = _TIME_RE.match(value)
    if not m:
        raise GTFSError(f"非法时刻 {value!r}，应为 HH:MM:SS")
    h, mi, s = (int(x) for x in m.groups())
    if mi >= 60 or s >= 60:
        raise GTFSError(f"非法时刻 {value!r}")
    return h * 3600 + mi * 60 + s


def _parse_date(value: str) -> str:
    m = _DATE_RE.match(value or "")
    if not m:
        raise GTFSError(f"非法日期 {value!r}，应为 YYYYMMDD")
    try:
        datetime(int(m[1]), int(m[2]), int(m[3]))
    except ValueError as exc:
        raise GTFSError(f"非法日期 {value!r}: {exc}") from exc
    return value


def _open_csv(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    try:
        raw = zf.read(name)
    except KeyError:
        raise GTFSError(f"缺少必需文件 {name}")
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise GTFSError(f"{name} 缺少表头")
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    return rows


def _require_headers(name: str, rows: list[dict], required: list[str]) -> None:
    if not rows:
        return
    missing = [h for h in required if h not in rows[0]]
    if missing:
        raise GTFSError(f"{name} 缺少必需列: {', '.join(missing)}")


def _int_opt(value: str, field: str, allowed: set[int] | None = None, default: int | None = None):
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except ValueError as exc:
        raise GTFSError(f"{field} 非整数: {value!r}") from exc
    if allowed is not None and n not in allowed:
        raise GTFSError(f"{field} 取值非法: {n}")
    return n


def _float_opt(value: str):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise GTFSError(f"坐标非数字: {value!r}") from exc


def parse_zip(data: bytes) -> dict:
    """纯解析 + 校验，返回内存中的结构化数据，不接触数据库。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        bad = zf.testzip()
        if bad is not None:
            raise GTFSError(f"ZIP 损坏: {bad}")
    except zipfile.BadZipFile as exc:
        raise GTFSError(f"不是合法 ZIP 文件: {exc}") from exc

    names = set(zf.namelist())
    for need in REQUIRED_FILES:
        if need not in names:
            raise GTFSError(f"缺少必需文件 {need}")
    if "calendar.txt" not in names and "calendar_dates.txt" not in names:
        raise GTFSError("calendar.txt 与 calendar_dates.txt 至少需要一个")

    # ---------- agency ----------
    agency_rows = _open_csv(zf, "agency.txt")
    _require_headers("agency.txt", agency_rows, ["agency_name", "agency_timezone"])
    agencies: list[dict] = []
    agency_ids: set[str] = set()
    default_tz = None
    for r in agency_rows:
        aid = r.get("agency_id", "") or ""
        if aid in agency_ids:
            raise GTFSError(f"agency_id 重复: {aid!r}")
        tzname = r.get("agency_timezone", "")
        if not tzname:
            raise GTFSError("agency_timezone 不能为空")
        try:
            tz = ZoneInfo(tzname)
        except ZoneInfoNotFoundError as exc:
            raise GTFSError(f"未知时区 {tzname!r}") from exc
        if default_tz is None:
            default_tz = tz
        agency_ids.add(aid)
        agencies.append({"id": aid, "name": r["agency_name"], "tz": tzname})
        _ = tz  # 校验通过即可

    # ---------- routes ----------
    route_rows = _open_csv(zf, "routes.txt")
    _require_headers("routes.txt", route_rows, ["route_id"])
    routes: list[dict] = []
    route_ids: set[str] = set()
    for r in route_rows:
        rid = r.get("route_id", "")
        if not rid:
            raise GTFSError("route_id 不能为空")
        if rid in route_ids:
            raise GTFSError(f"route_id 重复: {rid!r}")
        aid = r.get("agency_id", "") or ""
        if aid not in agency_ids:
            raise GTFSError(f"线路 {rid} 引用了不存在的 agency {aid!r}")
        routes.append({
            "id": rid,
            "agency_id": aid,
            "short": r.get("route_short_name", ""),
            "long": r.get("route_long_name", ""),
            "type": _int_opt(r.get("route_type", ""), "route_type"),
        })
        route_ids.add(rid)

    # ---------- stops ----------
    stop_rows = _open_csv(zf, "stops.txt")
    _require_headers("stops.txt", stop_rows, ["stop_id", "stop_name"])
    stops: list[dict] = []
    stop_ids: set[str] = set()
    for r in stop_rows:
        sid = r.get("stop_id", "")
        if not sid:
            raise GTFSError("stop_id 不能为空")
        if sid in stop_ids:
            raise GTFSError(f"stop_id 重复: {sid!r}")
        stops.append({
            "id": sid,
            "name": r.get("stop_name", ""),
            "lat": _float_opt(r.get("stop_lat", "")),
            "lon": _float_opt(r.get("stop_lon", "")),
            "location_type": _int_opt(r.get("location_type", ""), "location_type",
                                      allowed={0, 1, 2, 3, 4}, default=0),
        })
        stop_ids.add(sid)

    # ---------- calendar / calendar_dates ----------
    services: dict[str, dict] = {}
    if "calendar.txt" in names:
        cal_rows = _open_csv(zf, "calendar.txt")
        _require_headers("calendar.txt", cal_rows,
                         ["service_id", "monday", "tuesday", "wednesday",
                          "thursday", "friday", "saturday", "sunday",
                          "start_date", "end_date"])
        days = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]
        for r in cal_rows:
            sid = r.get("service_id", "")
            if not sid:
                raise GTFSError("calendar.service_id 不能为空")
            if sid in services:
                raise GTFSError(f"calendar 中 service_id 重复: {sid!r}")
            mask = 0
            for i, d in enumerate(days):
                v = _int_opt(r.get(d, ""), d, allowed={0, 1})
                if v is None:
                    raise GTFSError(f"service {sid} 的 {d} 缺失")
                mask |= v << i
            start = _parse_date(r["start_date"])
            end = _parse_date(r["end_date"])
            if start > end:
                raise GTFSError(f"service {sid} start_date 晚于 end_date")
            services[sid] = {"weekdays": mask, "start": start, "end": end}

    exceptions: list[tuple[str, str, int]] = []
    exc_services: set[str] = set()
    if "calendar_dates.txt" in names:
        exc_rows = _open_csv(zf, "calendar_dates.txt")
        _require_headers("calendar_dates.txt", exc_rows,
                         ["service_id", "date", "exception_type"])
        seen_exc: set[tuple[str, str]] = set()
        for r in exc_rows:
            sid = r.get("service_id", "")
            date = _parse_date(r.get("date", ""))
            etype = _int_opt(r.get("exception_type", ""), "exception_type",
                             allowed={1, 2})
            if etype is None:
                raise GTFSError("exception_type 缺失")
            key = (sid, date)
            if key in seen_exc:
                raise GTFSError(f"calendar_dates 重复条目: {sid} {date}")
            seen_exc.add(key)
            exceptions.append((sid, date, etype))
            exc_services.add(sid)
            # 仅靠例外日期存在的服务
            services.setdefault(sid, {"weekdays": None, "start": None, "end": None})

    # ---------- trips ----------
    trip_rows = _open_csv(zf, "trips.txt")
    _require_headers("trips.txt", trip_rows, ["trip_id", "route_id", "service_id"])
    trips: list[dict] = []
    trip_ids: set[str] = set()
    for r in trip_rows:
        tid = r.get("trip_id", "")
        rid = r.get("route_id", "")
        sid = r.get("service_id", "")
        if not tid:
            raise GTFSError("trip_id 不能为空")
        if tid in trip_ids:
            raise GTFSError(f"trip_id 重复: {tid!r}")
        if rid not in route_ids:
            raise GTFSError(f"班次 {tid} 引用了不存在的线路 {rid!r}")
        if sid not in services:
            raise GTFSError(f"班次 {tid} 引用了不存在的 service {sid!r}")
        trips.append({
            "id": tid, "route_id": rid, "service_id": sid,
            "direction_id": _int_opt(r.get("direction_id", ""), "direction_id",
                                     allowed={0, 1}),
            "headsign": r.get("trip_headsign", ""),
        })
        trip_ids.add(tid)

    # ---------- stop_times ----------
    st_rows = _open_csv(zf, "stop_times.txt")
    _require_headers("stop_times.txt", st_rows,
                     ["trip_id", "stop_sequence", "stop_id",
                      "arrival_time", "departure_time"])
    by_trip: dict[str, list[tuple]] = {}
    for r in st_rows:
        tid = r.get("trip_id", "")
        sid = r.get("stop_id", "")
        if tid not in trip_ids:
            raise GTFSError(f"站时引用了不存在的班次 {tid!r}")
        if sid not in stop_ids:
            raise GTFSError(f"班次 {tid} 的站时引用了不存在的站点 {sid!r}")
        seq = _int_opt(r.get("stop_sequence", ""), "stop_sequence")
        if seq is None or seq < 0:
            raise GTFSError(f"班次 {tid} 的 stop_sequence 非法: {r.get('stop_sequence')!r}")
        arr_s, dep_s = r.get("arrival_time", ""), r.get("departure_time", "")
        if not arr_s or not dep_s:
            raise GTFSError(
                f"班次 {tid} 序号 {seq} 缺少到达/出发时刻（定时班次不允许留空）")
        arr, dep = _parse_hms(arr_s), _parse_hms(dep_s)
        if arr > dep:
            raise GTFSError(f"班次 {tid} 序号 {seq} 到达时刻晚于出发时刻（站时倒序）")
        pickup = _int_opt(r.get("pickup_type", ""), "pickup_type",
                          allowed={0, 1, 2, 3}, default=0)
        dropoff = _int_opt(r.get("dropoff_type", ""), "dropoff_type",
                           allowed={0, 1, 2, 3}, default=0)
        by_trip.setdefault(tid, []).append((seq, arr, dep, sid, pickup, dropoff))

    stop_times: dict[str, list[tuple]] = {}
    total_st = 0
    for tid, rows_ in by_trip.items():
        rows_.sort(key=lambda x: x[0])
        prev_seq = None
        prev_dep = None
        for seq, arr, dep, _sid, _pu, _do in rows_:
            if prev_seq is not None and seq <= prev_seq:
                raise GTFSError(f"班次 {tid} 的 stop_sequence 未严格递增（站时倒序）")
            if prev_dep is not None and arr < prev_dep:
                raise GTFSError(
                    f"班次 {tid} 序号 {seq} 到达时刻早于上一站出发时刻（站时倒序）")
            prev_seq, prev_dep = seq, dep
        stop_times[tid] = rows_
        total_st += len(rows_)

    orphan_trips = trip_ids - set(stop_times)
    if orphan_trips:
        raise GTFSError(f"以下班次没有任何站时: {sorted(orphan_trips)[:5]}")

    # ---------- transfers（站点级、显式有向边） ----------
    transfers: list[dict] = []
    if "transfers.txt" in names:
        tr_rows = _open_csv(zf, "transfers.txt")
        _require_headers("transfers.txt", tr_rows,
                         ["from_stop_id", "to_stop_id", "transfer_type"])
        seen_tr: dict[tuple[str, str], dict] = {}
        for r in tr_rows:
            f, t = r.get("from_stop_id", ""), r.get("to_stop_id", "")
            if f not in stop_ids:
                raise GTFSError(f"换乘边引用了不存在的站点 {f!r}")
            if t not in stop_ids:
                raise GTFSError(f"换乘边引用了不存在的站点 {t!r}")
            ttype = _int_opt(r.get("transfer_type", ""), "transfer_type",
                             allowed={0, 1, 2, 3})
            if ttype is None:
                raise GTFSError(f"换乘边 {f}->{t} 缺少 transfer_type")
            mtt = _int_opt(r.get("min_transfer_time", ""), "min_transfer_time")
            if mtt is not None and mtt < 0:
                raise GTFSError(f"换乘边 {f}->{t} min_transfer_time 为负")
            # type=2 必须显式给出耗时（允许 0 秒，用于零秒换乘环测试与建模）
            if ttype == 2 and mtt is None:
                raise GTFSError(
                    f"换乘边 {f}->{t} transfer_type=2 必须给出 min_transfer_time")
            edge = {"from": f, "to": t, "type": ttype, "min": mtt or 0}
            seen_tr[(f, t)] = edge  # 重复边以后者为准
        transfers = list(seen_tr.values())

    return {
        "agencies": agencies,
        "routes": routes,
        "stops": stops,
        "services": services,
        "exceptions": exceptions,
        "trips": trips,
        "stop_times": stop_times,
        "transfers": transfers,
        "stats": {
            "agencies": len(agencies),
            "routes": len(routes),
            "stops": len(stops),
            "trips": len(trips),
            "stop_times": total_st,
            "transfers": len(transfers),
        },
    }


def import_zip(data: bytes) -> dict:
    """校验并原子导入。导入期间加进程锁；失败抛 GTFSError，旧数据保留。"""
    parsed = parse_zip(data)  # 先完整校验，再碰数据库

    with import_lock:
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("INSERT INTO datasets(status) VALUES ('ready')")
            ds_id = cur.lastrowid

            conn.executemany(
                "INSERT INTO agency(dataset_id, agency_id, agency_name, agency_timezone)"
                " VALUES (?,?,?,?)",
                [(ds_id, a["id"], a["name"], a["tz"]) for a in parsed["agencies"]],
            )
            conn.executemany(
                "INSERT INTO routes(dataset_id, route_id, agency_id,"
                " route_short_name, route_long_name, route_type)"
                " VALUES (?,?,?,?,?,?)",
                [(ds_id, r["id"], r["agency_id"], r["short"], r["long"], r["type"])
                 for r in parsed["routes"]],
            )
            conn.executemany(
                "INSERT INTO stops(dataset_id, stop_id, stop_name, stop_lat, stop_lon,"
                " location_type) VALUES (?,?,?,?,?,?)",
                [(ds_id, s["id"], s["name"], s["lat"], s["lon"], s["location_type"])
                 for s in parsed["stops"]],
            )
            svc_rows = [(ds_id, sid, s["weekdays"], s["start"], s["end"])
                        for sid, s in parsed["services"].items()]
            conn.executemany(
                "INSERT INTO services(dataset_id, service_id, weekdays,"
                " start_date, end_date) VALUES (?,?,?,?,?)", svc_rows)
            conn.executemany(
                "INSERT INTO service_exceptions(dataset_id, service_id, date,"
                " exception_type) VALUES (?,?,?,?)",
                [(ds_id, *e) for e in parsed["exceptions"]],
            )
            conn.executemany(
                "INSERT INTO trips(dataset_id, trip_id, route_id, service_id,"
                " direction_id, trip_headsign) VALUES (?,?,?,?,?,?)",
                [(ds_id, t["id"], t["route_id"], t["service_id"],
                  t["direction_id"], t["headsign"]) for t in parsed["trips"]],
            )
            st_batch = []
            for tid, rows_ in parsed["stop_times"].items():
                for seq, arr, dep, sid, pickup, dropoff in rows_:
                    st_batch.append((ds_id, tid, seq, sid, arr, dep, pickup, dropoff))
            conn.executemany(
                "INSERT INTO stop_times(dataset_id, trip_id, stop_sequence, stop_id,"
                " arrival_time, departure_time, pickup_type, dropoff_type)"
                " VALUES (?,?,?,?,?,?,?,?)", st_batch)
            conn.executemany(
                "INSERT INTO transfers(dataset_id, from_stop_id, to_stop_id,"
                " transfer_type, min_transfer_time) VALUES (?,?,?,?,?)",
                [(ds_id, e["from"], e["to"], e["type"], e["min"])
                 for e in parsed["transfers"]],
            )
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('active_dataset',?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(ds_id),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return {"dataset_id": ds_id, **parsed["stats"]}
