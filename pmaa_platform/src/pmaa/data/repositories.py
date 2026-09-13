"""
数据访问层：统一封装连接、查询与异常。

约定：
1. 所有仓储方法只抛 domain.errors 中的异常，不向上泄漏 SQLAlchemy 异常；
2. 写操作统一走 session_scope() 事务封装；
3. 幂等写入通过 (unique_key) 先查后插，重复导入不产生重复行。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, func, or_, select

from ..domain.errors import NotFoundError, PlatformError
from ..domain.models import ReferenceRecord
from . import database as db


from .database import new_id, sha256_bytes, sha256_json  # noqa: F401  （统一 ID/哈希入口）


def audit(entity: str, entity_id: str, action: str, payload: dict | None = None, actor: str = "system"):
    with db.session_scope() as s:
        s.add(db.AuditLogRow(entity=entity, entity_id=entity_id, action=action,
                             payload=payload or {}, actor=actor))


# ====================================================================== 源文件
class SourceFileRepo:
    @staticmethod
    def upsert(*, file_id: str, filename: str, sha256: str, byte_size: int,
               parser_version: str, import_kind: str, meta: dict | None = None) -> db.SourceFile:
        with db.session_scope() as s:
            row = s.get(db.SourceFile, file_id)
            if row is None:
                row = db.SourceFile(id=file_id, filename=filename, sha256=sha256,
                                    byte_size=byte_size, parser_version=parser_version,
                                    import_kind=import_kind, meta=meta or {})
                s.add(row)
            else:
                row.meta = {**(row.meta or {}), **(meta or {})}
                s.add(row)
            return row

    @staticmethod
    def get(file_id: str) -> db.SourceFile:
        with db.session_scope() as s:
            row = s.get(db.SourceFile, file_id)
            if row is None:
                raise NotFoundError(f"源文件不存在: {file_id}")
            return row

    @staticmethod
    def list_all() -> list[db.SourceFile]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.SourceFile).order_by(db.SourceFile.imported_at.desc())))

    @staticmethod
    def mark_superseded(file_id: str, *, superseded_by: str, actor: str = "system") -> None:
        """保留旧来源审计行，只标记其已被哪个新来源替代。"""
        with db.session_scope() as s:
            row = s.get(db.SourceFile, file_id)
            if row is None:
                raise NotFoundError(f"源文件不存在: {file_id}")
            row.meta = {**(row.meta or {}), "superseded_by": superseded_by}
            s.add(row)
        audit("source_file", file_id, "mark_superseded",
              {"superseded_by": superseded_by}, actor)


# ====================================================================== 参考库
class ReferenceRepo:
    @staticmethod
    def save(record: ReferenceRecord, *, source_file_id: str | None = None) -> db.ReferenceRecordRow:
        """按 record_id 幂等写入；连带离子一并替换。"""
        with db.session_scope() as s:
            row = s.get(db.ReferenceRecordRow, record.record_id)
            if row is None:
                row = db.ReferenceRecordRow(id=record.record_id)
            row.source_file_id = source_file_id
            row.source_row_id = record.source_row_id
            row.derivative_name_raw = record.derivative_name_raw
            row.parent_sugar = record.parent_sugar
            row.skeleton = record.skeleton
            row.ring_form = record.ring_form
            row.o_methyl_positions = record.o_methyl_positions
            row.o_acetyl_positions = record.o_acetyl_positions
            row.nitrogen_modifications = record.nitrogen_modifications
            row.reduction_label = record.reduction_label.model_dump()
            row.evidence_type = record.evidence_type.value
            row.spectrum_completeness = record.spectrum_completeness.value
            row.source = record.source.model_dump()
            row.method = record.method.model_dump()
            row.review_status = record.review_status.value
            row.source_linkage_raw = record.source_linkage_raw
            row.derived_linkage = record.derived_linkage
            row.linkage_conflict = record.linkage_conflict
            row.branch = record.branch
            row.quarantine_reason = record.quarantine_reason
            row.payload = record.model_dump(mode="json")
            s.add(row)
            s.flush()

            s.execute(delete(db.ReferenceIonRow).where(db.ReferenceIonRow.record_id == row.id))
            s.add_all([
                db.ReferenceIonRow(
                    record_id=row.id, mz=ion.mz, observation=ion.observation.value,
                    relative_intensity=ion.relative_intensity,
                    intensity_class=ion.intensity_class.value, source_cell=ion.source_cell,
                ) for ion in record.ions
            ])
            return row

    @staticmethod
    def get(record_id: str) -> db.ReferenceRecordRow:
        with db.session_scope() as s:
            row = s.get(db.ReferenceRecordRow, record_id)
            if row is None:
                raise NotFoundError(f"参考条目不存在: {record_id}")
            _ = row.ions  # 触发 selectin 加载
            return row

    @staticmethod
    def list_all(*, review_status: str | None = None,
                 include_quarantined: bool = True) -> list[db.ReferenceRecordRow]:
        with db.session_scope() as s:
            q = select(db.ReferenceRecordRow)
            if review_status:
                q = q.where(db.ReferenceRecordRow.review_status == review_status)
            if not include_quarantined:
                q = q.where(db.ReferenceRecordRow.review_status != "quarantined")
            return list(s.scalars(q.order_by(db.ReferenceRecordRow.id)))

    @staticmethod
    def count() -> int:
        with db.session_scope() as s:
            return s.scalar(select(func.count(db.ReferenceRecordRow.id))) or 0

    @staticmethod
    def delete_by_source_file(source_file_id: str, *, actor: str = "system") -> dict[str, int]:
        """精确撤下某一来源的参考记录，同时清理其离子与来源映射。

        SourceFile 本身保留，便于追溯、标记 superseded_by 和恢复备份。
        """
        with db.session_scope() as s:
            record_ids = list(s.scalars(select(db.ReferenceRecordRow.id).where(
                db.ReferenceRecordRow.source_file_id == source_file_id)))
            if not record_ids:
                return {"records": 0, "ions": 0, "mappings": 0}
            ion_count = s.scalar(select(func.count(db.ReferenceIonRow.id)).where(
                db.ReferenceIonRow.record_id.in_(record_ids))) or 0
            mapping_count = s.scalar(select(func.count(db.ResidueMappingRow.id)).where(
                db.ResidueMappingRow.source_record_id.in_(record_ids))) or 0
            s.execute(delete(db.ResidueMappingRow).where(
                db.ResidueMappingRow.source_record_id.in_(record_ids)))
            s.execute(delete(db.ReferenceIonRow).where(
                db.ReferenceIonRow.record_id.in_(record_ids)))
            s.execute(delete(db.ReferenceRecordRow).where(
                db.ReferenceRecordRow.id.in_(record_ids)))
            counts = {
                "records": len(record_ids),
                "ions": int(ion_count),
                "mappings": int(mapping_count),
            }
        audit("source_file", source_file_id, "reference_records_withdrawn", counts, actor)
        return counts

    @staticmethod
    def set_review_status(record_id: str, status: str, *, reason: str | None = None, actor: str = "system"):
        with db.session_scope() as s:
            row = s.get(db.ReferenceRecordRow, record_id)
            if row is None:
                raise NotFoundError(f"参考条目不存在: {record_id}")
            row.review_status = status
            if reason is not None:
                row.quarantine_reason = reason
            s.add(row)
        audit("reference_record", record_id, f"set_review_status:{status}", {"reason": reason}, actor)


class DerivativeRepo:
    @staticmethod
    def upsert(derivative_id: str, *, skeleton: str, o_methyl: Sequence[int],
               o_acetyl: Sequence[int], label_key: str, display_name: str):
        with db.session_scope() as s:
            row = s.get(db.DerivativeRow, derivative_id)
            if row is None:
                row = db.DerivativeRow(id=derivative_id)
            row.skeleton = skeleton
            row.o_methyl_positions = list(o_methyl)
            row.o_acetyl_positions = list(o_acetyl)
            row.label_key = label_key
            row.display_name = display_name
            s.add(row)
            return row

    @staticmethod
    def get(derivative_id: str) -> db.DerivativeRow:
        with db.session_scope() as s:
            row = s.get(db.DerivativeRow, derivative_id)
            if row is None:
                raise NotFoundError(f"化学实体不存在: {derivative_id}")
            return row

    @staticmethod
    def list_all() -> list[db.DerivativeRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.DerivativeRow).order_by(db.DerivativeRow.id)))


class ResidueMappingRepo:
    @staticmethod
    def add(derivative_id: str, *, framework_id: str, parent_sugar: str | None, ring_form: str,
            linkage_positions: Sequence[int], precondition: str, source_record_id: str | None):
        with db.session_scope() as s:
            row = db.ResidueMappingRow(
                derivative_id=derivative_id, framework_id=framework_id,
                parent_sugar=parent_sugar, ring_form=ring_form,
                linkage_positions=list(linkage_positions),
                precondition=precondition, source_record_id=source_record_id)
            s.add(row)
            return row

    @staticmethod
    def clear_for_derivative(derivative_id: str):
        with db.session_scope() as s:
            s.execute(delete(db.ResidueMappingRow).where(
                db.ResidueMappingRow.derivative_id == derivative_id))

    @staticmethod
    def clear_for_source_record(derivative_id: str, source_record_id: str):
        """幂等重导当前源行，但不抹掉其他源行贡献的相容原残基来源。"""
        with db.session_scope() as s:
            s.execute(delete(db.ResidueMappingRow).where(
                db.ResidueMappingRow.derivative_id == derivative_id,
                db.ResidueMappingRow.source_record_id == source_record_id,
            ))

    @staticmethod
    def by_derivative(derivative_id: str) -> list[db.ResidueMappingRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.ResidueMappingRow).where(
                db.ResidueMappingRow.derivative_id == derivative_id)))

    @staticmethod
    def list_all() -> list[db.ResidueMappingRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.ResidueMappingRow)))


class RetentionRepo:
    @staticmethod
    def add(derivative_id: str, *, method_id: str, kind: str, value: float, unit: str,
            reference_identity: str | None, algorithm: str | None,
            n_replicates: int | None = None, sd: float | None = None):
        with db.session_scope() as s:
            row = db.RetentionObservationRow(
                derivative_id=derivative_id, method_id=method_id, kind=kind, value=value,
                unit=unit, reference_identity=reference_identity, algorithm=algorithm,
                n_replicates=n_replicates, sd=sd)
            s.add(row)
            return row

    @staticmethod
    def by_derivative(derivative_id: str, method_id: str | None = None) -> list[db.RetentionObservationRow]:
        with db.session_scope() as s:
            q = select(db.RetentionObservationRow).where(
                db.RetentionObservationRow.derivative_id == derivative_id)
            if method_id:
                q = q.where(db.RetentionObservationRow.method_id == method_id)
            return list(s.scalars(q))


# ====================================================================== 方法
class MethodRepo:
    @staticmethod
    def upsert(method_id: str, **kw) -> db.MethodRow:
        with db.session_scope() as s:
            row = s.get(db.MethodRow, method_id)
            if row is None:
                row = db.MethodRow(id=method_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def get(method_id: str) -> db.MethodRow:
        with db.session_scope() as s:
            row = s.get(db.MethodRow, method_id)
            if row is None:
                raise NotFoundError(f"方法版本不存在: {method_id}")
            return row

    @staticmethod
    def list_all() -> list[db.MethodRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.MethodRow)))

    @staticmethod
    def lock(method_id: str):
        with db.session_scope() as s:
            row = s.get(db.MethodRow, method_id)
            if row:
                row.locked = True
                s.add(row)


# ====================================================================== 样本 / 制备
class SampleRepo:
    @staticmethod
    def upsert(sample_id: str, **kw) -> db.SampleRow:
        with db.session_scope() as s:
            row = s.get(db.SampleRow, sample_id)
            if row is None:
                row = db.SampleRow(id=sample_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def get(sample_id: str) -> db.SampleRow:
        with db.session_scope() as s:
            row = s.get(db.SampleRow, sample_id)
            if row is None:
                raise NotFoundError(f"样本不存在: {sample_id}")
            return row

    @staticmethod
    def list_all() -> list[db.SampleRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.SampleRow).order_by(db.SampleRow.created_at.desc())))


class PreparationRepo:
    @staticmethod
    def upsert(batch_id: str, **kw) -> db.PreparationBatchRow:
        with db.session_scope() as s:
            row = s.get(db.PreparationBatchRow, batch_id)
            if row is None:
                row = db.PreparationBatchRow(id=batch_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def by_sample(sample_id: str) -> list[db.PreparationBatchRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.PreparationBatchRow).where(
                db.PreparationBatchRow.sample_id == sample_id)))

    @staticmethod
    def get(batch_id: str) -> db.PreparationBatchRow:
        with db.session_scope() as s:
            row = s.get(db.PreparationBatchRow, batch_id)
            if row is None:
                raise NotFoundError(f"制备批次不存在: {batch_id}")
            return row


# ====================================================================== 分析
class AnalysisRepo:
    @staticmethod
    def create(analysis_id: str, **kw) -> db.AnalysisRow:
        with db.session_scope() as s:
            row = db.AnalysisRow(id=analysis_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def get(analysis_id: str) -> db.AnalysisRow:
        with db.session_scope() as s:
            row = s.get(db.AnalysisRow, analysis_id)
            if row is None:
                raise NotFoundError(f"分析任务不存在: {analysis_id}")
            return row

    @staticmethod
    def update(analysis_id: str, **kw):
        with db.session_scope() as s:
            row = s.get(db.AnalysisRow, analysis_id)
            if row is None:
                raise NotFoundError(f"分析任务不存在: {analysis_id}")
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def list_all() -> list[db.AnalysisRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.AnalysisRow).order_by(db.AnalysisRow.created_at.desc())))

    @staticmethod
    def by_idempotency_key(key: str) -> db.AnalysisRow | None:
        with db.session_scope() as s:
            return s.scalar(select(db.AnalysisRow).where(db.AnalysisRow.idempotency_key == key))


class ComponentRepo:
    @staticmethod
    def upsert(row_id: str, **kw) -> db.ComponentRow:
        with db.session_scope() as s:
            row = s.get(db.ComponentRow, row_id)
            if row is None:
                row = db.ComponentRow(id=row_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def by_analysis(analysis_id: str) -> list[db.ComponentRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.ComponentRow).where(
                db.ComponentRow.analysis_id == analysis_id).order_by(db.ComponentRow.component_id)))

    @staticmethod
    def get(row_id: str) -> db.ComponentRow:
        with db.session_scope() as s:
            row = s.get(db.ComponentRow, row_id)
            if row is None:
                raise NotFoundError(f"组分不存在: {row_id}")
            return row


class DecisionRepo:
    @staticmethod
    def add(component_id: str, *, decision: dict, reviewer: str, reason: str,
            supersedes_id: str | None = None) -> db.DecisionRow:
        rid = new_id("dec")
        with db.session_scope() as s:
            row = db.DecisionRow(id=rid, component_id=component_id, decision=decision,
                                 reviewer=reviewer, reason=reason, supersedes_id=supersedes_id)
            s.add(row)
        audit("component", component_id, "human_review", {"decision": decision}, reviewer)
        return row

    @staticmethod
    def by_component(component_id: str) -> list[db.DecisionRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.DecisionRow).where(
                db.DecisionRow.component_id == component_id).order_by(db.DecisionRow.created_at)))


# ====================================================================== LLM
class LLMProviderRepo:
    @staticmethod
    def upsert(provider_id: str, **kw) -> db.LLMProviderRow:
        with db.session_scope() as s:
            row = s.get(db.LLMProviderRow, provider_id)
            if row is None:
                row = db.LLMProviderRow(id=provider_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            if row.adapter:  # 配置变更 => 版本号自增，缓存键随之变化（B12）
                pass
            s.add(row)
            return row

    @staticmethod
    def get(provider_id: str) -> db.LLMProviderRow:
        with db.session_scope() as s:
            row = s.get(db.LLMProviderRow, provider_id)
            if row is None:
                raise NotFoundError(f"LLM 服务商配置不存在: {provider_id}")
            return row

    @staticmethod
    def list_all() -> list[db.LLMProviderRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.LLMProviderRow)))


class BundleRepo:
    @staticmethod
    def save(bundle_id: str, *, analysis_id: str, analysis_revision: int,
             component_id: str, bundle_hash: str, payload: dict) -> db.EvidenceBundleRow:
        with db.session_scope() as s:
            row = s.get(db.EvidenceBundleRow, bundle_id)
            if row is None:
                row = db.EvidenceBundleRow(id=bundle_id)
            row.analysis_id = analysis_id
            row.analysis_revision = analysis_revision
            row.component_id = component_id
            row.bundle_hash = bundle_hash
            row.payload = payload
            s.add(row)
            return row

    @staticmethod
    def get(bundle_id: str) -> db.EvidenceBundleRow:
        with db.session_scope() as s:
            row = s.get(db.EvidenceBundleRow, bundle_id)
            if row is None:
                raise NotFoundError(f"证据包不存在: {bundle_id}")
            return row

    @staticmethod
    def find_by_hash(bundle_hash: str) -> db.EvidenceBundleRow | None:
        with db.session_scope() as s:
            return s.scalar(select(db.EvidenceBundleRow).where(
                db.EvidenceBundleRow.bundle_hash == bundle_hash))


class AssessmentRepo:
    @staticmethod
    def save(assessment_id: str, **kw) -> db.LLMAssessmentRow:
        with db.session_scope() as s:
            row = s.get(db.LLMAssessmentRow, assessment_id)
            if row is None:
                row = db.LLMAssessmentRow(id=assessment_id)
            for k, v in kw.items():
                if hasattr(row, k):
                    setattr(row, k, v)
            s.add(row)
            return row

    @staticmethod
    def get(assessment_id: str) -> db.LLMAssessmentRow:
        with db.session_scope() as s:
            row = s.get(db.LLMAssessmentRow, assessment_id)
            if row is None:
                raise NotFoundError(f"LLM 评议不存在: {assessment_id}")
            return row

    @staticmethod
    def find_by_request_hash(h: str) -> db.LLMAssessmentRow | None:
        with db.session_scope() as s:
            return s.scalar(select(db.LLMAssessmentRow).where(
                db.LLMAssessmentRow.request_hash == h))

    @staticmethod
    def list_for_analysis(analysis_id: str) -> list[db.LLMAssessmentRow]:
        with db.session_scope() as s:
            bundles = s.scalars(select(db.EvidenceBundleRow.id).where(
                db.EvidenceBundleRow.analysis_id == analysis_id)).all()
            if not bundles:
                return []
            return list(s.scalars(select(db.LLMAssessmentRow).where(
                db.LLMAssessmentRow.bundle_id.in_(bundles))))


# ====================================================================== 报告 / 审计
class ReportRepo:
    @staticmethod
    def save(report_id: str, *, analysis_id: str, analysis_revision: int,
             kind: str, payload: dict, sha256: str) -> db.ReportRow:
        with db.session_scope() as s:
            row = db.ReportRow(id=report_id, analysis_id=analysis_id,
                               analysis_revision=analysis_revision, kind=kind,
                               payload=payload, sha256=sha256)
            s.add(row)
            return row

    @staticmethod
    def list_for_analysis(analysis_id: str) -> list[db.ReportRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.ReportRow).where(
                db.ReportRow.analysis_id == analysis_id).order_by(db.ReportRow.created_at.desc())))

    @staticmethod
    def get(report_id: str) -> db.ReportRow:
        with db.session_scope() as s:
            row = s.get(db.ReportRow, report_id)
            if row is None:
                raise NotFoundError(f"报告不存在: {report_id}")
            return row


class AuditRepo:
    @staticmethod
    def list_for(entity_id: str) -> list[db.AuditLogRow]:
        with db.session_scope() as s:
            return list(s.scalars(select(db.AuditLogRow).where(
                db.AuditLogRow.entity_id == entity_id).order_by(db.AuditLogRow.created_at)))
