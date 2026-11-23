"""Pydantic request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SupplementIn(BaseModel):
    """A newly submitted performance."""

    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="成绩编号（字符串，按编号字典序参与并列排序）",
        examples=["R-1001"],
    )
    athlete: str = Field(..., min_length=1, max_length=200, examples=["运动员甲"])
    finish_time: datetime = Field(
        ...,
        description="完赛时刻；无时区按 UTC 解释，带偏移会转换为 UTC",
        examples=["2026-05-01T10:30:00Z"],
    )
    duration_ms: int = Field(
        ...,
        gt=0,
        le=86_400_000,
        description="毫秒用时（1500 米完赛时间）",
        examples=[245_678],
    )


class WithdrawalIn(BaseModel):
    """A withdrawal only needs the result id."""

    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(..., min_length=1, max_length=64, examples=["R-1001"])


class AnnouncementIn(BaseModel):
    """One announcement, applied on top of exactly one base version."""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(
        ...,
        ge=1,
        description="提交所指定的当前版号；必须等于当前最新版",
        examples=[1],
    )
    supplements: list[SupplementIn] = Field(
        default_factory=list, description="补登的成绩"
    )
    withdrawals: list[WithdrawalIn] = Field(
        default_factory=list, description="撤销的成绩（仅需成绩编号）"
    )

    @model_validator(mode="after")
    def _at_least_one_operation(self) -> "AnnouncementIn":
        if not self.supplements and not self.withdrawals:
            raise ValueError("公告必须至少包含一条补登或撤销")
        return self


class ProgressionEntryOut(BaseModel):
    seq: int
    kind: Literal["first", "refresh", "tie"]
    result_id: str
    athlete: str
    finish_time: str
    duration_ms: int


class ResultOut(BaseModel):
    result_id: str
    athlete: str
    finish_time: str
    duration_ms: int


class RegistryResultOut(ResultOut):
    status: Literal["active", "withdrawn"]
    added_version: int
    withdrawn_version: int | None = None


class OperationOut(BaseModel):
    seq: int
    op: Literal["supplement", "withdraw"]
    result_id: str
    athlete: str | None = None
    finish_time: str | None = None
    duration_ms: int | None = None


class VersionSummary(BaseModel):
    version: int
    parent_version: int | None
    published_at: str
    supplement_count: int
    withdrawal_count: int
    progression_entry_count: int
    active_result_count: int


class VersionDetail(VersionSummary):
    recompute_from_finish_time: str | None
    reused_prefix_entries: int
    recomputed_active_results: int
    operations: list[OperationOut]
    progression: list[ProgressionEntryOut]
    active_results: list[ResultOut]


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorBody
