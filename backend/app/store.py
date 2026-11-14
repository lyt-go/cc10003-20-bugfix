"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

指标预警与指标口径之间存在跨模块的状态联动（发布新版本要冻结旧规则、下线指标要
批量冻结规则、回滚要生成新的评审版本），这些写操作必须在一个事务里完成。当前实现
用一把可重入锁串行化读写，并在最外层事务开始时给所有表做快照，出现异常时整体回滚，
避免出现「指标已经下线、规则却还停在待处理」之类的单侧状态。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 版本、审计、历史、通知不是业务模块，只在内部读写，不参与运营概览统计。
INTERNAL_MODULES = {
    "alert_history",
    "alert_notification",
    "alert_audit",
    "metric_history",
    "metric_version",
}


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._tx_depth = 0

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def module_names(self) -> list[str]:
        return sorted(name for name in self._tables if name not in INTERNAL_MODULES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def insert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        row = dict(row)
        row.setdefault("id", self.next_id(module))
        self.rows(module).append(row)
        return row

    def index_of(self, module: str, entry_id: int) -> int | None:
        for index, row in enumerate(self.rows(module)):
            if int(row.get("id", 0)) == entry_id:
                return index
        return None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """把一组跨表写操作包成原子事务。

        嵌套调用复用最外层快照：内层失败由内层回滚到最外层快照前，外层的异常再由
        外层统一回滚。这样服务之间互相调用也不会只写了一半。
        """
        with self._lock:
            outermost = self._tx_depth == 0
            snapshot = copy.deepcopy(self._tables) if outermost else None
            self._tx_depth += 1
            try:
                yield
            except Exception:
                if outermost and snapshot is not None:
                    self._tables = snapshot
                raise
            finally:
                self._tx_depth -= 1

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return copy.deepcopy(self._tables)

    def restore(self, snapshot: dict[str, list[dict[str, Any]]]) -> None:
        with self._lock:
            self._tables = copy.deepcopy(snapshot)

    def reset(self) -> None:
        """测试辅助：恢复成种子数据。"""
        with self._lock:
            self._tables = {
                name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
            }
            self._tx_depth = 0

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
