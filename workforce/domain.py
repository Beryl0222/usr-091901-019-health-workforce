"""区域医护能力领域规则：归属口径、可服务判定与冲突识别。

纯函数模块，不依赖存储与网络，便于独立测试。
日期统一为 ISO 字符串(YYYY-MM-DD)，时刻为带时区的 ISO 字符串；
同一格式下字典序与时间先后一致，因此全程使用字符串比较。
"""

# ---- 人员类别 -------------------------------------------------------------
STAFF_PHYSICIAN = "PHYSICIAN"   # 执业医师
STAFF_ASSISTANT = "ASSISTANT"   # 执业助理医师
STAFF_NURSE = "NURSE"           # 注册护士
STAFF_TYPES = {
    STAFF_PHYSICIAN: "执业医师",
    STAFF_ASSISTANT: "执业助理医师",
    STAFF_NURSE: "注册护士",
}

# 各类人员计入能力所需的执业资质
LICENSE_FOR = {
    STAFF_PHYSICIAN: "PHYSICIAN_LICENSE",
    STAFF_ASSISTANT: "ASSISTANT_LICENSE",
    STAFF_NURSE: "NURSE_LICENSE",
}

# 执业助理医师仅在基层机构可视为独立执业，其余机构需在执业医师指导下工作
ASSISTANT_INDEPENDENT_TIERS = ("乡镇卫生院", "村卫生室")

# ---- 劳动关系 -------------------------------------------------------------
REL_REGULAR = "REGULAR"         # 在编/合同等主要劳动关系
REL_SECONDMENT = "SECONDMENT"   # 借调(劳动关系保留在原机构)
REL_TRAINING = "TRAINING"       # 进修

# ---- 注册类型 -------------------------------------------------------------
REG_PRIMARY = "PRIMARY"         # 主执业注册
REG_MULTI_SITE = "MULTI_SITE"   # 多点执业备案

# ---- 可服务时段 -----------------------------------------------------------
AV_WORK = "WORK"                # 可服务时段
AV_LEAVE = "LEAVE"              # 休假
AV_LONG_LEAVE = "LONG_LEAVE"    # 长期离岗

FAR_FUTURE = "9999-12-31"

# ---- 人员状态桶 -----------------------------------------------------------
BUCKET_PHYSICIANS = "physicians"                    # 可独立接诊的执业医师
BUCKET_ASSISTANT_INDEPENDENT = "assistant_independent"  # 基层可独立执业的助理医师
BUCKET_ASSISTANT_SUPERVISED = "assistant_supervised"    # 需指导的助理医师
BUCKET_NURSES = "nurses"                            # 在岗可服务护士
BUCKET_ON_LEAVE = "on_leave"                        # 休假/长期离岗
BUCKET_OUT_OF_WINDOW = "out_of_window"              # 当日不在可服务时段
BUCKET_UNLICENSED = "unlicensed"                    # 在岗但无有效资质

BUCKET_LABELS = {
    BUCKET_PHYSICIANS: "可独立接诊",
    BUCKET_ASSISTANT_INDEPENDENT: "助理医师(基层可独立执业)",
    BUCKET_ASSISTANT_SUPERVISED: "助理医师(需指导)",
    BUCKET_NURSES: "护理在岗",
    BUCKET_ON_LEAVE: "休假/长期离岗",
    BUCKET_OUT_OF_WINDOW: "不在可服务时段",
    BUCKET_UNLICENSED: "无有效资质",
}


def covers(fact, day):
    """事实在统计日是否处于生效区间(含端点)。"""
    return fact["effective_from"] <= day <= (fact["effective_to"] or FAR_FUTURE)


def visible(fact, moment):
    """事实在知识时刻是否可见：已记录且尚未被更正取代。"""
    return fact["recorded_at"] <= moment and (
        fact["expired_at"] is None or fact["expired_at"] > moment
    )


def resolve_attribution(employments, registrations, day):
    """归属口径：主要劳动关系优先，主执业注册兜底。

    多点执业、借调、进修均不产生归属。返回 (机构ID, 归属依据, 是否重叠归属)；
    重叠时按生效日期最新者计入，保证同一口径下不重复计数。
    """
    primary = [
        e for e in employments
        if e["is_primary"] and e["relation_kind"] == REL_REGULAR and covers(e, day)
    ]
    if primary:
        pick = max(primary, key=lambda e: (e["effective_from"], e["recorded_at"]))
        return pick["institution_id"], "EMPLOYMENT", len(primary) > 1
    registered = [r for r in registrations if r["reg_kind"] == REG_PRIMARY and covers(r, day)]
    if registered:
        pick = max(registered, key=lambda r: (r["effective_from"], r["recorded_at"]))
        return pick["institution_id"], "REGISTRATION", len(registered) > 1
    return None, None, False


def classify(person, qualifications, availabilities, institution_tier, day):
    """把一名已归属人员分入状态桶，返回 (状态桶, 执业范围)。

    判定顺序：休假离岗 > 资质有效性 > 可服务时段 > 按人员类别分列。
    """
    on_leave = [
        a for a in availabilities
        if a["kind"] in (AV_LEAVE, AV_LONG_LEAVE) and covers(a, day)
    ]
    if on_leave:
        return BUCKET_ON_LEAVE, None
    license_type = LICENSE_FOR[person["staff_type"]]
    valid = [
        q for q in qualifications
        if q["qual_type"] == license_type
        and covers(q, day)
        and (q["expires_on"] is None or q["expires_on"] >= day)
    ]
    if not valid:
        return BUCKET_UNLICENSED, None
    windows = [a for a in availabilities if a["kind"] == AV_WORK]
    if windows and not any(covers(a, day) for a in windows):
        return BUCKET_OUT_OF_WINDOW, None
    specialty = max(valid, key=lambda q: q["effective_from"]).get("specialty")
    staff_type = person["staff_type"]
    if staff_type == STAFF_PHYSICIAN:
        return BUCKET_PHYSICIANS, specialty
    if staff_type == STAFF_NURSE:
        return BUCKET_NURSES, None
    if institution_tier in ASSISTANT_INDEPENDENT_TIERS:
        return BUCKET_ASSISTANT_INDEPENDENT, specialty
    return BUCKET_ASSISTANT_SUPERVISED, specialty


def detect_conflicts(persons, facts, attributed, day):
    """识别当前口径下的冲突项。

    attributed 为 {人员ID: 归属机构ID}，仅统计归属到本县的人员；
    冲突不阻断计数，但必须在下钻视图中可见。
    """
    conflicts = []
    by_id_number = {}
    for person in persons:
        if person["person_id"] in attributed:
            by_id_number.setdefault(person["id_number"], []).append(person["person_id"])
    for id_number, person_ids in sorted(by_id_number.items()):
        if len(person_ids) > 1:
            conflicts.append({
                "type": "DUPLICATE_IDENTITY",
                "person_ids": sorted(person_ids),
                "detail": f"证件号码 {id_number} 对应多个人员档案",
            })
    for person_id, institution_id in sorted(attributed.items()):
        employments = facts["employments"].get(person_id, [])
        registrations = facts["registrations"].get(person_id, [])
        overlapping = {
            e["institution_id"] for e in employments
            if e["is_primary"] and e["relation_kind"] == REL_REGULAR and covers(e, day)
        }
        if len(overlapping) > 1:
            conflicts.append({
                "type": "OVERLAPPING_PRIMARY_EMPLOYMENT",
                "person_id": person_id,
                "institution_ids": sorted(overlapping),
                "detail": "同一统计日存在多家机构的主要劳动关系，已按最新生效者计入一次",
            })
        has_multi = any(
            r["reg_kind"] == REG_MULTI_SITE and covers(r, day) for r in registrations
        )
        has_primary = any(
            r["reg_kind"] == REG_PRIMARY and covers(r, day) for r in registrations
        )
        if has_multi and not has_primary:
            conflicts.append({
                "type": "MULTI_SITE_WITHOUT_PRIMARY",
                "person_id": person_id,
                "institution_id": institution_id,
                "detail": "存在多点执业备案但缺少主执业注册",
            })
    return conflicts
