"""导入与参考库相关端点（M02 / M03），对应计划第 6 节的 /v1/imports 与 /v1/references。"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..config import (
    DEFAULT_MZ_RANGE,
    DOCUMENT_EXTENSIONS,
    LEGACY_DOC_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    SPECTRUM_IMAGE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    SUPPORTED_UPLOAD_FORMATS,
)
from ..data import repositories as repo
from ..domain.enums import EvidenceTier
from ..domain.errors import PlatformError
from ..ingest.doc_parser import extract_docx, extract_legacy_doc, extract_pdf
from ..ingest.importer import EISummaryTableImporter
from ..ingest.peak_table import parse_peak_table
from ..ingest.spectrum_image import extract_peaks_from_image
from ..ingest.upload import needs_calibration, parser_version_for, sniff_kind, store_upload
from .schemas import ImportCommitRequest, ImportPreviewRequest

router = APIRouter()


# OCR 能力探测：装了 rapidocr 才能自动读轴刻度；没装就退回人工填写。
# 用函数而非常量，便于反映运行期依赖是否真的可用。
def _ocr_available() -> bool:
    try:
        from ..ingest.ocr_axis import ocr_available
        return ocr_available()
    except Exception:
        return False


def _imp(req: ImportPreviewRequest) -> EISummaryTableImporter:
    return EISummaryTableImporter(
        source_title=req.source_title, source_url=req.source_url,
        evidence_tier=EvidenceTier(req.evidence_tier),
        redistribution_status=req.redistribution_status, method_id=req.method_id,
        reviewer=getattr(req, "reviewer", "system"))


@router.get("/imports/formats")
def supported_formats():
    """上传支持矩阵（单一数据源，前端直接渲染，避免前后端漂移）。"""
    return {
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "supported_extensions": sorted(SUPPORTED_EXTENSIONS),
        "formats": SUPPORTED_UPLOAD_FORMATS,
        "groups": {
            "structured": ["peak_table", "ei_reference_table"],
            "document": sorted(DOCUMENT_EXTENSIONS),
            "image": sorted(SPECTRUM_IMAGE_EXTENSIONS),
        },
        "default_mz_range": list(DEFAULT_MZ_RANGE),
        "rejected": {
            "archives": sorted({".zip", ".rar", ".7z", ".gz", ".tar", ".bz2", ".xz"}),
            "reason": "平台不自动解压/展开压缩包（开发计划第 6 节：压缩包展开限制）",
        },
        "legacy_doc": {
            "extensions": sorted(LEGACY_DOC_EXTENSIONS),
            "reason": "这些旧格式暂无受控转换流程；请另存为 .docx",
        },
        "ocr_available": _ocr_available(),
        "notes": [
            "上传文件按内容 SHA-256 去重，同一文件重复上传不产生副本，也不覆盖原件。",
            "定性离子表与数值谱两种输入模式不可混用；导入时即固化，后续评分器不得切换。",
            "PDF / Word / 图片走 /v1/imports/extract：结果一律标记为推导值，必须人工校准后才能使用。",
        ]
        + ([
            "已启用 OCR（rapidocr）：可自动识别 x 轴刻度并建议 m/z 轴范围；"
            "建议值仍需人工核对后才能使用。",
        ] if _ocr_available() else [
            "未启用 OCR：安装可选依赖 rapidocr-onnxruntime 后可自动识别 x 轴刻度；"
            "当前 m/z 轴范围需要使用者提供。",
        ]),
    }


@router.post("/imports/extract")
async def extract_spectrum(
    file: UploadFile = File(..., description="PDF / Word / 质谱图图片"),
    mz_min: float | None = Form(None, description="绘图区左端 m/z（图像路径必需）"),
    mz_max: float | None = Form(None, description="绘图区右端 m/z（图像路径必需）"),
    min_prominence_ratio: float = Form(0.02),
    min_peak_distance: int = Form(3),
    image_index: int = Form(0, description="文档含多张图时选择第几张"),
    run_analysis: bool = Form(False, description="提取后是否顺带跑一次候选检索"),
):
    """
    从 PDF / Word / 图片中**提取**质谱峰。三级策略：

      ① 文档有文本/表格 → 直接得到 m/z（±强度），质量最高；
      ② 文档只有图 / 本身就是图片 → 反解峰形得到**相对强度**；
      ③ m/z 轴未提供时只给强度与像素位置，**不猜刻度**（本机无 OCR）。

    返回值一律带 needs_calibration=True：必须经人工确认后才能作为分析输入。
    """
    try:
        content = await file.read()
        stored = store_upload(content, file.filename or "upload")
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())

    return _run_extraction(
        content=content, stored=stored, mz_min=mz_min, mz_max=mz_max,
        min_prominence_ratio=min_prominence_ratio,
        min_peak_distance=min_peak_distance,
        image_index=image_index, run_analysis=run_analysis)


def _run_extraction(*, content: bytes, stored, mz_min, mz_max,
                    min_prominence_ratio: float, min_peak_distance: int,
                    image_index: int, run_analysis: bool) -> dict:
    """
    提取流程的唯一实现，被 /imports/extract 与 /imports/upload 共用。

    放在一处是为了避免"两个入口行为不一致"——之前 upload 不认识图片，
    把 PNG 丢给峰表解析器报 422，用户就会以为平台不支持上传图片。
    """
    ext = stored.extension
    provenance = ("derived_from_image" if ext in SPECTRUM_IMAGE_EXTENSIONS
                  else "derived_from_document")
    out: dict = {
        "file": {"file_id": f"src_{stored.sha256[:16]}", "filename": stored.filename,
                 "sha256": stored.sha256, "byte_size": stored.byte_size,
                 "extension": ext, "reused": stored.reused},
        "needs_calibration": True,
        "provenance": provenance,
    }

    try:
        if ext == ".pdf":
            doc = extract_pdf(content)
        elif ext == ".doc":
            doc = extract_legacy_doc(content, filename=stored.filename)
        elif ext == ".docx":
            doc = extract_docx(content)
        elif ext in SPECTRUM_IMAGE_EXTENSIONS:
            doc = None
        else:
            raise HTTPException(422, {"error_code": "UNSUPPORTED_FOR_EXTRACTION",
                                      "message": f"{ext} 无需走提取流程，请直接上传"})
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())

    if doc is not None:
        out.update(kind="document", doc_kind=doc.kind,
                   warnings=doc.warnings, meta=doc.meta)
        if doc.kind in ("table", "text") and doc.components:
            out["components"] = _mark_derived(doc.components, provenance)
            out["extracted_peaks"] = doc.components[0]["ions"]
            out["message"] = "已从文档表格/文本提取到 m/z 列表，请人工核对后再使用。"
        elif doc.images:
            idx = min(max(image_index, 0), len(doc.images) - 1)
            labels = list((doc.meta or {}).get("image_labels") or [])
            inventory = []
            for i in range(len(doc.images)):
                label = labels[i] if i < len(labels) else None
                inventory.append({
                    "image_index": i,
                    "display_index": i + 1,
                    "label": label,
                })
            res, axis = _extract_with_auto_axis(
                doc.images[idx], mz_min, mz_max, min_prominence_ratio, min_peak_distance)
            image_out = _image_result(res, axis.mz_min, axis.mz_max, provenance)
            image_out["warnings"] = list(doc.warnings) + list(image_out.get("warnings", []))
            out.update(image_out)
            if axis.calibration:
                out["axis_calibration"] = axis.calibration
            out["preview_image"] = _preview_data_url(doc.images[idx])
            out["embedded_images"] = len(doc.images)
            out["selected_image_index"] = idx
            out["selected_image_label"] = inventory[idx]["label"]
            out["image_inventory"] = inventory
        else:
            out["components"] = []
            out["message"] = "文档中未找到可提取内容。"
    else:
        res, axis = _extract_with_auto_axis(
            content, mz_min, mz_max, min_prominence_ratio, min_peak_distance)
        out.update(_image_result(res, axis.mz_min, axis.mz_max, provenance))
        out["preview_image"] = _preview_data_url(content)
        if axis.calibration:
            out["axis_calibration"] = axis.calibration
    return _maybe_run(out, run_analysis)


@dataclass
class _AxisOutcome:
    mz_min: float | None
    mz_max: float | None
    calibration: dict | None = None


def _extract_with_auto_axis(data: bytes, mz_min, mz_max,
                            min_prominence_ratio: float,
                            min_peak_distance: int):
    """
    先跑一遍峰检测拿到绘图区像素范围；若用户没给轴范围且 OCR 可用，
    就用 OCR 读 x 轴刻度线性拟合出轴范围，再按标定结果重跑一次。

    OCR 失败/不可用一律静默退回手动——不影响主流程。
    """
    from ..ingest.spectrum_image import extract_peaks_from_image

    res = extract_peaks_from_image(
        data, mz_min=mz_min, mz_max=mz_max,
        min_prominence_ratio=min_prominence_ratio,
        min_peak_distance=min_peak_distance)

    if mz_min is not None and mz_max is not None:
        return res, _AxisOutcome(mz_min, mz_max)

    if not _ocr_available():
        return res, _AxisOutcome(mz_min, mz_max)

    try:
        from ..ingest.ocr_axis import detect_mz_axis
        cal = detect_mz_axis(data, plot_left=res.plot_left, plot_right=res.plot_right)
    except Exception:
        return res, _AxisOutcome(mz_min, mz_max)

    if not cal.ok:
        return res, _AxisOutcome(mz_min, mz_max,
                                 {"ok": False, "message": cal.message,
                                  "r2": cal.r2, "n_points": cal.n_points})

    res = extract_peaks_from_image(
        data, mz_min=cal.mz_min, mz_max=cal.mz_max,
        min_prominence_ratio=min_prominence_ratio,
        min_peak_distance=min_peak_distance)
    return res, _AxisOutcome(
        cal.mz_min, cal.mz_max,
        {"ok": True, "mz_min": cal.mz_min, "mz_max": cal.mz_max, "r2": cal.r2,
         "n_points": cal.n_points, "method": cal.method, "message": cal.message,
         "points": cal.points})


@router.post("/imports/extract/confirm")
def confirm_extraction(payload: dict):
    """
    人工校准后确认：把峰表固化为可分析的组分。

    单谱 body: {peaks:[{mz, intensity|marker}], component_id, label,
                provenance, extraction_params, qualitative}
    多谱 body: {spectra:[上述对象 + image_index]}

    多谱确认时每张图固化为一个独立组分，禁止把不同图片的峰合并成一张谱。
    """
    from ..ingest.peak_table import _normalise_component

    batch = payload.get("spectra")
    items = batch if batch is not None else [payload]
    if not isinstance(items, list) or not items:
        raise HTTPException(422, {"error_code": "NO_SPECTRA", "message": "没有可确认的谱图"})

    seen_image_indexes: set[int] = set()
    components: list[dict] = []
    for ordinal, item in enumerate(items):
        if not isinstance(item, dict):
            raise HTTPException(422, {"error_code": "BAD_SPECTRUM",
                                      "message": f"第 {ordinal + 1} 个谱图数据格式无效"})
        image_index = item.get("image_index")
        if batch is not None:
            try:
                image_index = int(image_index)
            except (TypeError, ValueError):
                raise HTTPException(422, {"error_code": "IMAGE_INDEX_REQUIRED",
                                          "message": f"第 {ordinal + 1} 个谱图缺少 image_index"})
            if image_index < 0:
                raise HTTPException(422, {"error_code": "BAD_IMAGE_INDEX",
                                          "message": f"第 {ordinal + 1} 个谱图的 image_index 非法"})
            if image_index in seen_image_indexes:
                raise HTTPException(422, {"error_code": "DUPLICATE_IMAGE_INDEX",
                                          "message": f"内嵌图片 {image_index + 1} 被重复提交"})
            seen_image_indexes.add(image_index)

        peaks = item.get("peaks") or []
        if not peaks:
            name = f"内嵌图片 {image_index + 1}" if image_index is not None else "当前谱图"
            raise HTTPException(422, {"error_code": "NO_PEAKS", "message": f"{name} 没有可确认的峰"})

        qual = bool(item.get("qualitative", True))
        ions: list[dict] = []
        for p in peaks:
            try:
                mz = float(p["mz"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (20.0 <= mz <= 800.0):
                continue
            if qual:
                marker = str(p.get("marker", "") or "")
                if marker not in ("+", "red"):
                    try:
                        marker = "red" if float(p.get("intensity", 0)) >= 20 else "+"
                    except (TypeError, ValueError):
                        marker = "+"
                ions.append({"mz": mz, "marker": marker})
            else:
                try:
                    intensity = float(p.get("intensity", 0.0))
                except (TypeError, ValueError):
                    intensity = 0.0
                ions.append({"mz": mz, "intensity": intensity})
        if not ions:
            name = f"内嵌图片 {image_index + 1}" if image_index is not None else "当前谱图"
            raise HTTPException(422, {"error_code": "NO_VALID_PEAKS",
                                      "message": f"{name} 没有 20–800 范围内的合法 m/z"})

        default_id = (f"extracted_image_{image_index + 1:03d}"
                      if batch is not None else "extracted_peak_1")
        comp = {
            "component_id": item.get("component_id") or default_id,
            "peak_label": item.get("label") or (
                f"内嵌图片 {image_index + 1}" if image_index is not None else "提取峰表"),
            "evidence_type": "qualitative_ion_list" if qual else "numeric_spectrum",
            "ions": ions,
            # 人工确认的是“提取值是否抄录正确”，不是把图片/文档升级成仪器全谱。
            "spectrum_completeness": "selected_ions",
        }
        normalised = _normalise_component(comp, ordinal)
        normalised["provenance"] = item.get("provenance", "derived_from_image")
        params = dict(item.get("extraction_params") or {})
        if image_index is not None:
            params["image_index"] = image_index
        normalised["extraction_params"] = params or None
        components.append(normalised)

    return {"components": components, "count": len(components),
            "note": "已人工确认。该数据仍非仪器直接导出，仅用于候选检索，不得作为确认或定量依据。"}


@router.post("/imports/upload")
async def upload_file(
    file: UploadFile = File(..., description="待导入文件"),
    kind: str | None = Form(None, description="ei_reference_table | peak_table；留空按扩展名自动推断"),
    commit: bool = Form(False, description="是否直接入库；False 则只预览"),
    source_title: str | None = Form(None),
    evidence_tier: str = Form("E2"),
    method_id: str | None = Form(None),
    reviewer: str = Form("web"),
    # 以下为文档/图片提取参数（结构化文件用不到）
    mz_min: float | None = Form(None),
    mz_max: float | None = Form(None),
    min_prominence_ratio: float = Form(0.02),
    min_peak_distance: int = Form(3),
    image_index: int = Form(0),
):
    """
    浏览器文件上传入口——**一个入口吃下所有格式**。

    自动按扩展名路由：
      * .xlsx / .xlsm → EI 汇总表导入（可预览或直接入库）
      * .csv / .tsv / .json → 峰表解析，直接可跑分析
      * .pdf / .doc / .docx / 图片 → **自动转提取流程**，返回可校准的峰
        （以前会掉进峰表解析器报"不支持的峰表格式"，用户就以为传不上去）

    压缩包一律拒收不展开；文件按 SHA-256 去重落盘。
    """
    try:
        content = await file.read()
        stored = store_upload(content, file.filename or "upload")
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())

    resolved_kind = kind or sniff_kind(stored.filename)
    file_id = f"src_{stored.sha256[:16]}"

    repo.SourceFileRepo.upsert(
        file_id=file_id, filename=stored.filename, sha256=stored.sha256,
        byte_size=stored.byte_size, parser_version=parser_version_for(stored.extension),
        import_kind=resolved_kind,
        meta={"uploaded": True, "reused": stored.reused, "original_name": stored.filename},
    )

    # 文档 / 图片：转提取流程，而不是塞给峰表解析器
    if resolved_kind in ("document", "spectrum_image"):
        return _run_extraction(
            content=content, stored=stored, mz_min=mz_min, mz_max=mz_max,
            min_prominence_ratio=min_prominence_ratio,
            min_peak_distance=min_peak_distance,
            image_index=image_index, run_analysis=False)

    try:
        if resolved_kind == "ei_reference_table":
            imp = EISummaryTableImporter(
                source_title=source_title or stored.filename,
                evidence_tier=EvidenceTier(evidence_tier),
                redistribution_status="restricted", method_id=method_id, reviewer=reviewer)
            rep = imp.commit(stored.path) if commit else imp.preview(stored.path)
            return {
                "file": {"file_id": file_id, "filename": stored.filename,
                         "sha256": stored.sha256, "byte_size": stored.byte_size,
                         "stored_path": stored.path, "reused": stored.reused},
                "kind": resolved_kind,
                "summary": rep.summary(),
                "rows": [{"source_row_id": r.source_row_id,
                          "derivative_name_raw": r.derivative_name_raw,
                          "status": r.status, "messages": r.messages,
                          "error_codes": r.error_codes,
                          "derived_linkage": r.derived_linkage,
                          "source_linkage": r.source_linkage_raw,
                          "preview": r.preview} for r in rep.rows],
            }

        # 峰表：解析后直接返回规范化组分，供前端确认后再提交分析
        comps = parse_peak_table(stored.path)
        return {
            "file": {"file_id": file_id, "filename": stored.filename,
                     "sha256": stored.sha256, "byte_size": stored.byte_size,
                     "stored_path": stored.path, "reused": stored.reused},
            "kind": resolved_kind,
            "components": comps,
            "count": len(comps),
        }
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())


@router.post("/imports/preview")
def preview_import(req: ImportPreviewRequest):
    """
    解析原表并给出布局与字段映射预览：每条源行的行号、识别到的语义、
    化学复核结果与冲突——不写库。
    """
    try:
        rep = _imp(req).preview(req.path)
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())
    return {
        "summary": rep.summary(),
        "rows": [{
            "source_row_id": r.source_row_id,
            "derivative_name_raw": r.derivative_name_raw,
            "status": r.status,
            "messages": r.messages,
            "error_codes": r.error_codes,
            "derived_linkage": r.derived_linkage,
            "source_linkage": r.source_linkage_raw,
            "preview": r.preview,
        } for r in rep.rows],
    }


@router.post("/imports/commit")
def commit_import(req: ImportCommitRequest):
    """提交审核通过的导入。错误行进入隔离队列并在响应中列出，不静默丢弃。"""
    imp = EISummaryTableImporter(
        source_title=req.source_title, source_url=req.source_url,
        evidence_tier=EvidenceTier(req.evidence_tier),
        redistribution_status=req.redistribution_status, method_id=req.method_id,
        reviewer=req.reviewer)
    try:
        rep = imp.commit(req.path)
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())
    return {
        "summary": rep.summary(),
        "isolated_rows": [r.source_row_id for r in rep.rows if r.status == "isolated"],
        "quarantined_rows": [r.source_row_id for r in rep.rows if r.status == "quarantined"],
    }


@router.post("/imports/peak-table")
def import_peak_table(payload: dict):
    """
    导入已积分峰表。支持 JSON 直接提交，或 {path: ...} 指向 CSV/JSON 文件。
    返回规范化后的组分列表（不立即分析）。
    """
    try:
        if "path" in payload:
            comps = parse_peak_table(payload["path"])
        else:
            comps = parse_peak_table(payload)
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())
    return {"components": comps, "count": len(comps)}


@router.get("/references")
def list_references(review_status: str | None = None, include_quarantined: bool = True):
    rows = repo.ReferenceRepo.list_all(review_status=review_status,
                                       include_quarantined=include_quarantined)
    return {"count": len(rows), "items": [{
        "record_id": r.id, "derivative_name_raw": r.derivative_name_raw,
        "parent_sugar": r.parent_sugar, "skeleton": r.skeleton, "ring_form": r.ring_form,
        "o_methyl_positions": r.o_methyl_positions, "o_acetyl_positions": r.o_acetyl_positions,
        "nitrogen_modifications": r.nitrogen_modifications, "review_status": r.review_status,
        "branch": r.branch, "linkage_conflict": r.linkage_conflict,
        "source_row_id": r.source_row_id, "ion_count": len(r.ions),
    } for r in rows]}


@router.get("/references/{record_id}")
def get_reference(record_id: str):
    try:
        r = repo.ReferenceRepo.get(record_id)
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())
    return {
        "record_id": r.id,
        "payload": r.payload,
        "ions": [{"mz": i.mz, "observation": i.observation,
                  "intensity_class": i.intensity_class, "source_cell": i.source_cell}
                 for i in r.ions],
    }


@router.get("/references/{record_id}/provenance")
def reference_provenance(record_id: str):
    """来源链：源文件/行/单元格、审核与版本历史。"""
    try:
        r = repo.ReferenceRepo.get(record_id)
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())
    src_file = None
    if r.source_file_id:
        try:
            f = repo.SourceFileRepo.get(r.source_file_id)
            src_file = {"file_id": f.id, "filename": f.filename, "sha256": f.sha256,
                        "parser_version": f.parser_version, "imported_at": f.imported_at.isoformat()}
        except PlatformError:
            src_file = None
    return {
        "record_id": r.id,
        "source_file": src_file,
        "source_row_id": r.source_row_id,
        "source": r.source,
        "ion_source_cells": [i.source_cell for i in r.ions][:20],
        "audit_trail": [{"action": a.action, "actor": a.actor,
                         "at": a.created_at.isoformat(), "payload": a.payload}
                        for a in repo.AuditRepo.list_for(r.id)],
    }


@router.post("/references/{record_id}/review")
def review_reference(record_id: str, payload: dict):
    status = payload.get("review_status")
    if status not in ("unreviewed", "quarantined", "reference_only", "locally_validated"):
        raise HTTPException(422, {"error_code": "BAD_REVIEW_STATUS",
                                  "message": "review_status 取值非法"})
    try:
        repo.ReferenceRepo.set_review_status(
            record_id, status, reason=payload.get("reason"),
            actor=payload.get("reviewer", "system"))
    except PlatformError as e:
        raise HTTPException(e.http_status, e.to_dict())
    return {"record_id": record_id, "review_status": status}


@router.get("/derivatives")
def list_derivatives():
    """化学实体及其全部原残基来源（同一实体多行）。"""
    out = []
    for d in repo.DerivativeRepo.list_all():
        maps = repo.ResidueMappingRepo.by_derivative(d.id)
        out.append({
            "derivative_id": d.id, "display_name": d.display_name, "skeleton": d.skeleton,
            "o_methyl_positions": d.o_methyl_positions, "o_acetyl_positions": d.o_acetyl_positions,
            "origins": [{"framework_id": m.framework_id, "ring_form": m.ring_form,
                         "linkage_positions": m.linkage_positions,
                         "parent_sugar": m.parent_sugar,
                         "source_record_id": m.source_record_id} for m in maps],
            "multi_origin": len(maps) > 1,
        })
    return {"count": len(out), "items": out}


# ============================================================ 提取相关辅助
def _image_result(res, mz_min, mz_max, provenance: str) -> dict:
    """把图像反解结果整理成前端可校准的形状。"""
    peaks = [{"mz": p.mz, "x_pixel": p.x_pixel,
              "relative_intensity": p.relative_intensity} for p in res.peaks]
    calibrated = mz_min is not None and mz_max is not None and mz_max > mz_min
    return {
        "kind": "spectrum_image",
        "extracted_peaks": peaks,
        "peak_count": len(peaks),
        "confidence": res.confidence,
        "image": {"width": res.image_width, "height": res.image_height,
                  "plot_left": res.plot_left, "plot_right": res.plot_right,
                  "baseline_y": res.baseline_y},
        "params": res.params,
        "warnings": res.warnings,
        "calibrated": calibrated,
        "components": (_build_components_from_peaks(peaks, provenance) if calibrated else []),
        "message": ("已按给定 m/z 范围标定，请人工核对峰表后再使用。" if calibrated else
                    "未给出 m/z 轴范围：只解出相对强度与像素位置。"
                    "请在左侧填入轴范围后重新提取，或在下方表格中手工填写 m/z。"),
    }


def _build_components_from_peaks(peaks: list[dict], provenance: str) -> list[dict]:
    usable = [p for p in peaks if p.get("mz") is not None]
    if not usable:
        return []
    ions = [{"mz": p["mz"],
             "marker": "red" if (p.get("relative_intensity") or 0) >= 20 else "+"}
            for p in usable]
    return [{
        "component_id": "extracted_peak_1",
        "peak_label": "图像反解峰",
        "evidence_type": "qualitative_ion_list",
        "ions": ions,
        "spectrum_completeness": "selected_ions",
        "provenance": provenance,
    }]


def _mark_derived(components: list[dict], provenance: str) -> list[dict]:
    for c in components:
        c["provenance"] = provenance
    return components


def _maybe_run(out: dict, run: bool) -> dict:
    """可选地顺带跑一次候选检索，方便当场看到比对结果。失败不掩盖提取结果。"""
    if not run or not out.get("components"):
        return out
    try:
        from ..analysis.pipeline import AnalysisContext, ComponentInput, analyze_component
        from ..domain.enums import EvidenceType, SpectrumCompleteness

        comps = []
        for c in out["components"]:
            ions: dict[float, object] = {}
            for ion in c.get("ions", []):
                ions[float(ion["mz"])] = (ion.get("intensity")
                                          if "intensity" in ion
                                          else ("red" if str(ion.get("marker")) == "red" else "+"))
            ci = ComponentInput(
                component_id=c.get("component_id", "p1"),
                evidence_type=EvidenceType(c.get("evidence_type", "qualitative_ion_list")),
                ions=ions,
                spectrum_completeness=SpectrumCompleteness(
                    c.get("spectrum_completeness", "selected_ions")),
                provenance=c.get("provenance", "derived_from_image"),
                extraction_params=c.get("extraction_params"))
            comps.append(analyze_component(ci, AnalysisContext()).model_dump(mode="json"))
        out["analysis"] = comps
    except Exception as e:
        out["analysis_error"] = f"{type(e).__name__}: {e}"
    return out


def _preview_data_url(data: bytes, max_w: int = 900) -> str | None:
    """缩到预览尺寸转 data URL，便于前端对着原图校准。"""
    try:
        import base64
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data))
        if img.width > max_w:
            img = img.resize((max_w, max(1, int(img.height * max_w / img.width))))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
