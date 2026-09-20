"""区域医护能力后端的领域行为测试。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, load_contract
from workforce import domain
from workforce.api import Api
from workforce.store import Store, StoreError

LICENSE = {
    "PHYSICIAN": "PHYSICIAN_LICENSE",
    "ASSISTANT": "ASSISTANT_LICENSE",
    "NURSE": "NURSE_LICENSE",
}


class WorkforceCase(unittest.TestCase):
    """通过 API 路由驱动内存事实库，时钟可操控。"""

    def setUp(self):
        self.now = ["2026-09-01T09:00:00+00:00"]
        self.store = Store(":memory:", clock=lambda: self.now[0])
        self.contract = load_contract()
        self.api = Api(self.store, self.contract)
        self.store.upsert_institution("H1", "县人民医院", "C1", "县级")
        self.store.upsert_institution("H2", "镇卫生院", "C1", "乡镇卫生院")
        self.store.upsert_institution("H3", "邻县人民医院", "C2", "县级")
        self.store.add_verifier("C1", "核验员甲")
        self.store.add_verifier("C2", "核验员乙")

    def tearDown(self):
        self.store.close()

    # ---- 请求辅助 -----------------------------------------------------------
    def get(self, path, **params):
        return self.api.dispatch("GET", path, {k: [v] for k, v in params.items()}, {})[1]

    def post(self, path, body):
        return self.api.dispatch("POST", path, {}, body)[1]

    def submit(self, institution_id, change_type, payload, key=None):
        body = {"institution_id": institution_id, "change_type": change_type,
                "payload": payload}
        if key:
            body["idempotency_key"] = key
        return self.post("/changes", body)

    def verify(self, change_id, verifier="核验员甲", decision="APPROVE"):
        return self.post(f"/changes/{change_id}/verify",
                         {"verifier": verifier, "decision": decision})

    def submit_and_verify(self, institution_id, change_type, payload, key=None):
        submitted = self.submit(institution_id, change_type, payload, key)
        return self.verify(submitted["change"]["change_id"])["change"]

    def register_person(self, institution="H1", id_number="ID001", name="张医",
                        staff_type="PHYSICIAN", specialty="内科", expires_on=None,
                        effective_from="2026-01-01", with_employment=True,
                        with_registration=True):
        payload = {
            "person": {"id_number": id_number, "name": name, "staff_type": staff_type},
            "qualification": {
                "cert_no": f"CERT-{id_number}", "qual_type": LICENSE[staff_type],
                "specialty": specialty, "issued_on": effective_from,
                "expires_on": expires_on, "effective_from": effective_from,
            },
        }
        if with_registration:
            payload["registration"] = {
                "institution_id": institution, "reg_kind": "PRIMARY",
                "effective_from": effective_from}
        if with_employment:
            payload["employment"] = {
                "institution_id": institution, "relation_kind": "REGULAR",
                "is_primary": True, "effective_from": effective_from}
        change = self.submit_and_verify(institution, "PERSON_REGISTER", payload)
        return change["result"]["person_id"]

    def county(self, county="C1", date="2026-09-01", as_of=None):
        params = {"date": date}
        if as_of:
            params["as_of"] = as_of
        return self.get(f"/counties/{county}/capacity", **params)

    # ---- 归属口径 -----------------------------------------------------------
    def test_attribution_excludes_multi_site_secondment_and_no_double_count(self):
        person = self.register_person("H1", "ID001")
        # 多点执业备案与借调都不产生新的归属
        self.submit_and_verify("H2", "REGISTRATION_ADD", {
            "person_id": person, "institution_id": "H2",
            "reg_kind": "MULTI_SITE", "effective_from": "2026-02-01"})
        self.submit_and_verify("H2", "EMPLOYMENT_START", {
            "person_id": person, "institution_id": "H2",
            "relation_kind": "SECONDMENT", "effective_from": "2026-02-01"})
        # 只有主执业注册、无劳动关系的人员按注册机构归属
        self.register_person("H2", "ID002", "李医", with_employment=False)

        capacity = self.county()["capacity"]
        self.assertEqual(capacity["physicians_independent"], 2)
        self.assertEqual(capacity["headcount"], 2)
        summaries = {s["institution_id"]: s["capacity"] for s in self.county()["institutions"]}
        self.assertEqual(summaries["H1"]["headcount"], 1)
        self.assertEqual(summaries["H2"]["headcount"], 1)

    def test_assistant_counted_separately_and_township_independent(self):
        self.register_person("H1", "ID001", "助理甲", staff_type="ASSISTANT", specialty="全科医学")
        self.register_person("H2", "ID002", "助理乙", staff_type="ASSISTANT", specialty="全科医学")
        capacity = self.county()["capacity"]
        self.assertEqual(capacity["physicians_independent"], 0)
        self.assertEqual(capacity["assistant_total"], 2)
        self.assertEqual(capacity["assistant_supervised"], 1)   # 县级机构需指导
        self.assertEqual(capacity["assistant_independent"], 1)  # 乡镇可独立执业
        self.assertEqual(capacity["doctors_total"], 2)

    def test_nurse_ratio(self):
        self.register_person("H1", "ID001")
        self.register_person("H1", "ID002", "护一", staff_type="NURSE", specialty=None)
        self.register_person("H1", "ID003", "护二", staff_type="NURSE", specialty=None)
        capacity = self.county()["capacity"]
        self.assertEqual(capacity["nurses"], 2)
        self.assertEqual(capacity["nurse_to_doctor_ratio"], 2.0)

    # ---- 生效日期与历史 -------------------------------------------------------
    def test_certificate_expiry_and_renewal(self):
        self.register_person("H1", "ID001", expires_on="2026-06-30")
        self.assertEqual(self.county(date="2026-06-30")["capacity"]["physicians_independent"], 1)
        expired = self.county(date="2026-07-01")
        self.assertEqual(expired["capacity"]["physicians_independent"], 0)
        self.assertEqual(expired["capacity"]["unlicensed"], 1)
        self.assertIn("ACTIVE_WITHOUT_VALID_CERT",
                      {c["type"] for c in expired["conflicts"]})
        # 续期自生效日起恢复能力，到期前的历史不变
        self.submit_and_verify("H1", "QUALIFICATION_RENEW", {
            "person_id": self.store.persons_as_of(self.now[0])[0]["person_id"],
            "cert_no": "CERT-ID001", "new_expires_on": "2031-06-30",
            "effective_from": "2026-07-01"})
        self.assertEqual(self.county(date="2026-07-01")["capacity"]["physicians_independent"], 1)
        self.assertEqual(self.county(date="2026-06-30")["capacity"]["physicians_independent"], 1)

    def test_leave_blocks_capacity_only_within_leave_period(self):
        person = self.register_person("H1", "ID001")
        self.submit_and_verify("H1", "AVAILABILITY_SET", {
            "person_id": person, "kind": "LEAVE",
            "effective_from": "2026-09-10", "effective_to": "2026-09-20"})
        during = self.county(date="2026-09-15")["capacity"]
        self.assertEqual(during["on_leave"], 1)
        self.assertEqual(during["physicians_independent"], 0)
        after = self.county(date="2026-09-21")["capacity"]
        self.assertEqual(after["physicians_independent"], 1)

    def test_work_window_limits_availability(self):
        person = self.register_person("H1", "ID001")
        self.submit_and_verify("H1", "AVAILABILITY_SET", {
            "person_id": person, "kind": "WORK",
            "effective_from": "2026-01-01", "effective_to": "2026-06-30"})
        self.assertEqual(self.county(date="2026-06-15")["capacity"]["physicians_independent"], 1)
        outside = self.county(date="2026-07-01")["capacity"]
        self.assertEqual(outside["physicians_independent"], 0)
        self.assertEqual(outside["out_of_window"], 1)

    def test_employment_end_removes_attribution_from_effective_date(self):
        person = self.register_person("H1", "ID001")
        self.submit_and_verify("H1", "EMPLOYMENT_END", {
            "person_id": person, "effective_from": "2026-08-01"})
        self.assertEqual(self.county(date="2026-07-31")["capacity"]["headcount"], 1)
        self.assertEqual(self.county(date="2026-08-01")["capacity"]["headcount"], 0)

    def test_late_correction_never_rewrites_published_baseline(self):
        self.now[0] = "2026-03-01T09:00:00+00:00"
        person = self.register_person("H1", "ID001")
        self.now[0] = "2026-06-01T09:00:00+00:00"
        baseline = self.post("/baselines", {"county_code": "C1", "date": "2026-05-31"})["baseline"]
        self.assertEqual(baseline["snapshot"]["capacity"]["physicians_independent"], 1)
        # 迟到更正：9 月才入账一次 4 月起生效的调动
        self.now[0] = "2026-09-01T09:00:00+00:00"
        self.submit_and_verify("H1", "TRANSFER", {
            "person_id": person, "to_institution_id": "H3", "effective_from": "2026-04-01"})
        # 已发布基线原样复现
        reread = self.get(f"/baselines/{baseline['baseline_id']}")["baseline"]
        self.assertEqual(reread["snapshot"], baseline["snapshot"])
        # 按当时知识时刻复现，与基线一致
        replay = self.county(date="2026-05-31", as_of="2026-06-01")
        self.assertEqual(replay["capacity"], baseline["snapshot"]["capacity"])
        # 按当前知识时刻，调动自 4 月生效日起影响能力
        self.assertEqual(self.county(date="2026-05-31")["capacity"]["physicians_independent"], 0)
        self.assertEqual(
            self.county(county="C2", date="2026-05-31")["capacity"]["physicians_independent"], 1)

    # ---- 核验与幂等 -----------------------------------------------------------
    def test_changes_take_effect_only_after_local_verification(self):
        payload = {
            "person": {"id_number": "ID001", "name": "张医", "staff_type": "PHYSICIAN"},
            "qualification": {"cert_no": "CERT-ID001", "qual_type": "PHYSICIAN_LICENSE",
                              "specialty": "内科", "effective_from": "2026-01-01"},
            "employment": {"institution_id": "H1", "relation_kind": "REGULAR",
                           "is_primary": True, "effective_from": "2026-01-01"},
        }
        submitted = self.submit("H1", "PERSON_REGISTER", payload)
        self.assertEqual(submitted["change"]["status"], "PENDING")
        self.assertEqual(self.county()["capacity"]["headcount"], 0)
        self.assertEqual(self.county()["freshness"]["pending_total"], 1)
        # 非本县核验员无权核验
        with self.assertRaises(StoreError) as ctx:
            self.verify(submitted["change"]["change_id"], verifier="核验员乙")
        self.assertEqual(ctx.exception.status, 403)
        # 退回后事实不入账
        rejected = self.verify(submitted["change"]["change_id"], decision="REJECT")["change"]
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertEqual(self.county()["capacity"]["headcount"], 0)
        # 已办结的变更不可重复核验
        with self.assertRaises(StoreError) as ctx:
            self.verify(submitted["change"]["change_id"])
        self.assertEqual(ctx.exception.status, 409)

    def test_duplicate_reports_form_a_single_fact(self):
        payload = {
            "person": {"id_number": "ID001", "name": "张医", "staff_type": "PHYSICIAN"},
            "qualification": {"cert_no": "CERT-ID001", "qual_type": "PHYSICIAN_LICENSE",
                              "specialty": "内科", "effective_from": "2026-01-01"},
            "employment": {"institution_id": "H1", "relation_kind": "REGULAR",
                           "is_primary": True, "effective_from": "2026-01-01"},
        }
        first = self.submit("H1", "PERSON_REGISTER", payload, key="key-1")
        self.assertFalse(first["deduplicated"])
        # 同一幂等键重发：返回原变更
        again = self.submit("H1", "PERSON_REGISTER", payload, key="key-1")
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["change"]["change_id"], first["change"]["change_id"])
        # 不同键但内容相同：标记 DUPLICATE，只形成一次有效事实
        twin = self.submit("H1", "PERSON_REGISTER", payload, key="key-2")
        self.assertTrue(twin["deduplicated"])
        self.assertEqual(twin["change"]["status"], "DUPLICATE")
        self.assertEqual(twin["change"]["duplicate_of"], first["change"]["change_id"])
        # 同一幂等键提交不同内容：拒绝
        changed = dict(payload, person=dict(payload["person"], name="改名"))
        with self.assertRaises(StoreError) as ctx:
            self.submit("H1", "PERSON_REGISTER", changed, key="key-1")
        self.assertEqual(ctx.exception.status, 409)
        # 核验后全县只计入一人
        self.verify(first["change"]["change_id"])
        self.assertEqual(self.county()["capacity"]["headcount"], 1)

    def test_duplicate_identity_rejected_at_verification(self):
        self.register_person("H1", "ID001")
        payload = {
            "person": {"id_number": "ID001", "name": "同名异人", "staff_type": "NURSE"},
            "qualification": {"cert_no": "CERT-OTHER", "qual_type": "NURSE_LICENSE",
                              "effective_from": "2026-01-01"},
        }
        submitted = self.submit("H1", "PERSON_REGISTER", payload)
        with self.assertRaises(StoreError) as ctx:
            self.verify(submitted["change"]["change_id"])
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "DUPLICATE_IDENTITY")

    # ---- 冲突与新鲜度 -----------------------------------------------------------
    def test_overlapping_primary_employment_is_flagged_but_counted_once(self):
        person = self.register_person("H1", "ID001")
        # 另一家机构也上报了主要劳动关系(口径冲突)
        self.submit_and_verify("H2", "EMPLOYMENT_START", {
            "person_id": person, "institution_id": "H2",
            "relation_kind": "REGULAR", "is_primary": True,
            "effective_from": "2026-03-01"})
        county = self.county()
        self.assertEqual(county["capacity"]["headcount"], 1)  # 同一口径只计一次
        conflicts = {c["type"] for c in county["conflicts"]}
        self.assertIn("OVERLAPPING_PRIMARY_EMPLOYMENT", conflicts)
        # 按生效日期最新者归属 H2
        summaries = {s["institution_id"]: s["capacity"] for s in county["institutions"]}
        self.assertEqual(summaries["H2"]["headcount"], 1)
        self.assertEqual(summaries["H1"]["headcount"], 0)

    def test_multi_site_without_primary_registration_is_flagged(self):
        person = self.register_person("H1", "ID001", with_registration=False)
        self.submit_and_verify("H2", "REGISTRATION_ADD", {
            "person_id": person, "institution_id": "H2",
            "reg_kind": "MULTI_SITE", "effective_from": "2026-02-01"})
        conflicts = {c["type"] for c in self.county()["conflicts"]}
        self.assertIn("MULTI_SITE_WITHOUT_PRIMARY", conflicts)

    def test_freshness_marks_stale_institutions_and_pending(self):
        self.register_person("H1", "ID001")  # H1 于 2026-09-01 有核验记录
        freshness = self.county()["freshness"]
        self.assertEqual(freshness["stale_institutions"], ["H2"])  # H2 从未上报
        self.assertEqual(freshness["pending_total"], 0)
        self.submit("H1", "AVAILABILITY_SET", {
            "person_id": self.store.persons_as_of(self.now[0])[0]["person_id"],
            "kind": "LEAVE", "effective_from": "2026-09-05", "effective_to": "2026-09-06"})
        self.assertEqual(self.county()["freshness"]["pending_total"], 1)

    # ---- 下钻与基线 -----------------------------------------------------------
    def test_institution_drilldown_matches_county_totals(self):
        self.register_person("H1", "ID001", specialty="儿科")
        self.register_person("H1", "ID002", name="王医", specialty="内科")
        self.register_person("H1", "ID003", "护一", staff_type="NURSE", specialty=None)
        self.register_person("H2", "ID004", "助理", staff_type="ASSISTANT", specialty="全科医学")
        county = self.county()
        drilldown = self.get("/institutions/H1/capacity", date="2026-09-01")
        self.assertEqual(drilldown["capacity"]["headcount"], 3)
        self.assertEqual(len(drilldown["roster"]), 3)
        self.assertEqual(drilldown["capacity"]["physicians_independent"], 2)
        self.assertEqual(drilldown["capacity"]["nurses"], 1)
        self.assertEqual(drilldown["shortage_specialties"]["儿科"], 1)
        labels = {entry["status_label"] for entry in drilldown["roster"]}
        self.assertIn("可独立接诊", labels)
        total = sum(s["capacity"]["headcount"] for s in county["institutions"])
        self.assertEqual(total, county["capacity"]["headcount"])

    def test_person_profile_lists_separate_facts(self):
        person = self.register_person("H1", "ID001")
        profile = self.get(f"/persons/{person}")
        self.assertEqual(profile["person"]["person_id"], person)
        self.assertEqual(len(profile["facts"]["qualifications"]), 1)
        self.assertEqual(len(profile["facts"]["registrations"]), 1)
        self.assertEqual(len(profile["facts"]["employments"]), 1)

    # ---- 规划情景 -----------------------------------------------------------
    def test_scenario_projection_stays_out_of_official_capacity(self):
        physician = self.register_person("H1", "ID001", specialty="内科")
        assistant = self.register_person("H2", "ID002", "助理", staff_type="ASSISTANT",
                                         specialty="全科医学")
        scenario = self.post("/scenarios", {
            "name": "2030 扩容情景", "county_code": "C1",
            "adjustments": [
                {"type": "ADD_POST", "staff_type": "NURSE", "institution_id": "H1",
                 "count": 2, "effective_on": "2027-01-01"},
                {"type": "ATTRITION", "person_id": physician, "effective_on": "2027-02-01"},
                {"type": "TRAINING_COMPLETE", "person_id": assistant,
                 "promote_to": "PHYSICIAN", "specialty": "全科医学",
                 "effective_on": "2027-03-01"},
                {"type": "ADD_POST", "staff_type": "PHYSICIAN", "institution_id": "H1",
                 "count": 1, "specialty": "精神科", "effective_on": "2028-01-01"},
            ]})["scenario"]
        projection = self.get(f"/scenarios/{scenario['scenario_id']}/projection",
                              date="2027-06-01")
        self.assertEqual(projection["kind"], "projection")
        self.assertFalse(projection["official"])
        projected = projection["projected"]["capacity"]
        self.assertEqual(projected["physicians_independent"], 1)  # 流失 1、晋升 1
        self.assertEqual(projected["assistant_total"], 0)
        self.assertEqual(projected["nurses"], 2)
        self.assertEqual(projection["projected"]["shortage_specialties"]["全科医学"], 1)
        self.assertEqual(projection["delta"]["capacity"]["nurses"], 2)
        # 生效日晚于统计日的动作不生效
        not_yet = [a for a in projection["adjustments"] if not a["applied"]]
        self.assertEqual(len(not_yet), 1)
        # 正式现状不受情景影响
        official = self.county(date="2027-06-01")
        self.assertEqual(official["kind"], "official")
        self.assertEqual(official["capacity"]["physicians_independent"], 1)
        self.assertEqual(official["capacity"]["assistant_total"], 1)
        self.assertEqual(official["capacity"]["nurses"], 0)

    def test_unknown_routes_and_bad_params(self):
        with self.assertRaises(StoreError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(StoreError) as ctx:
            self.county(date="2026/09/01")
        self.assertEqual(ctx.exception.status, 400)


class DomainUnitTest(unittest.TestCase):
    """领域纯函数的边界行为。"""

    def test_duplicate_identity_conflict_detected(self):
        persons = [
            {"person_id": "p1", "id_number": "X", "name": "甲", "staff_type": "PHYSICIAN"},
            {"person_id": "p2", "id_number": "X", "name": "乙", "staff_type": "NURSE"},
        ]
        facts = {"employments": {}, "registrations": {}}
        conflicts = domain.detect_conflicts(
            persons, facts, {"p1": "H1", "p2": "H1"}, "2026-09-01")
        self.assertEqual([c["type"] for c in conflicts], ["DUPLICATE_IDENTITY"])

    def test_attribution_falls_back_to_primary_registration(self):
        registrations = [{"institution_id": "H9", "reg_kind": domain.REG_PRIMARY,
                          "effective_from": "2026-01-01", "effective_to": None,
                          "recorded_at": "2026-01-01T00:00:00+00:00"}]
        institution_id, basis, overlap = domain.resolve_attribution(
            [], registrations, "2026-09-01")
        self.assertEqual((institution_id, basis, overlap), ("H9", "REGISTRATION", False))


class HttpSmokeTest(unittest.TestCase):
    """真实 HTTP 层面的联通性。"""

    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        Handler.configure(cls.store, load_contract())
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.store.close()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            payload = json.load(error)
            error.close()
            return error.code, payload

    def test_full_flow_over_http(self):
        status, _ = self.request("POST", "/institutions", {
            "institution_id": "HX", "name": "县医院", "county_code": "C9", "tier": "县级"})
        self.assertEqual(status, 201)
        status, _ = self.request("POST", "/counties/C9/verifiers", {"verifier": "核验员"})
        self.assertEqual(status, 201)
        status, submitted = self.request("POST", "/changes", {
            "institution_id": "HX", "change_type": "PERSON_REGISTER",
            "payload": {
                "person": {"id_number": "ID9", "name": "远程医", "staff_type": "PHYSICIAN"},
                "qualification": {"cert_no": "CERT-9", "qual_type": "PHYSICIAN_LICENSE",
                                  "specialty": "儿科", "effective_from": "2026-01-01"},
                "employment": {"institution_id": "HX", "relation_kind": "REGULAR",
                               "is_primary": True, "effective_from": "2026-01-01"}}})
        self.assertEqual(status, 201)
        change_id = submitted["change"]["change_id"]
        status, _ = self.request("POST", f"/changes/{change_id}/verify",
                                 {"verifier": "核验员", "decision": "APPROVE"})
        self.assertEqual(status, 200)
        status, capacity = self.request("GET", "/counties/C9/capacity?date=2026-09-01")
        self.assertEqual(status, 200)
        self.assertEqual(capacity["capacity"]["physicians_independent"], 1)
        self.assertEqual(capacity["shortage_specialties"]["儿科"], 1)

    def test_bad_json_and_unknown_route(self):
        request = Request(f"{self.base}/changes", data=b"not-json", method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(request, timeout=2)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()
        status, _ = self.request("GET", "/unknown")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
