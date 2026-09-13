"""
命令行入口。

    python -m pmaa.cli init-db
    python -m pmaa.cli import-ei <xlsx路径> [--commit]
    python -m pmaa.cli seed-demo          # 建示例样本/方法/峰表并跑一次分析
    python -m pmaa.cli serve [--port 8000]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import DATA_DIR, DEFAULT_DB_URL
from .data.database import init_db

_bundled_tables = sorted((DATA_DIR / "uploads").glob("*.xlsx"))
EI_TABLE_DEFAULT = os.environ.get(
    "PMAA_EI_TABLE", str(_bundled_tables[0]) if _bundled_tables else "")


def cmd_init_db(_a):
    init_db()
    print(f"数据库已初始化: {DEFAULT_DB_URL}")


def cmd_import_ei(a):
    from .domain.enums import EvidenceTier
    from .ingest.importer import EISummaryTableImporter

    if not a.path or not Path(a.path).is_file():
        raise SystemExit(
            "未找到 EI 汇总表。请传入文件路径，或设置 PMAA_EI_TABLE；也可启动后在网页上传。")
    init_db()
    imp = EISummaryTableImporter(
        source_title=a.title, evidence_tier=EvidenceTier(a.tier),
        redistribution_status="restricted", method_id=a.method_id, reviewer=a.reviewer)
    rep = imp.commit(a.path) if a.commit else imp.preview(a.path)
    print(json.dumps(rep.summary(), ensure_ascii=False, indent=2))
    if not a.commit:
        print("\n（预览模式：未写库。加 --commit 提交）")
    return 0


def cmd_seed_demo(_a):
    """建一个示例样本、方法、峰表，并跑一次完整分析，便于端到端验证。"""
    init_db()
    from .data import repositories as repo

    repo.MethodRepo.upsert("SP2380_demo_v1", column="Supelco SP-2380 60m",
                           ei_energy_ev=70.0, scan_range_mz=[45, 350],
                           reduction_route="NaBD4", version=1)
    repo.SampleRepo.upsert("demo_sample_1", code="DEMO-001", batch="B1",
                           composition={"Glc": "major", "Gal": "minor"}, branch="neutral_aldose")
    repo.LLMProviderRepo.upsert("mock_default", name="离线模拟适配器",
                                adapter="mock", model="mock-deterministic-v1")
    print("已写入示例：方法 SP2380_demo_v1、样本 demo_sample_1、LLM 配置 mock_default")


def cmd_serve(a):
    import uvicorn
    uvicorn.run("pmaa.api.main:app", host=a.host, port=a.port, reload=a.reload)


def main(argv=None):
    p = argparse.ArgumentParser("pmaa", description="PMAA 甲基化分析平台")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    sp = sub.add_parser("import-ei")
    sp.add_argument("path", nargs="?", default=EI_TABLE_DEFAULT)
    sp.add_argument("--commit", action="store_true")
    sp.add_argument("--title", default="PMAA的EI质谱图的数据汇总表")
    sp.add_argument("--tier", default="E2", choices=["E1", "E2", "E3", "E4", "unreviewed"])
    sp.add_argument("--method-id", default=None)
    sp.add_argument("--reviewer", default="system")
    sp.set_defaults(func=cmd_import_ei)

    sub.add_parser("seed-demo").set_defaults(func=cmd_seed_demo)

    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
