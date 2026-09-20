"""能力汇总：把归属后的人员状态聚合为县级/机构能力指标。

县级汇总与机构下钻共用同一条聚合管线，规划情景也复用 aggregate，
保证正式现状与预测值的口径完全一致。
"""
from datetime import date as date_type
from datetime import timedelta

from . import domain
from .store import StoreError

BUCKET_TO_KEY = {
    domain.BUCKET_PHYSICIANS: "physicians_independent",
    domain.BUCKET_ASSISTANT_INDEPENDENT: "assistant_independent",
    domain.BUCKET_ASSISTANT_SUPERVISED: "assistant_supervised",
    domain.BUCKET_NURSES: "nurses",
    domain.BUCKET_ON_LEAVE: "on_leave",
    domain.BUCKET_OUT_OF_WINDOW: "out_of_window",
    domain.BUCKET_UNLICENSED: "unlicensed",
}


def build_status_rows(store, county_code, day, as_of):
    """按归属口径把县域人员逐一分类。

    返回 (状态行列表, 机构字典, 冲突项列表)；每行对应一名归属到本县的人员。
    """
    institutions = {i["institution_id"]: i for i in store.institutions(county_code)}
    facts = store.facts_as_of(as_of)
    persons = store.persons_as_of(as_of)
    rows, conflicts, attributed = [], [], {}
    for person in persons:
        person_id = person["person_id"]
        institution_id, basis, _overlap = domain.resolve_attribution(
            facts["employments"].get(person_id, []),
            facts["registrations"].get(person_id, []),
            day,
        )
        if institution_id not in institutions:
            continue
        attributed[person_id] = institution_id
        bucket, specialty = domain.classify(
            person,
            facts["qualifications"].get(person_id, []),
            facts["availability"].get(person_id, []),
            institutions[institution_id]["tier"],
            day,
        )
        if bucket == domain.BUCKET_UNLICENSED:
            conflicts.append({
                "type": "ACTIVE_WITHOUT_VALID_CERT",
                "person_id": person_id,
                "institution_id": institution_id,
                "detail": "已归属且未休假，但缺少有效执业资质",
            })
        rows.append({
            "person_id": person_id,
            "name": person["name"],
            "staff_type": person["staff_type"],
            "institution_id": institution_id,
            "attribution": basis,
            "bucket": bucket,
            "specialty": specialty,
        })
    conflicts.extend(domain.detect_conflicts(persons, facts, attributed, day))
    return rows, institutions, conflicts


def aggregate(rows, shortage_specialties=()):
    """把状态行聚合为能力指标；执业医师与执业助理医师严格分列。"""
    capacity = {key: 0 for key in BUCKET_TO_KEY.values()}
    capacity["headcount"] = 0
    by_specialty = {}
    for row in rows:
        capacity["headcount"] += 1
        capacity[BUCKET_TO_KEY[row["bucket"]]] += 1
        if row["bucket"] == domain.BUCKET_PHYSICIANS and row.get("specialty"):
            by_specialty[row["specialty"]] = by_specialty.get(row["specialty"], 0) + 1
    capacity["assistant_total"] = (
        capacity["assistant_independent"] + capacity["assistant_supervised"]
    )
    capacity["doctors_total"] = capacity["physicians_independent"] + capacity["assistant_total"]
    doctors = capacity["doctors_total"]
    capacity["nurse_to_doctor_ratio"] = (
        round(capacity["nurses"] / doctors, 2) if doctors else None
    )
    shortage = {name: by_specialty.get(name, 0) for name in shortage_specialties}
    return {
        "capacity": capacity,
        "by_specialty": by_specialty,
        "shortage_specialties": shortage,
    }


def county_capacity(store, contract, county_code, day, as_of):
    """县级汇总：能力构成、紧缺专科、数据新鲜度、冲突项与机构分表。"""
    rows, institutions, conflicts = build_status_rows(store, county_code, day, as_of)
    shortage = contract.get("shortage_specialties", [])
    grouped = {institution_id: [] for institution_id in institutions}
    for row in rows:
        grouped[row["institution_id"]].append(row)
    summaries = [
        {
            "institution_id": institution_id,
            "name": institutions[institution_id]["name"],
            "tier": institutions[institution_id]["tier"],
            "capacity": aggregate(grouped[institution_id], shortage)["capacity"],
        }
        for institution_id in sorted(institutions)
    ]
    total = aggregate(rows, shortage)
    return {
        "kind": "official",
        "county_code": county_code,
        "date": day,
        "as_of": as_of,
        **total,
        "freshness": _freshness(store, contract, county_code, as_of),
        "conflicts": conflicts,
        "institutions": summaries,
    }


def institution_capacity(store, contract, institution_id, day, as_of):
    """机构下钻：能力指标 + 人员名册 + 本机构相关冲突项。"""
    institution = store.institution(institution_id)
    if not institution:
        raise StoreError(404, "UNKNOWN_INSTITUTION", f"机构 {institution_id} 不存在")
    rows, _institutions, conflicts = build_status_rows(
        store, institution["county_code"], day, as_of)
    mine = [row for row in rows if row["institution_id"] == institution_id]
    person_ids = {row["person_id"] for row in mine}
    related = [
        conflict for conflict in conflicts
        if conflict.get("institution_id") == institution_id
        or institution_id in conflict.get("institution_ids", [])
        or conflict.get("person_id") in person_ids
        or person_ids & set(conflict.get("person_ids", []))
    ]
    roster = [
        {
            "person_id": row["person_id"],
            "name": row["name"],
            "staff_type": row["staff_type"],
            "staff_type_label": domain.STAFF_TYPES[row["staff_type"]],
            "status": row["bucket"],
            "status_label": domain.BUCKET_LABELS[row["bucket"]],
            "specialty": row["specialty"],
            "attribution": row["attribution"],
        }
        for row in sorted(mine, key=lambda r: r["person_id"])
    ]
    return {
        "kind": "official",
        "institution": institution,
        "date": day,
        "as_of": as_of,
        **aggregate(mine, contract.get("shortage_specialties", [])),
        "roster": roster,
        "conflicts": related,
    }


def _freshness(store, contract, county_code, as_of):
    days = int(contract.get("freshness_days", 30))
    stale_before = (date_type.fromisoformat(as_of[:10]) - timedelta(days=days)).isoformat()
    entries = store.institution_freshness(county_code, as_of, stale_before)
    return {
        "as_of": as_of,
        "stale_after_days": days,
        "institutions": entries,
        "stale_institutions": [e["institution_id"] for e in entries if e["stale"]],
        "pending_total": sum(e["pending"] for e in entries),
    }
