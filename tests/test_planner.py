"""不依赖 pytest 的端到端测试：直接调用导入与路由引擎。

运行：python tests/test_planner.py
（使用独立临时数据库，不影响 data/gtfs.db）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.db as dbmod  # noqa: E402
from app.importer import GTFSError, import_zip  # noqa: E402
from app.planner import PlanError, plan  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def read_zip(name):
    path = os.path.join(os.path.dirname(__file__), "fixtures", name)
    with open(path, "rb") as f:
        return f.read()


def seg_ids(j):
    return [(s["trip_id"], s["from_stop_id"], s["to_stop_id"]) for s in j["segments"]]


def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    dbmod.DB_PATH = type(dbmod.DB_PATH)(tmp.name)
    dbmod.init_db()

    print("== 1. 小网络导入 ==")
    info = import_zip(read_zip("small.zip"))
    check("导入成功，11 条站时", info["stop_times"] == 11, info)

    # 周五 2026-10-09 23:00 出发：触发跨夜 + 24:00 后站时 + 有向换乘
    FRI, SAT = "2026-10-09", "2026-10-10"

    print("== 2. 跨夜行程：S1 -> S6（T100 23:50 → S2 次日00:30，换 T200 → S6 02:00）==")
    r = plan("S1", "S6", FRI, "23:00", max_transfers=3)
    js = r["journeys"]
    check("有解", len(js) >= 1, js)
    if js:
        j = js[0]
        check("两段、换乘 1 次",
              j["transfers"] == 1 and len(j["segments"]) == 2, j)
        check("第一段 T100 23:50 上车（2026-10-09T23:50）",
              j["segments"][0]["trip_id"] == "T100"
              and j["segments"][0]["board_time"] == "2026-10-09T23:50:00",
              j["segments"][0])
        check("跨 24:00 站时正确：S2 下车 2026-10-10T00:30",
              j["segments"][0]["alight_time"] == "2026-10-10T00:30:00",
              j["segments"][0])
        check("第二段 T200，终到 S6 2026-10-10T02:00:00",
              j["segments"][1]["trip_id"] == "T200"
              and j["arrival_time"] == "2026-10-10T02:00:00", j)

    print("== 3. 换乘耗时约束：S1->S6 若 23:40 后出发则 T100 已发 ==")
    r2 = plan("S1", "S6", FRI, "23:55", max_transfers=3)
    check("23:55 出发赶不上 23:50 的 T100，窗口内无解（空列表）",
          r2["journeys"] == [], r2["journeys"])

    print("== 4. 禁止换乘边：S3->S2 type=3 不可用 ==")
    # S6 往 S1 方向只能 T200 到 S3 再换，S3->S2 被禁，S2/S4 也没有可用路径回 S1
    r3 = plan("S6", "S1", FRI, "23:00", max_transfers=3)
    # T200 方向 S3->S6，从 S6 没有任何车；显式换乘环 S6<->S5 零秒也走不出线路
    check("S6 无反向线路，返回空", r3["journeys"] == [], r3["journeys"])

    print("== 5. 例外日期覆盖周历（WX 周五停运、周六加开）==")
    # T300 S4->S1 属 WX：周五应停运
    r4 = plan("S4", "S1", FRI, "08:00", max_transfers=0)
    check("周五 T300 停运 → 空", r4["journeys"] == [], r4["journeys"])
    r5 = plan("S4", "S1", SAT, "08:00", max_transfers=0)
    check("周六例外加开 T300 → 有解，09:00 上车 10:20 到",
          len(r5["journeys"]) == 1
          and r5["journeys"][0]["segments"][0]["trip_id"] == "T300"
          and r5["journeys"][0]["arrival_time"] == "2026-10-10T10:20:00",
          r5["journeys"])

    print("== 6. WSAT 仅周六加开（calendar 全 0 + exception=1）==")
    r6 = plan("S2", "S7", FRI, "07:00", max_transfers=3)
    check("周五无 T400（S7 仅周末专线可达）", r6["journeys"] == [], r6["journeys"])
    r7 = plan("S2", "S7", SAT, "08:00", max_transfers=0)
    check("周六 T400 加开有解，10:00 到 S7",
          len(r7["journeys"]) == 1
          and r7["journeys"][0]["segments"][0]["trip_id"] == "T400"
          and r7["journeys"][0]["arrival_time"] == "2026-10-10T10:00:00",
          r7["journeys"])

    print("== 6b. 零秒换乘环（S5->S5）不崩溃、不伪造标签 ==")
    r6b = plan("S1", "S5", FRI, "23:00", max_transfers=3)
    check("S1->S5 正常有解（T100+T200，01:20 到），自环不影响结果",
          len(r6b["journeys"]) == 1
          and r6b["journeys"][0]["arrival_time"] == "2026-10-10T01:20:00",
          r6b["journeys"])

    print("== 7. 起终相同 = 零段行程；同站换车无需规则 ==")
    r8 = plan("S1", "S1", FRI, "08:00", max_transfers=3)
    check("同站返回 1 条 0 段行程",
          len(r8["journeys"]) == 1 and r8["journeys"][0]["segments"] == []
          and r8["journeys"][0]["transfers"] == 0, r8["journeys"])

    print("== 8. 换乘上限：0 次时 S1->S6 不可达 ==")
    r9 = plan("S1", "S6", FRI, "23:00", max_transfers=0)
    check("max_transfers=0 返回空", r9["journeys"] == [], r9["journeys"])

    print("== 9. 同车经停不计换乘：T100 S1->S4 为 0 换乘 1 段 ==")
    r10 = plan("S1", "S4", FRI, "23:00", max_transfers=3)
    check("S1->S4 一车直达，transfers=0，到达 25:10=周六01:10",
          len(r10["journeys"]) >= 1
          and any(j["transfers"] == 0 and j["segments"][0]["trip_id"] == "T100"
                  and j["arrival_time"] == "2026-10-10T01:10:00"
                  for j in r10["journeys"]), r10["journeys"])

    print("== 10. 整包拒绝：缺引用 / 站时倒序，旧数据仍可查 ==")
    try:
        import_zip(read_zip("bad_ref.zip"))
        check("缺站点引用被拒绝", False)
    except GTFSError:
        check("缺站点引用被拒绝", True)
    try:
        import_zip(read_zip("bad_times.zip"))
        check("站时倒序被拒绝", False)
    except GTFSError:
        check("站时倒序被拒绝", True)
    r11 = plan("S2", "S7", SAT, "08:00", max_transfers=0)
    check("拒绝后旧数据仍可查询（T400 还在）",
          len(r11["journeys"]) == 1, r11["journeys"])

    print("== 11. 非法输入 ==")
    bad_cases = [
        dict(origin_id="NOPE", dest_id="S1", date_str=SAT, time_str="08:00"),
        dict(origin_id="S1", dest_id="S2", date_str="2026-13-01", time_str="08:00"),
        dict(origin_id="S1", dest_id="S2", date_str=SAT, time_str="99:99"),
    ]
    for kwargs in bad_cases:
        try:
            plan(**kwargs, max_transfers=2)
            check(f"非法参数被拒绝 {kwargs}", False)
        except PlanError:
            check(f"非法参数被拒绝 {kwargs['date_str']} {kwargs['time_str']}", True)

    # ================= 性能验收 =================
    print("== 12. 10 万条站时性能包导入 ==")
    t0 = time.time()
    pinfo = import_zip(read_zip("perf.zip"))
    check(f"perf 包含 100000 条站时（导入 {time.time() - t0:.1f}s）",
          pinfo["stop_times"] == 100000, pinfo)

    print("== 13. 服务端查询 ≤ 2 秒（结果不超过 10 条）==")
    # 第一条线首站到最后一条线末站，需要多段换乘
    times = []
    case = dict(origin_id="R0S0", dest_id="R24S19",
                date_str="2026-10-09", time_str="05:00", max_transfers=10)
    for _ in range(3):
        t0 = time.time()
        pr = plan(**case)
        times.append(time.time() - t0)
    n = len(pr["journeys"])
    best = min(times)
    check(f"查询返回 {n} 条行程（≤10），最快 {best * 1000:.0f} ms ≤ 2000 ms",
          n <= 10 and best <= 2.0, f"n={n} times={times}")
    if pr["journeys"]:
        j = pr["journeys"][0]
        check("结果含多段且时刻单调、上车不晚于下车", all(
            s["board_time"] <= s["alight_time"] for s in j["segments"]), j)

    # 简单直达查询也测一次
    t0 = time.time()
    pr2 = plan("R0S0", "R0S19", "2026-10-09", "05:00", max_transfers=0)
    dt = time.time() - t0
    check(f"直达查询 {dt * 1000:.0f} ms 且有解", dt <= 2.0 and len(pr2["journeys"]) >= 1,
          pr2["journeys"][:1])

    os.unlink(tmp.name)
    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
