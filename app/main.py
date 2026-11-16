"""FastAPI 入口：导入、查询接口。

所有接口均为同步实现（Starlette 线程池执行），写路径由
app.db.write_txn 全局串行化，读路径走 WAL 快照，互不一致性问题。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, service
from .csvio import CsvError, parse_mappings, parse_swipes
from .service import ConflictError, ReviewNotFound, StaleReviewError

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

## 可复核导入（补传离线流水）

`POST /api/imports/swipes/reviewable` 用于数据员补传离线刷卡：

* **文件内**同号异内容：仍然**整批拒收**（409）；
* 与**库中**冲突的行：连同原值、新值留作**待核**，不阻塞同文件的其它行；
* 其余无冲突的新流水：**立即入库**并计入统计；同号同内容：跳过；
* 「原值 + 新值」相同重传不会重复挂起；上传刷新原值基准时，旧待核自动失效；
* `GET /api/reviews` 查待核，`POST /api/reviews/{id}/decision` 按条选择
  `keep`（保留原值）或 `apply`（采用新值并按当前映射重算归站与月客流）；
* 审批、流水变更与统计重算同一事务提交；审批失败原值与待核状态不变；
  别的审批先改了该流水时，旧待核返回 409 失效，需重新上传按现值复核。
"""


class ImportResult(BaseModel):
    batch_id: str
    file_type: str
    filename: str
    rows_received: int
    applied: int
    skipped_duplicates: int
    note: str | None = None


class ReviewableImportResult(ImportResult):
    pending_new: int
    pending_duplicate: int
    superseded: int
    review_ids: list[str]


class ReviewItem(BaseModel):
    review_id: str
    txn_id: str
    status: str
    base_swipe_time_utc: str
    base_line: str
    base_temp_station_id: str
    new_swipe_time_utc: str
    new_line: str
    new_temp_station_id: str
    batch_id: str
    filename: str
    created_at: str
    decided_at: str | None
    note: str | None


class ReviewPage(BaseModel):
    total: int
    items: list[ReviewItem]


class ReviewDecision(str, Enum):
    keep = "keep"
    apply = "apply"


class ReviewDecisionRequest(BaseModel):
    decision: ReviewDecision = Field(
        ...,
        description="keep = 保留库中原值；apply = 采用文件里的新值（按当前映射重算归站）",
    )


class ReviewDecisionResult(BaseModel):
    review: ReviewItem
    swipe: dict | None
    stats_recomputed: bool


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
    pending_reviews: int
    superseded_reviews: int


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="公交刷卡流水归站 API",
    description=DESCRIPTION,
    version="1.1.0",
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
    "/api/imports/swipes/reviewable",
    response_model=ReviewableImportResult,
    tags=["导入"],
    responses={
        409: {"description": "同一文件内同号不同内容，整批拒收（已有数据不变）"},
        422: {"description": "CSV 结构或内容非法，整批拒收"},
    },
)
def import_swipe_file_reviewable(
    file: UploadFile = File(..., description="刷卡流水 CSV：txn_id, swipe_time_utc, line, temp_station_id"),
):
    """可复核导入刷卡流水 CSV。

    - 文件**内部**同号不同内容：整批拒收（409，与原导入规则一致）；
    - 与**库中**冲突的行（同号不同内容）：不拒收，连同原值、新值留作待核；
    - 其余无冲突的新流水：立即入库并计入统计；
    - 同号同内容：跳过，不重复计数；
    - 同一原值下重传同一内容：复用已挂起的待核记录，不重复挂起；
    - 新流水、待核记录与月度统计在**同一事务**提交；
    - 本次上传会刷新某条流水的原值基准时，该流水此前仍待核的记录自动失效。
    """
    try:
        rows = parse_swipes(_read_upload(file))
    except CsvError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "CSV 校验失败，整批拒收", "errors": e.errors},
        )
    try:
        return service.import_swipes_reviewable(rows, file.filename or "upload.csv")
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "同一文件内同号不同内容，整批拒收；已有数据未改变",
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


# ---------------------------------------------------------------- 待核复核


@app.get("/api/reviews", response_model=ReviewPage, tags=["复核"])
def get_reviews(
    status: str | None = Query(
        default="pending",
        pattern=r"^(pending|applied|kept|superseded|all)$",
        description="待核状态；all 表示不过滤（默认只看 pending）",
    ),
    txn_id: str | None = Query(default=None, description="按流水号过滤"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """待核记录查询：原值、新值、来源文件与状态。"""
    return service.list_reviews(
        None if status in (None, "all") else status, txn_id, limit, offset
    )


@app.get("/api/reviews/{review_id}", response_model=ReviewItem, tags=["复核"])
def get_review(review_id: str):
    """单条待核记录。"""
    try:
        return service.get_review(review_id)
    except ReviewNotFound:
        raise HTTPException(status_code=404, detail="待核记录不存在")


@app.post(
    "/api/reviews/{review_id}/decision",
    response_model=ReviewDecisionResult,
    tags=["复核"],
    responses={
        404: {"description": "待核记录不存在"},
        409: {"description": "记录不再是待核状态，或流水原值已被别处改变（审批失败，数据不变）"},    },
)
def decide_review(review_id: str, body: ReviewDecisionRequest):
    """按条决定：保留原值或采用新值。

    - `keep`：保留库中原值，统计不变；
    - `apply`：把流水改为文件新值，并按**当前映射**重算归站与月客流；
    - 决定、流水变更、统计重算在同一事务提交；失败时原值与待核状态不变；
    - 若别的审批先把该流水改成了别的值，本记录已失效（409），
      需要重新上传该文件，按新的现值重新挂起才能复核。
    """
    try:
        return service.decide_review(review_id, body.decision.value)
    except ReviewNotFound:
        raise HTTPException(status_code=404, detail="待核记录不存在")
    except StaleReviewError as e:
        detail = {
            "message": "审批失败：该待核记录已失效，数据未改变；请重新上传以按现值复核",
            "review_id": e.review_id,
            "status": e.status,
        }
        detail["reason"] = "库中该流水的内容已不再是待核记录里的原值"
        raise HTTPException(status_code=409, detail=detail)


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
