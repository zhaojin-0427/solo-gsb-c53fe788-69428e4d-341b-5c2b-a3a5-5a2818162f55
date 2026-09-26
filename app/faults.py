"""故障注入钩子（仅供测试使用）。

生产代码在关键步骤调用 :func:`faults.fire`；默认没有注册任何钩子，行为与
无此模块完全一致（零开销分支）。测试中可临时令某个点抛出异常，以验证
事务回滚不会留下部分状态；或令某个点阻塞，以确定性复现并发竞争。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class FaultGate:
    def __init__(self) -> None:
        self._hooks: dict[str, Callable[..., None]] = {}
        self._lock = threading.RLock()

    def set(self, point: str, fn: Optional[Callable[..., None]]) -> None:
        with self._lock:
            if fn is None:
                self._hooks.pop(point, None)
            else:
                self._hooks[point] = fn

    def fail_once(self, point: str, exc: Optional[BaseException] = None) -> None:
        """下次到达 point 时抛出 exc（默认 RuntimeError），随后自动移除。"""
        err = exc or RuntimeError(f"fault injected at {point}")
        done = threading.Event()

        def hook(**kw):
            if not done.is_set():
                done.set()
                raise err

        self.set(point, hook)

    def fire(self, point: str, **kwargs) -> None:
        with self._lock:
            fn = self._hooks.get(point)
        if fn is not None:
            fn(**kwargs)

    def reset(self) -> None:
        with self._lock:
            self._hooks.clear()


# 进程内单例；测试直接 monkeypatch / 调用本单例
faults = FaultGate()
