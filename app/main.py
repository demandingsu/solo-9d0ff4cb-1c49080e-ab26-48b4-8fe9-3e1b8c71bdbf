"""FastAPI 入口：导入、查询接口。

所有接口均为同步实现（Starlette 线程池执行），写路径由
app.db.write_txn 全局串行化，读路径走 WAL 快照，互不一致性问题。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, service
from .csvio import CsvError, parse_mappings, parse_swipes
from .service import ConflictError

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"

DESCRIPTION = """
把刷卡流水里的**临时站号**归属到**真实站点**，供月度客流分析。

## 两类 CSV

* **刷卡流水**：`txn_id, swipe_time_utc, line, temp_station_id`
  （流水号、UTC 时间、线路、临时站号）
* **站号映射**：`line, temp_station_id, valid_from_utc, valid_to_utc, real_station`
  （线路、临时站号、生效起止 UTC、真实站点）

## 规则

* 同一临时站号可被不同线路复用 —— 映射按 **(线路, 临时站号)** 区分；
* 映射按 (线路, 临时站号) **整组替换**；组内时段重叠则 **整批拒收**（409）；
* 统计口径：刷卡时刻落在映射生效时段 `[起, 止)` 内，按 **UTC 月份** 汇总真实站点上车量；
* 流水按流水号幂等：**同号同内容**重传不重复计数，**同号不同内容**整批拒收（409）；
* 映射晚到会**重算旧流水**；导入与统计在同一事务提交，查询只见一致的已提交状态；
* 导入失败（校验/冲突）**不改变已有结果**。

## 快速体验

`/samples` 下有成组样例文件，按 README 的顺序在下方导入接口里上传即可
演示「迟到改站重算」「幂等重传」「冲突拒收」「时段重叠拒收」。
"""


class ImportResult(BaseModel):
    batch_id: str
    file_type: str
    filename: str
    rows_received: int
    applied: int
    skipped_duplicates: int
    note: str | None = None


class MonthlyStat(BaseModel):
    month: str
    real_station: str
    boardings: int


class UnattributedItem(BaseModel):
    txn_id: str
    swipe_time_utc: str
    line: str
    temp_station_id: str
    reason: str


class UnattributedPage(BaseModel):
    total: int
    items: list[UnattributedItem]


class MappingItem(BaseModel):
    line: str
    temp_station_id: str
    valid_from_utc: str
    valid_to_utc: str
    real_station: str
    batch_id: str


class BatchItem(BaseModel):
    batch_id: str
    file_type: str
    filename: str
    rows_received: int
    rows_applied: int
    rows_skipped: int
    note: str | None
    created_at: str


class Overview(BaseModel):
    swipes: int
    mapping_intervals: int
    mapping_groups: int
    import_batches: int
    monthly_stat_rows: int
    attributed_boardings: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="公交刷卡流水归站 API",
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
)

if SAMPLES_DIR.is_dir():
    app.mount("/samples", StaticFiles(directory=str(SAMPLES_DIR)), name="samples")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


# ---------------------------------------------------------------- 导入


def _read_upload(file: UploadFile) -> bytes:
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="上传文件为空")
    return data


@app.post(
    "/api/imports/swipes",
    response_model=ImportResult,
    tags=["导入"],
    responses={
        409: {"description": "同号不同内容，整批拒收（已有数据不变）"},
        422: {"description": "CSV 结构或内容非法，整批拒收"},
    },
)
def import_swipe_file(
    file: UploadFile = File(..., description="刷卡流水 CSV：txn_id, swipe_time_utc, line, temp_station_id"),
):
    """导入刷卡流水 CSV。

    - 流水号已存在且**内容完全相同**：跳过，不重复计数（可安全重传）；
    - 流水号已存在但**内容不同**：整批拒收，返回 409 与冲突明细；
    - 导入成功后同事务重算月度统计。
    """
    try:
        rows = parse_swipes(_read_upload(file))
    except CsvError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV 校验失败，整批拒收", "errors": e.errors},
        )
    try:
        return service.import_swipes(rows, file.filename or "upload.csv")
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "同号不同内容，整批拒收；已有数据未改变",
                "conflicts": e.errors,
            },
        )


@app.post(
    "/api/imports/mappings",
    response_model=ImportResult,
    tags=["导入"],
    responses={
        409: {"description": "组内时段重叠，整批拒收（已有数据不变）"},
        422: {"description": "CSV 结构或内容非法，整批拒收"},
    },
)
def import_mapping_file(
    file: UploadFile = File(..., description="站号映射 CSV：line, temp_station_id, valid_from_utc, valid_to_utc, real_station"),
):
    """导入站号映射 CSV。

    - 按 (线路, 临时站号) **整组替换**：文件里出现的组整体覆盖旧记录，
      未出现的组不受影响；
    - 组内生效时段重叠：整批拒收，返回 409 与重叠明细；
    - 映射晚到时，旧流水在新统计中自动重新归属。
    """
    try:
        rows = parse_mappings(_read_upload(file))
    except CsvError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV 校验失败，整批拒收", "errors": e.errors},
        )
    try:
        return service.import_mappings(rows, file.filename or "upload.csv")
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "组内时段重叠，整批拒收；已有数据未改变",
                "conflicts": e.errors,
            },
        )


# ---------------------------------------------------------------- 查询


@app.get("/api/stats/monthly", response_model=list[MonthlyStat], tags=["查询"])
def get_monthly_stats(
    month: str | None = Query(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        description="按 UTC 月份过滤，如 2026-01；缺省返回全部月份",
        examples=["2026-01"],
    ),
):
    """真实站点的 UTC 月上车量（按刷卡时刻归属）。"""
    return service.monthly_stats(month)


@app.get(
    "/api/transactions/unattributed",
    response_model=UnattributedPage,
    tags=["查询"],
)
def get_unattributed(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """未归属流水：刷卡时刻没有命中任何映射时段，附原因说明。"""
    return service.unattributed(limit, offset)


@app.get("/api/mappings", response_model=list[MappingItem], tags=["查询"])
def get_mappings(
    line: str | None = Query(default=None, description="按线路过滤"),
    temp_station_id: str | None = Query(default=None, description="按临时站号过滤"),
):
    """当前生效的站号映射（整组替换后的最新状态）。"""
    return service.list_mappings(line, temp_station_id)


@app.get("/api/imports", response_model=list[BatchItem], tags=["查询"])
def get_import_batches():
    """已成功提交的导入批次审计（失败的导入不留痕、不改数据）。"""
    return service.list_batches()


@app.get("/api/overview", response_model=Overview, tags=["查询"])
def get_overview():
    """库内数据总览。"""
    return service.overview()
