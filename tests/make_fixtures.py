"""生成两种 GTFS Schedule ZIP 测试包：

- small：6 站小网络，覆盖跨夜 25:xx 站时、周历例外（加开/停运）、
  显式有向换乘边、禁止换乘边、零秒换乘环、字典序 tie-break 等边界。
- perf：25 线路 × 20 站 × 200 班次 = 100000 条站时，用于 2 秒性能验收。

用法：python tests/make_fixtures.py
输出：tests/fixtures/small.zip、tests/fixtures/perf.zip
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import date, timedelta

OUT = os.path.join(os.path.dirname(__file__), "fixtures")
# 基准日：固定一个周一，保证周历逻辑可复现
MONDAY = date(2026, 10, 5)
FRI_DATE = (MONDAY + timedelta(days=4)).strftime("%Y%m%d")
SAT_DATE = (MONDAY + timedelta(days=5)).strftime("%Y%m%d")


def write_zip(path: str, files: dict[str, str]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def hms(sec: int) -> str:
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def small_feed() -> dict[str, str]:
    agency = ("agency_id,agency_name,agency_url,agency_timezone\n"
              "A1,市公交,http://example.com,Asia/Shanghai\n")
    routes = ("route_id,agency_id,route_short_name,route_long_name,route_type\n"
              "R1,A1,1路,,3\nR2,A1,2路,,3\n"
              "R3,A1,3路,,3\nR4,A1,4路,,3\n")
    stops = ("stop_id,stop_name,stop_lat,stop_lon\n"
             "S1,一广场,30.0,120.0\nS2,二路口,30.0,120.1\n"
             "S3,三桥,30.0,120.2\nS4,四里,30.0,120.3\n"
             "S5,五村,30.0,120.4\nS6,六站,30.0,120.5\n"
             "S7,七号周末专线站,30.0,120.6\n")
    # W12345 工作日；WSAT 仅周六加开（calendar_dates）；WX 工作日但周六加开/周五停运
    calendar = ("service_id,monday,tuesday,wednesday,thursday,friday,"
                "saturday,sunday,start_date,end_date\n"
                f"W12345,1,1,1,1,1,0,0,{MONDAY:%Y%m%d},{MONDAY + timedelta(days=13):%Y%m%d}\n"
                f"WX,1,1,1,1,1,0,0,{MONDAY:%Y%m%d},{MONDAY + timedelta(days=13):%Y%m%d}\n"
                f"WSAT,0,0,0,0,0,0,0,{MONDAY:%Y%m%d},{MONDAY + timedelta(days=13):%Y%m%d}\n")
    exceptions = ("service_id,date,exception_type\n"
                  f"WSAT,{SAT_DATE},1\n"          # WSAT 周六加开
                  f"WX,{FRI_DATE},2\n"            # WX 周五停运（覆盖周历）
                  f"WX,{SAT_DATE},1\n")           # WX 周六加开
    trips = ("trip_id,route_id,service_id,trip_headsign\n"
             "T100,R1,W12345,四里方向\n"      # S1->S2->S4
             "T200,R2,W12345,六站方向\n"      # S3->S5->S6
             "T300,R3,WX,一广场方向\n"        # S4->S3->S1（周五停运）
             "T400,R4,WSAT,六站方向\n")       # S2->S6（仅周六）
    st = io.StringIO()
    st.write("trip_id,stop_sequence,stop_id,arrival_time,departure_time,"
             "pickup_type,dropoff_type\n")
    # T100 跨夜：S1 23:50 发，S2 到达 24:30（次日凌晨），S4 25:10
    rows = [
        ("T100", 1, "S1", "23:50:00", "23:50:00"),
        ("T100", 2, "S2", "24:30:00", "24:30:00"),
        ("T100", 3, "S4", "25:10:00", "25:10:00"),
        # T200 次日凌晨：S3 24:50 发，S5 25:20，S6 26:00
        ("T200", 1, "S3", "24:50:00", "24:50:00"),
        ("T200", 2, "S5", "25:20:00", "25:20:00"),
        ("T200", 3, "S6", "26:00:00", "26:00:00"),
        # T300 白天：S4 09:00, S3 09:40, S1 10:20
        ("T300", 1, "S4", "09:00:00", "09:00:00"),
        ("T300", 2, "S3", "09:40:00", "09:40:00"),
        ("T300", 3, "S1", "10:20:00", "10:20:00"),
        # T400 周六：S2 09:00, S7 10:00（S7 只有此周末专线停靠）
        ("T400", 1, "S2", "09:00:00", "09:00:00"),
        ("T400", 2, "S7", "10:00:00", "10:00:00"),
    ]
    for t, seq, sid, a, d in rows:
        st.write(f"{t},{seq},{sid},{a},{d},0,0\n")

    tr = ("from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
          # S2 -> S3 唯一允许的跨站通道，需要 300 秒（T100 24:30 到，T200 24:50 发，可换）
          "S2,S3,2,300\n"
          # 反向存在但禁止换乘
          "S3,S2,3,\n"
          # S4 -> S3 10 分钟（T100 25:10 到，接不上 T200；白天 T300 可用）
          "S4,S3,2,600\n"
          # 零秒换乘环（自环）：验证扫描时零秒边的环必须立即终止，
          # 且不会伪造其他站点的提前到达
          "S5,S5,2,0\n")
    return {
        "agency.txt": agency, "routes.txt": routes, "stops.txt": stops,
        "calendar.txt": calendar, "calendar_dates.txt": exceptions,
        "trips.txt": trips, "stop_times.txt": st.getvalue(),
        "transfers.txt": tr,
    }


def perf_feed() -> dict[str, str]:
    """100000 条站时：25 环线路 × 20 站，每条线路 200 班。

    换乘边在每两条相邻线路的共用站（下标相同的站）给出，便于产生多段行程。
    """
    N_ROUTES, N_STOPS, N_TRIPS = 25, 20, 200
    assert N_ROUTES * N_STOPS * N_TRIPS == 100000

    agency = ("agency_id,agency_name,agency_url,agency_timezone\n"
              "P1,快速公交,http://example.com,Asia/Shanghai\n")
    routes = io.StringIO()
    routes.write("route_id,agency_id,route_short_name,route_long_name,route_type\n")
    for r in range(N_ROUTES):
        routes.write(f"RR{r},P1,K{r},,3\n")

    stops = io.StringIO()
    stops.write("stop_id,stop_name,stop_lat,stop_lon\n")
    for r in range(N_ROUTES):
        for s in range(N_STOPS):
            stops.write(f"R{r}S{s},{r}线{s}号站,30.0,120.0\n")

    calendar = ("service_id,monday,tuesday,wednesday,thursday,friday,"
                "saturday,sunday,start_date,end_date\n"
                f"EVERY,1,1,1,1,1,1,1,{MONDAY:%Y%m%d},"
                f"{MONDAY + timedelta(days=30):%Y%m%d}\n")
    trips = io.StringIO()
    trips.write("trip_id,route_id,service_id\n")
    for r in range(N_ROUTES):
        for k in range(N_TRIPS):
            trips.write(f"R{r}T{k:03d},RR{r},EVERY\n")

    st = io.StringIO()
    st.write("trip_id,stop_sequence,stop_id,arrival_time,departure_time\n")
    # 首班 5*60，站间 10 分钟，班次间隔 5 分钟；部分班次必然 >24:00
    for r in range(N_ROUTES):
        for k in range(N_TRIPS):
            base = 5 * 3600 + k * 5 * 60
            for s in range(N_STOPS):
                t = base + s * 10 * 60
                st.write(f"R{r}T{k:03d},{s + 1},R{r}S{s},{hms(t)},{hms(t)}\n")

    # 同序号站之间给双向 120 秒换乘边（相邻线路），制造多段换乘机会
    tr = io.StringIO()
    tr.write("from_stop_id,to_stop_id,transfer_type,min_transfer_time\n")
    for r in range(N_ROUTES - 1):
        for s in range(N_STOPS):
            tr.write(f"R{r}S{s},R{r + 1}S{s},2,120\n")

    return {
        "agency.txt": agency, "routes.txt": routes.getvalue(),
        "stops.txt": stops.getvalue(), "calendar.txt": calendar,
        "trips.txt": trips.getvalue(), "stop_times.txt": st.getvalue(),
        "transfers.txt": tr.getvalue(),
    }


def bad_feed_missing_ref() -> dict[str, str]:
    f = small_feed()
    f["stop_times.txt"] = f["stop_times.txt"].replace("S6,26:00:00", "SX,26:00:00")
    return f


def bad_feed_reversed_times() -> dict[str, str]:
    f = small_feed()
    # T200 第二站到达 25:20 早于首站发 24:50 看似正常，改为首站发 25:30 → 倒序
    f["stop_times.txt"] = f["stop_times.txt"].replace(
        "T200,1,S3,24:50:00,24:50:00", "T200,1,S3,25:30:00,25:30:00")
    return f


if __name__ == "__main__":
    write_zip(os.path.join(OUT, "small.zip"), small_feed())
    write_zip(os.path.join(OUT, "perf.zip"), perf_feed())
    write_zip(os.path.join(OUT, "bad_ref.zip"), bad_feed_missing_ref())
    write_zip(os.path.join(OUT, "bad_times.zip"), bad_feed_reversed_times())
    print("fixtures written to", OUT)
