"""演示数据：两个县、三家机构与若干人员，一律经正式变更-核验流程入账。"""

_LICENSE = {
    "PHYSICIAN": "PHYSICIAN_LICENSE",
    "ASSISTANT": "ASSISTANT_LICENSE",
    "NURSE": "NURSE_LICENSE",
}


def load_demo_data(store):
    """幂等载入：重复执行时因内容指纹去重不会产生重复事实。"""
    store.upsert_institution("YX-RM", "云溪县人民医院", "330127", "县级")
    store.upsert_institution("YX-XH", "云溪县西塘卫生院", "330127", "乡镇卫生院")
    store.upsert_institution("HK-RM", "湖口县人民医院", "330128", "县级")
    store.add_verifier("330127", "王岚")
    store.add_verifier("330128", "李湛")

    def submit_verify(institution_id, change_type, payload):
        change, _ = store.submit_change(
            institution_id, change_type, payload, submitted_by="院办")
        if change["status"] == "PENDING":
            verifier = "王岚" if change["county_code"] == "330127" else "李湛"
            change = store.verify_change(change["change_id"], verifier, "APPROVE")
        return change

    def register(institution_id, id_number, name, staff_type, specialty=None,
                 expires_on=None):
        qualification = {
            "cert_no": f"ZS-{id_number}",
            "qual_type": _LICENSE[staff_type],
            "specialty": specialty,
            "issued_on": "2020-01-01",
            "expires_on": expires_on,
            "effective_from": "2020-01-01",
        }
        return submit_verify(institution_id, "PERSON_REGISTER", {
            "person": {"id_number": id_number, "name": name, "staff_type": staff_type},
            "qualification": qualification,
            "registration": {"institution_id": institution_id, "reg_kind": "PRIMARY",
                             "effective_from": "2020-01-01"},
            "employment": {"institution_id": institution_id, "relation_kind": "REGULAR",
                           "is_primary": True, "effective_from": "2020-01-01"},
        })

    chen = register("YX-RM", "3301270001", "陈立", "PHYSICIAN", specialty="全科医学")
    register("YX-RM", "3301270002", "周敏", "PHYSICIAN", specialty="儿科")
    register("YX-RM", "3301270003", "吴芳", "NURSE")
    register("YX-RM", "3301270004", "郑洁", "NURSE")
    zhao = register("YX-XH", "3301270005", "赵强", "ASSISTANT", specialty="全科医学")
    register("HK-RM", "3301280001", "孙宁", "PHYSICIAN", specialty="急诊医学")

    # 陈立在西塘卫生院多点执业备案(不产生新的归属)
    chen_id = chen["result"]["person_id"]
    submit_verify("YX-XH", "REGISTRATION_ADD", {
        "person_id": chen_id, "institution_id": "YX-XH",
        "reg_kind": "MULTI_SITE", "effective_from": "2026-01-01",
    })
    # 赵强 2026 年下半年休假
    submit_verify("YX-XH", "AVAILABILITY_SET", {
        "person_id": zhao["result"]["person_id"], "kind": "LEAVE",
        "effective_from": "2026-07-01", "effective_to": "2026-12-31",
    })
