# 病例月报封版 API

总院数仓汇编病例月报的服务。各院区到数有早有晚，本服务把「报送 — 封版 — 查询」
拆开，保证**新版未收齐之前，下游始终能查到上一版整表**：

- 各院区按病例归属统计，用 JSON 报送**当月全量计数**，每行含
  `乡镇 × 年龄组 × 病例数`；**零病例也要报空表**（`rows: []`）。
- 文件按 `统计月 + 院区 + 文件版本` 存入 SQLite；**同版本重传**时计数相同
  则沿用已收数据（幂等），不同则报 409，已收数据不被覆盖。
- 工程师**封版**时提交月份、月报版号和每个应报院区的指定文件版本：
  - **缺件** → 409，报出缺失的院区和文件版本，**保留上次封版结果**；
  - **收齐** → 按乡镇和年龄组加总病例数，**一次替换整张月报**（单事务）。
- 月报版号**只能切换到更高版号**；版本检查与整表替换在同一
  `BEGIN IMMEDIATE` 事务内完成，**并发封版也不会回退**。
- 下游按月查询**当前版号和整表**。

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 应用与路由（含 Swagger 示例，应用工厂）
│   ├── schemas.py     # Pydantic 请求/响应模型与演示示例
│   └── store.py       # SQLite 存储：收件幂等、封版事务、版本单调
├── tests/             # pytest：存储层 + API 层（含并发封版用例）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── requirements-dev.txt
```

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

启动后：

- Swagger UI：<http://localhost:8000/docs>（内置演示示例，可直接点 Try it out）
- ReDoc：<http://localhost:8000/redoc>
- 健康检查：<http://localhost:8000/health>

SQLite 数据库落在命名卷 `case-report-data`（容器内 `/data/reports.db`，
由环境变量 `CASE_REPORT_DB` 指定），`docker compose down` 不丢数据；
`docker compose down -v` 才会清空。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| PUT | `/api/v1/report-files/{month}/{campus}/{file_version}` | 院区报送当月全量计数文件（幂等） |
| GET | `/api/v1/report-files/{month}` | 查看该月已收到的全部院区文件 |
| GET | `/api/v1/report-files/{month}/{campus}/{file_version}` | 查看单个已收文件内容 |
| POST | `/api/v1/monthly-reports/finalize` | 封版：收齐指定版本后整表替换月报 |
| GET | `/api/v1/monthly-reports/{month}` | 下游查询当前版号与整表 |
| GET | `/health` | 健康检查 |

`month` 格式为 `YYYY-MM`；`file_version`、`report_version` 均为 ≥1 的整数。

## Swagger 演示：补齐迟到文件后封版并查询

场景：统计月 `2026-10`，应报院区为 **东城院区、西城院区、滨湖院区**，
月报版号从 1 开始。以下步骤既可在 Swagger UI 上逐个点选，也可用等价
curl 复现。

### 1. 先到的两个院区报送第 1 版文件

`PUT /api/v1/report-files/2026-10/东城院区/1`（Swagger 中选用内置示例
「东城院区 2026-10 第 1 版」）：

```json
{
  "rows": [
    {"township": "城关镇", "age_group": "0-17",  "count": 3},
    {"township": "城关镇", "age_group": "18-59", "count": 12},
    {"township": "桃源镇", "age_group": "60+",   "count": 2}
  ]
}
```

`PUT /api/v1/report-files/2026-10/西城院区/1`：

```json
{
  "rows": [
    {"township": "城关镇", "age_group": "0-17",  "count": 1},
    {"township": "青溪镇", "age_group": "18-59", "count": 7}
  ]
}
```

均返回 200 `{"status": "received", ...}`。

### 2. 滨湖院区迟到，封版报缺件

`POST /api/v1/monthly-reports/finalize`（选用内置示例「封版 2026-10
第 1 版月报」）：

```json
{
  "month": "2026-10",
  "report_version": 1,
  "files": [
    {"campus": "东城院区", "file_version": 1},
    {"campus": "西城院区", "file_version": 1},
    {"campus": "滨湖院区", "file_version": 1}
  ]
}
```

返回 **409**，指出缺失的院区和文件版本；已封版结果（若有）保持不变：

```json
{
  "status": "missing_files",
  "missing": [{"campus": "滨湖院区", "file_version": 1}],
  "current_report_version": null
}
```

此时 `GET /api/v1/monthly-reports/2026-10` 返回 404（该月从未封版）；
若此前已封过旧版，下游查到的仍是旧版整表。

### 3. 补齐迟到文件（零病例院区报空表）

`PUT /api/v1/report-files/2026-10/滨湖院区/1`（选用内置示例「零病例：
报送空表」）：

```json
{"rows": []}
```

### 4. 再次封版：整表一次替换

重发第 2 步的封版请求，返回 200 `finalized`，`rows` 为跨院区按
乡镇 × 年龄组加总后的整表：

```json
{
  "status": "finalized",
  "report_version": 1,
  "total_cases": 25,
  "rows": [
    {"township": "城关镇", "age_group": "0-17",  "count": 4},
    {"township": "城关镇", "age_group": "18-59", "count": 12},
    {"township": "桃源镇", "age_group": "60+",   "count": 2},
    {"township": "青溪镇", "age_group": "18-59", "count": 7}
  ]
}
```

### 5. 下游查询当前版号与整表

`GET /api/v1/monthly-reports/2026-10` → 200，含 `report_version`、
`finalized_at` 与整表 `rows`。

### 6. 可选：验证重传与版本规则

- 同版本同计数重传第 1 步的任意文件 → 200 `already_received`（沿用已收数据）；
- 同版本改计数重传 → 409 `file_conflict`，已收数据不被覆盖；
- 东城院区改报第 2 版文件后，以 `report_version: 2` 封版（东城取
  `file_version: 2`，其余院区仍取 1）→ 200，整表切换；
- 再用 `report_version: 1` 或 `2` 封版 → 409 `version_conflict`，
  版号不会回退。

## 状态码约定

| 场景 | 状态码 |
| --- | --- |
| 报送成功 / 同版本同计数重传 | 200，`received` / `already_received` |
| 同版本重传但计数不同 | 409，`file_conflict`（含已收与被拒摘要） |
| 封版缺件 | 409，`missing_files`（含缺失清单与当前版号） |
| 封版版号未高于当前版号 | 409，`version_conflict` |
| 查询未封版的月份 / 未收到的文件 | 404，`not_found` |
| 请求结构或取值非法（月份格式、负数、布尔、重复乡镇+年龄组等） | 422 |

## 并发与一致性设计

- 报送按 `(month, campus, file_version)` 主键落库，内容规范化（按
  乡镇、年龄组排序）后取 SHA-256 比对，重传判定与行顺序无关。
- 封版在单个 `BEGIN IMMEDIATE` 事务内完成「版号检查 → 缺件检查 →
  加总 → 整表 UPSERT」：任一步失败整体回滚，已封版结果不变；并发
  封版彼此串行，后进入者看到已提交的新版号并被拒绝，因此版号只会
  单调上升。测试包含 20 线程并发封版用例，断言最终版号为最大值。
- 月报整表只有一行记录（按 `month` 主键），封版即原子替换，下游任何
  时刻读到的都是某个完整版本，不会读到新旧混合的中间态。

## 本地开发（无 Docker 时）

```bash
python3 -m venv --without-pip .venv
.venv/bin/python get-pip.py            # 若 ensurepip 不可用
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
CASE_REPORT_DB=./reports.db .venv/bin/uvicorn app.main:app --reload --port 8000
.venv/bin/python -m pytest tests -q
```
