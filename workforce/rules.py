"""归属口径与能力汇总规则。

归属口径（同一人员跨机构出现时的唯一归属）：
1. 取生效区间覆盖统计日、状态为「在职」的劳动关系；
2. 借调关系优先于在编关系（人实际在岗于借入机构）；
3. 同级取生效日最早、事实编号最小者，保证结果确定。

人员仅在归属机构计入一次；多点执业注册不重复计人头。
计入能力还须同时满足：资质证书在统计日未到期、在归属机构有有效注册。
"""
from __future__ import annotations

from datetime import timedelta

from .model import (
    ACTIVE, ASSISTANT, AVAIL_LEAVE, AVAIL_SLOT, CATEGORIES, CATEGORY_RANK,
    EMP_IN_SERVICE, KIND_AVAILABILITY, KIND_EMPLOYMENT, KIND_QUALIFICATION,
    KIND_REGISTRATION, NURSE, PHYSICIAN, REL_SECONDMENT, REL_STAFF,
    parse_date, status_as_of,
)

DEFAULT_SHORTAGE_THRESHOLDS = {"全科医学": 2, "儿科": 1, "精神科": 1, "急诊医学": 1, "麻醉科": 1}


class CapacityConfig:
    """能力汇总参数：紧缺专科阈值与数据新鲜度阈值。"""

    def __init__(self, shortage_thresholds=None, stale_days=90):
        self.shortage_thresholds = dict(shortage_thresholds or DEFAULT_SHORTAGE_THRESHOLDS)
        self.stale_days = stale_days


def attribute(employments, day):
    """按归属口径选出统计日当天的归属劳动关系；无在职关系时返回 None。"""
    candidates = [f for f in employments
                  if f.payload.get("status") == EMP_IN_SERVICE and f.covers(day)]
    if not candidates:
        return None
    return min(candidates, key=lambda f: (
        0 if f.payload.get("relation") == REL_SECONDMENT else 1,
        f.valid_from, f.fact_id))


def _cert_valid(qual, day) -> bool:
    expires = qual.payload.get("expires")
    return not expires or parse_date(expires, "expires") >= day


def week_available_days(slots, leaves, org, day):
    """统计日所在 ISO 周内，归属机构未被休假覆盖的可服务日。"""
    monday = day - timedelta(days=day.weekday())
    days = []
    for offset in range(7):
        current = monday + timedelta(days=offset)
        has_slot = any(s.covers(current) and s.payload.get("org") == org
                       and s.payload.get("weekday") == current.weekday() for s in slots)
        on_leave = any(leave.covers(current) for leave in leaves)
        if has_slot and not on_leave:
            days.append(current)
    return days


def build_person_rows(persons, facts, day, knowledge):
    """knowledge 事务时间截点下，统计日全部可计数人员及其归属与当周可服务日。"""
    active_by_person: dict[str, list] = {}
    for fact in facts:
        if status_as_of(fact, knowledge) == ACTIVE:
            active_by_person.setdefault(fact.person_id, []).append(fact)
    rows = []
    for person_id in sorted(persons):
        own = active_by_person.get(person_id, [])
        held = {f.payload["category"] for f in own
                if f.kind == KIND_QUALIFICATION and f.covers(day) and _cert_valid(f, day)}
        if not held:
            continue
        category = max(held, key=lambda c: CATEGORY_RANK[c])
        employment = attribute([f for f in own if f.kind == KIND_EMPLOYMENT], day)
        if employment is None:
            continue
        org = employment.payload["org"]
        registration = next(
            (r for r in sorted((f for f in own if f.kind == KIND_REGISTRATION),
                               key=lambda r: r.fact_id)
             if r.covers(day) and r.payload.get("org") == org), None)
        if registration is None:
            continue  # 在归属机构无有效注册，不计入任何口径
        row = {
            "person_id": person_id,
            "name": persons[person_id].get("name"),
            "category": category,
            "org": org,
            "specialty": registration.payload.get("specialty") or "未标注",
            "attribution": REL_SECONDMENT
            if employment.payload.get("relation") == REL_SECONDMENT else REL_STAFF,
            "available_days": [],
        }
        if category in (PHYSICIAN, ASSISTANT):
            slots = [f for f in own if f.kind == KIND_AVAILABILITY
                     and f.payload.get("type") == AVAIL_SLOT]
            leaves = [f for f in own if f.kind == KIND_AVAILABILITY
                      and f.payload.get("type") == AVAIL_LEAVE]
            row["available_days"] = [d.isoformat()
                                     for d in week_available_days(slots, leaves, org, day)]
        rows.append(row)
    return rows


def shortage_list(specialty_availability, thresholds):
    """对照紧缺专科阈值，列出当周可独立接诊人数不足的专科。"""
    items = []
    for specialty, threshold in sorted(thresholds.items()):
        available = specialty_availability.get(specialty, 0)
        if available < threshold:
            items.append({"specialty": specialty, "threshold": threshold,
                          "available": available, "gap": threshold - available})
    return items


def summarize(rows, config: CapacityConfig):
    """按规划口径分列汇总：可独立执业、护理配置与紧缺专科。"""
    headcount = {category: 0 for category in CATEGORIES}
    available_week = {PHYSICIAN: 0, ASSISTANT: 0}
    specialty_availability: dict[str, int] = {}
    for row in rows:
        headcount[row["category"]] += 1
        if row["category"] in (PHYSICIAN, ASSISTANT) and row["available_days"]:
            available_week[row["category"]] += 1
            specialty = row["specialty"]
            specialty_availability[specialty] = specialty_availability.get(specialty, 0) + 1
    physicians = headcount[PHYSICIAN] + headcount[ASSISTANT]
    nurses = headcount[NURSE]
    return {
        "headcount": headcount,
        "available_week": available_week,
        "nursing": {"注册护士": nurses,
                    "护医比": round(nurses / physicians, 2) if physicians else None},
        "specialty_availability": dict(sorted(specialty_availability.items())),
        "shortage_specialties": shortage_list(specialty_availability,
                                              config.shortage_thresholds),
    }
