"""领域模型：人员档案事实、双时间语义与状态机。

每条事实携带两个时间维度：
- 生效时间（valid_from/valid_to，闭区间）：证书到期、调动、休假自生效日起影响能力；
- 事务时间（verified_at/activated_at/superseded_at）：事实经属地核验进入台账、
  被更正版本取代而退出台账的时刻。

任意历史日期发布的能力基线 = 以该时刻为事务时间截点，重放当时已确认的事实版本；
更正只追加新版本并闭合原版本的生效区间，不倒改历史。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

# ---- 人员类别（规划口径分列） ----
PHYSICIAN = "执业医师"
ASSISTANT = "执业助理医师"
NURSE = "注册护士"
CATEGORIES = (PHYSICIAN, ASSISTANT, NURSE)
# 一人持多类资质时按最高类别计入，避免跨口径重复计人头。
CATEGORY_RANK = {NURSE: 1, ASSISTANT: 2, PHYSICIAN: 3}

# ---- 事实状态机（与领域契约 states 对齐） ----
PENDING = "待核验"
ACTIVE = "当前有效"
CONFLICT = "存在冲突"
RETIRED = "已经失效"
SUPERSEDED = "已更替"  # 派生标记：被更正版本取代，仅出现于事务时间回放

DECISION_APPROVE = "通过"
DECISION_REJECT = "驳回"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT)
RESOLUTION_SUPERSEDE = "更正既有"

# ---- 事实事类 ----
KIND_QUALIFICATION = "qualification"   # 人员资质
KIND_REGISTRATION = "registration"     # 注册地点与执业范围
KIND_EMPLOYMENT = "employment"         # 劳动关系
KIND_AVAILABILITY = "availability"     # 可服务时段
FACT_KINDS = (KIND_QUALIFICATION, KIND_REGISTRATION, KIND_EMPLOYMENT, KIND_AVAILABILITY)

# 劳动关系
EMP_IN_SERVICE = "在职"
EMP_TRAINING = "进修"
EMP_LONG_LEAVE = "长期离岗"
EMP_RESIGNED = "离职"
EMP_STATUSES = (EMP_IN_SERVICE, EMP_TRAINING, EMP_LONG_LEAVE, EMP_RESIGNED)
REL_STAFF = "在编"
REL_SECONDMENT = "借调"
RELATIONS = (REL_STAFF, REL_SECONDMENT)

# 注册类型
REG_PRIMARY = "主执业点"
REG_MULTI = "多点执业"
REG_TYPES = (REG_PRIMARY, REG_MULTI)

# 可服务时段
AVAIL_SLOT = "slot"    # 每周固定接诊单元
AVAIL_LEAVE = "leave"  # 休假等离岗区间
AVAIL_TYPES = (AVAIL_SLOT, AVAIL_LEAVE)


class DomainError(Exception):
    """业务规则违例，携带建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_date(value, field="date") -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise DomainError(f"{field} 须为 YYYY-MM-DD 格式：{value!r}")


def parse_time(value, field="as_of") -> datetime:
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise DomainError(f"{field} 须为 ISO 8601 时间格式：{value!r}")
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def new_fact_id() -> str:
    return f"F{uuid.uuid4().hex[:12]}"


def submission_key(person_id: str, kind: str, valid_from: date, payload: dict,
                   corrects: Optional[str]) -> str:
    """重复上报幂等键：同人、同事类、同生效日、同内容、同更正对象只形成一次上报。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{person_id}|{kind}|{valid_from.isoformat()}|{canonical}|{corrects or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class Fact:
    """一条人员档案事实（资质/注册/劳动关系/可服务时段）的某个版本。"""

    fact_id: str
    person_id: str
    kind: str
    payload: dict
    valid_from: date
    valid_to: Optional[date]  # 闭区间终点；None 表示开口有效
    status: str
    source_org: str
    county: str
    submitted_at: datetime
    verified_at: Optional[datetime]
    verified_by: Optional[str]
    activated_at: Optional[datetime]
    key: str
    supersedes: Optional[str]
    superseded_at: Optional[datetime]

    def covers(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)

    def to_dict(self) -> dict:
        return {
            "fact_id": self.fact_id,
            "person_id": self.person_id,
            "kind": self.kind,
            "payload": self.payload,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "status": self.status,
            "source_org": self.source_org,
            "county": self.county,
            "submitted_at": self.submitted_at.isoformat(),
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "verified_by": self.verified_by,
            "supersedes": self.supersedes,
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
        }


def status_as_of(fact: Fact, knowledge: datetime) -> str:
    """事务时间截点 knowledge 下事实所处的状态。"""
    if fact.verified_at is None or fact.verified_at > knowledge:
        return PENDING
    if fact.superseded_at is not None and fact.superseded_at <= knowledge:
        return RETIRED if fact.status == RETIRED else SUPERSEDED
    if fact.activated_at is not None and fact.activated_at <= knowledge:
        return ACTIVE
    return CONFLICT


def ranges_overlap(a: Fact, b: Fact) -> bool:
    a_to = a.valid_to or date.max
    b_to = b.valid_to or date.max
    return a.valid_from <= b_to and b.valid_from <= a_to


def facts_conflict(a: Fact, b: Fact) -> bool:
    """同一人员两条同事类事实是否构成口径冲突，需属地核验裁决。"""
    if a.person_id != b.person_id or a.kind != b.kind or not ranges_overlap(a, b):
        return False
    pa, pb = a.payload, b.payload
    if a.kind == KIND_EMPLOYMENT:
        return (
            pa.get("relation") == REL_STAFF and pb.get("relation") == REL_STAFF
            and pa.get("status") == EMP_IN_SERVICE and pb.get("status") == EMP_IN_SERVICE
            and pa.get("org") != pb.get("org")
        )
    if a.kind == KIND_QUALIFICATION:
        return pa.get("category") == pb.get("category") and pa.get("cert_no") != pb.get("cert_no")
    if a.kind == KIND_REGISTRATION:
        return pa.get("type") == REG_PRIMARY and pb.get("type") == REG_PRIMARY and pa.get("org") != pb.get("org")
    return False
