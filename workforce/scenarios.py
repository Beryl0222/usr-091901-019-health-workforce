"""规划情景：在正式现状之上演算新增岗位、培训完成与人员流失。

情景只读取正式能力汇总并返回标注为预测的结果，不持有任何写入事实台账的
路径，预测值与正式现状严格分离。
"""
from __future__ import annotations

from .model import ASSISTANT, CATEGORIES, DomainError, NURSE, PHYSICIAN
from .rules import shortage_list

ADJ_NEW_POST = "新增岗位"
ADJ_TRAINING = "培训完成"
ADJ_ATTRITION = "人员流失"
ADJUSTMENT_TYPES = (ADJ_NEW_POST, ADJ_TRAINING, ADJ_ATTRITION)


def validate_adjustment(adj) -> dict:
    if not isinstance(adj, dict):
        raise DomainError("调整项须为对象")
    kind = adj.get("type")
    if kind not in ADJUSTMENT_TYPES:
        raise DomainError(f"未知调整类型：{kind!r}，支持 {list(ADJUSTMENT_TYPES)}")
    count = adj.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise DomainError("调整项 count 须为正整数")
    if kind in (ADJ_NEW_POST, ADJ_ATTRITION) and adj.get("category") not in CATEGORIES:
        raise DomainError(f"未知人员类别：{adj.get('category')!r}")
    if kind == ADJ_TRAINING:
        for field in ("from_category", "to_category"):
            if adj.get(field) not in CATEGORIES:
                raise DomainError(f"未知人员类别：{adj.get(field)!r}")
        if adj["from_category"] == adj["to_category"]:
            raise DomainError("培训完成须改变人员类别")
    return dict(adj)


def project(base: dict, adjustments: list, config, *, scenario_id, scenario_name) -> dict:
    """以正式能力汇总为底稿演算情景；返回结果始终标注 is_projection。"""
    headcount = dict(base["headcount"])
    available = dict(base["available_week"])
    specialties = dict(base["specialty_availability"])
    for adj in adjustments:
        kind, count = adj["type"], adj["count"]
        if kind == ADJ_NEW_POST:
            category = adj["category"]
            headcount[category] += count
            if category in available:
                available[category] += count
                specialty = adj.get("specialty")
                if specialty:
                    specialties[specialty] = specialties.get(specialty, 0) + count
        elif kind == ADJ_TRAINING:
            moved = min(count, headcount[adj["from_category"]])
            headcount[adj["from_category"]] -= moved
            headcount[adj["to_category"]] += moved
            if adj["from_category"] in available and adj["to_category"] in available:
                shifted = min(moved, available[adj["from_category"]])
                available[adj["from_category"]] -= shifted
                available[adj["to_category"]] += shifted
        elif kind == ADJ_ATTRITION:
            category = adj["category"]
            lost = min(count, headcount[category])
            headcount[category] -= lost
            if category in available:
                available[category] = max(0, available[category] - lost)
            specialty = adj.get("specialty")
            if specialty and specialty in specialties:
                specialties[specialty] = max(0, specialties[specialty] - lost)
    physicians = headcount[PHYSICIAN] + headcount[ASSISTANT]
    nurses = headcount[NURSE]
    return {
        "is_projection": True,
        "scenario_id": scenario_id,
        "scenario": scenario_name,
        "county": base["county"],
        "date": base["date"],
        "as_of": base["as_of"],
        "headcount": headcount,
        "available_week": available,
        "nursing": {"注册护士": nurses,
                    "护医比": round(nurses / physicians, 2) if physicians else None},
        "specialty_availability": dict(sorted(specialties.items())),
        "shortage_specialties": shortage_list(specialties, config.shortage_thresholds),
        "base_headcount": base["headcount"],
    }
