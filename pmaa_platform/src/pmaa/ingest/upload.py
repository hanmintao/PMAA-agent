"""
文件上传落盘（M02 的入口部分）。

安全与合规要点：
  * 只用 basename 重命名落盘，防路径穿越；
  * 扩展名白名单 + 压缩包黑名单（计划第 6 节：压缩包展开限制，本实现直接拒收不展开）；
  * 先算 SHA-256 再落盘，同名内容不重复保存（同一文件幂等导入，不覆盖原件）；
  * 保留原始文件名仅作元数据显示，不参与文件系统路径。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    DOCUMENT_EXTENSIONS,
    LEGACY_DOC_EXTENSIONS,
    MAX_UPLOAD_BYTES,
    PARSER_VERSION,
    REJECTED_EXTENSIONS,
    SPECTRUM_IMAGE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    UPLOAD_DIR,
)
from ..domain.enums import StableErrorCode
from ..domain.errors import IngestError

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class StoredUpload:
    path: str
    sha256: str
    filename: str
    byte_size: int
    extension: str
    reused: bool          # 已有相同内容 → 不重复写盘


def _classify(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in REJECTED_EXTENSIONS:
        raise IngestError(
            f"不支持压缩包 {ext}：平台不自动解压/展开压缩包，请解压后上传其中的数据文件",
            details={"extension": ext},
            code=StableErrorCode.IMPORT_ROW_ISOLATED,
        )
    if ext in LEGACY_DOC_EXTENSIONS:
        raise IngestError(
            f"当前不能直接解析 {ext}；请用 Word/WPS 另存为 .docx 后上传",
            details={"extension": ext, "hint": "另存为 .docx"},
            code=StableErrorCode.IMPORT_ROW_ISOLATED,
        )
    if ext not in SUPPORTED_EXTENSIONS:
        raise IngestError(
            f"不支持的文件类型 {ext}；当前支持：{sorted(SUPPORTED_EXTENSIONS)}",
            details={"extension": ext, "supported": sorted(SUPPORTED_EXTENSIONS)},
            code=StableErrorCode.IMPORT_ROW_ISOLATED,
        )
    return ext


def sniff_kind(filename: str) -> str:
    """按扩展名推断用途。"""
    from ..config import SUPPORTED_UPLOAD_FORMATS
    ext = Path(filename).suffix.lower()
    for f in SUPPORTED_UPLOAD_FORMATS:
        if f["extension"] == ext:
            return f["kind"]
    return "peak_table"


def needs_calibration(filename: str) -> bool:
    """文档/图像类提取结果必须经人工校准，不能直接当实测数据。"""
    ext = Path(filename).suffix.lower()
    return ext in DOCUMENT_EXTENSIONS or ext in SPECTRUM_IMAGE_EXTENSIONS


def store_upload(content: bytes, filename: str) -> StoredUpload:
    """
    校验并落盘。返回可直接交给导入器的本地路径。

    :raises IngestError: 类型不支持 / 体积超限 / 内容为空
    """
    ext = _classify(filename)
    size = len(content)
    if size == 0:
        raise IngestError("上传文件为空", code=StableErrorCode.IMPORT_ROW_ISOLATED)
    if size > MAX_UPLOAD_BYTES:
        raise IngestError(
            f"文件 {size} 字节超过上限 {MAX_UPLOAD_BYTES} 字节",
            details={"size": size, "limit": MAX_UPLOAD_BYTES},
            code=StableErrorCode.IMPORT_ROW_ISOLATED,
        )

    sha = hashlib.sha256(content).hexdigest()
    safe = _SAFE_NAME.sub("_", Path(filename).name)[:120] or "upload"
    target = UPLOAD_DIR / f"{sha[:32]}_{safe}"

    if target.exists() and target.stat().st_size == size:
        return StoredUpload(path=str(target), sha256=sha, filename=filename,
                            byte_size=size, extension=ext, reused=True)

    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(content)
    tmp.replace(target)
    return StoredUpload(path=str(target), sha256=sha, filename=filename,
                        byte_size=size, extension=ext, reused=False)


def parser_version_for(ext: str) -> str:
    return f"{PARSER_VERSION}:{ext}"
