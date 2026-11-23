# 女子 1500 米纪录沿革档案

FastAPI + SQLite 服务，接收成绩公告、维护女子 1500 米纪录沿革，并保留每一版的可回查快照。

## 启动

```bash
docker compose up -d --build
```

- API 文档：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/health>
- 数据保存在命名卷 `records-data`（容器内 `/data/records.db`），重建容器不丢数据。
- 服务启动时自动建库，并创建第 1 版空基线（尚无成绩）。

运行自测脚本（补登、过期提交、撤销、回查旧版）：

```bash
./scripts/selftest.sh                      # 默认 http://localhost:8000
BASE_URL=http://localhost:8000 ./scripts/selftest.sh
```

## 领域规则

**成绩**由成绩编号（`result_id`，字符串）、选手（`athlete`）、完赛时刻（`finish_time`）和毫秒用时（`duration_ms`）组成。完赛时刻统一规范化为 UTC ISO-8601（微秒精度、`Z` 结尾），因此可直接按字典序排序。

**每版沿革**：取该版当时所有有效成绩，按 `(finish_time, result_id)` 升序排列，依次比较毫秒用时：

| 类型 | 含义 |
| --- | --- |
| `first` | 排序后的第一条成绩，即该版的首个纪录 |
| `refresh` | 比当前纪录用时更短 |
| `tie` | 与当前纪录用时完全相同（追平） |

比当前纪录慢的成绩不进入沿革，但仍计入该版的有效成绩。追平不改变纪录保持者；后续成绩仍与最早创造该用时的成绩比较。每条沿革都带 `result_id`、选手、完赛时刻和用时。

**公告与版本**：一次提交 = 一个公告，指定 `base_version`，可同时含多条补登与撤销。

- `base_version` 必须等于提交时的最新版，否则返回 `409 stale_base_version`。
- 由于版本号单调递增，且发版在同一写事务内校验，两个基于同一版的公告最多只有一个能发布成功。
- 撤销只需成绩编号；编号不存在返回 `404 unknown_result`，已撤销返回 `409 already_withdrawn`。
- 成绩编号全局唯一且不会被复用：重复补登返回 `409 duplicate_result_id`，复用已撤销编号返回 `409 retired_result_id`。
- 同一公告内编号重复、或同一编号既补登又撤销，返回 `422 invalid_announcement`。

**重算范围**：补登或撤销后，取本次改动涉及的成绩完赛时刻的最小值 `recompute_from_finish_time`（撤销用其在原版中的完赛时刻）。沿革中完赛时刻严格早于该值的条目原样复用（`reused_prefix_entries`），从该时刻起的有效成绩重新分类计算（`recomputed_active_results`）。响应会返回这两个计数和重算起点，便于核对。重算结果与"全量重算"一致（见测试）。

**旧版可查**：每一版的沿革与公告操作都按版号持久化；某版的有效成绩由全局登记簿中每条成绩的生效区间（`added_version` ≤ 版号 < `withdrawn_version`）实时重建，与当年整版快照的内容完全一致。发布新版不影响任何历史版，`GET /versions/{version}` 随时返回当时的结果。

**存储去重**：不再为每版另存一整份有效成绩。每发一份公告，登记簿只为本次补登各插入一行、为本次撤销各原地更新一行（写入 `withdrawn_version`），每版新增记录数只随本次补登和撤销的条数增长。撤销从发布撤销公告的版号起生效，被撤销的编号永久保留、拒绝复用。

**原子性**：校验、沿革写入、操作日志写入、全局登记簿更新全部在单个 `BEGIN IMMEDIATE` 事务中完成。提交过期、校验失败或任一写入失败都会整体回滚，已发布版本保持原样。

**数据卷升级**：服务启动时若发现旧版（v1，含整版快照表 `version_results`）数据卷，会在单个事务内完成合并升级：先校验登记簿能逐条重建全部历史快照（成员与字段完全一致），再重建沿革表、删除快照表并写入 `user_version = 2`。升级中断或写入失败会整体回滚——旧版服务仍能使用原数据卷，下次启动自动重试升级。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/announcements` | 提交公告并发布新版，成功返回 `201` 和新版详情 |
| `GET` | `/versions` | 按版号升序列出所有版本（`offset` / `limit`） |
| `GET` | `/versions/latest` | 最新版详情 |
| `GET` | `/versions/{version}` | 指定版详情：沿革、有效成绩、操作、重算信息 |
| `GET` | `/versions/{version}/progression` | 只取指定版的沿革 |
| `GET` | `/results?status=active\|withdrawn\|all` | 全局成绩登记簿 |
| `GET` | `/health` | 健康检查 |

### 示例

```bash
# 补登三条成绩（基于第 1 版空基线）
curl -X POST localhost:8000/announcements -H 'Content-Type: application/json' -d '{
  "base_version": 1,
  "supplements": [
    {"result_id":"R-001","athlete":"甲","finish_time":"2026-05-01T10:00:00Z","duration_ms":250000},
    {"result_id":"R-002","athlete":"乙","finish_time":"2026-05-02T10:00:00Z","duration_ms":240000},
    {"result_id":"R-003","athlete":"丙","finish_time":"2026-05-03T10:00:00Z","duration_ms":240000}
  ]
}'

# 撤销 R-002（只需成绩编号），基于第 2 版
curl -X POST localhost:8000/announcements -H 'Content-Type: application/json' \
  -d '{"base_version":2,"withdrawals":[{"result_id":"R-002"}]}'

# 回查旧版
curl localhost:8000/versions/2
```

错误响应统一为：

```json
{"detail": {"code": "stale_base_version", "message": "..."}}
```

## 实现结构

```
app/
  main.py          FastAPI 路由、异常映射、启动初始化
  schemas.py       请求/响应模型
  announcement.py  公告校验与纯函数式应用
  progression.py   排序与 first/refresh/tie 分类（支持前缀复用）
  service.py       事务发布、按版重建有效成绩、查询
  database.py      SQLite 连接、表结构与旧卷迁移
  timeutil.py      完赛时刻规范化
tests/             端到端测试（含回滚、迁移与存储增长测试）
scripts/selftest.sh 容器内自测脚本
```

核心表（schema v2）：

- `versions`：版号、父版号、发布时间、操作计数、重算起点与计数；`parent_version` 唯一索引保证一版最多一个后继。
- `version_progression`：每版的沿革条目（`seq`、`kind`、`result_id` 等）。
- `version_operations`：每版公告具体做了什么（补登/撤销及其成绩信息）。
- `results`：全局登记簿，每条成绩一行，`added_version`/`withdrawn_version` 界定其在哪些版有效；既用于编号唯一性和撤销定位，也用于重建任意版的有效成绩。历史查询不依赖额外快照表。

> v1 数据卷中的 `version_results`（每版整份有效成绩快照）会在启动迁移时合并进上述区间表示并删除；`PRAGMA user_version` 记录数据结构版本。

## 本地测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## 本地运行（不用 Docker）

```bash
pip install -r requirements.txt
APP_DB_PATH=./data/records.db uvicorn app.main:app --port 8000
```
