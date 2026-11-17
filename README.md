# 地震波形片段拼接服务（Waveform Assembly API）

把乱序到达的波形片段按 **台站 + UTC 分钟** 收集、校验、拼装，
仅对 **完整且无冲突** 的分钟生成不可变的发布记录，供震相识别程序取用。

- 框架：FastAPI（自带 `/docs` Swagger UI、`/redoc`）
- 存储：SQLite（WAL 模式，单文件，挂在 `/data` 卷上，重启数据不变）
- 并发：所有写操作走 `BEGIN IMMEDIATE` 事务，上传与发布交错时在同一已提交片段集合上串行执行

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
```

## 四、测试与自测

```bash
pip install pytest httpx
python -m pytest tests/ -v          # 22 个用例：校验/修订/拼装/发布/过期/重启/并发
python scripts/smoke.py             # 对运行中的服务做 32 项端到端检查
```

并发用例会启动真实 uvicorn 进程，用 6 个线程同时上传并尝试发布，
断言中间态发布只可能干净失败（409），完成态恰好产生一条完整记录，无半份数据。

## 五、目录结构

```
app/
  config.py      # 环境变量配置（WAVEFORM_DB_PATH）
  database.py    # SQLite 连接与建表
  models.py      # Pydantic 请求/响应模型
  service.py     # 事务、拼装算法、发布与过期逻辑
  main.py        # FastAPI 路由
tests/test_api.py
scripts/smoke.py
Dockerfile
docker-compose.yml
requirements.txt
```
