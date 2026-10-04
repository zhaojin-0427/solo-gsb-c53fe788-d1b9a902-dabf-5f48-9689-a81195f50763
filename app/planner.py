"""行程搜索引擎。

算法：按绝对出发时刻排序的多准则连接扫描（CSA）变体。
- 服务展开：用户日期的前一日/当日/次日三天，例外日期覆盖周历；
  GTFS 站时允许超过 24:00（如 25:30），按"服务日午夜 + 秒数"换算绝对时刻。
- 搜索窗：[用户出发时刻, +24h]，只在窗内上车、窗内到达的连接参与。
- 换乘：仅走 transfers.txt 的显式有向边并满足 min_transfer_time；
  type=3（禁止换乘）的边不可用；同站无需规则即可换车；
  同车（同一 trip 实例）经停不产生换乘；零秒换乘边的环由 Pareto 去重终止。
- 每个站点保留关于 (到达时刻, 乘车次数, 班次ID序列) 的非支配标签；
  同 (到达, 次数) 只保留班次 ID 序列字典序最小者。
"""
from __future__ import annotations

import heapq
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import get_conn, get_active_dataset

EPOCH_ORD = date(1970, 1, 1).toordinal()
MAX_TRANSFERS_CAP = 10
WEEKDAY_BITS = ("monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday")


class PlanError(ValueError):
    pass


def _parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as exc:
        raise PlanError(f"日期格式应为 YYYY-MM-DD: {s!r}") from exc


def _parse_clock(s: str) -> int:
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError) as exc:
        raise PlanError(f"时刻格式应为 HH:MM: {s!r}") from exc
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise PlanError(f"时刻超出范围: {s!r}")
    return h * 3600 + m * 60


def _iso(abs_seconds: int) -> str:
    ordn, rem = divmod(abs_seconds, 86400)
    d = date.fromordinal(EPOCH_ORD + ordn)
    return (f"{d.isoformat()}T{rem // 3600:02d}:"
            f"{(rem % 3600) // 60:02d}:{rem % 60:02d}")


def _load_dataset_tz(conn: sqlite3.Connection, ds: int) -> str:
    row = conn.execute(
        "SELECT agency_timezone FROM agency WHERE dataset_id=?"
        " ORDER BY rowid LIMIT 1", (ds,)).fetchone()
    if not row:
        raise PlanError("数据中没有 agency，无法确定时区")
    return row["agency_timezone"]


def _active_services(conn, ds: int, dates: list[date]) -> list[tuple[str, int]]:
    """计算三天内实际运营的 (service_id, 日序数)。例外日期覆盖周历。"""
    cal = {r["service_id"]: r for r in conn.execute(
        "SELECT service_id, weekdays, start_date, end_date"
        " FROM services WHERE dataset_id=?", (ds,)).fetchall()}
    date_strs = [d.strftime("%Y%m%d") for d in dates]
    overrides: dict[tuple[str, str], int] = {}
    for r in conn.execute(
            "SELECT service_id, date, exception_type FROM service_exceptions"
            " WHERE dataset_id=? AND date IN (?,?,?)",
            (ds, *date_strs)):
        overrides[(r["service_id"], r["date"])] = r["exception_type"]

    active: list[tuple[str, int]] = []
    for d, ds_str in zip(dates, date_strs):
        ordn = d.toordinal() - EPOCH_ORD
        bit = 1 << d.weekday()
        for sid, c in cal.items():
            etype = overrides.get((sid, ds_str))
            if etype == 2:          # 例外停运覆盖周历
                continue
            if etype == 1:          # 例外加开
                active.append((sid, ordn))
                continue
            if c["weekdays"] is None:
                continue            # 仅由例外定义的服务，今天无例外
            if not (c["weekdays"] & bit):
                continue
            if c["start_date"] and not (c["start_date"] <= ds_str <= c["end_date"]):
                continue
            active.append((sid, ordn))
    return active


@dataclass
class Connection:
    dep: int
    arr: int
    fstop: int       # 出发站内部下标
    tstop: int       # 到达站内部下标
    pickup: int
    dropoff: int
    tinst: int       # trip 实例（trip_id × 运营日）下标
    fseq: int        # 上车站 stop_sequence


def _fetch_connections(conn, ds, active, t0, t1):
    """取窗内连接。

    两阶段（避免大表 × 临时服务表的低效连接计划）：
    1) 直接走 stop_times(dataset_id,departure_time) 索引，找各运营日
       相对秒数区间内有出发事件的班次（再与当日活动 service 集合取交）；
    2) 只取这些班次的完整站时，在 Python 内按运营日拼成连接。
    """
    active_by_day: dict[int, set[str]] = {}
    for sid, ordn in active:
        active_by_day.setdefault(ordn, set()).add(sid)

    # 各运营日活动 service 下的 trip 集合
    svc_days = sorted(active_by_day)
    day_trips: dict[int, set[str]] = {}
    if svc_days:
        all_svcs = {s for ss in active_by_day.values() for s in ss}
        qmarks = ",".join("?" * len(all_svcs))
        for r in conn.execute(
                f"SELECT trip_id, service_id FROM trips WHERE dataset_id=?"
                f" AND service_id IN ({qmarks})", (ds, *sorted(all_svcs))):
            for ordn in svc_days:
                if r["service_id"] in active_by_day[ordn]:
                    day_trips.setdefault(ordn, set()).add(r["trip_id"])

    tinst_ids: dict[tuple[str, int], int] = {}
    relevant_by_day: dict[int, list[str]] = {}
    for ordn, trips_of_day in day_trips.items():
        # GTFS 站时小时数理论上无上界（如 32:00），不能截断到 28h，
        # 否则恰好落在窗口右端的跨夜班次会被漏掉；下界可为负，
        # 用于让次日实例覆盖前一日晚发车的跨夜车。
        lo, hi = t0 - ordn * 86400, t1 - ordn * 86400
        if lo > hi:
            continue
        # 索引区间扫描（约 10 万条目，Python 内与当日 trip 集合取交）
        hits = {r["trip_id"] for r in conn.execute(
            "SELECT DISTINCT trip_id FROM stop_times WHERE dataset_id=?"
            " AND departure_time BETWEEN ? AND ?", (ds, lo, hi))}
        chosen = sorted(trips_of_day & hits)
        relevant_by_day[ordn] = chosen
        for tid in chosen:
            tinst_ids[(tid, ordn)] = len(tinst_ids)

    # 阶段 2：批量取相关班次完整站时（分批，兼容 SQL 参数上限）
    trip_ids = sorted({tid for ts in relevant_by_day.values() for tid in ts})
    rows_by_trip: dict[str, list] = {}
    for i in range(0, len(trip_ids), 500):
        chunk = trip_ids[i:i + 500]
        qmarks = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT trip_id, stop_sequence, stop_id, arrival_time,"
                f" departure_time, pickup_type, dropoff_type FROM stop_times"
                f" WHERE dataset_id=? AND trip_id IN ({qmarks})"
                f" ORDER BY trip_id, stop_sequence", (ds, *chunk)):
            rows_by_trip.setdefault(r["trip_id"], []).append(r)

    stops = [r["stop_id"] for r in conn.execute(
        "SELECT stop_id FROM stops WHERE dataset_id=? ORDER BY stop_id", (ds,))]
    stop_idx = {sid: i for i, sid in enumerate(stops)}

    conns: list[Connection] = []
    for (tid, ordn), ti in tinst_ids.items():
        rows_ = rows_by_trip.get(tid, ())
        for a, b in zip(rows_, rows_[1:]):
            dep = a["departure_time"] + ordn * 86400
            if t0 <= dep <= t1:
                arr = b["arrival_time"] + ordn * 86400
                if arr <= t1:
                    conns.append(Connection(
                        dep=dep, arr=arr,
                        fstop=stop_idx[a["stop_id"]],
                        tstop=stop_idx[b["stop_id"]],
                        pickup=a["pickup_type"], dropoff=b["dropoff_type"],
                        tinst=ti, fseq=a["stop_sequence"]))
    conns.sort(key=lambda c: (c.dep, c.fseq))

    tinst_trip = [None] * len(tinst_ids)
    for (tid, _ord), ti in tinst_ids.items():
        tinst_trip[ti] = tid
    return stops, stop_idx, conns, tinst_trip


def _load_transfer_edges(conn, ds, stop_idx):
    """返回 (零耗时有向边, 正耗时有向边)，均为邻接表；type=3 边剔除。"""
    n = len(stop_idx)
    zero = [[] for _ in range(n)]
    positive = [[] for _ in range(n)]
    for r in conn.execute(
            "SELECT from_stop_id, to_stop_id, transfer_type, min_transfer_time"
            " FROM transfers WHERE dataset_id=?", (ds,)):
        if r["transfer_type"] == 3:      # 禁止换乘：边不可用
            continue
        f, t = stop_idx.get(r["from_stop_id"]), stop_idx.get(r["to_stop_id"])
        if f is None or t is None:
            continue
        dur = r["min_transfer_time"] or 0  # type 0/1 未给耗时则为 0
        if dur <= 0:
            zero[f].append(t)
        else:
            positive[f].append((t, dur))
    return zero, positive


def plan(origin_id: str, dest_id: str, date_str: str, time_str: str,
         max_transfers: int = 3) -> dict:
    """主入口。返回 {query, journeys:[{transfers, segments:[...]}, ...]}。"""
    max_transfers = max(0, min(int(max_transfers), MAX_TRANSFERS_CAP))
    max_legs = max_transfers + 1  # 乘车段数上限

    conn = get_conn()
    try:
        ds = get_active_dataset(conn)
        if ds is None:
            raise PlanError("尚未导入时刻表，请先上传 GTFS ZIP")
        tzname = _load_dataset_tz(conn, ds)

        o = conn.execute("SELECT stop_id FROM stops WHERE dataset_id=? AND stop_id=?",
                         (ds, origin_id)).fetchone()
        d = conn.execute("SELECT stop_id FROM stops WHERE dataset_id=? AND stop_id=?",
                         (ds, dest_id)).fetchone()
        if not o:
            raise PlanError(f"起点站不存在: {origin_id}")
        if not d:
            raise PlanError(f"终点站不存在: {dest_id}")

        d0 = _parse_date(date_str)
        secs = _parse_clock(time_str)
        t0 = (d0.toordinal() - EPOCH_ORD) * 86400 + secs
        t1 = t0 + 86400

        # 起终相同：零段行程
        if origin_id == dest_id:
            return {"query": _query_echo(origin_id, dest_id, date_str, time_str,
                                         max_transfers, tzname, t0, t1),
                    "journeys": [_zero_journey(t0)]}

        dates3 = [d0 - timedelta(days=1), d0, d0 + timedelta(days=1)]
        active = _active_services(conn, ds, dates3)
        stops, stop_idx, conns, tinst_trip = _fetch_connections(
            conn, ds, active, t0, t1)
        zero, positive = _load_transfer_edges(conn, ds, stop_idx)
        oi, di = stop_idx[origin_id], stop_idx[dest_id]

        journeys = _csa(oi, di, conns, tinst_trip, zero, positive,
                        t0, t1, max_legs, stops)

        # 补充线路名
        trip_ids = {seg["trip"] for j in journeys for seg in j["segments"]}
        route_names = {}
        if trip_ids:
            qmarks = ",".join("?" * len(trip_ids))
            for r in conn.execute(
                    f"SELECT t.trip_id, r.route_short_name, r.route_long_name,"
                    f" t.trip_headsign FROM trips t JOIN routes r"
                    f" ON r.dataset_id=t.dataset_id AND r.route_id=t.route_id"
                    f" WHERE t.dataset_id=? AND t.trip_id IN ({qmarks})",
                    (ds, *trip_ids)):
                route_names[r["trip_id"]] = (
                    r["route_short_name"] or r["route_long_name"] or "",
                    r["trip_headsign"] or "")
        stop_names = {r["stop_id"]: r["stop_name"] for r in conn.execute(
            "SELECT stop_id, stop_name FROM stops WHERE dataset_id=?", (ds,))}

        out = []
        for j in journeys:
            segs = []
            for s in j["segments"]:
                rn, hs = route_names.get(s["trip"], ("", ""))
                segs.append({
                    "trip_id": s["trip"],
                    "route_name": rn,
                    "headsign": hs,
                    "from_stop_id": s["fstop"],
                    "from_stop_name": stop_names.get(s["fstop"], ""),
                    "to_stop_id": s["tstop"],
                    "to_stop_name": stop_names.get(s["tstop"], ""),
                    "board_time": _iso(s["dep"]),
                    "alight_time": _iso(s["arr"]),
                })
            out.append({"transfers": len(segs) - 1,
                        "departure_time": _iso(j["dep"]),
                        "arrival_time": _iso(j["arr"]),
                        "segments": segs})
        return {"query": _query_echo(origin_id, dest_id, date_str, time_str,
                                     max_transfers, tzname, t0, t1),
                "timezone": tzname,
                "journeys": out}
    finally:
        conn.close()


def _query_echo(o, d, date_str, time_str, max_tr, tz, t0, t1):
    return {"origin": o, "destination": d, "date": date_str,
            "time": time_str, "max_transfers": max_tr,
            "timezone": tz, "window_start": _iso(t0), "window_end": _iso(t1)}


def _zero_journey(t0):
    return {"transfers": 0, "departure_time": _iso(t0),
            "arrival_time": _iso(t0), "segments": []}


# ---------------- CSA 核心 ----------------
#
# 节点（行程链重建用），并行数组存储：
# kind: 'o' 起点 / 'a' 下车（可换乘标签）/ 't' 换乘到达（可换乘标签）/ 'b' 上车
# 站点标签只含 o/a/t 三类"车下等待"节点；b 节点是车上状态。

def _csa(oi, di, conns, tinst_trip, zero, positive, t0, t1, max_legs, stops):
    n_stops = len(zero)
    # 节点数组
    n_stop: list[int] = []
    n_arr: list[int] = []
    n_nt: list[int] = []
    n_seq: list[tuple] = []
    n_parent: list[int] = []
    n_kind: list[str] = []
    n_tinst: list[int] = []
    n_fseq: list[int] = []

    labels: list[list[int]] = [[] for _ in range(n_stops)]  # 各站等待标签节点 id
    # 车上状态：tinst -> {ntrips: (dep, fseq, seq, board_node)}
    onboard: dict[int, dict[int, tuple]] = {}
    # 等待标签 id -> 由它登车产生的 (tinst, ntrips) 车上状态；
    # 标签被 Pareto 剪枝时必须级联撤销，否则会出现"幽灵换乘"。
    boards_from: dict[int, list[tuple]] = {}
    dead: list[bool] = []  # 节点（等待标签）失效标记
    edge_heap: list = []
    heap_counter = 0

    def add_node(stop, arr, nt, seq, parent, kind, tinst=-1, fseq=-1):
        nid = len(n_stop)
        n_stop.append(stop); n_arr.append(arr); n_nt.append(nt)
        n_seq.append(seq); n_parent.append(parent); n_kind.append(kind)
        n_tinst.append(tinst); n_fseq.append(fseq)
        dead.append(False)
        return nid

    def rejects(a_arr, a_nt, a_seq, b_arr, b_nt, b_seq):
        """a 使 b 无效：三项均不劣（含相等→重复也拒绝）。"""
        return a_arr <= b_arr and a_nt <= b_nt and a_seq <= b_seq

    def kill_label(lid):
        """级联失效一个等待标签及其创建的车上状态。"""
        if dead[lid]:
            return
        dead[lid] = True
        for ti, nt in boards_from.pop(lid, ()):
            st = onboard.get(ti)
            if st and st.get(nt) and st[nt][3] and n_parent[st[nt][3]] == lid:
                st.pop(nt, None)

    def add_wait(stop, arr, nt, seq, parent, kind):
        """登记一个车下等待标签；返回节点 id，被支配则返回 None。

        接受后立即：沿零秒换乘边克隆（显式栈，环由去重终止）、
        将正耗时边加入堆。
        """
        existing = labels[stop]
        for eid in existing:
            if rejects(n_arr[eid], n_nt[eid], n_seq[eid], arr, nt, seq):
                return None
        nid = add_node(stop, arr, nt, seq, parent, kind)
        # 剪枝被新标签支配的旧等待标签（并撤销其车上状态）
        kept = []
        for eid in existing:
            if rejects(arr, nt, seq, n_arr[eid], n_nt[eid], n_seq[eid]):
                kill_label(eid)
            else:
                kept.append(eid)
        kept.append(nid)
        labels[stop] = kept

        # 传播换乘边
        stack = [nid]
        while stack:
            src = stack.pop()
            sa, snt, sseq, sstop = (n_arr[src], n_nt[src], n_seq[src], n_stop[src])
            for z in zero[sstop]:
                zid = add_wait(z, sa, snt, sseq, src, "t")
                if zid is not None:
                    stack.append(zid)
            for to, dur in positive[sstop]:
                nonlocal heap_counter
                heapq.heappush(edge_heap,
                               (sa + dur, heap_counter, src, to, snt, sseq))
                heap_counter += 1
        return nid

    # 起点标签（也允许沿起点的显式换乘边先到邻站再乘车）
    add_wait(oi, t0, 0, (), -1, "o")

    def pop_edges(until):
        nonlocal heap_counter
        while edge_heap and edge_heap[0][0] <= until:
            ready, _c, src, to, snt, sseq = heapq.heappop(edge_heap)
            if dead[src]:
                continue  # 源标签已被剪枝
            add_wait(to, ready, snt, sseq, src, "t")

    def try_alight(c):
        states = onboard.get(c.tinst)
        if not states or c.dropoff == 1:
            return False
        changed = False
        for _nt, st in list(states.items()):
            dep, fseq, _seq, bnode = st
            if fseq <= c.fseq:  # 登车连接（fseq 相同）的终点即可下车
                nid = add_wait(c.tstop, c.arr, n_nt[bnode], n_seq[bnode],
                               bnode, "a")
                if nid is not None:
                    changed = True
        return changed

    def try_board(c):
        if c.pickup == 1:
            return False
        changed = False
        states = onboard.setdefault(c.tinst, {})
        trip_id = tinst_trip[c.tinst]
        for lid in labels[c.fstop]:
            if dead[lid] or n_arr[lid] > c.dep:
                continue
            nt = n_nt[lid] + 1
            if nt > max_legs:
                continue
            new_seq = n_seq[lid] + (trip_id,)
            old = states.get(nt)
            # 越早登车越优；同时刻则靠上游（fseq 小）、班次序列字典序小者胜
            cand = (c.dep, c.fseq, new_seq)
            if old is not None:
                old_key = (old[0], old[1], old[2])
                if old_key <= cand:
                    continue
                # 旧登车状态来自被替换：撤销（其等待标签仍合法，
                # 但该 tinst 上同 nt 的旧状态已无用）
                old_bnode = old[3]
                old_parent = n_parent[old_bnode]
                lst = boards_from.get(old_parent)
                if lst and (c.tinst, nt) in lst:
                    lst.remove((c.tinst, nt))
            bnode = add_node(c.fstop, c.dep, nt, new_seq, lid, "b",
                             tinst=c.tinst, fseq=c.fseq)
            states[nt] = (c.dep, c.fseq, new_seq, bnode)
            boards_from.setdefault(lid, []).append((c.tinst, nt))
            changed = True
        return changed

    # 按出发时刻分组扫描；同一出发时刻内做不动点迭代，
    # 使零秒换乘/零区间连接在该时刻内传播，且天然终止（标签受 Pareto 剪枝）。
    i, nc = 0, len(conns)
    while i < nc:
        dep = conns[i].dep
        j = i + 1
        while j < nc and conns[j].dep == dep:
            j += 1
        group = conns[i:j]

        pop_edges(dep)
        for _pass in range(max_legs + 2):
            changed = False
            for c in group:
                if try_alight(c):
                    changed = True
                if try_board(c):
                    changed = True
            if not changed:
                break
        i = j

    pop_edges(t1)

    # 收集终点站的非支配等待标签（全部经过 Pareto 剪枝），重建行程
    results = []
    for nid in labels[di]:
        if n_nt[nid] == 0 or n_arr[nid] > t1:
            continue
        segs = _rebuild(nid, n_parent, n_kind, n_tinst, n_stop, n_arr, n_fseq,
                        tinst_trip, stops)
        results.append((n_arr[nid], n_nt[nid], n_seq[nid], segs))

    results.sort(key=lambda x: (x[0], x[1], x[2]))
    # 全局再做一次非支配保留（正常情况下站点内剪枝已保证）
    pareto = []
    for arr, nt, seq, segs in results:
        if any(ea <= arr and ent <= nt and eseq <= seq
               for ea, ent, eseq, _ in pareto):
            continue
        pareto = [p for p in pareto
                  if not (arr <= p[0] and nt <= p[1] and seq <= p[2])]
        pareto.append((arr, nt, seq, segs))

    journeys = []
    for arr, nt, seq, segs in pareto:
        journeys.append({"dep": segs[0]["dep"], "arr": arr, "segments": segs})
    journeys.sort(key=lambda x: (x["arr"], len(x["segments"])))
    return journeys


def _rebuild(nid, n_parent, n_kind, n_tinst, n_stop, n_arr, n_fseq,
             tinst_trip, stops):
    """从终点标签回溯，配对 (a,b) 节点得到各乘车段（返回站点 ID）。"""
    raw = []
    cur = nid
    while cur >= 0:
        raw.append(cur)
        cur = n_parent[cur]
    raw.reverse()

    segs = []
    k = 0
    while k < len(raw):
        if n_kind[raw[k]] == "b":
            bnode = raw[k]
            # 找配对的 a 节点（链上 b 之后第一个 a）
            a_idx = k + 1
            while a_idx < len(raw) and n_kind[raw[a_idx]] != "a":
                a_idx += 1
            if a_idx < len(raw):
                anode = raw[a_idx]
                ti = n_tinst[bnode]
                segs.append({
                    "trip": tinst_trip[ti],
                    "fstop": stops[n_stop[bnode]],
                    "tstop": stops[n_stop[anode]],
                    "dep": n_arr[bnode], "arr": n_arr[anode],
                })
                k = a_idx + 1
                continue
        k += 1
    return segs
