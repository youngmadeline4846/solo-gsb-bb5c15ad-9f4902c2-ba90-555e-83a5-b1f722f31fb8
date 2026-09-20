"""请求 / 响应数据模型。"""
from __future__ import annotations

import re
from typing import Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .aliquots import clean_decimal, clean_unit

_TIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?$"
)


def _normalize_time(value: Optional[str], field_name: str) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    if not _TIME_RE.match(value):
        raise ValueError(f"{field_name} 格式应为 YYYY-MM-DD HH:MM:SS")
    value = value.replace("T", " ")
    if len(value) == 16:  # YYYY-MM-DD HH:MM
        value += ":00"
    return value


class SampleCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=512)

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


class EventCreate(BaseModel):
    custodian: str = Field(..., min_length=1, max_length=64)
    start_time: str
    end_time: Optional[str] = None
    note: str = Field("", max_length=512)

    @field_validator("custodian")
    @classmethod
    def _strip_custodian(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("经手人不能为空")
        return v

    @field_validator("start_time")
    @classmethod
    def _norm_start(cls, v: str) -> str:
        normalized = _normalize_time(v, "起始时间")
        if normalized is None:
            raise ValueError("起始时间不能为空")
        return normalized

    @field_validator("end_time")
    @classmethod
    def _norm_end(cls, v: Optional[str]) -> Optional[str]:
        return _normalize_time(v, "结束时间")

    @model_validator(mode="after")
    def _check_order(self) -> "EventCreate":
        if self.end_time and self.end_time < self.start_time:
            raise ValueError("结束时间不能早于起始时间（逆序问题将在审计中标注，请先修正数据）")
        return self


class BulkImportConfirm(BaseModel):
    """确认批量导入：必须携带预览阶段返回的、绑定当前事件链的摘要令牌。"""

    token: str = Field(..., min_length=1)

    @field_validator("token")
    @classmethod
    def _strip_token(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("预览令牌不能为空，请先上传 CSV 并完成预览")
        return v


class SignoffCreate(BaseModel):
    """结论签署：在当前审计结果上生成不可变快照。"""

    signer: str = Field(..., min_length=1, max_length=64)
    signed_at: str
    remark: str = Field("", max_length=512)

    @field_validator("signer")
    @classmethod
    def _strip_signer(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("签署人不能为空")
        return v

    @field_validator("signed_at")
    @classmethod
    def _norm_signed_at(cls, v: str) -> str:
        normalized = _normalize_time(v, "签署时间")
        if normalized is None:
            raise ValueError("签署时间不能为空")
        return normalized


class SignoffRevoke(BaseModel):
    """撤销签署：必须填写原因，原快照保留。"""

    reason: str = Field(..., min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("撤销原因不能为空")
        return v


class HandoverConfirm(BaseModel):
    """双向交接确认：交出方或接收方填写姓名与确认时间。"""

    side: str = Field(..., pattern="^(from|to)$")
    confirmer_name: str = Field(..., min_length=1, max_length=64)
    confirmed_at: str

    @field_validator("confirmer_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("确认人姓名不能为空")
        return v

    @field_validator("confirmed_at")
    @classmethod
    def _norm_confirmed_at(cls, v: str) -> str:
        normalized = _normalize_time(v, "确认时间")
        if normalized is None:
            raise ValueError("确认时间不能为空")
        return normalized


class FindingDispose(BaseModel):
    """异常处置：标记已处理 / 重新打开。

    必须携带客户端看到的指纹版本号 ``expected_version``（乐观锁）；
    版本过期时服务端返回 409，客户端应刷新队列后重试。

    标记已处理（resolve）只有该发现**当前未到期租约的认领人**可以执行，
    ``handler`` 必须与认领人一致。
    """

    action: str = Field(..., pattern="^(resolve|reopen)$")
    expected_version: int = Field(..., ge=1)
    handler: str = Field(..., min_length=1, max_length=64)
    note: str = Field("", max_length=512)

    @field_validator("handler")
    @classmethod
    def _strip_handler(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("处置人不能为空")
        return v

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return v.strip()


class FindingClaim(BaseModel):
    """限时认领 / 过期接管：未处理发现认领 15 分钟。

    认领与接管在服务端单一事务内先重算发现指纹，再同时校验
    ``expected_version`` 与租约状态；租约未到期且属于他人时返回 409，
    到期后允许接管并递增版本。
    """

    expected_version: int = Field(..., ge=1)
    handler: str = Field(..., min_length=1, max_length=64)

    @field_validator("handler")
    @classmethod
    def _strip_handler(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("认领人不能为空")
        return v


class FindingRelease(BaseModel):
    """主动释放认领：只有当前（未到期租约的）认领人可以释放。"""

    expected_version: int = Field(..., ge=1)
    handler: str = Field(..., min_length=1, max_length=64)
    note: str = Field("", max_length=512)

    @field_validator("handler")
    @classmethod
    def _strip_handler(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("认领人不能为空")
        return v

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return v.strip()


# ---------- 样本分装谱系 ----------

# 精确十进制数量：仅接受字符串 / 整数（拒绝 float 的二进制误差）；
# 归一化在 service 层 clean_decimal 完成，这里先做形态与精度校验。
QuantityInput = Union[str, int]


def _validate_quantity(value, field_name: str = "数量") -> str:
    if isinstance(value, bool):
        raise ValueError(f"{field_name}必须是正数")
    if isinstance(value, float):
        raise ValueError(f"{field_name}请以字符串或整数提交精确十进制（不接受浮点数）")
    try:
        return clean_decimal(value, field=field_name)
    except ValueError as exc:
        raise ValueError(str(exc))


class SampleQuantityInit(BaseModel):
    """首次分装前设置父样本初始数量与单位（仅允许一次）。"""

    initial_quantity: QuantityInput
    unit: str = Field(..., min_length=1, max_length=16)
    expected_version: int = Field(..., ge=0)

    @field_validator("initial_quantity", mode="before")
    @classmethod
    def _qty(cls, v):
        return _validate_quantity(v, "初始数量")

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        try:
            return clean_unit(v)
        except ValueError as exc:
            raise ValueError(str(exc))


class AliquotItem(BaseModel):
    """一次分装中的单个子样本：编号 + 分装量（名称可选，默认取编号）。"""

    code: str = Field(..., min_length=1, max_length=64)
    name: Optional[str] = Field(None, max_length=128)
    quantity: QuantityInput

    @field_validator("code")
    @classmethod
    def _strip_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("子样本编号不能为空")
        return v

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v):
        return v.strip() if v else None

    @field_validator("quantity", mode="before")
    @classmethod
    def _qty(cls, v):
        return _validate_quantity(v, "分装量")


class AliquotCreate(BaseModel):
    """从当前样本发起一次分装：多个子样本编号与分装量。

    必须携带父样本当前版本 ``expected_version``；分装时间缺省取服务端当前
    时间。单位由父样本继承，客户端不能为子样本指定不同单位。
    """

    items: list[AliquotItem] = Field(..., min_length=1)
    operator: str = Field(..., min_length=1, max_length=64)
    aliquot_at: Optional[str] = None
    note: str = Field("", max_length=512)
    expected_version: int = Field(..., ge=0)

    @field_validator("operator")
    @classmethod
    def _strip_operator(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("操作人不能为空")
        return v

    @field_validator("aliquot_at")
    @classmethod
    def _norm_aliquot_at(cls, v):
        return _normalize_time(v, "分装时间")

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check_duplicate_codes(self) -> "AliquotCreate":
        codes = [i.code for i in self.items]
        dup = {c for c in codes if codes.count(c) > 1}
        if dup:
            raise ValueError(f"子样本编号在本次请求中重复：{'、'.join(sorted(dup))}")
        return self


# ---------- 样本库位管理 ----------


class LocationCreate(BaseModel):
    """创建库位：编号全局唯一，容量为正整数。"""

    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    capacity: int = Field(..., ge=1, le=1000000)

    @field_validator("code", "name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("不能为空")
        return v


class LocationMoveCreate(BaseModel):
    """样本库位移动：放入（源为 null）、库位间转移或移出（目标为 null）。

    必须携带样本与源 / 目标库位的当前版本（乐观锁）；服务端在单一
    ``BEGIN IMMEDIATE`` 事务内校验样本当前库位、目标剩余容量与全部版本，
    并发争抢最后空位、陈旧来源或版本过期均返回 409。
    """

    to_location_id: Optional[int] = Field(None, ge=1)
    operator: str = Field(..., min_length=1, max_length=64)
    moved_at: Optional[str] = None
    note: str = Field("", max_length=512)
    expected_sample_version: int = Field(..., ge=0)
    expected_from_location_id: Optional[int] = Field(None, ge=1)
    expected_from_version: Optional[int] = Field(None, ge=0)
    expected_to_version: Optional[int] = Field(None, ge=0)

    @field_validator("operator")
    @classmethod
    def _strip_operator(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("操作人不能为空")
        return v

    @field_validator("moved_at")
    @classmethod
    def _norm_moved_at(cls, v):
        return _normalize_time(v, "移动时间")

    @field_validator("note")
    @classmethod
    def _strip_note(cls, v: str) -> str:
        return v.strip()

    @model_validator(mode="after")
    def _check_versions(self) -> "LocationMoveCreate":
        if self.to_location_id is not None and self.expected_to_version is None:
            raise ValueError("转移到目标库位时必须携带 expected_to_version")
        if self.expected_from_location_id is not None and self.expected_from_version is None:
            raise ValueError("样本当前在库位中时必须携带 expected_from_version")
        return self
