# 公交刷卡流水归站 API

把刷卡流水里的**临时站号**归属到**真实站点**，供月度客流分析。

- FastAPI + SQLite（WAL），单容器即可运行；
- 同一临时站号可被不同线路复用，映射按 **(线路, 临时站号)** 整组替换；
- 改站记录晚到也没关系：每次导入后旧流水会在同一事务里**重新归属**；
- 流水按流水号幂等：同号同内容重传不重复计数，同号不同内容整批拒收；
- 映射、流水、月度统计在同一事务提交，查询永远只见一致的已提交状态；
- 导入失败（校验错误 / 业务冲突）不改变已有结果。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000/docs
```

服务端口：`8000`。SQLite 数据落在命名卷 `transit_data`（容器内 `/data/transit.db`）。

## CSV 格式

**刷卡流水**（`POST /api/imports/swipes`）

```csv
txn_id,swipe_time_utc,line,temp_station_id
S001,2026-01-05T08:15:00Z,L1,T100
```

**站号映射**（`POST /api/imports/mappings`）

```csv
line,temp_station_id,valid_from_utc,valid_to_utc,real_station
L1,T100,2026-01-01T00:00:00Z,2026-02-01T00:00:00Z,Central Station
```

- 时间为 ISO 8601；无显式时区时按 UTC 处理，带偏移（如 `+08:00`）会归一到 UTC；
- 映射生效时段为**左闭右开** `[valid_from_utc, valid_to_utc)`；
- 文件需 UTF-8 编码（容忍 BOM），表头列名必须与上表一致。

## 业务规则

| 规则 | 行为 |
| --- | --- |
| 临时站号复用 | 映射键是 (线路, 临时站号)，不同线路互不影响 |
| 映射导入 | 按 (线路, 临时站号) **整组替换**；文件未涉及的组不受影响 |
| 组内时段重叠 | **整批拒收**，HTTP 409，已有映射不变 |
| 流水号重复 + 内容相同 | 跳过，不重复计数（安全重传） |
| 流水号重复 + 内容不同 | **整批拒收**，HTTP 409，已有流水不变 |
| 映射晚到 | 旧流水在新统计中自动重新归属 |
| 统计口径 | 按刷卡时刻（UTC）落入的映射时段归属真实站点，按 UTC 月份汇总 |
| 并发导入 | 写事务串行；查询走 WAL 快照，只见同一已提交状态 |

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/imports/swipes` | 导入刷卡流水 CSV |
| POST | `/api/imports/mappings` | 导入站号映射 CSV |
| GET | `/api/stats/monthly?month=YYYY-MM` | 真实站点 UTC 月上车量（month 可省） |
| GET | `/api/transactions/unattributed` | 未归属流水（含原因、分页） |
| GET | `/api/mappings` | 当前生效映射（可按线路/站号过滤） |
| GET | `/api/imports` | 已提交导入批次审计 |
| GET | `/api/overview` | 数据总览 |
| GET | `/samples/{文件名}` | 下载内置样例 CSV |

## 在 /docs 里演示（含迟到改站样例）

`samples/` 目录是一组有故事线的样例（容器内也可从 `/samples/xxx.csv` 下载）：

1. **导流水**：`POST /api/imports/swipes` 上传 `swipes_1.csv`（10 条）。
   此时查 `GET /api/transactions/unattributed` → 全部 10 条未归属。
2. **导映射**：`POST /api/imports/mappings` 上传 `mappings_1_initial.csv`。
   查 `GET /api/stats/monthly` → 1–2 月各站上车量；未归属剩 3 条
   （`S006`/`S007` 超出时段、`S008` 无映射）。注意 `T100` 被 L1、L2 复用，归属到不同真实站点。
3. **迟到改站**：上传 `mappings_2_late_correction.csv` —— 整组替换 `(L1, T100)`，
   把 3 月窗口延长。再查 `GET /api/stats/monthly?month=2026-03` →
   旧流水 `S007`（3 月 2 日刷卡）被重新归属到 `Central Station North Gate`，未归属减为 2 条。
4. **幂等重传**：上传 `swipes_2_more.csv`（含已存在的 `S001`，内容相同）→
   `applied=2, skipped_duplicates=1`，1 月统计不重复计数。
5. **冲突拒收**：上传 `swipes_3_conflict.csv`（`S001` 内容被改 + 新流水 `S013`）→
   HTTP 409 整批拒收；查 `/api/overview`，`S013` 未入库，统计不变。
6. **重叠拒收**：上传 `mappings_3_overlap.csv`（`(L1, T200)` 组内两时段重叠）→
   HTTP 409 整批拒收；查 `/api/mappings?line=L1&temp_station_id=T200`，旧映射原样保留。

命令行等价演示：`docker compose up -d` 后执行 `bash scripts/demo.sh`。

## 一致性设计

- **单写事务**：所有导入经全局写锁串行化，`BEGIN IMMEDIATE` 开启事务；
  映射/流水写入与月度统计全量重算在**同一事务**内完成并提交；
- **快照读**：SQLite WAL 模式，查询连接读到的是某一已提交快照，
  不会看到导入进行中的中间态；两个并发导入也只会以某种先后顺序整体生效；
- **失败即回滚**：校验失败（422）在写事务之前返回；业务冲突（409）在事务内抛出，
  触发回滚 —— 包括批次审计表在内，失败的导入不留任何痕迹；
- **幂等键**：流水以 `txn_id` 为主键，内容逐字段比对，同内容跳过、异内容拒收。

## 本地开发与测试

```bash
pip install -r requirements.txt
pip install pytest httpx
TRANSIT_DB_PATH=/tmp/transit.db uvicorn app.main:app --reload
python -m pytest tests/ -v
```

## 目录结构

```
app/
  main.py     # FastAPI 入口与接口定义
  service.py  # 导入/重算/查询核心业务逻辑
  csvio.py    # CSV 解析与 UTC 时间规范化
  db.py       # SQLite 连接、schema、写事务（WAL + 串行化）
samples/      # 成组样例 CSV（/samples 可下载）
scripts/demo.sh
tests/test_app.py
Dockerfile / docker-compose.yml
```
