# 水培营养液配比求解 API

用 Python / FastAPI / SQLite 构建的水培营养液配比（mixing）求解服务。用户提交若干**浓缩液**各自的氮（N）、磷（P）、钾（K）及微量元素（Ca / Mg / S / Fe / Mn / Zn / B / Cu / Mo）浓度、可用体积与目标配方体积，服务用 **minimax 线性规划**（scipy / HiGHS）求出各浓缩液与稀释水的用量，目标是：

> **最小化各元素实际配出量相对目标量的最大相对偏差。**

求解中同时满足以下**硬约束**：

- **总量约束**：浓缩液 + 稀释水 = 目标配方体积；浓缩液合计占比可设上限。
- **单液上限**：每瓶浓缩液用量不超过其可用体积（可另设 `max_volume`）。
- **稀释水约束**：可设稀释水最小用量（强制保留稀释空间）。

当容差范围内无可行解时，会在 `violations` 中返回**具体超限元素与原因**（瓶子到顶导致供给不足、或成分配比耦合导致某元素无法压低等），并附带最优折中方案供参考。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

- 服务端口：**`8000`**（映射到宿主机同端口）
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- SQLite 数据文件保存在命名卷 `hydroponic-data`（容器内 `/data/hydroponic.db`），**容器启动时自动建表初始化**。

停止 / 清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除数据
```

<details>
<summary>不用 Docker，本地运行</summary>

```bash
pip install -r requirements.txt
DATABASE_PATH=./hydroponic.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

</details>

## 接口一览

| 方法 | 路径 | 说明 |
| ---- | ---- | ---- |
| POST | `/api/solve` | **试算**：仅求解，不入库 |
| POST | `/api/recipes?force=false` | **保存配方版本**：同名配方自动追加版本号 |
| GET | `/api/recipes` | 配方列表 |
| GET | `/api/recipes/{id}` | 配方详情（含最新版本号） |
| GET | `/api/recipes/{id}/versions` | 某配方的全部版本 |
| GET | `/api/recipes/{id}/versions/{v}` | 读取某版本完整内容（请求 + 结果） |
| GET | `/api/recipes/{id}/versions/{v}/export` | **导出该版本 JSON 文件** |
| POST | `/api/recipes/{id}/versions/{v}/consumption-forecast` | **库存消耗预测**：按灌溉计划逐批扣减浓缩液，输出逐日消耗、剩余库存与首次不足日期 |
| POST | `/api/irrigation/plan` | **灌溉计划**：按 IANA 时区生成逐日执行批次（DST/跨午夜/容量校验） |
| GET | `/health` | 健康检查 |

### 单位约定

- 体积：**mL**；元素浓度：**mg/L**（以纯元素计，如 N、P、K、Ca、Mg…）。
- 目标量（mg）= 目标浓度 × 目标体积 ÷ 1000。
- 元素键大小写不敏感（`n`、`ca`、`FE` 都会归一化）；支持 `N/P/K/Ca/Mg/S/Fe/Mn/Zn/B/Cu/Mo`；拼错字段名（如把 `concentrations` 写成 `concentrates`）会直接返回 422 而不是被静默忽略。

## curl 示例

### 1. 试算

目标：配 10 L 工作液，N=180、P=50、K=85 mg/L；A 液富含 N/K，B 液富含 P/K；浓缩液最多占总体积 20%，至少加 7 L 稀释水，容差 5%。

下面这组数据恰好存在精确解：**A 液 1500 mL + B 液 125 mL + 水 8375 mL**。

```bash
curl -s http://localhost:8000/api/solve \
  -H 'Content-Type: application/json' \
  -d '{
    "target_volume_ml": 10000,
    "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
    "concentrates": [
      {"name": "A液", "available_volume": 2000,
       "concentrations": {"N": 1200, "K": 400}},
      {"name": "B液", "available_volume": 2000,
       "concentrations": {"P": 4000, "K": 2000}}
    ],
    "max_concentrate_fraction": 0.20,
    "min_water_ml": 7000,
    "tolerance": 0.05
  }'
```

成功时返回（节选）：

```json
{
  "feasible": true,
  "optimal": true,
  "max_relative_deviation": 0.0,
  "water_volume_ml": 8375.0,
  "concentrate_total_ml": 1625.0,
  "allocations": [
    {"concentrate": "A液", "volume_ml": 1500.0, "cap_ml": 2000.0},
    {"concentrate": "B液", "volume_ml": 125.0,  "cap_ml": 2000.0}
  ],
  "elements": [
    {"element": "N", "target_concentration": 180.0, "actual_concentration": 180.0,
     "target_amount_mg": 1800.0, "actual_amount_mg": 1800.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "P", "target_concentration": 50.0, "actual_concentration": 50.0,
     "relative_deviation": 0.0, "status": "ok"},
    {"element": "K", "target_concentration": 85.0, "actual_concentration": 85.0,
     "relative_deviation": 0.0, "status": "ok"}
  ]
}
```

### 2. 无可行解时的诊断信息

若浓缩液库存不足以达到目标（如 A 液只剩 50 mL），响应中 `feasible=false`，并给出具体元素与约束原因：

```json
{
  "feasible": false,
  "message": "无可行解：最优最大相对偏差为 96.67%，超过容差 5.00%，详见 violations。",
  "violations": [
    {
      "code": "ELEMENT_SHORTAGE",
      "element": "N",
      "actual": 60.0,
      "limit": 1710.0,
      "message": "元素 N 最多只能配出 60.000 mg，低于目标下限 1710.000 mg（目标 1800.000 mg，容差 5%）；受浓缩液可用体积/单液使用量上限约束，已到上限的浓缩液：A液。"
    }
  ]
}
```

`violations.code` 取值：

- `WATER_MIN_EXCEEDS_VOLUME` —— 稀释水下限超过目标总体积；
- `CONCENTRATE_FRACTION_CONFLICT` —— 稀释水下限与浓缩液最大占比互相冲突；
- `ELEMENT_SHORTAGE` —— 某元素即使所有相关浓缩液用到上限仍不足（附到达上限的瓶子）；
- `ELEMENT_SURPLUS` —— 满足其他元素下限时该元素必然超量（浓缩液成分耦合）；
- `ELEMENT_DEVIATION` —— 多个元素互相制约，最优折中仍超出容差；
- `TOLERANCE_UNREACHABLE` —— 兜底说明。

### 3. 保存配方版本

```bash
curl -s -X POST http://localhost:8000/api/recipes \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "叶菜通用配方",
    "description": "生菜/小白菜通用",
    "note": "v1 基线",
    "request": {
      "target_volume_ml": 10000,
      "targets": {"concentrations": {"N": 180, "P": 50, "K": 85}},
      "concentrates": [
        {"name": "A液", "available_volume": 2000,
         "concentrations": {"N": 1200, "K": 400}},
        {"name": "B液", "available_volume": 2000,
         "concentrations": {"P": 4000, "K": 2000}}
      ],
      "max_concentrate_fraction": 0.20,
      "min_water_ml": 7000,
      "tolerance": 0.05
    }
  }'
```

> 超出容差的试算默认不允许保存（HTTP 422 并返回求解结果）；确认要留档时加 `?force=true`。
> 同名配方再次保存会自动生成 `version: 2、3 …`，历史版本不可变。

### 4. 查询 / 导出 JSON

```bash
# 列出所有配方
curl -s http://localhost:8000/api/recipes

# 某配方的版本列表
curl -s http://localhost:8000/api/recipes/1/versions

# 读取第 1 版完整内容
curl -s http://localhost:8000/api/recipes/1/versions/1

# 导出为 JSON 文件（Content-Disposition attachment）
curl -s -OJ http://localhost:8000/api/recipes/1/versions/1/export
```

## 灌溉计划（POST /api/irrigation/plan）

给定 **IANA 时区**、日期范围、每日若干开始时间与持续分钟数、每次目标水量以及设备**每日容量**，服务生成逐日执行批次，并处理好三件麻烦事：

- **跨午夜**：批次用真实流逝时间的 UTC 区间表示，结束时间落在次日也能正确显示；
- **夏令时**：春季跳变时**不存在的本地时间**自动顺延（如纽约 `02:30` 缺失 → `03:30 EDT`，批次上标注 `dst_event: "gap"`）；秋季回拨时**重复的本地时间**取第一次出现（`"duplicate"`，并说明第二次出现的 UTC 偏移）；
- **合并与容量**：按 UTC 开始时间排序，相邻且**间隔 ≤ 1 分钟（含重叠）**的批次合并（合并可以跨越午夜）；每日容量按**合并前**当日计划总水量校验，超出的日期返回**明确缺口**（`shortfall_ml`），批次照常返回、**绝不截断水量**。

### 请求

```bash
curl -s http://localhost:8000/api/irrigation/plan \
  -H 'Content-Type: application/json' \
  -d '{
    "timezone": "Asia/Shanghai",
    "start_date": "2026-05-01",
    "end_date": "2026-05-03",
    "sessions": [
      {"start_time": "08:00",     "duration_minutes": 10, "water_ml": 500},
      {"start_time": "08:10:30",  "duration_minutes": 5,  "water_ml": 200},
      {"start_time": "23:55",     "duration_minutes": 10, "water_ml": 300}
    ],
    "daily_capacity_ml": 1100
  }'
```

字段说明：

| 字段 | 说明 |
| ---- | ---- |
| `timezone` | IANA 时区名（如 `Asia/Shanghai`、`America/New_York`、`Europe/London`）；非法名称返回 422 |
| `start_date` / `end_date` | 本地日历日期，含首尾；范围最多 370 天，`end < start` 返回 422 |
| `sessions[].start_time` | 每日重复的本地开始时间 `HH:MM` 或 `HH:MM:SS`（24 小时制）；同一日时间不可重复 |
| `sessions[].duration_minutes` | 持续分钟数，必须为正；批次按真实流逝时间结束，可自然跨过午夜与 DST 跳变 |
| `sessions[].water_ml` | 本次目标水量 (mL)，必须为正 |
| `daily_capacity_ml` | 设备每日供水容量 (mL)，非负（`0` 表示当日不可供水，必然产生缺口） |

### 响应（节选）

`08:00` 的批次在 `08:10` 结束，与 `08:10:30` 开始的批次只隔 30 秒（≤ 1 分钟），被合并成一个 700 mL、`08:00–08:15:30` 的批次；`23:55` 的批次跨到次日 `00:05`：

```json
{
  "timezone": "Asia/Shanghai",
  "start_date": "2026-05-01",
  "end_date": "2026-05-03",
  "days": 3,
  "batch_count_before_merge": 9,
  "batch_count_after_merge": 6,
  "merge_gap_seconds": 60,
  "planned_water_ml": 3000.0,
  "daily_capacity_ml": 1100,
  "capacity_ok": true,
  "capacity_violations": [],
  "day_results": [
    {
      "date": "2026-05-01",
      "batch_count_before_merge": 3,
      "batch_count_after_merge": 2,
      "planned_water_ml": 1000.0,
      "daily_capacity_ml": 1100,
      "within_capacity": true,
      "shortfall_ml": 0.0,
      "dst_events": []
    }
  ],
  "batches": [
    {
      "start_utc": "2026-05-01T00:00:00Z",
      "end_utc": "2026-05-01T00:15:30Z",
      "local_start": "2026-05-01T08:00:00+0800",
      "local_end": "2026-05-01T08:15:30+0800",
      "duration_seconds": 930,
      "water_ml": 700.0,
      "merged": true,
      "batch_count_before_merge": 2,
      "dst_event": null,
      "crosses_midnight": false,
      "sources": [
        {"day": "2026-05-01", "requested_time": "08:00:00",
         "local_start": "2026-05-01T08:00:00+0800", "water_ml": 500.0},
        {"day": "2026-05-01", "requested_time": "08:10:30",
         "local_start": "2026-05-01T08:10:30+0800", "water_ml": 200.0}
      ]
    },
    {
      "start_utc": "2026-05-01T15:55:00Z",
      "end_utc": "2026-05-01T16:05:00Z",
      "local_start": "2026-05-01T23:55:00+0800",
      "local_end": "2026-05-02T00:05:00+0800",
      "duration_seconds": 600,
      "water_ml": 300.0,
      "merged": false,
      "batch_count_before_merge": 1,
      "crosses_midnight": true,
      "sources": [/* ... */]
    }
  ]
}
```

### 夏令时与容量缺口

纽约 2024-03-10 春季跳变，请求 `02:30` 浇水 30 分钟：该本地时间不存在，批次顺延到 `03:30 EDT`（UTC `07:30Z`）并在批次与 `day_results[].dst_events` 上标注：

```json
{
  "start_utc": "2024-03-10T07:30:00Z",
  "end_utc": "2024-03-10T08:00:00Z",
  "local_start": "2024-03-10T03:30:00-0400",
  "dst_event": "gap",
  "dst_note": "请求的本地时间 2024-03-10T02:30:00 不存在（夏令时跳变，缺失 60 分钟），已顺延到 2024-03-10T03:30:00-04:00 执行。"
}
```

当某日计划总量超过 `daily_capacity_ml` 时，接口仍返回 **200**（这是计划结果而非请求错误），并在顶层与每日结果中给出缺口，批次水量保持完整：

```json
{
  "capacity_ok": false,
  "capacity_violations": [
    {
      "date": "2026-05-01",
      "planned_water_ml": 1000.0,
      "daily_capacity_ml": 900,
      "shortfall_ml": 100.0,
      "message": "2026-05-01 计划用水 1000 mL，超出设备日容量 900 mL，缺口 100 mL；批次未截断，请扩容或减少当日批次。"
    }
  ]
}
```

以下情况一律返回 **422**：非法时区（`Mars/Olympus`、`+08:00`、空串）、日期顺序颠倒或超过 370 天、时间格式非法（`25:00`）、`sessions` 为空或开始时间重复、持续分钟数/水量非正、容量为负、多余字段。

## 浓缩液库存消耗预测（POST /api/recipes/{id}/versions/{v}/consumption-forecast）

在某个**已保存的配方版本**上，给出未来一段灌溉排程下各浓缩液的库存消耗预测。服务先读取该版本求解结果中的配比，再把灌溉计划展开为**实际执行批次**（与 `/api/irrigation/plan` 同一引擎，已处理 DST、跨午夜与相邻批次合并），然后：

- **等比例扣减**：每批次所需浓缩液 `i` = `批次水量 × allocation_i / 目标配方总体积`。合并批次按合并后总水量只扣一次，跨午夜的单个会话也只扣一次。
- **按本地日归集**：批次计入其**本地开始时间所在的日历日**（合并跨午夜的批次整体计入开始日，绝不拆分或重复计）。输出每日所需 / 实取 / 缺口 / 日末库存。
- **库存不足也保留完整预测**：库存按批次累计扣减；某瓶不够时，该批次仍给出完整的 `required_ml`，同时用 `served_ml` / `shortfall_ml` 与 `shortages` 标记缺口，剩余库存夹在 **0**（绝不为负），并返回 `first_shortage_date`（首次出现任一浓缩液不足的本地日期）。

### 请求

```bash
curl -s http://localhost:8000/api/recipes/1/versions/1/consumption-forecast \
  -H 'Content-Type: application/json' \
  -d '{
    "plan": {
      "timezone": "Asia/Shanghai",
      "start_date": "2026-05-01",
      "end_date": "2026-05-03",
      "sessions": [
        {"start_time": "08:00",    "duration_minutes": 10, "water_ml": 500},
        {"start_time": "08:10:30", "duration_minutes": 5,  "water_ml": 200},
        {"start_time": "23:55",    "duration_minutes": 10, "water_ml": 300}
      ],
      "daily_capacity_ml": 1100
    },
    "stocks": [
      {"name": "A液", "volume_ml": 2000},
      {"name": "B液", "volume_ml": 500}
    ]
  }'
```

字段说明：

| 字段 | 说明 |
| ---- | ---- |
| `plan` | 完整的灌溉计划参数，字段与校验规则同 [POST /api/irrigation/plan](#灌溉计划post-apirrigationplan)（时区/日期范围/每日时段与水量/日容量） |
| `stocks[].name` | 浓缩液名，必须与该版本求解结果 `allocations[].concentrate` 完全一致；出现未知液体或缺少某瓶（空瓶也要显式填 `0`）返回 422 |
| `stocks[].volume_ml` | 当前库存 (mL)，必须为有限非负数；负数、`NaN`、`Infinity` 返回 422 |

> 该配方版本若以 `?force=true` 保存了不可行结果，预测仍按其中的最优折中配比计算，但响应中 `recipe_feasible=false` 以示提醒。

### 响应（节选与说明）

以 10 L 配方（A 液 1500 mL、B 液 125 mL，配比 0.15 / 0.0125）为例，每个 700 mL 的合并批次需要 A 液 105 mL、B 液 8.75 mL：

```json
{
  "recipe_id": 1,
  "version": 1,
  "recipe_feasible": true,
  "recipe_message": "求解成功：所有目标元素偏差均在容差范围内。",
  "timezone": "Asia/Shanghai",
  "start_date": "2026-05-01",
  "end_date": "2026-05-03",
  "mix_total_ml": 10000.0,
  "dosage": [
    {"concentrate": "A液", "allocation_ml": 1500.0, "mix_total_ml": 10000.0,
     "ratio": 0.15, "ml_per_liter_water": 150.0},
    {"concentrate": "B液", "allocation_ml": 125.0, "mix_total_ml": 10000.0,
     "ratio": 0.0125, "ml_per_liter_water": 12.5}
  ],
  "initial_stock_ml": {"A液": 2000.0, "B液": 500.0},
  "total_water_ml": 3000.0,
  "total_required_ml": {"A液": 450.0, "B液": 37.5},
  "total_served_ml": {"A液": 450.0, "B液": 37.5},
  "total_shortfall_ml": {"A液": 0.0, "B液": 0.0},
  "final_stock_ml": {"A液": 1550.0, "B液": 462.5},
  "stock_sufficient": true,
  "first_shortage_date": null,
  "day_results": [
    {
      "date": "2026-05-01",
      "batch_count": 2,
      "water_ml": 1000.0,
      "required_ml": {"A液": 150.0, "B液": 12.5},
      "served_ml": {"A液": 150.0, "B液": 12.5},
      "shortfall_ml": {"A液": 0.0, "B液": 0.0},
      "end_stock_ml": {"A液": 1850.0, "B液": 487.5},
      "stock_sufficient": true
    }
  ],
  "batches": [
    {
      "date": "2026-05-01",
      "start_utc": "2026-05-01T00:00:00Z",
      "local_start": "2026-05-01T08:00:00+0800",
      "local_end": "2026-05-01T08:15:30+0800",
      "water_ml": 700.0,
      "merged": true,
      "batch_count_before_merge": 2,
      "crosses_midnight": false,
      "required_ml": {"A液": 105.0, "B液": 8.75},
      "served_ml": {"A液": 105.0, "B液": 8.75},
      "shortfall_ml": {"A液": 0.0, "B液": 0.0},
      "end_stock_ml": {"A液": 1895.0, "B液": 491.25},
      "stock_sufficient": true,
      "shortages": []
    }
  ]
}
```

当某瓶在计划开始前只剩 150 mL（恰够第一天），**后续批次的预测不削减**，缺口在批次上显式标出，库存不会出现负数：

```json
{
  "stock_sufficient": false,
  "first_shortage_date": "2026-05-02",
  "total_required_ml": {"A液": 450.0, "B液": 37.5},
  "total_served_ml": {"A液": 150.0, "B液": 37.5},
  "total_shortfall_ml": {"A液": 300.0, "B液": 0.0},
  "final_stock_ml": {"A液": 0.0, "B液": 462.5}
}
```

首个不足批次（05-02 第一个 700 mL 合并批次，A 液已无库存）示例：

```json
{
  "date": "2026-05-02",
  "water_ml": 700.0,
  "required_ml": {"A液": 105.0, "B液": 8.75},
  "served_ml": {"A液": 0.0, "B液": 8.75},
  "shortfall_ml": {"A液": 105.0, "B液": 0.0},
  "end_stock_ml": {"A液": 0.0, "B液": 478.75},
  "stock_sufficient": false,
  "shortages": [
    {"concentrate": "A液", "required_ml": 105.0,
     "served_ml": 0.0, "shortfall_ml": 105.0}
  ]
}
```

### 错误响应

| 状态码 | 情形 |
| ---- | ---- |
| 404 | `recipe_id` 不存在（`配方不存在`），或该配方下 `version` 不存在（`该版本不存在`） |
| 422 | 库存中出现该版本**未知液体**（detail 给出未知名称与本版本全部液体名）；**缺少**某瓶液体的库存条目；库存为负 / `NaN` / 无穷大 / 非数字；库存条目重名 |
| 422 | 灌溉计划校验失败：非法时区、日期顺序颠倒或超 370 天、非法时间、`sessions` 为空或时间重复、水量/时长非正、容量为负、多余字段等（规则同灌溉计划接口） |

## 项目结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由（试算 / 保存 / 版本 / 导出 / 灌溉计划 / 消耗预测）
│   ├── solver.py     # minimax LP 建模与无可行解诊断
│   ├── irrigation.py # 灌溉批次生成：时区/DST 解析、跨午夜、合并、容量校验
│   ├── forecast.py   # 浓缩液消耗预测：按实际批次等比例扣减、缺口标记
│   ├── models.py     # Pydantic 请求/响应模型
│   ├── database.py   # SQLite 初始化与配方/版本存取
│   └── config.py     # 元素清单、默认容差、DB 路径
├── Dockerfile
├── docker-compose.yml
├── smoke_test.py     # TestClient 端到端冒烟测试
└── requirements.txt
```
