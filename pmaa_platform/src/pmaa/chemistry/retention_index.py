"""保留指数（Retention Index, RI）匹配骨架。

设计目标
--------
PMAA-GC-EI-MS 的 EI 碎片只取决于取代基位置，**无法区分同一骨架内的差向异构体**
（Glc/Man/Gal、Rha/Fuc、Ara/Xyl/Rib …）。唯一能把糖种分开的维度是气相色谱保留
行为——即在**特定柱子 + 特定升温程序**下的保留指数（或相对保留）。

本模块把"RI 鉴定糖种"做成**数据驱动、可插拔**的证据通道：
- 标准品 RI 存在 ``data/ri_standards.json``（或数据库），按 (method_id, skeleton,
  甲基化位点) 索引；
- ``ComponentInput`` 带 ``retention_index``（推荐）或 ``retention_time``；
- 引擎在已有 MS 骨架+连接结论后，调用 ``match_species_by_ri`` 把糖种收窄；
- **无 RI 数据 / 未提供 observed RI 时，本通道完全静默（no-op），不改变现有行为。**

⚠️ 填表即可生效：你只需把标准品 RI 填进 ``data/ri_standards.json``（schema 见文件尾
  注释 / ``DEFAULT_RI_TABLE_PATH`` 同目录示例），无需改任何逻辑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..config import DATA_DIR, SEARCH_DEFAULTS

# 标准表与参考库同目录（PROJECT_ROOT/data），受 PMAA_DATA_DIR 环境变量控制；
# 不要用 __file__.parents 推算，避免包层级变化时错位。
DEFAULT_RI_TABLE_PATH = DATA_DIR / "ri_standards.json"


@dataclass(frozen=True)
class RetentionStandard:
    """一条标准品保留指数记录。

    method_id        与 GC 方法绑定（柱子 + 升温程序），RI 是柱子相关的，必须同源。
    skeleton         单糖骨架：Hex / dHex / Pen / HexN / HexA …
    o_methyl_positions  该衍生物的 O-甲基化位点（与参考库一致），用于和 MS 结论对齐。
    parent_sugar     糖种（species）：Glc / Man / Gal / Rha / Fuc / Ara …
    retention_index  绝对保留指数（Kováts）；与 observed 同单位比较。
    retention_time   可选：若实验室习惯记保留时间（分钟），也可直接存这里、与 observed 同单位比较。
    source           来源标注（"in-house standard" / 文献出处），仅作审计。
    """

    method_id: str
    skeleton: str
    o_methyl_positions: tuple[int, ...]
    parent_sugar: str
    retention_index: float | None = None
    retention_time: float | None = None
    source: str = ""


def _norm_std(raw: dict) -> RetentionStandard:
    raw = dict(raw)
    raw["o_methyl_positions"] = tuple(int(x) for x in raw.get("o_methyl_positions", []))
    if raw.get("retention_index") is not None:
        raw["retention_index"] = float(raw["retention_index"])
    if raw.get("retention_time") is not None:
        raw["retention_time"] = float(raw["retention_time"])
    return RetentionStandard(**raw)


def load_ri_standards(path: str | Path | None = None) -> list[RetentionStandard]:
    """读取 RI 标准表。文件缺失 / 为空 / 非法 JSON 一律返回 []（no-op 友好）。"""
    p = Path(path) if path else DEFAULT_RI_TABLE_PATH
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out: list[RetentionStandard] = []
    for item in data:
        try:
            out.append(_norm_std(item))
        except (TypeError, ValueError):
            # 跳过单条坏数据，不连坐整表
            continue
    return out


@dataclass
class RIMatchResult:
    status: str  # no_data | matched | ambiguous | nearest | no_hit
    sugar: str | None = None
    candidates: list[str] = field(default_factory=list)
    delta: float | None = None
    note: str = ""


def _keyed(table: Iterable[RetentionStandard], method_id: str, skeleton: str,
           methyl: tuple[int, ...]) -> list[RetentionStandard]:
    return [s for s in table
            if s.method_id == method_id and s.skeleton == skeleton
            and s.o_methyl_positions == methyl]


def match_species_by_ri(
    observed: float,
    *,
    method_id: str,
    skeleton: str,
    o_methyl_positions: Iterable[int],
    table: Iterable[RetentionStandard],
    tolerance: float | None = None,
    wide_tolerance: float | None = None,
    use_time: bool = False,
) -> RIMatchResult:
    """在 (method_id, skeleton, methyl) 范围内，用 observed RI/RT 匹配糖种。

    observed    observed retention_index 或 retention_time（与 use_time 对应）。
    tolerance       精确容差（默认 SEARCH_DEFAULTS["ri_tolerance"]）。
    wide_tolerance   放宽容差，用于"最近邻但超出精确容差"的告警（默认 ri_wide_tolerance）。
    use_time    True 时用 retention_time 字段比较（observed 也须是时间）。

    返回状态：
    - no_data   该 (method, skeleton, methyl) 在表中无标准 → 通道静默。
    - matched   唯一标准落在精确容差内 → sugar 确定。
    - ambiguous 多个标准同时落在精确容差内 → candidates 列出，需正交确认。
    - nearest   无精确命中，但最近邻落在放宽容差内 → sugar=最近邻，附 delta 告警。
    - no_hit    全超出放宽容差 → 不匹配，返回最近邻 delta 供人工核对。
    """
    if tolerance is None:
        tolerance = SEARCH_DEFAULTS["ri_tolerance"]
    if wide_tolerance is None:
        wide_tolerance = SEARCH_DEFAULTS["ri_wide_tolerance"]

    methyl = tuple(int(x) for x in o_methyl_positions)
    pool = _keyed(table, method_id, skeleton, methyl)
    if not pool:
        return RIMatchResult(status="no_data", note="该 GC 方法/骨架/甲基化模式无 RI 标准，通道静默")

    def _val(s: RetentionStandard) -> float | None:
        return s.retention_time if use_time else s.retention_index

    usable = [s for s in pool if _val(s) is not None]
    if not usable:
        return RIMatchResult(status="no_data",
                             note="标准表未提供对应单位的保留值（RI/RT 不匹配）")

    hits = [s for s in usable if abs(_val(s) - observed) <= tolerance]
    if len(hits) == 1:
        s = hits[0]
        return RIMatchResult(status="matched", sugar=s.parent_sugar,
                             delta=round(abs(_val(s) - observed), 3),
                             note=f"RI 命中 {s.parent_sugar}（Δ={abs(_val(s)-observed):.1f}）")
    if len(hits) > 1:
        return RIMatchResult(status="ambiguous",
                             candidates=[s.parent_sugar for s in hits],
                             note="多标准落在容差内，需正交证据（NMR/酶法）定种")

    nearest = min(usable, key=lambda s: abs(_val(s) - observed))
    d = abs(_val(nearest) - observed)
    if d <= wide_tolerance:
        return RIMatchResult(status="nearest", sugar=nearest.parent_sugar, delta=round(d, 3),
                             note=f"无精确命中，最近邻 {nearest.parent_sugar}（Δ={d:.1f}，超精确容差）")
    return RIMatchResult(status="no_hit", sugar=nearest.parent_sugar, delta=round(d, 3),
                         note=f"无 RI 匹配（最近 {nearest.parent_sugar}，Δ={d:.1f}）")
