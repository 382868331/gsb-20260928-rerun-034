"""按时段变化的总功率上限。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PowerLimit:
    start_s: float
    end_s: float
    heating_kw: float
    cooling_kw: float

    def contains(self, timestamp: float) -> bool:
        return self.start_s <= timestamp < self.end_s


class PowerSchedule:
    """按时间区间查询制热/制冷总功率限制。

    未命中任何区间时使用默认上限。区间不要求排序，但不能重叠。
    """

    def __init__(
        self,
        limits: list[PowerLimit] | None = None,
        default_heating_kw: float = float("inf"),
        default_cooling_kw: float = float("inf"),
    ) -> None:
        ordered = sorted(limits or [], key=lambda item: item.start_s)
        for earlier, later in zip(ordered, ordered[1:]):
            if later.start_s < earlier.end_s:
                raise ValueError("功率限制时段不能重叠")
        self.limits = tuple(ordered)
        self.default_heating_kw = default_heating_kw
        self.default_cooling_kw = default_cooling_kw

    def limit_kw(self, mode: str, timestamp: float) -> float:
        for item in self.limits:
            if item.contains(timestamp):
                return item.heating_kw if mode == "heat" else item.cooling_kw
        return self.default_heating_kw if mode == "heat" else self.default_cooling_kw

    def snapshot(self) -> dict[str, object]:
        return {
            "default_heating_kw": self.default_heating_kw,
            "default_cooling_kw": self.default_cooling_kw,
            "limits": [
                {
                    "start_s": item.start_s,
                    "end_s": item.end_s,
                    "heating_kw": item.heating_kw,
                    "cooling_kw": item.cooling_kw,
                }
                for item in self.limits
            ],
        }

    @classmethod
    def restore(cls, data: Mapping[str, object] | None) -> "PowerSchedule":
        if data is None:
            return cls()
        limits = [
            PowerLimit(
                start_s=float(item["start_s"]),  # type: ignore[index]
                end_s=float(item["end_s"]),  # type: ignore[index]
                heating_kw=float(item["heating_kw"]),  # type: ignore[index]
                cooling_kw=float(item["cooling_kw"]),  # type: ignore[index]
            )
            for item in data.get("limits", [])  # type: ignore[union-attr]
        ]
        return cls(
            limits=limits,
            default_heating_kw=float(data.get("default_heating_kw", float("inf"))),
            default_cooling_kw=float(data.get("default_cooling_kw", float("inf"))),
        )
