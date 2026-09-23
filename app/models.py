"""Pydantic request/response models."""
from __future__ import annotations

import math
from datetime import date, time
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import DEFAULT_TOLERANCE, ELEMENTS, normalize_element


class Concentrate(BaseModel):
    """One stock concentrate bottle.

    Concentrations are in mg of the *element* per L of concentrate
    (water-soluble N/P/K, elemental for micros). ``available_volume`` and
    ``max_volume`` are in mL.
    """

    name: str = Field(..., min_length=1, examples=["A液-钙肥"])
    available_volume: float = Field(..., gt=0, description="可用体积 (mL)")
    max_volume: Optional[float] = Field(
        None,
        gt=0,
        description="本配方中单液使用量上限 (mL)，缺省取可用体积",
    )
    concentrations: dict[str, float] = Field(
        default_factory=dict,
        description="各元素浓度 mg/L，键如 N/P/K/Ca/Mg/Fe ...",
    )

    # Reject misspelled keys (e.g. "concentrates" inside a concentrate)
    # instead of silently dropping them.
    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_concentrations(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 浓度不能为负")
            normalized[elem] = float(val)
        return normalized


class Targets(BaseModel):
    """Target nutrient concentrations of the final solution (mg/L)."""

    concentrations: dict[str, float]

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_targets(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 目标不能为负")
            normalized[elem] = float(val)
        return normalized


class SolveRequest(BaseModel):
    target_volume_ml: float = Field(..., gt=0, description="目标配方总体积 (mL)")
    targets: Targets
    concentrates: list[Concentrate] = Field(..., min_length=1)
    min_water_ml: float = Field(
        0,
        ge=0,
        description="稀释水最低用量 (mL)，用于强制保留稀释空间",
    )
    max_concentrate_fraction: float = Field(
        1.0,
        gt=0,
        le=1.0,
        description="浓缩液合计占总体积的最大比例 (0~1]",
    )
    tolerance: float = Field(
        DEFAULT_TOLERANCE,
        ge=0,
        le=1,
        description="可接受的最大相对偏差；超过则在 violations 中列出",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrates")
    @classmethod
    def _unique_names(cls, v: list[Concentrate]) -> list[Concentrate]:
        names = [c.name for c in v]
        if len(names) != len(set(names)):
            raise ValueError("浓缩液名称不能重复")
        return v


class ElementResult(BaseModel):
    element: str
    target_concentration: float
    actual_concentration: float
    target_amount_mg: float
    actual_amount_mg: float
    relative_deviation: Optional[float]
    status: str  # ok | under | over | unconstrained


class Violation(BaseModel):
    code: str
    message: str
    element: Optional[str] = None
    limit: Optional[float] = None
    actual: Optional[float] = None


class SolveResult(BaseModel):
    feasible: bool
    optimal: bool
    max_relative_deviation: Optional[float]
    total_volume_ml: float
    water_volume_ml: float
    concentrate_total_ml: float
    allocations: list[dict[str, float | str]]
    elements: list[ElementResult]
    violations: list[Violation] = []
    message: str = ""


class SaveRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    note: str = ""
    request: SolveRequest

    model_config = ConfigDict(extra="forbid")


class RecipeSummary(BaseModel):
    id: int
    name: str
    description: str
    created_at: str
    latest_version: int


class VersionSummary(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str


class RecipeVersion(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str
    request: dict
    result: dict


# --------------------------------------------------------------------------- #
# Irrigation planning
# --------------------------------------------------------------------------- #
# Planning over an unbounded range would make the response arbitrarily large;
# a full year + leap slack is far beyond any practical irrigation schedule.
MAX_PLAN_DAYS = 370


class IrrigationSessionInput(BaseModel):
    """One daily irrigation start in local wall-clock time."""

    start_time: time = Field(
        ...,
        description=(
            "本地开始时间 HH:MM 或 HH:MM:SS（按 timezone 解释，"
            "可落在前一日 00:00~次日 23:59 的普通一天内；持续到次日即跨午夜）"
        ),
    )
    duration_minutes: float = Field(..., gt=0, description="持续分钟数，必须为正")
    water_ml: float = Field(..., gt=0, description="本次目标水量 (mL)，必须为正")

    model_config = ConfigDict(extra="forbid")


class IrrigationPlanRequest(BaseModel):
    timezone: str = Field(
        ...,
        min_length=1,
        examples=["Asia/Shanghai"],
        description="IANA 时区名，如 Asia/Shanghai、America/New_York",
    )
    start_date: date = Field(..., description="起始日期（含，本地日历日）")
    end_date: date = Field(..., description="结束日期（含，本地日历日）")
    sessions: list[IrrigationSessionInput] = Field(
        ..., min_length=1, description="每日重复的灌溉开始时段（至少 1 个）"
    )
    daily_capacity_ml: float = Field(
        ..., ge=0, description="设备每日供水容量 (mL)，非负；0 表示当日不可供水"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        key = v.strip()
        if not key:
            raise ValueError("时区不能为空")
        try:
            ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(
                f"非法 IANA 时区 '{v}'，例如 Asia/Shanghai、"
                "Europe/London、America/New_York"
            ) from exc
        return key

    @field_validator("sessions")
    @classmethod
    def _unique_sessions(cls, v: list[IrrigationSessionInput]) -> list[IrrigationSessionInput]:
        seen: set[time] = set()
        for s in v:
            if s.start_time in seen:
                raise ValueError(
                    f"每日开始时间 {s.start_time.strftime('%H:%M:%S')} 重复"
                )
            seen.add(s.start_time)
        return v

    @model_validator(mode="after")
    def _validate_range(self) -> "IrrigationPlanRequest":
        if self.end_date < self.start_date:
            raise ValueError(
                "end_date 不能早于 start_date"
                f"（{self.start_date.isoformat()} > "
                f"{self.end_date.isoformat()}）"
            )
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_PLAN_DAYS:
            raise ValueError(
                f"日期范围最多 {MAX_PLAN_DAYS} 天，当前 {span} 天"
            )
        return self


class PlanSessionRef(BaseModel):
    """A constituent session occurrence inside a (possibly merged) batch."""

    day: str
    requested_time: str
    local_start: str
    dst_event: Optional[str] = None
    dst_note: Optional[str] = None
    water_ml: float


class IrrigationBatch(BaseModel):
    start_utc: str = Field(..., description="批次 UTC 开始时间 RFC3339")
    end_utc: str = Field(..., description="批次 UTC 结束时间 RFC3339")
    local_start: str = Field(..., description="本地显示开始时间（含 UTC 偏移）")
    local_end: str = Field(..., description="本地显示结束时间（含 UTC 偏移）")
    duration_seconds: int
    water_ml: float = Field(..., description="合并后总水量；超容也不截断")
    merged: bool
    batch_count_before_merge: int
    dst_event: Optional[str] = Field(
        None, description="gap=夏令时缺失本地时间（已顺延）；duplicate=重复本地时间"
    )
    dst_note: Optional[str] = None
    crosses_midnight: bool
    sources: list[PlanSessionRef]


class CapacityViolation(BaseModel):
    date: str
    planned_water_ml: float
    daily_capacity_ml: float
    shortfall_ml: float = Field(..., description="明确缺口 = 计划水量 - 日容量")
    message: str


class IrrigationDayResult(BaseModel):
    date: str
    batch_count_before_merge: int
    batch_count_after_merge: int
    planned_water_ml: float
    daily_capacity_ml: float
    within_capacity: bool
    shortfall_ml: float
    dst_events: list[str]


class IrrigationPlanResult(BaseModel):
    timezone: str
    start_date: str
    end_date: str
    days: int
    batch_count_before_merge: int
    batch_count_after_merge: int
    merge_gap_seconds: int
    planned_water_ml: float
    daily_capacity_ml: float
    capacity_ok: bool
    capacity_violations: list[CapacityViolation]
    day_results: list[IrrigationDayResult]
    batches: list[IrrigationBatch]


# --------------------------------------------------------------------------- #
# Concentrate consumption forecast
# --------------------------------------------------------------------------- #
class StockInput(BaseModel):
    """Current on-hand volume (mL) of one concentrate bottle."""

    name: str = Field(..., min_length=1, description="配方求解结果中的浓缩液名")
    volume_ml: float = Field(
        ..., description="当前库存 (mL)，必须为有限非负数；0 表示空瓶"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("volume_ml", mode="before")
    @classmethod
    def _non_negative_finite(cls, v) -> float:
        # mode="before" so strings like "nan"/"inf" are caught here rather
        # than being coerced to float NaN/inf by Pydantic first.
        try:
            f = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError("库存必须是数字 (mL)") from exc
        if not math.isfinite(f):
            raise ValueError("库存不能为 NaN 或无穷大")
        if f < 0:
            raise ValueError("库存不能为负")
        return f


class ConsumptionForecastRequest(BaseModel):
    """Body for POST .../versions/{v}/consumption-forecast.

    ``plan`` is a full irrigation-plan request (same validation rules as
    ``/api/irrigation/plan``); ``stocks`` gives the current volume of every
    concentrate named in the version's solve result.
    """

    plan: IrrigationPlanRequest
    stocks: list[StockInput] = Field(
        ..., min_length=1, description="各浓缩液当前库存（至少 1 项）"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("stocks")
    @classmethod
    def _unique_stock_names(cls, v: list[StockInput]) -> list[StockInput]:
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            raise ValueError("浓缩液库存条目名称不能重复")
        return v


class ForecastDosage(BaseModel):
    """Per-mL mix ratio of one concentrate in this recipe version."""

    concentrate: str
    allocation_ml: float = Field(
        ..., description="该版本求解结果中每批次目标体积下的标准用量 (mL)"
    )
    mix_total_ml: float = Field(..., description="求解时的工作液目标总体积 (mL)")
    ratio: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="配比 = allocation_ml / mix_total_ml，浓缩液 mL / 工作液 mL",
    )
    ml_per_liter_water: float = Field(
        ..., description="每 1 L 灌溉水需要的浓缩液 (mL) = 1000 * ratio"
    )


class ForecastBatchShortage(BaseModel):
    concentrate: str
    required_ml: float
    served_ml: float
    shortfall_ml: float


class ForecastBatch(BaseModel):
    """One actual (post-merge) irrigation batch and its draw on stocks."""

    date: str = Field(..., description="批次本地开始时间所在的本地日历日")
    start_utc: str
    local_start: str
    local_end: str
    water_ml: float
    merged: bool
    batch_count_before_merge: int
    crosses_midnight: bool
    required_ml: dict[str, float] = Field(
        ..., description="本批次完整预测所需各浓缩液 (mL)，库存不足也不削减"
    )
    served_ml: dict[str, float] = Field(
        ..., description="本批次实际可从库存支取的各浓缩液 (mL)"
    )
    shortfall_ml: dict[str, float] = Field(
        ..., description="本批次各浓缩液缺口 (mL)，无缺口为 0"
    )
    end_stock_ml: dict[str, float] = Field(
        ...,
        description="本批次结束后各浓缩液剩余库存 (mL)，恒非负",
    )
    stock_sufficient: bool
    shortages: list[ForecastBatchShortage]


class ForecastDay(BaseModel):
    """All batches whose local start falls on one local calendar day."""

    date: str
    batch_count: int = Field(..., description="当日实际（合并后）批次数")
    water_ml: float = Field(..., description="当日实际批次水量合计 (mL)")
    required_ml: dict[str, float]
    served_ml: dict[str, float]
    shortfall_ml: dict[str, float]
    end_stock_ml: dict[str, float] = Field(
        ..., description="当日最后一个批次结束后的剩余库存 (mL)，恒非负"
    )
    stock_sufficient: bool = Field(
        ..., description="当日所有批次是否全部足额支取"
    )


class ConsumptionForecastResult(BaseModel):
    recipe_id: int
    version: int
    recipe_feasible: bool = Field(
        ...,
        description=(
            "该版本求解结果是否在容差内可行；不可行版本（force 保存）"
            "的配比仅为最优折中，预测照算但会显式标注"
        ),
    )
    recipe_message: str
    timezone: str
    start_date: str
    end_date: str
    mix_total_ml: float = Field(..., description="配方版本的工作液目标总体积 (mL)")
    dosage: list[ForecastDosage]
    initial_stock_ml: dict[str, float]
    total_water_ml: float
    total_required_ml: dict[str, float]
    total_served_ml: dict[str, float]
    total_shortfall_ml: dict[str, float]
    final_stock_ml: dict[str, float] = Field(..., description="全部批次结束后的剩余库存")
    stock_sufficient: bool = Field(..., description="整个计划内是否从未出现缺口")
    first_shortage_date: Optional[str] = Field(
        None, description="首次出现任一浓缩液不足的本地日期；全程充足为 null"
    )
    day_results: list[ForecastDay]
    batches: list[ForecastBatch]
