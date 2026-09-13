"""数据库模型（SQLAlchemy 2.0 ORM）。

默认 SQLite。模型通过 SQLAlchemy 隔离大部分方言差异，但 PostgreSQL 尚未完成驱动、
迁移脚本、约束/并发与备份恢复验证，不能仅凭修改 PMAA_DB_URL 宣称生产可迁移。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from ..config import DATA_DIR, DEFAULT_DB_URL


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ------------------------------------------------------------------ ID / 哈希工具
def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------- 导入与来源
class SourceFile(Base):
    __tablename__ = "source_files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    filename: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String, index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    parser_version: Mapped[str] = mapped_column(String)
    imported_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)
    import_kind: Mapped[str] = mapped_column(String, default="ei_reference_table")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class ReferenceRecordRow(Base):
    """PMAA 参考条目。一条 = 源表一行的规范化结果。"""
    __tablename__ = "reference_records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_file_id: Mapped[str | None] = mapped_column(ForeignKey("source_files.id"))
    source_row_id: Mapped[str | None] = mapped_column(String)           # 例 Sheet1!R3
    derivative_name_raw: Mapped[str] = mapped_column(String)
    parent_sugar: Mapped[str | None] = mapped_column(String)
    skeleton: Mapped[str | None] = mapped_column(String)                # Hex / Pen / dHex
    ring_form: Mapped[str | None] = mapped_column(String)               # p / f / unknown
    o_methyl_positions: Mapped[list] = mapped_column(JSON, default=list)
    o_acetyl_positions: Mapped[list | None] = mapped_column(JSON)
    nitrogen_modifications: Mapped[list] = mapped_column(JSON, default=list)
    reduction_label: Mapped[dict] = mapped_column(JSON)
    evidence_type: Mapped[str] = mapped_column(String)
    spectrum_completeness: Mapped[str] = mapped_column(String)
    source: Mapped[dict] = mapped_column(JSON)
    method: Mapped[dict] = mapped_column(JSON)
    review_status: Mapped[str] = mapped_column(String, default="unreviewed")

    source_linkage_raw: Mapped[str | None] = mapped_column(String)
    derived_linkage: Mapped[list] = mapped_column(JSON, default=list)
    linkage_conflict: Mapped[bool] = mapped_column(default=False)
    branch: Mapped[str] = mapped_column(String, default="neutral_aldose")
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)

    ions: Mapped[list["ReferenceIonRow"]] = relationship(
        back_populates="record", cascade="all, delete-orphan", lazy="selectin"
    )


class ReferenceIonRow(Base):
    __tablename__ = "reference_ions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    record_id: Mapped[str] = mapped_column(ForeignKey("reference_records.id"), index=True)
    mz: Mapped[float] = mapped_column(Float)
    observation: Mapped[str] = mapped_column(String)
    relative_intensity: Mapped[float | None] = mapped_column(Float)
    intensity_class: Mapped[str] = mapped_column(String)
    source_cell: Mapped[str | None] = mapped_column(String)

    record: Mapped[ReferenceRecordRow] = relationship(back_populates="ions")


# ---------------------------------------------------------------------- 化学实体与映射
class DerivativeRow(Base):
    """PMAA 化学实体。主键是规范键，不是"原残基名称"（计划 14.6.4）。"""
    __tablename__ = "derivatives"

    id: Mapped[str] = mapped_column(String, primary_key=True)   # canonical_key
    skeleton: Mapped[str] = mapped_column(String)
    o_methyl_positions: Mapped[list] = mapped_column(JSON)
    o_acetyl_positions: Mapped[list] = mapped_column(JSON)
    label_key: Mapped[str] = mapped_column(String, default="NaBD4_CH3I")
    display_name: Mapped[str] = mapped_column(String)


class ResidueMappingRow(Base):
    """同一 derivative_id 允许多行——同一 PMAA 的多个原始来源。"""
    __tablename__ = "residue_mappings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    derivative_id: Mapped[str] = mapped_column(ForeignKey("derivatives.id"), index=True)
    framework_id: Mapped[str] = mapped_column(String)
    parent_sugar: Mapped[str | None] = mapped_column(String)
    ring_form: Mapped[str] = mapped_column(String)
    linkage_positions: Mapped[list] = mapped_column(JSON)
    precondition: Mapped[str] = mapped_column(Text, default="")
    source_record_id: Mapped[str | None] = mapped_column(String)


# ---------------------------------------------------------------------- 方法与保留
class MethodRow(Base):
    __tablename__ = "methods"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    column: Mapped[str | None] = mapped_column(String)
    temperature_program: Mapped[str | None] = mapped_column(Text)
    carrier_gas: Mapped[str | None] = mapped_column(String)
    ei_energy_ev: Mapped[float | None] = mapped_column(Float)
    scan_range_mz: Mapped[list | None] = mapped_column(JSON)
    reduction_route: Mapped[str | None] = mapped_column(String)
    locked: Mapped[bool] = mapped_column(default=False)   # 一旦用于已出报告即锁定
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class RetentionObservationRow(Base):
    """与谱图解耦，允许多柱多批次。"""
    __tablename__ = "retention_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    derivative_id: Mapped[str] = mapped_column(ForeignKey("derivatives.id"), index=True)
    method_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)          # RT / RRT / RI
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String)
    reference_identity: Mapped[str | None] = mapped_column(String)
    algorithm: Mapped[str | None] = mapped_column(String)
    n_replicates: Mapped[int | None] = mapped_column(Integer)
    sd: Mapped[float | None] = mapped_column(Float)


# ---------------------------------------------------------------------- 样本与制备
class SampleRow(Base):
    __tablename__ = "samples"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    batch: Mapped[str | None] = mapped_column(String)
    dry_mass_mg: Mapped[float | None] = mapped_column(Float)
    composition: Mapped[dict] = mapped_column(JSON, default=dict)
    modifications: Mapped[dict] = mapped_column(JSON, default=dict)
    branch: Mapped[str] = mapped_column(String, default="neutral_aldose")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class PreparationBatchRow(Base):
    __tablename__ = "preparation_batches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    sample_id: Mapped[str] = mapped_column(ForeignKey("samples.id"), index=True)
    batch_code: Mapped[str] = mapped_column(String)
    # 甲基化后分装不算独立制备（A20）
    independent_preparation_count: Mapped[int] = mapped_column(Integer, default=1)
    post_methylation_aliquots: Mapped[int] = mapped_column(Integer, default=1)
    steps: Mapped[dict] = mapped_column(JSON, default=dict)


# ---------------------------------------------------------------------- 分析与结果
class AnalysisRow(Base):
    __tablename__ = "analyses"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    sample_id: Mapped[str | None] = mapped_column(ForeignKey("samples.id"))
    method_id: Mapped[str | None] = mapped_column(String)
    library_version: Mapped[str] = mapped_column(String)
    rule_version: Mapped[str] = mapped_column(String)
    input_hash: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default="queued")
    qc_status: Mapped[str] = mapped_column(String, default="not_assessed")
    idempotency_key: Mapped[str | None] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class ComponentRow(Base):
    __tablename__ = "components"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_id: Mapped[str] = mapped_column(ForeignKey("analyses.id"), index=True)
    component_id: Mapped[str] = mapped_column(String)
    peak_label: Mapped[str | None] = mapped_column(String)
    rt: Mapped[float | None] = mapped_column(Float)
    area: Mapped[float | None] = mapped_column(Float)
    evidence_type: Mapped[str] = mapped_column(String)
    spectrum_completeness: Mapped[str] = mapped_column(String, default="unknown")
    ions: Mapped[list] = mapped_column(JSON, default=list)
    input_hash: Mapped[str] = mapped_column(String)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class DecisionRow(Base):
    """人工复核决定。覆盖判断保留前值，不原地改写历史。"""
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    component_id: Mapped[str] = mapped_column(String, index=True)
    decision: Mapped[dict] = mapped_column(JSON)
    reviewer: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text, default="")
    supersedes_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ---------------------------------------------------------------------- LLM
class LLMProviderRow(Base):
    __tablename__ = "llm_providers"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    adapter: Mapped[str] = mapped_column(String)          # mock / openai_chat / openai_responses
    base_url: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    secret_ref: Mapped[str | None] = mapped_column(String)   # 只存引用，不存密钥
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class EvidenceBundleRow(Base):
    __tablename__ = "evidence_bundles"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String, index=True)
    analysis_revision: Mapped[int] = mapped_column(Integer)
    component_id: Mapped[str] = mapped_column(String)
    bundle_hash: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class LLMAssessmentRow(Base):
    __tablename__ = "llm_assessments"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("evidence_bundles.id"), index=True)
    provider_id: Mapped[str] = mapped_column(String)
    provider_config_version: Mapped[int] = mapped_column(Integer, default=1)
    prompt_version: Mapped[str] = mapped_column(String)
    prompt_hash: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String, index=True)
    schema_version: Mapped[str] = mapped_column(String)
    generation_parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    raw_response: Mapped[str | None] = mapped_column(Text)
    parsed_response: Mapped[dict | None] = mapped_column(JSON)
    provider_response_model: Mapped[str | None] = mapped_column(String)
    provider_request_id: Mapped[str | None] = mapped_column(String)
    finish_status: Mapped[str] = mapped_column(String)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String)            # accepted / rejected / failed
    reviewer_decision: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ---------------------------------------------------------------------- 报告与审计
class ReportRow(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String, index=True)
    analysis_revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String)             # formal / draft_labelled
    payload: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity: Mapped[str] = mapped_column(String)
    entity_id: Mapped[str] = mapped_column(String, index=True)
    action: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String, default="system")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow)


# ---------------------------------------------------------------------- 引擎
def build_engine(db_url: str | None = None):
    url = db_url or DEFAULT_DB_URL
    if url.startswith("sqlite"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _pragma(dbapi_conn, _):  # 外键 + WAL，保证并发读写不互相阻塞
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
    else:
        engine = create_engine(url, future=True, pool_pre_ping=True)
    return engine


ENGINE = build_engine()
SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False, future=True)


def init_db() -> None:
    """幂等建表。"""
    Base.metadata.create_all(ENGINE)


def session_scope():
    """统一事务封装：正常提交、异常回滚、始终关闭。"""
    import contextlib

    @contextlib.contextmanager
    def _inner():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return _inner()
