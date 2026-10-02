"""时钟抽象：生产用真时钟，测试可注入假时钟。"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_seconds(iso_ts: str, seconds: int) -> str:
    dt = datetime.fromisoformat(iso_ts) + timedelta(seconds=seconds)
    return dt.isoformat(timespec="seconds")


def remaining_seconds(deadline_iso: str) -> int:
    deadline = datetime.fromisoformat(deadline_iso)
    now = datetime.now(timezone.utc)
    return max(0, int((deadline - now).total_seconds()))


def is_past(deadline_iso: str) -> bool:
    return remaining_seconds(deadline_iso) <= 0


class Clock:
    """可注入时钟；sleep 仅推进虚拟时间，测试无需真实等待。

    go_live() 后：now() = 虚拟时间 + 自 go_live 起经过的真实时间，
    供 demo 在种子数据快进后让页面倒计时按真实时间继续。
    """

    def __init__(self, start_iso: str | None = None):
        self._now = (
            datetime.fromisoformat(start_iso)
            if start_iso
            else datetime.now(timezone.utc)
        )
        self._live_since: datetime | None = None

    def now(self) -> datetime:
        if self._live_since is not None:
            return self._now + (datetime.now(timezone.utc) - self._live_since)
        return self._now

    def iso(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def advance(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)

    def go_live(self) -> None:
        """锁定当前虚拟时间，之后叠加真实流逝时间。"""
        self._live_since = datetime.now(timezone.utc)

    def real_sleep(self, seconds: float) -> None:
        time.sleep(seconds)
