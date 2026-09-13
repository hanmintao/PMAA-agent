"""
PMAA 名称解析。

支持源表写法：
    2,3,6-Gal            甲基位 2,3,6；母糖 Gal
    2,3,4-Gal(2NAc)      甲基位 2,3,4；其中 C2 为 N-乙酰（氨基糖分支）
    2,3,4-Fuc            Fuc → 6-脱氧己糖
    2,3,5-Ara            Ara → 戊糖

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
_MOD_RE = re.compile(r"(?P<pos>\d+)\s*(?P<kind>NAc|NMe|NAcMe|N)")
_ACCEPTED_N_KINDS = {"NAc": "N-acetyl", "NMe": "N-methyl", "NAcMe": "N-acetyl-N-methyl", "N": "N-substituted"}


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

    m = _NAME_RE.match(text)
    if not m:
        return ParsedName(text, [], [], [], frozenset(), None, None, "unknown", False,
                          [f"无法解析名称: {text!r}"])

    listed = [int(x) for x in re.split(r"[,\s]+", m.group("positions").strip()) if x]
    sugar = m.group("sugar")
    mods_raw = (m.group("mods") or "").strip()

    n_positions: set[int] = set()
    n_mods: list[str] = []
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

    # 关键：N 取代碳位不是 O-甲基位（氨基糖 C2 不能算普通 O-Me）
    o_methyl = [p for p in listed if p not in n_positions]

    skeleton = skeleton_of(sugar)
    if skeleton is None:
        problems.append(f"未知糖名 {sugar!r}：无法推断骨架，按未审条目入库")

    return ParsedName(
        raw=text,
        listed_positions=sorted(set(listed)),
        o_methyl_positions=sorted(set(o_methyl)),
        n_modifications=sorted(n_mods),
        n_positions=frozenset(n_positions),
        parent_sugar=sugar,
        skeleton=skeleton,
        default_ring=DEFAULT_RING_PRIOR.get(sugar, "unknown"),
        parse_ok=True,
        problems=problems,
    )
