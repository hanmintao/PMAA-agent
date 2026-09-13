"""测试脚手架：独立临时数据库 + 轻量断言框架（不依赖 pytest 也能跑）。"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_TMP = tempfile.mkdtemp(prefix="pmaa_test_")
os.environ["PMAA_DATA_DIR"] = _TMP
os.environ["PMAA_DB_URL"] = f"sqlite:///{_TMP}/test.db"

_bundled_tables = sorted((ROOT / "data" / "uploads").glob("*.xlsx"))
EI_TABLE = os.environ.get("PMAA_TEST_EI_TABLE", str(_bundled_tables[0]) if _bundled_tables else "")


def fresh_db():
    """重建数据库。所有测试共用同一临时库，导入一次即可。"""
    import pmaa.data.database as db
    db.ENGINE.dispose()
    db.Base.metadata.drop_all(db.ENGINE)
    db.init_db()
    return db


class Suite:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def case(self, cid: str, name: str):
        """装饰器：注册一个验收用例。"""
        def deco(fn):
            self._cases.append((cid, name, fn))
            return fn
        return deco

    _cases: list = []

    def run(self):
        for cid, name, fn in self._cases:
            try:
                fn()
                self.passed.append(f"{cid} {name}")
                print(f"  \033[32mPASS\033[0m  {cid}  {name}")
            except Exception as e:
                self.failed.append((f"{cid} {name}", f"{e}\n{traceback.format_exc(limit=3)}"))
                print(f"  \033[31mFAIL\033[0m  {cid}  {name}\n        {e}")
        print()
        print(f"通过 {len(self.passed)} / {len(self._cases)}")
        if self.failed:
            print("\n失败详情：")
            for n, msg in self.failed:
                print(f"--- {n}\n{msg}")
            return 1
        return 0


SUITE = Suite()
case = SUITE.case


def eq(actual, expected, msg=""):
    assert actual == expected, f"{msg} 期望 {expected!r}，实际 {actual!r}"


def ok(cond, msg="断言失败"):
    assert cond, msg


def has(cond, msg):
    ok(cond, msg)
