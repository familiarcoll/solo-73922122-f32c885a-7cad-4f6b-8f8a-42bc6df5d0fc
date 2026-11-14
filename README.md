# 港口潮位 UTC 复核发布系统

记录仪导出的是 **America/New_York 本地时间**水位 CSV，预测模型只接受 **UTC**。
本服务提供上传、逐行复核（含夏令时处理）、UTC 冲突检测与发布下载，
基于 **FastAPI + SQLite + Jinja2**，用 **Docker Compose** 一键启动。

## 快速启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库保存在命名卷 `tide-data`（容器内 `/data/tide.db`），
重启容器后复核会话与历史发布仍然保留。

本地不用 Docker 直接运行（需要 Python 3.11+，含 tzdata）：

```bash
pip install -r requirements.txt
TIDEDB_PATH=./data/tide.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## CSV 输入格式

表头固定 5 列（也接受常见别名与中文列名）：

| 列名 | 说明 |
|---|---|
| `device_id` | 设备号 |
| `source_record_id` | 源记录号 |
| `revision` | 修订号（整数） |
| `local_time` | NY 本地墙钟时间，不带时区，如 `2024-11-03 01:30:00` |
| `water_level` | 水位，必须是有限数值（NaN/Inf 报错） |

编码支持 UTF-8（含 BOM）与 GB18030。示例文件见 `sample_data/`。

## 业务规则与处理流程

1. **修订处理**（同 `device_id + source_record_id`）
   - 只保留**最高修订号**的行，其余标记为「已取代」并展示；
   - 同修订号重复且**内容完全一致** → 折叠为一行并记录行号；
   - 同修订号**内容不一致** → 硬性错误（给出两个 CSV 行号）。
2. **夏令时判定**（PEP 495 `fold` + `zoneinfo`，按两种 fold 的 UTC 与墙钟往返分类）
   - 春季向前跳时中**不存在的本地时刻** → 硬性错误；
   - 秋季回退的**重复一小时** → 数据员在复核页对每行选择
     **早时段（EDT, fold=0）/ 晚时段（EST, fold=1）**，选择前该行无 UTC 值。
3. **UTC 冲突**：同设备在同一 UTC 时刻对应**不同源记录**时，列出冲突并阻止发布；
   不同设备共享 UTC 时刻不算冲突。
4. **发布条件**：硬性错误为 0、所有重复时段已选择、无 UTC 冲突，三者同时满足才可发布。
5. **发布产物**：按设备号 + UTC 时刻排序的 UTC CSV
   （`device_id, source_record_id, revision, utc_time, water_level`，时间形如 `2024-11-03T05:30:00Z`）。

### 会话隔离（跨浏览器失效）

- 每次上传生成带随机 ID 的复核会话，数据库中以自增序号管理；
- 新文件上传后，**所有旧会话立即置为 `superseded`**，仅最新会话为 `active`；
- 旧复核页仍可只读查看，但提交早/晚选择或发布会返回 **HTTP 409**，
  数据绝不会写入新文件；页面每 4 秒轮询 `/api/session/{id}/status` 自动锁定表单；
- 这保证了「预览和选择只对本次文件有效」，即使另一个浏览器上传了新文件。

### 失败不影响上一份结果

每次成功发布的完整 CSV 都存一行记录，`/download/latest` 永远返回最新一份。
后续文件校验失败或无法发布时，上一份完整结果依然可以下载（页面顶部常驻入口）。

### 并发安全：改选与发布的原子性（多进程）

复核页可在两个窗口同时打开，因此「保存早/晚选择」与「发布」可能并发到达。
服务以多 worker 进程运行（见 `docker-compose.yml`），多个进程共用同一个
SQLite 文件，靠以下机制保证顺序确定：

- SQLite 运行在 **WAL** 模式，并设置 `busy_timeout`（10 秒）：写事务互斥，
  抢不到写锁的请求等待而非立刻报错；
- 所有写操作（保存选择、发布、上传）都以 **`BEGIN IMMEDIATE`** 显式开启，
  发布是**单条写事务**：先拿写锁，再在锁内**重新读取全部行并重新校验**
  （含同设备同一 UTC 时刻的冲突），最后才插入 CSV 快照并提交。

由此两种交错都有确定结果：

1. **改选先提交**：发布在锁内看到最新选择，若两条不同源记录此时落在同一
   UTC 时刻即判定冲突，整事务回滚——**不登记新发布**，上一份完整 CSV 仍是
   `/download/latest` 可下载的内容；
2. **发布先提交**：已发布的 CSV 是提交时刻的不可变快照，之后再改选只会更新
   复核会话里的选择，**不会改变已发布 CSV 的任何一行**。

发布事务若校验不通过会返回 422 并在复核页展示原因；并发实现细节见 `app/db.py`
顶部的模块说明，交错测试见 `tests/test_race_condition.py`。

## 页面与接口

| 方法/路径 | 作用 |
|---|---|
| `GET /` | 上传页、规则说明、最新发布入口 |
| `POST /upload` | 上传 CSV（multipart），成功后 303 跳转到复核页 |
| `GET /review/{session_id}` | 复核页：错误、冲突、逐行选择、发布按钮 |
| `POST /review/{session_id}/choice` | 提交某行早/晚选择（表单：`line_no`, `choice=early|late`） |
| `POST /review/{session_id}/publish` | 校验通过则发布并重定向，否则 422 展示剩余问题 |
| `GET /api/session/{session_id}/status` | 会话有效状态（供页面轮询） |
| `GET /download/latest` | 下载最新已发布 UTC CSV |
| `GET /download/publication/{id}` | 下载指定历史发布 |

## 目录结构

```
app/
  tz.py          # NY 本地时间 normal/gap/ambiguous 分类与 UTC 换算
  models.py      # 数据类
  parser.py      # CSV 解析 + 修订处理 + 时间分类
  publishing.py  # 早/晚选择、UTC 冲突检测、发布 CSV 渲染
  db.py          # SQLite 持久化、会话代际控制、发布存档
  main.py        # FastAPI 路由
  templates/     # Jinja2 页面（上传/复核/失效提示）
sample_data/     # 可直接上传的正常与异常示例
tests/           # pytest 用例（时区/解析/发布/端到端/竞态交错）
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

覆盖：gap/ambiguous/normal 三类时间、最高修订与同修订冲突、NaN 拒绝、
UTC 冲突阻止发布、重复时段选择后可发布、跨浏览器旧会话 409、
发布失败后上一份结果仍可下载，以及 `test_race_condition.py` 中
改选/发布两种交错的定向测试（含两个 OS 进程共用一个 SQLite 文件的场景）。
