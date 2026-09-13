"""
PMAA 名称解析。

支持源表写法：
    2,3,6-Gal            甲基位 2,3,6；母糖 Gal
    2,3,4-Gal(2NAc)      甲基位 2,3,4；其中 C2 为 N-乙酰（氨基糖分支）
    2,3,4-Fuc            Fuc → 6-脱氧己糖
    2,3,5-Ara            Ara → 戊糖
    3-Glcp                显式 p 后缀 → Glc，吡喃型
    2,3,5-Ribf            显式 f 后缀 → Rib，呋喃型
    T-Galp                terminal → 吡喃型 Gal 的全部可甲基化 O 位
    3-GalpNAc             NAc 后缀 → C2 为 N-乙酰，不计作 O-甲基

解析结果只做**语法**解析，化学相容性交由 chemistry 层判定，
职责分离保证同一解析器可复用于任意来源表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..chemistry.frameworks import DEFAULT_RING_PRIOR, skeleton_of

_NAME_RE = re.compile(
    r"^\s*(?P<positions>[\d,\s]+)\s*[-–—]\s*(?P<sugar>[A-Za-z]+)"
    r"(?:\s*\(\s*(?P<mods>[^)]*)\))?\s*$"
)
_TERMINAL_RE = re.compile(
    r"^\s*[Tt]\s*[-–—]\s*(?P<sugar>[A-Za-z]+)"
    r"(?:\s*\(\s*(?P<mods>[^)]*)\))?\s*$"
)
_MOD_RE = re.compile(r"(?P<pos>\d+)\s*(?P<kind>NAc|NMe|NAcMe|N)")
_ACCEPTED_N_KINDS = {"NAc": "N-acetyl", "NMe": "N-methyl", "NAcMe": "N-acetyl-N-methyl", "N": "N-substituted"}

_AVAILABLE_BY_SKELETON_RING = {
    ("Hex", "p"): (2, 3, 4, 6),
    ("Hex", "f"): (2, 3, 5, 6),
    ("Pen", "p"): (2, 3, 4),
    ("Pen", "f"): (2, 3, 5),
    ("dHex", "p"): (2, 3, 4),
}


@dataclass
class ParsedName:
    raw: str
    listed_positions: list[int]          # 源表列出的取代位（含 N 位）
    o_methyl_positions: list[int]        # 剔除 N 位后的真实 O-甲基位
    n_modifications: list[str]           # 例 ["C2:N-acetyl"]
    n_positions: frozenset[int]
    parent_sugar: str | None
    skeleton: str | None                 # Hex / Pen / dHex
    default_ring: str                    # 数据表默认环型先验（可能为 unknown）
    parse_ok: bool = True
    problems: list[str] = field(default_factory=list)

    @property
    def is_amino_sugar(self) -> bool:
        return bool(self.n_positions)


def parse_pmaa_name(raw: str) -> ParsedName:
    """
    解析 PMAA 名称。解析失败不抛异常——返回 parse_ok=False 的记录，
    由导入流程隔离该行（14.8 第 5–7 步：错误行隔离，不静默丢弃）。
    """
    text = (raw or "").strip()
    if not text:
        return ParsedName(text, [], [], [], frozenset(), None, None, "unknown", False, ["名称为空"])

    terminal = _TERMINAL_RE.match(text)
    m = terminal or _NAME_RE.match(text)
    if not m:
        return ParsedName(text, [], [], [], frozenset(), None, None, "unknown", False,
                          [f"无法解析名称: {text!r}"])

    sugar_token = m.group("sugar")
    sugar, explicit_ring, suffix_n_acetyl = _normalise_sugar_token(sugar_token)
    mods_raw = (m.group("mods") or "").strip()

    n_positions: set[int] = {2} if suffix_n_acetyl else set()
    n_mods: list[str] = ["C2:N-acetyl"] if suffix_n_acetyl else []
    problems: list[str] = []

    if mods_raw:
        for piece in re.split(r"[,;/]", mods_raw):
            piece = piece.strip()
            if not piece:
                continue
            mm = _MOD_RE.match(piece)
            if not mm:
                problems.append(f"无法解析修饰片段: {piece!r}")
                continue
            pos = int(mm.group("pos"))
            kind = _ACCEPTED_N_KINDS.get(mm.group("kind"), mm.group("kind"))
            n_positions.add(pos)
            n_mods.append(f"C{pos}:{kind}")

    skeleton = skeleton_of(sugar)
    default_ring = explicit_ring or DEFAULT_RING_PRIOR.get(sugar, "unknown")

    if terminal:
        listed = list(_AVAILABLE_BY_SKELETON_RING.get((skeleton, default_ring), ()))
        if not listed:
            problems.append(
                f"terminal 名称 {text!r} 缺少可解析的骨架/环型，无法展开全部 O-甲基位")
    else:
        listed = [int(x) for x in re.split(r"[,\s]+", m.group("positions").strip()) if x]

    # 关键：N 取代碳位不是 O-甲基位（氨基糖 C2 不能算普通 O-Me）
    o_methyl = [p for p in listed if p not in n_positions]

    if skeleton is None:
        problems.append(f"未知糖名 {sugar_token!r}：无法推断骨架，按未审条目入库")

    return ParsedName(
        raw=text,
        listed_positions=sorted(set(listed)),
        o_methyl_positions=sorted(set(o_methyl)),
        n_modifications=sorted(n_mods),
        n_positions=frozenset(n_positions),
        parent_sugar=sugar,
        skeleton=skeleton,
        default_ring=default_ring,
        parse_ok=True,
        problems=problems,
    )


def _normalise_sugar_token(token: str) -> tuple[str, str | None, bool]:
    """把 Glcp/GalpNAc/Ribf 规范为基础糖名，同时保留显式环型与 C2 NAc。"""
    core = token.strip()
    suffix_n_acetyl = core.endswith("NAc")
    if suffix_n_acetyl:
        core = core[:-3]

    explicit_ring: str | None = None
    if len(core) > 1 and core[-1] in ("p", "f"):
        candidate = core[:-1]
        if skeleton_of(candidate) is not None:
            core = candidate
            explicit_ring = token[len(candidate)].lower()

    return core, explicit_ring, suffix_n_acetyl
