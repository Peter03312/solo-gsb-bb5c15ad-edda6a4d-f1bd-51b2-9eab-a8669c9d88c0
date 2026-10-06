"""Pydantic 请求模型与校验。

时间字段强制时区感知；读数非负；限值数值必须有限且 min < max；
区间端点必须带时区，left>=right 的无效区间在模型层直接拒绝（422）。
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .timeutil import to_utc

_NON_EMPTY = Field(min_length=1, max_length=64)
_REQUEST_NO = Field(min_length=1, max_length=128)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndicatorCreate(StrictModel):
    code: str = _NON_EMPTY
    name: str = Field(min_length=1, max_length=128)
    unit: str = Field(min_length=1, max_length=32)


class PartitionCreate(StrictModel):
    code: str = _NON_EMPTY
    name: str = Field(min_length=1, max_length=128)


class MandatoryIndicatorIn(StrictModel):
    indicator_code: str = _NON_EMPTY


class LimitCreate(StrictModel):
    indicator_code: str = _NON_EMPTY
    # min 省略表示仅设上限（如浑浊度）；max 省略表示仅设下限
    min_value: float | None = Field(default=None)
    max_value: float | None = Field(default=None)
    valid_from: datetime
    valid_to: datetime

    @field_validator("min_value", "max_value")
    @classmethod
    def _finite_nonnegative(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("must be a finite number")
        if v < 0:
            raise ValueError("limit value must be non-negative")
        return v

    @field_validator("valid_from", "valid_to")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return to_utc(v)

    def validate_bounds(self) -> None:
        if self.min_value is None and self.max_value is None:
            raise ValueError("at least one of min_value/max_value is required")
        if self.min_value is not None and self.max_value is not None:
            if self.min_value >= self.max_value:
                raise ValueError("min_value must be strictly less than max_value")
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from must be strictly earlier than valid_to (half-open [from, to))")


class SampleCreate(StrictModel):
    partition_code: str = _NON_EMPTY
    sampled_at: datetime

    @field_validator("sampled_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return to_utc(v)


class ReadingIn(StrictModel):
    indicator_code: str = _NON_EMPTY
    value: float = Field(ge=0)  # 非负读数


class ReadingUpsert(StrictModel):
    """录入/更正读数：必带预期样本版本做乐观并发控制。

    - 首次录入：expected_version 为当前版本（初始 0），request_no 可空。
    - 更正已存在读数：request_no 必填，作为唯一请求号支持重放。
    """

    indicator_code: str = _NON_EMPTY
    value: float = Field(ge=0)
    expected_version: int = Field(ge=0)
    request_no: str | None = Field(default=None, max_length=128)


class PublishIn(StrictModel):
    expected_version: int = Field(ge=0)
