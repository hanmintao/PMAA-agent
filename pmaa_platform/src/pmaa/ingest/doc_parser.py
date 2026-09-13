"""
PDF / Word 文档解析：优先走**文本与表格**，走不通才退到图像反解。

路径优先级（质量递减、人工介入递增）：
  ① 表格/文本  —— pdfplumber 抽表格、python-docx 抽表格，直接得到 m/z 与强度；
  ② 内嵌图片  —— 把文档里的图取出来走 spectrum_image 管线；
  ③ 页面光栅化 —— PDF 整页渲染成图再走图像管线（最兜底，噪声最大）。

老版 .doc（二进制）先通过本地 LibreOffice 无界面转换为 .docx；macOS 的
textutil 仅作为文本兜底。转换后的内容仍是 derived 数据，不升级证据等级。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_NUM = re.compile(r"^\s*(\d{2,3}(?:\.\d+)?)\s*$")
_NUM_ROW = re.compile(r"\d{2,3}(?:\.\d+)?")
_LINKAGE_LABEL = re.compile(r"\b[0-9]+(?:,[0-9]+)*-[A-Za-z][A-Za-z0-9()]*\b")


@dataclass
class DocumentExtraction:
    kind: str                      # table | text | image | empty
    components: list[dict] = field(default_factory=list)   # 可直接用的峰表
    tables: list[list[list[str]]] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)      # 供图像管线的候选图
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def needs_image_calibration(self) -> bool:
        return self.kind == "image"


def _libreoffice_binary() -> str | None:
    """寻找本地转换器；环境变量用于桌面启动器或受管部署显式指定。"""
    candidates = [
        os.environ.get("PMAA_LIBREOFFICE_BIN"),
        shutil.which("libreoffice"),
        shutil.which("soffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/local/bin/libreoffice",
        "/opt/homebrew/bin/libreoffice",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def extract_legacy_doc(data: bytes, *, filename: str = "legacy.doc") -> DocumentExtraction:
    """把老式 OLE Word 文档转换为 docx 后复用同一解析管线。"""
    from ..domain.enums import StableErrorCode
    from ..domain.errors import IngestError

    if not data.startswith(bytes.fromhex("d0cf11e0")):
        raise IngestError(
            "文件扩展名是 .doc，但内容不是可识别的 Word OLE 文档",
            details={"filename": filename}, code=StableErrorCode.IMPORT_ROW_ISOLATED)

    with tempfile.TemporaryDirectory(prefix="pmaa_legacy_doc_") as tmp:
        root = Path(tmp)
        source = root / "source.doc"
        source.write_bytes(data)
        converted = root / "source.docx"
        converter = _libreoffice_binary()
        diagnostics = ""

        if converter:
            profile = root / "lo_profile"
            cmd = [
                converter, "--headless", "--norestore", "--nodefault", "--nolockcheck",
                f"-env:UserInstallation={profile.as_uri()}",
                "--convert-to", "docx", "--outdir", str(root), str(source),
            ]
            try:
                run = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
                diagnostics = (run.stdout or "") + (run.stderr or "")
            except (OSError, subprocess.TimeoutExpired) as exc:
                diagnostics = f"{type(exc).__name__}: {exc}"

        conversion_name = "LibreOffice"
        if not converted.exists():
            textutil = Path("/usr/bin/textutil")
            if textutil.is_file():
                try:
                    run = subprocess.run(
                        [str(textutil), "-convert", "docx", "-output", str(converted), str(source)],
                        capture_output=True, text=True, timeout=30, check=False)
                    diagnostics += (run.stdout or "") + (run.stderr or "")
                    conversion_name = "textutil"
                except (OSError, subprocess.TimeoutExpired) as exc:
                    diagnostics += f"{type(exc).__name__}: {exc}"

        if not converted.exists() or converted.stat().st_size == 0:
            raise IngestError(
                "无法转换老版 .doc；请安装 LibreOffice，或在 Word/WPS 中另存为 .docx",
                details={"filename": filename, "converter_output": diagnostics[-500:]},
                code=StableErrorCode.IMPORT_ROW_ISOLATED)

        try:
            result = extract_docx(converted.read_bytes())
        except Exception as exc:
            raise IngestError(
                "老版 .doc 已转换，但转换结果无法解析",
                details={"filename": filename, "type": type(exc).__name__},
                code=StableErrorCode.IMPORT_ROW_ISOLATED) from exc

        result.meta = {**result.meta, "converted_from": ".doc", "converter": conversion_name}
        result.warnings.insert(0, f"老版 .doc 已由 {conversion_name} 本地转换；请核对版式和谱图完整性")
        if conversion_name == "textutil" and result.kind == "empty":
            result.warnings.append("textutil 可能未保留内嵌图片；建议安装 LibreOffice 或另存为 .docx")
        return result


def _parse_text_lines(text: str) -> list[dict]:
    """
    逐行解析 "m/z  强度" 形式的正文。

    **必须按行解析**：如果直接把全文所有数字都当成 m/z，
    强度列（12、20、55…）会被误收为 m/z（实测出现过凭空多出 20/25/33/35/50 的假峰）。
    约定：一行内第 1 个数字 = m/z，第 2 个 = 强度（若存在）。
    """
    peaks: list[dict] = []
    seen: dict[float, dict] = {}
    for line in (text or "").splitlines():
        nums: list[float] = []
        for tok in _NUM_ROW.findall(line):
            try:
                nums.append(float(tok))
            except ValueError:
                continue
        if not nums:
            continue
        mz = nums[0]
        if not (20.0 <= mz <= 800.0):
            continue
        if len(nums) >= 2 and 0.0 <= nums[1] <= 100.0:
            item = {"mz": mz, "intensity": nums[1]}
        else:
            item = {"mz": mz, "marker": "+"}
        # 同一 m/z 若既有带强度也有定性，保留带强度的（信息更多）
        prev = seen.get(mz)
        if prev is None or ("intensity" in item and "intensity" not in prev):
            seen[mz] = item
    peaks = [seen[k] for k in sorted(seen)]
    return peaks


def _rows_to_peaks(rows: list[list[str]]) -> list[dict]:
    """
    从表格行里认出 (m/z, 强度) 对。

    兼容三种常见排版：
      A. 两列数值：m/z, 强度
      B. 单列数值：只有 m/z（强度视为定性"有信号"）
      C. 首列是峰名/标记，其后是数值
    """
    peaks: list[dict] = []
    for row in rows:
        cells = [str(c).strip() for c in row if str(c).strip() not in ("", "None")]
        if not cells:
            continue
        nums: list[float] = []
        for c in cells:
            m = _NUM.match(c)
            if m:
                try:
                    nums.append(float(m.group(1)))
                except ValueError:
                    pass
        if not nums:
            continue
        mz = nums[0]
        if not (20.0 <= mz <= 800.0):
            continue
        if len(nums) >= 2:
            peaks.append({"mz": mz, "intensity": nums[1]})
        else:
            peaks.append({"mz": mz, "marker": "+"})
    # 按 m/z 去重排序
    seen: dict[float, dict] = {}
    for p in peaks:
        seen.setdefault(p["mz"], p)
    return [seen[k] for k in sorted(seen)]


def _peaks_to_components(peaks: list[dict], *, label: str,
                         qualitative: bool) -> list[dict]:
    """把峰列表组装成一个可提交的组分。"""
    if not peaks:
        return []
    if qualitative:
        ions = [{"mz": p["mz"], "marker": p.get("marker", "+")} for p in peaks]
        et = "qualitative_ion_list"
    else:
        ions = [{"mz": p["mz"], "intensity": p.get("intensity", 100.0)} for p in peaks]
        et = "numeric_spectrum"
    return [{
        "component_id": re.sub(r"[^A-Za-z0-9_]+", "_", label)[:40] or "doc_peak_1",
        "peak_label": label,
        "evidence_type": et,
        "ions": ions,
        # 文档里的数值峰表即使带强度，也不能证明覆盖了仪器的完整采集范围。
        # 只有仪器原始数据/明确声明的全谱导出才可标 full_acquired_range。
        "spectrum_completeness": "selected_ions",
    }]


# ---------------------------------------------------------------------- PDF
def extract_pdf(data: bytes, *, max_pages: int = 20) -> DocumentExtraction:
    import pdfplumber

    tables: list[list[list[str]]] = []
    warnings: list[str] = []
    meta: dict = {"pages": 0}

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        meta["pages"] = len(pdf.pages)
        for i, page in enumerate(pdf.pages[:max_pages]):
            try:
                for t in (page.extract_tables() or []):
                    if t:
                        tables.append([[("" if c is None else str(c)) for c in row] for row in t])
            except Exception as e:                      # 个别页解析失败不应中断整体
                warnings.append(f"第 {i + 1} 页表格提取失败：{type(e).__name__}")

    flat_rows = [row for t in tables for row in t]
    peaks = _rows_to_peaks(flat_rows)
    if peaks:
        qual = all("marker" in p for p in peaks)
        comps = _peaks_to_components(peaks, label="pdf_table", qualitative=qual)
        return DocumentExtraction(kind="table", components=comps, tables=tables[:5],
                                  warnings=warnings, meta={**meta, "peak_count": len(peaks)})

    # 没有表格 → 试正文（逐行解析 m/z 与强度，避免把强度列当成 m/z）
    text_peaks: list[dict] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            text_peaks.extend(_parse_text_lines(page.extract_text() or ""))
    if text_peaks:
        qual = all("marker" in p for p in text_peaks)
        return DocumentExtraction(
            kind="text",
            components=_peaks_to_components(text_peaks, label="pdf_text", qualitative=qual),
            warnings=warnings + ([
                "正文文本提取：只有 m/z 列表，无强度信息，按定性离子表处理"] if qual else []),
            meta={**meta, "peak_count": len(text_peaks)})

    # 无文本 → 光栅化首页，交给图像管线
    images = _render_pdf_pages(data, pages=min(3, max_pages))
    if images:
        return DocumentExtraction(
            kind="image", images=images,
            warnings=warnings + ["PDF 无可提取文本/表格，已渲染页面图像，需人工校准 m/z 轴"],
            meta={**meta, "rendered_pages": len(images)})
    return DocumentExtraction(kind="empty", warnings=warnings + ["PDF 中未找到任何可提取内容"],
                              meta=meta)


def _render_pdf_pages(data: bytes, *, pages: int, dpi: int = 200) -> list[bytes]:
    try:
        import pymupdf as fitz
    except ImportError:
        return []
    out: list[bytes] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        for i in range(min(pages, doc.page_count)):
            pix = doc[i].get_pixmap(dpi=dpi)
            out.append(pix.tobytes("png"))
        doc.close()
    except Exception:
        return []
    return out


# ---------------------------------------------------------------------- DOCX
def extract_docx(data: bytes) -> DocumentExtraction:
    import docx  # python-docx

    warnings: list[str] = []
    doc = docx.Document(io.BytesIO(data))
    tables = [[[c.text.strip() for c in row.cells] for row in t.rows] for t in doc.tables]
    flat = [row for t in tables for row in t]
    peaks = _rows_to_peaks(flat)

    if peaks:
        qual = all("marker" in p for p in peaks)
        return DocumentExtraction(
            kind="table",
            components=_peaks_to_components(peaks, label="docx_table", qualitative=qual),
            tables=tables[:5], meta={"tables": len(tables), "peak_count": len(peaks)})

    # 正文（逐行解析，同 PDF 文本路径）
    text_peaks: list[dict] = []
    for p in doc.paragraphs:
        text_peaks.extend(_parse_text_lines(p.text or ""))
    if text_peaks:
        qual = all("marker" in x for x in text_peaks)
        return DocumentExtraction(
            kind="text",
            components=_peaks_to_components(text_peaks, label="docx_text", qualitative=qual),
            warnings=(["正文文本提取：只有 m/z 列表，无强度，按定性离子表处理"] if qual else []),
            meta={"peak_count": len(text_peaks)})

    # 内嵌图片：按正文出现顺序提取，并把最近的连接标签（如 1,2,3,4-Galp）带给界面。
    # 直接遍历 related_parts 虽然通常可用，但关系表顺序不是文档显示顺序的契约。
    images: list[bytes] = []
    image_labels: list[str | None] = []
    try:
        current_label: str | None = None
        seen_relationships: set[str] = set()
        embed_key = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
        for paragraph in doc.paragraphs:
            match = _LINKAGE_LABEL.search(paragraph.text or "")
            if match:
                current_label = match.group(0)
            for blip in paragraph._p.xpath(".//a:blip"):
                relationship_id = blip.get(embed_key)
                if not relationship_id or relationship_id in seen_relationships:
                    continue
                seen_relationships.add(relationship_id)
                part = doc.part.related_parts.get(relationship_id)
                content_type = getattr(part, "content_type", "") or ""
                blob = getattr(part, "blob", b"")
                if content_type.startswith("image/") and blob and len(blob) > 1024:
                    images.append(blob)
                    image_labels.append(current_label)

        # 少数转换器把图片关系放在正文树之外；保留一个无序兜底，且不重复。
        if not images:
            for relationship_id, part in doc.part.related_parts.items():
                content_type = getattr(part, "content_type", "") or ""
                blob = getattr(part, "blob", b"")
                if content_type.startswith("image/") and blob and len(blob) > 1024:
                    images.append(blob)
                    image_labels.append(None)
    except Exception as e:
        warnings.append(f"内嵌图片读取失败：{type(e).__name__}")

    if images:
        return DocumentExtraction(
            kind="image", images=images,
            warnings=["Word 中无表格/文本可提取，已取出内嵌图片，需人工校准 m/z 轴"],
            meta={"embedded_images": len(images), "image_labels": image_labels})
    return DocumentExtraction(kind="empty", warnings=["Word 文档中未找到可提取内容"])
