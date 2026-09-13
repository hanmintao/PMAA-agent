"""
峰表导入（已积分峰表 / 仪器导出峰表）。

两种输入模式严格区分，导入时即固化，后续评分器不得混用：
  * qualitative_ion_list —— marker 为 "+"/"red"，绝不生成数值强度；
  * numeric_spectrum     —— intensity 为数值。

导入不做任何"看起来像数字就当强度"的推断。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from ..domain.enums import EvidenceType, SpectrumCompleteness
from ..domain.errors import IngestError
from ..domain.enums import StableErrorCode


def _parse_ions(raw: Any, evidence_type: EvidenceType) -> list[dict]:
    """
    ions 接受三种写法：
      * 列表：[{"mz":118,"marker":"red"}] 或 [{"mz":118,"intensity":100}]
      * 字符串："118:red,129:+,233:+"
      * 字符串："118:100,129:65.9"
    """
    out: list[dict] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and raw.strip():
        items = []
        for piece in raw.replace("；", ";").replace("，", ",").split(","):
            piece = piece.strip()
            if not piece:
                continue
            if ":" in piece:
                mz_s, val_s = piece.split(":", 1)
            elif " " in piece:
                mz_s, val_s = piece.split(None, 1)
            else:
                mz_s, val_s = piece, "+"
            items.append({"mz": mz_s, "marker": val_s, "intensity": val_s})
    else:
        return out

    for it in items:
        try:
            mz = float(it.get("mz"))
        except (TypeError, ValueError):
            raise IngestError(f"无法解析 m/z: {it!r}", code=StableErrorCode.NUMERIC_INTENSITY_FORBIDDEN)
        if evidence_type == EvidenceType.numeric_spectrum:
            v = it.get("intensity")
            if v is None:
                raise IngestError(
                    "numeric_spectrum 模式下每个离子必须给 intensity",
                    code=StableErrorCode.NUMERIC_INTENSITY_FORBIDDEN)
            try:
                out.append({"mz": mz, "intensity": float(v)})
            except (TypeError, ValueError):
                raise IngestError(
                    f"numeric_spectrum 模式下 intensity 必须是数值，收到 {v!r}",
                    code=StableErrorCode.NUMERIC_INTENSITY_FORBIDDEN)
        else:
            marker = str(it.get("marker", "+")).strip()
            if marker.lower() not in ("+", "red", "＋", "强"):
                # 定性表里出现数值 → 明确报错，不悄悄当强度用
                try:
                    float(marker)
                except ValueError:
                    pass
                else:
                    raise IngestError(
                        f"定性离子表出现数值 {marker!r}：不得把定性标记当作强度（A01）",
                        code=StableErrorCode.NUMERIC_INTENSITY_FORBIDDEN)
            out.append({"mz": mz, "marker": "red" if marker.lower() in ("red", "强") else "+"})
    return out


def _normalise_component(item: dict, idx: int) -> dict:
    cid = item.get("component_id") or item.get("peak_id") or f"peak_{idx + 1:03d}"
    try:
        et = EvidenceType(item.get("evidence_type", "qualitative_ion_list"))
    except ValueError:
        raise IngestError(f"未知 evidence_type: {item.get('evidence_type')}")
    ions = _parse_ions(item.get("ions"), et)
    if not ions:
        raise IngestError(f"组分 {cid} 没有任何离子，无法检索")
    sc = item.get("spectrum_completeness", "unknown")
    try:
        sc_enum = SpectrumCompleteness(sc)
    except ValueError:
        sc_enum = SpectrumCompleteness.unknown
    return {
        "component_id": cid,
        "peak_label": item.get("peak_label"),
        "rt": float(item["rt"]) if item.get("rt") not in (None, "") else None,
        "area": float(item["area"]) if item.get("area") not in (None, "") else None,
        "area_is": float(item["area_is"]) if item.get("area_is") not in (None, "") else None,
        "evidence_type": et.value,
        "ions": ions,
        "spectrum_completeness": sc_enum.value,
        "detector": item.get("detector", "EI_TIC"),
        "integration_method": item.get("integration_method", "tic"),
        "unresolved_group_id": item.get("unresolved_group_id"),
    }


def parse_peak_table(source: Any) -> list[dict]:
    """source 可以是路径（.json/.csv）或已解析的 dict/list。"""
    if isinstance(source, (str, Path)):
        p = Path(source)
        if not p.exists():
            raise IngestError(f"峰表文件不存在: {p}")
        if p.suffix.lower() == ".json":
            data = json.loads(p.read_text(encoding="utf-8"))
        elif p.suffix.lower() in (".csv", ".tsv"):
            delim = "\t" if p.suffix.lower() == ".tsv" else ","
            with p.open(encoding="utf-8-sig") as f:
                data = list(csv.DictReader(f, delimiter=delim))
        else:
            raise IngestError(f"不支持的峰表格式: {p.suffix}")
    else:
        data = source

    if isinstance(data, dict):
        data = data.get("components") or data.get("peaks") or []
    if not isinstance(data, list):
        raise IngestError("峰表必须是列表或含 components 的对象")
    return [_normalise_component(it, i) for i, it in enumerate(data)]
