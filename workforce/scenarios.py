"""规划情景：在正式现状的内存副本上模拟，预测值绝不写回事实库。

情景只读取已核验事实，输出独立的 projection 对象；
正式现状接口永远不读取情景数据，两个命名空间严格分离。
"""
from . import capacity as cap
from . import domain


def project(store, contract, scenario, day):
    """以当前已确认事实为基线，应用情景动作，返回预测与差值。"""
    as_of = store.now()
    rows, institutions, _conflicts = cap.build_status_rows(
        store, scenario["county_code"], day, as_of)
    shortage = contract.get("shortage_specialties", [])
    baseline = cap.aggregate(rows, shortage)
    simulated = [dict(row) for row in rows]
    applied = []
    for index, adjustment in enumerate(scenario["adjustments"]):
        note = {"adjustment": adjustment, "applied": True}
        effective_on = adjustment.get("effective_on") or "0000-01-01"
        if effective_on > day:
            note.update(applied=False, reason="生效日晚于统计日")
        elif adjustment["type"] == "ADD_POST":
            _add_posts(simulated, institutions, adjustment, index)
        elif adjustment["type"] == "ATTRITION":
            note.update(_apply_attrition(simulated, adjustment))
        elif adjustment["type"] == "TRAINING_COMPLETE":
            note.update(_apply_training(simulated, adjustment))
        applied.append(note)
    projected = cap.aggregate(simulated, shortage)
    return {
        "kind": "projection",
        "official": False,
        "scenario_id": scenario["scenario_id"],
        "name": scenario["name"],
        "county_code": scenario["county_code"],
        "date": day,
        "based_on": as_of,
        "baseline": baseline,
        "projected": projected,
        "delta": _diff(baseline, projected),
        "adjustments": applied,
    }


def _add_posts(rows, institutions, adjustment, index):
    """新增岗位：假设到岗即持证可上岗的规划口径。"""
    institution = institutions.get(adjustment.get("institution_id"))
    tier = institution["tier"] if institution else ""
    staff_type = adjustment.get("staff_type")
    if staff_type == domain.STAFF_PHYSICIAN:
        bucket = domain.BUCKET_PHYSICIANS
    elif staff_type == domain.STAFF_NURSE:
        bucket = domain.BUCKET_NURSES
    elif staff_type == domain.STAFF_ASSISTANT:
        bucket = (domain.BUCKET_ASSISTANT_INDEPENDENT
                  if tier in domain.ASSISTANT_INDEPENDENT_TIERS
                  else domain.BUCKET_ASSISTANT_SUPERVISED)
    else:
        return
    for n in range(int(adjustment.get("count", 1))):
        rows.append({
            "person_id": f"scenario-add-{index}-{n}",
            "name": "情景新增岗位",
            "staff_type": staff_type,
            "institution_id": adjustment.get("institution_id"),
            "attribution": "SCENARIO",
            "bucket": bucket,
            "specialty": adjustment.get("specialty"),
        })


def _apply_attrition(rows, adjustment):
    """人员流失：把该人员从模拟现状中移除。"""
    before = len(rows)
    rows[:] = [row for row in rows if row["person_id"] != adjustment.get("person_id")]
    if len(rows) == before:
        return {"applied": False, "reason": "人员不在当前现状中"}
    return {"applied": True}


def _apply_training(rows, adjustment):
    """培训完成：助理医师晋升为执业医师，和/或获得新的执业范围。"""
    for row in rows:
        if row["person_id"] != adjustment.get("person_id"):
            continue
        if (adjustment.get("promote_to") == domain.STAFF_PHYSICIAN
                and row["bucket"] in (domain.BUCKET_ASSISTANT_INDEPENDENT,
                                      domain.BUCKET_ASSISTANT_SUPERVISED)):
            row["bucket"] = domain.BUCKET_PHYSICIANS
            row["staff_type"] = domain.STAFF_PHYSICIAN
        if adjustment.get("specialty"):
            row["specialty"] = adjustment["specialty"]
        return {"applied": True}
    return {"applied": False, "reason": "人员不在当前现状中"}


def _diff(baseline, projected):
    """两个聚合结果的数值差(比值类指标不做差)。"""
    delta = {}
    for section in ("capacity", "by_specialty", "shortage_specialties"):
        base, proj = baseline[section], projected[section]
        diff = {}
        for key in set(base) | set(proj):
            if key == "nurse_to_doctor_ratio":
                continue
            value = (proj.get(key) or 0) - (base.get(key) or 0)
            if value:
                diff[key] = value
        delta[section] = diff
    return delta
