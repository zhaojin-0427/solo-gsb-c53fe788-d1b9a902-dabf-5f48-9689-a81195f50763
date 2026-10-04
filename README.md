# 公交班次换乘 Web 查询台

基于 **Python / FastAPI + SQLite + 原生 JavaScript** 的 GTFS Schedule 公交换乘查询服务。
上传 GTFS Schedule ZIP 后即可在网页选择起终站、出发日期时刻与换乘上限，
系统在出发后 24 小时内搜索并返回按「到达时刻 + 换乘次数」非支配的行程集合。

## 一键启动（Docker Compose）

前置条件：已安装 Docker 与 Docker Compose（v2）。

```bash
docker compose up --build
```

启动后访问：

- 查询台页面：<http://localhost:8000/>
- 数据集状态 JSON：<http://localhost:8000/api/status>
- OpenAPI 文档：<http://localhost:8000/docs>

SQLite 数据库持久化在宿主机 `./data/gtfs.db`（首次启动自动创建）。
停止服务：`docker compose down`（数据保留）。

## 本地（无 Docker）运行

需要 Python 3.11+（容器内为 3.12）：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 时刻表导入方式

1. 打开 <http://localhost:8000/>；
2. 在「1. 导入时刻表」处选择 GTFS Schedule **ZIP 文件**，点击「上传并整包校验」；
3. 也可以用命令行/HTTP 直接上传：

   ```bash
   curl -F "file=@你的时刻表.zip" http://localhost:8000/api/import
   ```

或在 `/docs` 页面用 `/api/import` 上传。

### 导入校验规则（整包拒绝，失败不影响旧数据）

解析并校验以下 GTFS 文件：`agency.txt`、`routes.txt`、`stops.txt`、`trips.txt`、
`stop_times.txt`，以及 `calendar.txt` / `calendar_dates.txt`（二者至少其一），
可选 `transfers.txt`。

- **引用完整性**：线路的 agency、班次的线路与 service、站时的班次与站点、
  换乘边的起终站点，引用缺失即整包拒绝；
- **站时合法性**：每个班次 `stop_sequence` 严格递增，
  且到达/出发时刻不倒序（含跨站到达早于上一站出发）；
- 定时班次要求每条 `stop_times.txt` 记录都显式给出 `arrival_time` / `departure_time`；
- 时区、日期、`transfer_type`(0–3)、`pickup_type`/`dropoff_type`(0–3) 等取值合法。

整个导入在**单个数据库事务**中完成：校验通过前不写库；
任何错误都回滚，已发布的旧版本数据集继续正常查询
（每次成功导入生成新版本，`active_dataset` 原子切换）。

## 查询语义说明

页面输入：起点站、终点站、出发日期、出发时刻（按 agency 时区的本地时间）、
换乘次数上限（0–10）。

- **服务展开**：自动展开出发日的**前一日、当日、次日**三个服务日；
- **例外优先**：`calendar_dates.txt` 的加开(1)/停运(2) 覆盖 `calendar.txt` 周历；
  仅有例外日期的 service（周历全 0）也能在加开日运营；
- **搜索窗口**：`[出发时刻, 出发时刻 + 24 小时]`，只保留窗内上车且窗内到达的连接；
- **24:00 后站时**：支持 GTFS 的 `25:30:00` 这类跨夜时刻，
  按「服务日午夜 + 偏移秒数」换算为绝对时间；
- **换乘规则**：
  - 跨站只能沿 `transfers.txt` 中**显式的有向边**换乘，并满足
    `min_transfer_time`（transfer_type=2）；
  - `transfer_type=3`（禁止换乘）的边不可用；
  - **同站换车无需任何规则**；
  - 同一班车（trip 实例）连续经停不计算换乘；
  - 零秒换乘边构成的环由标签 Pareto 去重立即终止，不会死循环；
- **乘降限制**：`pickup_type=1` 的站不上客，`dropoff_type=1` 的站不落客；
- **结果筛选**：按 `(到达时刻, 换乘次数, 班次ID序列)` 保留非支配行程；
  到达时刻与换乘次数完全相同时，只保留**班次 ID 序列字典序最小**的一条；
- **特例**：起终站相同返回 1 条**零段**行程；窗口内无可行行程返回**空列表**。

每条行程列出每个乘车段：线路/班次、上下车站点及**上车、下车时刻**（绝对本地时间）。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/api/status` | 当前数据集版本、时区与统计 |
| POST | `/api/import` | 上传 GTFS ZIP（multipart 字段 `file`） |
| GET  | `/api/stops?q=&limit=` | 按 ID/名称模糊检索站点 |
| GET  | `/api/plan?origin=&destination=&date=YYYY-MM-DD&time=HH:MM&max_transfers=` | 换乘查询 |

`/api/plan` 返回示例（无解封在 `journeys` 数组）：

```json
{
  "timezone": "Asia/Shanghai",
  "journeys": [
    {
      "transfers": 1,
      "departure_time": "2026-10-09T23:50:00",
      "arrival_time": "2026-10-10T02:00:00",
      "segments": [
        {"trip_id": "T100", "route_name": "1路",
         "from_stop_id": "S1", "to_stop_id": "S2",
         "board_time": "2026-10-09T23:50:00",
         "alight_time": "2026-10-10T00:30:00"},
        {"trip_id": "T200", "route_name": "2路",
         "from_stop_id": "S3", "to_stop_id": "S6",
         "board_time": "2026-10-10T00:50:00",
         "alight_time": "2026-10-10T02:00:00"}
      ]
    }
  ]
}
```

## 目录结构

```
app/
  main.py        # FastAPI 路由（上传/检索/查询/静态页面）
  db.py          # SQLite 连接、表结构与索引
  importer.py    # GTFS ZIP 解析、整包校验、事务化版本导入
  planner.py     # 多准则连接扫描（CSA）引擎
  static/        # 纯 JS 查询台页面（index.html + app.js）
tests/
  make_fixtures.py   # 生成小网络/10 万站时性能/坏包 GTFS ZIP
  test_planner.py    # 端到端规则与 ≤2 秒性能验收
  test_edge_cases.py # tie-break、乘降限制、换乘耗时、Pareto 等边界
data/            # SQLite 数据持久化目录（compose 挂载）
```

## 测试

```bash
python3 tests/make_fixtures.py   # 生成 tests/fixtures/*.zip
python3 tests/test_planner.py    # 全部业务规则 + 10 万站时/≤2 秒性能
python3 tests/test_edge_cases.py
```

性能验收口径：100,000 条站时的数据集上，结果不超过 10 条的服务端查询
在普通容器内约 0.8–1.0 秒返回（要求 ≤ 2 秒）。

## 说明与取舍

- 查询时区：使用数据集中首个 agency 的时区（页面与 `/api/status` 会显示）；
  站时以服务日本地午夜为零点，因此跨夜与日历日运算均为该时区下的绝对秒数。
- 为保证 2 秒响应，连接抓取走 `stop_times(dataset_id, departure_time)` 索引
  的两阶段查询，避免对 10 万行级表反复做窗口函数全扫描；
  生成的连接集已与参考窗口函数 SQL 做过逐行比对（0 差异）。
