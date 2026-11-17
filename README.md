# 地震波形拼接与震级换算服务（Waveform Assembly API）

把乱序到达的波形片段按 **台站 + UTC 分钟** 收集、校验、拼装，
仅对 **完整且无冲突** 的分钟生成不可变的发布记录，供震相识别程序取用；
再依据台站 **校准记录**（零点计数、微伽/计数增益）把已发布的计数波形
逐秒换算为微伽值，生成可按号重读的换算记录。

- 框架：FastAPI（自带 `/docs` Swagger UI、`/redoc`）
- 存储：SQLite（WAL 模式，单文件，挂在 `/data` 卷上，重启数据不变）
- 并发：所有写操作走 `BEGIN IMMEDIATE` 事务，上传、发布、校准、换算交错时
  在同一已提交快照上串行执行，失败整体回滚，不留半份记录

## 一、启动

### Docker Compose（推荐）

```bash
docker compose up --build
# API:    http://localhost:8000
# 文档:   http://localhost:8000/docs
# 健康:   http://localhost:8000/health
```

数据库保存在命名卷 `waveform-data`（容器内 `/data/waveform.db`）。

### 本地直接运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
WAVEFORM_DB_PATH=./data/waveform.db uvicorn app.main:app --reload
```

## 二、数据模型与规则

片段（fragment）：

| 字段 | 约束 |
| --- | --- |
| `station` | 非空字符串，台站代码 |
| `minute_utc` | 整分钟 UTC 时间（秒必须为 0），如 `2026-11-17T05:06:00Z` |
| `fragment_no` | 片段号，非负整数 |
| `revision` | 修订号，**正整数** |
| `start_sample` | 起始采样位，0–59 |
| `samples` | 非空整数数组；`start_sample + len(samples) ≤ 60`，越界拒绝（422） |

修订与重传规则（同一 `station + minute_utc + fragment_no`）：

- 修订号 **更高** → 接受并覆盖旧版（`status: "superseded"`）
- 修订号 **更低** → 拒绝（409），旧数据不动
- 同修订号、**同内容**（SHA-256 一致）→ 幂等重传，沿用原结果（`status: "unchanged"`），不产生新行
- 同修订号、**异文** → 拒绝（409），旧数据不动

拼装（assembly）：

- 每分钟固定 60 个 1 Hz 采样位
- 缺位在 `gaps` 中逐个列出；覆盖数见 `coverage`
- 重叠位数值不同即为冲突，`conflicts` 给出 **位置、涉及片段号、各自数值**；
  重叠但数值相同不算冲突
- `complete = 60 位全覆盖且无冲突`

发布（publication）：

- 只有完整且无冲突的分钟可发布；缺位或冲突时返回 409 并在 `detail` 中说明，
  **失败不留下任何半份记录**（单事务回滚）
- 发布记录冻结当时的 60 点波形 `wave`、每个片段的修订号与内容（`items`），
  以及片段集合指纹 `fragment_set_hash`
- 同一片段集合重复发布是幂等的，返回同一条记录
- 之后补传/更正只改变当前拼装；该分钟片段集合一旦变化，旧记录自动标 `stale=true`，
  仍可按 ID 读取，波形与修订保持冻结
- 每份记录只来自同一个已提交片段集合（事务快照保证）

校准（calibration）：

| 字段 | 约束 |
| --- | --- |
| `station` | 非空字符串，台站代码 |
| `effective_at` | 整秒 UTC 时间（微秒必须为 0），校准自该秒起生效 |
| `revision` | 修订号，**正整数** |
| `zero_count` | 零点计数，整数 |
| `gain` | 增益（微伽/计数），**非零整数**（允许负增益） |

修订与重传规则（同一 `station + effective_at`，与片段一致）：

- 修订号 **更高** → 接受并覆盖旧版（`status: "superseded"`）
- 修订号 **更低** → 拒绝（409），旧数据不动
- 同修订号、**同内容**（零点与增益的 SHA-256 一致）→ 幂等重传（`status: "unchanged"`），不产生新行
- 同修订号、**异文** → 拒绝（409），旧数据不动

换算（conversion，计数 → 微伽）：

- 仅对 **当前未过期（stale=false）的发布记录** 发起；发布缺失或已过期返回 409，
  需先重新发布该分钟
- 分钟的 60 个采样位各取 **采样时刻最近生效**（`effective_at <= 采样时刻` 中最大者）
  的校准，按 `(计数 − zero_count) × gain` 计算整数微伽值
- 任一采样时刻没有可用校准（首个采样秒之前无生效记录）→ **整分钟拒绝**（409），
  失败不留下任何半份记录
- 换算记录固定 **发布号**（`publication_id`）、每个采样位所用校准的生效时刻与
  **修订号**（`items`）、60 个结果值（`values`）以及来源指纹，可按 ID 重读
- 同一发布 + 同一校准集合重复换算是幂等的，返回同一条记录
- 片段更正或校准追补/修订后，受影响的旧换算记录 **保留但标 `stale=true`**，
  结果与来源修订保持冻结；读取时还会按当前来源自愈过期标记
- 所有写入在单个 `BEGIN IMMEDIATE` 事务内完成，换算与写入交错时只会引用
  已提交且未失效的来源快照

## 三、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/fragments` | 上传片段（JSON body） |
| GET | `/fragments?station=&minute_utc=` | 查看某分钟当前片段 |
| GET | `/assembly?station=&minute_utc=` | 查看拼装状态：覆盖率、缺口、冲突 |
| POST | `/publications?station=&minute_utc=` | 发布完整无冲突分钟 |
| GET | `/publications?station=&minute_utc=` | 发布记录列表（可按台站/分钟过滤） |
| GET | `/publications/latest?station=&minute_utc=` | 该分钟最新一条记录 |
| GET | `/publications/{id}` | 按 ID 读取（过期记录同样可读） |
| POST | `/calibrations` | 上传/修订台站校准（JSON body） |
| GET | `/calibrations?station=` | 列出某台站全部校准（按生效时刻排序） |
| POST | `/conversions?station=&minute_utc=` | 把当前未过期发布换算为逐秒微伽值 |
| GET | `/conversions?station=&minute_utc=` | 换算记录列表（可按台站/分钟过滤） |
| GET | `/conversions/latest?station=&minute_utc=` | 该分钟最新一条换算记录 |
| GET | `/conversions/{id}` | 按 ID 读取换算记录（过期记录同样可读） |

### 典型流程

```bash
MIN=2026-11-17T05:06:00Z
# 乱序上传两个片段，恰好铺满 60 位
curl -s -X POST localhost:8000/fragments -H 'content-type: application/json' -d '{
  "station":"STA1","minute_utc":"'"$MIN"'","fragment_no":2,"revision":1,
  "start_sample":30,"samples":'"$(/workspace/.venv/bin/python -c 'print(list(range(30,60)))')"'}'
curl -s -X POST localhost:8000/fragments -H 'content-type: application/json' -d '{
  "station":"STA1","minute_utc":"'"$MIN"'","fragment_no":1,"revision":1,
  "start_sample":0,"samples":'"$(/workspace/.venv/bin/python -c 'print(list(range(0,30)))')"'}'

# 检查拼装
curl -s "localhost:8000/assembly?station=STA1&minute_utc=$MIN"
# 发布
curl -s -X POST "localhost:8000/publications?station=STA1&minute_utc=$MIN"

# 上传校准：自 05:05:00Z 起零点 10 计数、增益 2 微伽/计数
curl -s -X POST localhost:8000/calibrations -H 'content-type: application/json' -d '{
  "station":"STA1","effective_at":"2026-11-17T05:05:00Z",
  "revision":1,"zero_count":10,"gain":2}'
# 逐秒换算为微伽值（每个采样位 = (计数-10)*2）
curl -s -X POST "localhost:8000/conversions?station=STA1&minute_utc=$MIN"
# 按号重读
curl -s localhost:8000/conversions/1
```

## 四、测试与自测

```bash
pip install pytest httpx
python -m pytest tests/ -v          # 44 个用例：校验/修订/拼装/发布/过期/重启/并发
                                    #   + 校准修订语义、换算数学、过期与并发换算
python scripts/smoke.py             # 对运行中的服务做端到端检查（含校准与换算）
```

并发用例会启动真实 uvicorn 进程：片段侧用 6 个线程同时上传并尝试发布，
断言中间态发布只可能干净失败（409），完成态恰好产生一条完整记录，无半份数据；
校准侧用 8 个线程交错修订校准与发起换算，断言每份落库换算都内部完整、
来源一致，且失败的换算不留任何记录。

## 五、目录结构

```
app/
  config.py      # 环境变量配置（WAVEFORM_DB_PATH）
  database.py    # SQLite 连接与建表（fragments/publications/calibrations/conversions）
  models.py      # Pydantic 请求/响应模型
  service.py     # 事务、拼装算法、发布、校准与换算/过期逻辑
  main.py        # FastAPI 路由
tests/test_api.py
tests/test_calibration.py
scripts/smoke.py
Dockerfile
docker-compose.yml
requirements.txt
```
