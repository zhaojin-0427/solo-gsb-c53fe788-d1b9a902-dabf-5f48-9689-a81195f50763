"""额外边界规则测试：
- 同指标（到达时刻+换乘数）只保留班次 ID 序列字典序最小者
- pickup_type=1 不可上车、dropoff_type=1 不可下车
- min_transfer_time 不足时不能换乘
- 多个 Pareto 点（更早到达 vs 更少换乘）同时保留
- 同站换车无需换乘规则
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as dbmod  # noqa: E402
from app.importer import import_zip  # noqa: E402
from app.planner import plan  # noqa: E402

PASS = FAIL = 0
MONDAY = date(2026, 10, 5)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for n, c in files.items():
            zf.writestr(n, c)
    return buf.getvalue()


def base_files(cal_start=None, cal_end=None, tz="Asia/Shanghai"):
    cal_start = cal_start or MONDAY
    cal_end = cal_end or MONDAY + timedelta(days=13)
    return {
        "agency.txt": ("agency_id,agency_name,agency_url,agency_timezone\n"
                       f"A1,公交,http://x,{tz}\n"),
        "calendar.txt": ("service_id,monday,tuesday,wednesday,thursday,friday,"
                         "saturday,sunday,start_date,end_date\n"
                         f"WK,1,1,1,1,1,0,0,{cal_start:%Y%m%d},{cal_end:%Y%m%d}\n"),
    }


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dbmod.DB_PATH = type(dbmod.DB_PATH)(tmp.name)
    dbmod.init_db()
    D = "2026-10-09"

    print("== A. 字典序 tie-break ==")
    f = base_files()
    f["routes.txt"] = ("route_id,agency_id,route_short_name,route_type\n"
                       "RA,A1,A线,3\n")
    f["stops.txt"] = ("stop_id,stop_name\nS1,甲\nS2,乙\n")
    f["trips.txt"] = ("trip_id,route_id,service_id\nT_A,RA,WK\nT_B,RA,WK\n")
    # 两班同时刻同停站：只应保留 T_A（班次 ID 字典序更小）
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T_A,1,S1,10:00:00,10:00:00\nT_A,2,S2,10:30:00,10:30:00\n"
        "T_B,1,S1,10:00:00,10:00:00\nT_B,2,S2,10:30:00,10:30:00\n")
    import_zip(make_zip(f))
    r = plan("S1", "S2", D, "09:00", 2)
    check("同指标只留一条", len(r["journeys"]) == 1, r["journeys"])
    check("保留班次 ID 字典序最小者 T_A",
          r["journeys"] and r["journeys"][0]["segments"][0]["trip_id"] == "T_A",
          r["journeys"])

    print("== B. pickup/dropoff 受限站点 ==")
    f = base_files()
    f["routes.txt"] = ("route_id,agency_id,route_short_name,route_type\nRA,A1,A,3\n")
    f["stops.txt"] = ("stop_id,stop_name\nS1,甲\nS2,乙\nS3,丙\n")
    f["trips.txt"] = "trip_id,route_id,service_id\nT1,RA,WK\n"
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time,pickup_type,dropoff_type\n"
        "T1,1,S1,10:00:00,10:00:00,0,0\n"
        # S2 只下不上
        "T1,2,S2,10:20:00,10:20:00,1,0\n"
        # S3 只上不下
        "T1,3,S3,10:40:00,10:40:00,0,1\n")
    import_zip(make_zip(f))
    check("pickup_type=1 的 S2 不能上车 → S2->S3 无解",
          plan("S2", "S3", D, "09:00", 0)["journeys"] == [], [])
    check("S1 到 dropoff_type=1 的 S3 不能下车 → 无解",
          plan("S1", "S3", D, "09:00", 0)["journeys"] == [], [])
    check("S1->S2 正常（S2 可下车）",
          len(plan("S1", "S2", D, "09:00", 0)["journeys"]) == 1, [])

    print("== C. 换乘耗时不足 ==")
    f = base_files()
    f["routes.txt"] = ("route_id,agency_id,route_short_name,route_type\n"
                       "R1,A1,1,3\nR2,A1,2,3\n")
    f["stops.txt"] = ("stop_id,stop_name\nS1,甲\nS2,乙\nS3,丙\nS4,丁\n")
    f["trips.txt"] = ("trip_id,route_id,service_id\nT1,R1,WK\nT2,R2,WK\n")
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,1,S1,10:00:00,10:00:00\nT1,2,S2,10:10:00,10:10:00\n"
        "T2,1,S3,10:11:00,10:11:00\nT2,2,S4,10:30:00,10:30:00\n")
    f["transfers.txt"] = ("from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
                          "S2,S3,2,120\n")
    import_zip(make_zip(f))
    # 10:10 到 S2，需要 120 秒，T2 10:11 发 → 赶不上
    check("120 秒换乘边接不上 60 秒后发车的 T2 → 无解",
          plan("S1", "S4", D, "09:00", 2)["journeys"] == [], [])
    # 改耗时为 30 秒则可行
    f["transfers.txt"] = ("from_stop_id,to_stop_id,transfer_type,min_transfer_time\n"
                          "S2,S3,2,30\n")
    import_zip(make_zip(f))
    r2 = plan("S1", "S4", D, "09:00", 2)
    check("30 秒换乘边可行，两段行程",
          len(r2["journeys"]) == 1 and r2["journeys"][0]["transfers"] == 1,
          r2["journeys"])

    print("== D. Pareto：早到多换乘 与 晚到少换乘 同时保留 ==")
    f = base_files()
    f["routes.txt"] = ("route_id,agency_id,route_short_name,route_type\n"
                       "RE,A1,快线,3\nRW,A1,慢线,3\nRM,A1,接驳,3\n")
    f["stops.txt"] = ("stop_id,stop_name\nS1,甲\nS2,枢纽\nS3,乙\n")
    f["trips.txt"] = ("trip_id,route_id,service_id\nTE,RE,WK\nTM,RM,WK\nTW,RW,WK\n")
    # 快线+接驳：10:00 发 → S2 10:20，换 10:30 接驳 → S3 10:40（1 换乘）
    # 慢线同站直达：10:05 发 → S3 11:00（0 换乘）
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "TE,1,S1,10:00:00,10:00:00\nTE,2,S2,10:20:00,10:20:00\n"
        "TM,1,S2,10:30:00,10:30:00\nTM,2,S3,10:40:00,10:40:00\n"
        "TW,1,S1,10:05:00,10:05:00\nTW,2,S3,11:00:00,11:00:00\n")
    import_zip(make_zip(f))  # 无 transfers.txt：S2 同站换车无需规则
    r3 = plan("S1", "S3", D, "09:00", 2)
    arr = sorted((j["arrival_time"], j["transfers"]) for j in r3["journeys"])
    check("保留 2 条非支配行程（10:40/1换乘 与 11:00/0换乘）",
          arr == [("2026-10-09T10:40:00", 1), ("2026-10-09T11:00:00", 0)], arr)

    print("== E. 同站换车无需规则（无 transfers.txt 也可同站换）==")
    f = base_files()
    f["routes.txt"] = ("route_id,agency_id,route_short_name,route_type\nR1,A1,1,3\nR2,A1,2,3\n")
    f["stops.txt"] = ("stop_id,stop_name\nS1,甲\nS2,枢纽\nS3,乙\n")
    f["trips.txt"] = ("trip_id,route_id,service_id\nT1,R1,WK\nT2,R2,WK\n")
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,1,S1,09:00:00,09:00:00\nT1,2,S2,09:30:00,09:30:00\n"
        "T2,1,S2,09:40:00,09:40:00\nT2,2,S3,10:00:00,10:00:00\n")
    import_zip(make_zip(f))  # 无 transfers.txt
    r4 = plan("S1", "S3", D, "08:00", 3)
    check("无换乘边文件时同站 S2 可换车，1 次换乘两段",
          len(r4["journeys"]) == 1 and r4["journeys"][0]["transfers"] == 1
          and [s["trip_id"] for s in r4["journeys"][0]["segments"]] == ["T1", "T2"],
          r4["journeys"])

    print("== F. 无显式边不能跨站（S2->S3 即使近在咫尺）==")
    f["routes.txt"] = ("route_id,agency_id,route_short_name,route_type\nR1,A1,1,3\nR2,A1,2,3\n")
    f["stops.txt"] = ("stop_id,stop_name\nS1,甲\nS2,A站\nS3,B站\nS4,乙\n")
    f["trips.txt"] = ("trip_id,route_id,service_id\nT1,R1,WK\nT2,R2,WK\n")
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,1,S1,09:00:00,09:00:00\nT1,2,S2,09:30:00,09:30:00\n"
        "T2,1,S3,09:40:00,09:40:00\nT2,2,S4,10:00:00,10:00:00\n")
    import_zip(make_zip(f))  # 无 S2->S3 边
    check("缺有向换乘边 S2->S3 → 无解",
          plan("S1", "S4", D, "08:00", 3)["journeys"] == [], [])

    print("== G. 搜索窗口严格 24 小时（到达超出 24h 不算）==")
    f = base_files()
    f["routes.txt"] = "route_id,agency_id,route_short_name,route_type\nR1,A1,1,3\n"
    f["stops.txt"] = "stop_id,stop_name\nS1,甲\nS2,乙\n"
    f["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,WK\n"
    # 周六服务（由次日服务展开）：09:30 发 10:00 到。
    # 周五 08:00 出发的窗口截止周六 08:00，故无解；09:00 出发截止周六 09:00，
    # 仍赶不上 09:30；而 09:31 出发…… 改为周五当天班次验证边界更直接。
    f2 = dict(f)
    # 周五晚 23:30 发，跨夜 32:00（=次日 08:00）到：08:00 出发窗内恰好到达
    f2["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,1,S1,23:30:00,23:30:00\nT1,2,S2,32:00:00,32:00:00\n")
    import_zip(make_zip(f2))
    r = plan("S1", "S2", D, "08:00", 0)
    check("周五 08:00 出发，次日 08:00（窗口右端）到达 → 有解",
          len(r["journeys"]) == 1
          and r["journeys"][0]["arrival_time"] == "2026-10-10T08:00:00",
          r["journeys"])
    # 到达 32:01 超出窗口右端 1 秒
    f2["stop_times.txt"] = f2["stop_times.txt"].replace("32:00:00,32:00:00",
                                                        "32:01:00,32:01:00")
    import_zip(make_zip(f2))
    check("到达晚于窗口右端 1 秒 → 空",
          plan("S1", "S2", D, "08:00", 0)["journeys"] == [], [])
    # 窗内正常跨夜：25:00 发 = 次日 01:00
    f2["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,1,S1,25:00:00,25:00:00\nT1,2,S2,25:30:00,25:30:00\n")
    import_zip(make_zip(f2))
    check("09:30 出发则 25:00=次日01:00 在窗内 → 有解",
          len(plan("S1", "S2", D, "09:30", 0)["journeys"]) == 1, [])

    print("== H. 成功导入新版本后查询切换 ==")
    test_version_switch()
    print("== I. 同车多站只算一段 ==")
    test_multistop_same_trip()

    os.unlink(tmp.name)
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


def test_version_switch():
    """成功导入新版本后查询立即切到新数据集。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dbmod.DB_PATH = type(dbmod.DB_PATH)(tmp.name)
    dbmod.init_db()
    f = base_files()
    f["routes.txt"] = "route_id,agency_id,route_short_name,route_type\nR1,A1,1,3\n"
    f["stops.txt"] = "stop_id,stop_name\nS1,甲\nS2,乙\n"
    f["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,WK\n"
    f["stop_times.txt"] = ("trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
                           "T1,1,S1,09:00:00,09:00:00\nT1,2,S2,10:00:00,10:00:00\n")
    import_zip(make_zip(f))
    before = plan("S1", "S2", "2026-10-09", "08:00", 0)
    f["stop_times.txt"] = ("trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
                           "T1,1,S1,09:00:00,09:00:00\nT1,2,S2,11:00:00,11:00:00\n")
    info2 = import_zip(make_zip(f))
    after = plan("S1", "S2", "2026-10-09", "08:00", 0)
    check("新版本 dataset_id=2", info2["dataset_id"] == 2, info2)
    check("旧版本 10:00 到", before["journeys"][0]["arrival_time"] == "2026-10-09T10:00:00")
    check("查询切换到新版本 11:00 到",
          after["journeys"][0]["arrival_time"] == "2026-10-09T11:00:00",
          after["journeys"])
    os.unlink(tmp.name)


def test_multistop_same_trip():
    """同一趟车经停多站，只计 1 段 0 换乘。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dbmod.DB_PATH = type(dbmod.DB_PATH)(tmp.name)
    dbmod.init_db()
    f = base_files()
    f["routes.txt"] = "route_id,agency_id,route_short_name,route_type\nR1,A1,1,3\n"
    f["stops.txt"] = "stop_id,stop_name\nS1,甲\nS2,中1\nS3,中2\nS4,乙\n"
    f["trips.txt"] = "trip_id,route_id,service_id\nT1,R1,WK\n"
    f["stop_times.txt"] = (
        "trip_id,stop_sequence,stop_id,arrival_time,departure_time\n"
        "T1,1,S1,09:00:00,09:00:00\nT1,2,S2,09:10:00,09:10:00\n"
        "T1,3,S3,09:20:00,09:20:00\nT1,4,S4,09:30:00,09:30:00\n")
    import_zip(make_zip(f))
    j = plan("S1", "S4", "2026-10-09", "08:00", 3)["journeys"]
    check("S1->S4 同车 4 站仅 1 段 0 换乘",
          len(j) == 1 and j[0]["transfers"] == 0
          and len(j[0]["segments"]) == 1
          and j[0]["segments"][0]["board_time"] == "2026-10-09T09:00:00"
          and j[0]["segments"][0]["alight_time"] == "2026-10-09T09:30:00", j)
    os.unlink(tmp.name)


if __name__ == "__main__":
    main()
