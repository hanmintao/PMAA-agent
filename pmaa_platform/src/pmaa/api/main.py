"""
FastAPI 应用入口。

接口清单与开发计划第 6 节一一对应；错误统一返回稳定错误码。
前端静态站点挂载在 / （无构建步骤，直接打开即可用）。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config import DATA_DIR, PROJECT_ROOT, RULE_VERSION
from ..data.database import init_db
from ..domain.errors import PlatformError
from ..domain.models import EvidenceBundle
from .routers_analysis import router as analysis_router
from .routers_core import router as core_router
from .routers_llm import router as llm_router

WEB_DIR = PROJECT_ROOT / "web"

DESCRIPTION = """
多糖甲基化分析平台（PMAA）—— 以 PMAA 的 EI 质谱汇总表为参考库，
将峰表/定性离子表转化为可复核的糖残基连接组成候选。

**科学边界（不可绕过）**
* 定性离子表不生成数值强度，证据上限为 `candidate`；
* 未列出的离子不等于未检出；
* α/β 与 D/L 不由本分析产生；
* 无校准时不输出准确 mol%；
* 同一 PMAA 的多个原残基来源必须全部保留；
* 最高匹配分数不是正确概率。
"""

APP_VERSION = "0.3.0"
STARTED_AT = datetime.now(timezone.utc).isoformat()

app = FastAPI(title="PMAA 甲基化分析平台", version=APP_VERSION, description=DESCRIPTION)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


@app.exception_handler(PlatformError)
async def _platform_error_handler(request: Request, exc: PlatformError):
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


@app.exception_handler(StarletteHTTPException)
async def _http_error_handler(request: Request, exc: StarletteHTTPException):
    """统一错误响应形状：一律 {error_code, message, details}，便于前端按码处理。"""
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={
        "error_code": "HTTP_ERROR",
        "message": str(exc.detail),
        "details": {},
    })


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # 不向客户端泄漏堆栈；服务端日志记录细节
    return JSONResponse(status_code=500, content={
        "error_code": "INTERNAL_ERROR",
        "message": "服务器内部错误，已记录日志",
        "details": {"type": type(exc).__name__},
    })


app.include_router(core_router, prefix="/v1")
app.include_router(analysis_router, prefix="/v1")
app.include_router(llm_router, prefix="/v1")


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/v1/health")
def health():
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "rule_version": RULE_VERSION,
        "pid": os.getpid(),
        "started_at": STARTED_AT,
    }


@app.get("/v1/enums")
def enums():
    """前端下拉框与状态色所用枚举；单一来源，避免前后端漂移。"""
    from ..domain import enums as E
    return {
        "qc_status": [e.value for e in E.QCStatus],
        "derivative_identity_level": [e.value for e in E.DerivativeIdentityLevel],
        "residue_mapping_level": [e.value for e in E.ResidueMappingLevel],
        "quantification_level": [e.value for e in E.QuantificationLevel],
        "review_status": [e.value for e in E.ReviewStatus],
        "evidence_type": [e.value for e in E.EvidenceType],
        "intensity_class": [e.value for e in E.IntensityClass],
        "ion_observation": [e.value for e in E.IonObservation],
        "reference_review_status": [e.value for e in E.ReferenceReviewStatus],
        "evidence_tier": [e.value for e in E.EvidenceTier],
        "error_codes": [e.value for e in E.StableErrorCode],
        "missing_evidence_codes": sorted(E.MISSING_EVIDENCE_CODES)
        if hasattr(E, "MISSING_EVIDENCE_CODES") else [],
    }


@app.get("/v1/chemistry/frameworks")
def frameworks():
    from ..chemistry.frameworks import FRAMEWORKS
    return {"items": [{"id": f.id, "skeleton": f.skeleton, "ring_form": f.ring_form,
                       "anomeric_position": f.anomeric_position,
                       "ring_closure_position": f.ring_closure_position,
                       "available_positions": list(f.available_positions), "note": f.note}
                      for f in FRAMEWORKS.values()]}


@app.post("/v1/chemistry/map")
def map_positions(payload: dict):
    """
    化学映射规则的自检端点：给定骨架与 O-甲基位，返回连接位与乙酰位。
    用于前端"规则试算"与自动化验收。
    """
    from ..chemistry.mapping import compute_mapping, map_derivative_by_methyl
    from ..domain.enums import StableErrorCode
    from ..domain.errors import ChemistryRuleError

    skeleton = payload.get("skeleton")
    methyl = payload.get("o_methyl_positions", [])
    n_pos = frozenset(int(x) for x in payload.get("n_positions", []))
    if payload.get("framework_id"):
        try:
            oc = compute_mapping(payload["framework_id"], methyl, n_pos)
        except ChemistryRuleError as e:
            raise HTTPException(422, e.to_dict())
        return {"framework_id": oc.framework.id, "linkage_positions": list(oc.linkage_positions),
                "o_acetyl_positions": list(oc.o_acetyl_positions),
                "requires_artifact_review": oc.requires_artifact_review}
    mappings = map_derivative_by_methyl(methyl, skeleton, n_pos, payload.get("parent_sugar"))
    return {"candidates": [m.model_dump() for m in mappings]}


if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
