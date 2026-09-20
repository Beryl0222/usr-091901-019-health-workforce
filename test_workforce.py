"""区域医护能力台账的领域规则与接口测试。"""
import json
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from workforce.api import make_handler
from workforce.core import WorkforceService
from workforce.model import ACTIVE, CONFLICT, PENDING, RETIRED, DomainError

UTC = timezone.utc
T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)  # 周二


class FakeClock:
    def __init__(self, start=T0):
        self.moment = start

    def __call__(self):
        return self.moment

    def set(self, moment):
        self.moment = moment


def make_service(clock):
    svc = WorkforceService(clock=clock)
    svc.add_org({"org_id": "ORG-A", "name": "县人民医院", "county": "青山县"})
    svc.add_org({"org_id": "ORG-B", "name": "县中医院", "county": "青山县"})
    svc.add_org({"org_id": "ORG-C", "name": "市立医院", "county": "邻县"})
    svc.add_verifier({"verifier_id": "V-QS", "county": "青山县"})
    svc.add_verifier({"verifier_id": "V-LX", "county": "邻县"})
    return svc


def report(svc, body):
    _, payload = svc.submit_report(body)
    return payload


def verify(svc, fact_id, verifier="V-QS", decision="通过", resolution=None):
    body = {"fact_id": fact_id, "verifier_id": verifier, "decision": decision}
    if resolution:
        body["resolution"] = resolution
    _, payload = svc.verify(body)
    return payload


def report_and_verify(svc, body, verifier="V-QS"):
    created = report(svc, body)
    return verify(svc, created["fact"]["fact_id"], verifier)["fact"]


def hire(svc, pid, org, verifier="V-QS", *, category="执业医师", specialty="全科医学",
         start="2026-01-01", expires=None, extra_regs=(), slot_orgs=None, relation="在编"):
    """建档并核验一名人员所需的完整事实链（资质/劳动关系/注册/可服务时段）。"""
    svc.add_person({"person_id": pid, "name": f"员工{pid}"})
    qual = {"category": category, "cert_no": f"CERT-{pid}"}
    if expires:
        qual["expires"] = expires
    report_and_verify(svc, {"person_id": pid, "kind": "qualification", "payload": qual,
                            "valid_from": start, "source_org": org}, verifier)
    report_and_verify(svc, {"person_id": pid, "kind": "employment",
                            "payload": {"org": org, "relation": relation, "status": "在职"},
                            "valid_from": start, "source_org": org}, verifier)
    report_and_verify(svc, {"person_id": pid, "kind": "registration",
                            "payload": {"org": org, "type": "主执业点", "specialty": specialty},
                            "valid_from": start, "source_org": org}, verifier)
    for reg_org, reg_type in extra_regs:
        report_and_verify(svc, {"person_id": pid, "kind": "registration",
                                "payload": {"org": reg_org, "type": reg_type,
                                            "specialty": specialty},
                                "valid_from": start, "source_org": org}, verifier)
    for slot_org in (slot_orgs if slot_orgs is not None else [org]):
        for weekday in range(5):
            report_and_verify(svc, {"person_id": pid, "kind": "availability",
                                    "payload": {"type": "slot", "org": slot_org,
                                                "weekday": weekday, "period": "全天"},
                                    "valid_from": start, "source_org": org}, verifier)


def capacity(svc, day, county="青山县", as_of=None, drill=False):
    _, payload = svc.county_capacity(county, day=day, knowledge=as_of, drill=drill)
    return payload


def staffed_orgs(payload):
    return {i["org"] for i in payload["institutions"] if i["headcount"]["执业医师"]}


class DomainTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.svc = make_service(self.clock)

    def test_unverified_report_never_counts_and_needs_local_verifier(self):
        self.svc.add_person({"person_id": "P1", "name": "医生甲"})
        created = report(self.svc, {"person_id": "P1", "kind": "employment",
                                    "payload": {"org": "ORG-A", "relation": "在编",
                                                "status": "在职"},
                                    "valid_from": "2026-01-01", "source_org": "ORG-A"})
        self.assertEqual(created["fact"]["status"], PENDING)
        self.assertEqual(capacity(self.svc, date(2026, 9, 1))["headcount"]["执业医师"], 0)
        with self.assertRaises(DomainError) as ctx:
            self.svc.verify({"fact_id": created["fact"]["fact_id"],
                             "verifier_id": "V-LX", "decision": "通过"})
        self.assertEqual(ctx.exception.status, 403)
        done = verify(self.svc, created["fact"]["fact_id"])
        self.assertEqual(done["fact"]["status"], ACTIVE)

    def test_duplicate_report_forms_single_fact(self):
        self.svc.add_person({"person_id": "P1", "name": "医生甲"})
        body = {"person_id": "P1", "kind": "qualification",
                "payload": {"category": "执业医师", "cert_no": "C-1"},
                "valid_from": "2026-01-01", "source_org": "ORG-A"}
        first = report(self.svc, body)
        second = report(self.svc, body)
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["fact"]["fact_id"], second["fact"]["fact_id"])
        self.assertEqual(len(self.svc.store.facts), 1)
        verify(self.svc, first["fact"]["fact_id"])
        with self.assertRaises(DomainError) as ctx:  # 重复核验只形成一次有效事实
            verify(self.svc, first["fact"]["fact_id"])
        self.assertEqual(ctx.exception.status, 409)

    def test_multi_site_registration_not_double_counted(self):
        hire(self.svc, "P1", "ORG-A", extra_regs=[("ORG-C", "多点执业")],
             slot_orgs=["ORG-A", "ORG-C"])
        home = capacity(self.svc, date(2026, 9, 1), "青山县")
        away = capacity(self.svc, date(2026, 9, 1), "邻县")
        self.assertEqual(home["headcount"]["执业医师"], 1)
        self.assertEqual(away["headcount"]["执业医师"], 0)
        self.assertEqual(home["headcount"]["执业医师"] + away["headcount"]["执业医师"], 1)

    def test_secondment_attributes_to_host_until_it_ends(self):
        hire(self.svc, "P1", "ORG-A", extra_regs=[("ORG-C", "多点执业")],
             slot_orgs=["ORG-A", "ORG-C"])
        report_and_verify(self.svc, {"person_id": "P1", "kind": "employment",
                                     "payload": {"org": "ORG-C", "relation": "借调",
                                                 "status": "在职"},
                                     "valid_from": "2026-03-01", "valid_to": "2026-12-31",
                                     "source_org": "ORG-C"}, verifier="V-LX")
        self.assertEqual(capacity(self.svc, date(2026, 9, 1), "邻县")["headcount"]["执业医师"], 1)
        self.assertEqual(capacity(self.svc, date(2026, 9, 1), "青山县")["headcount"]["执业医师"], 0)
        self.assertEqual(capacity(self.svc, date(2027, 1, 15), "青山县")["headcount"]["执业医师"], 1)

    def test_dual_staff_employment_conflict_and_resolution(self):
        hire(self.svc, "P1", "ORG-A", extra_regs=[("ORG-B", "多点执业")],
             slot_orgs=["ORG-A", "ORG-B"])
        created = report(self.svc, {"person_id": "P1", "kind": "employment",
                                    "payload": {"org": "ORG-B", "relation": "在编",
                                                "status": "在职"},
                                    "valid_from": "2026-06-01", "source_org": "ORG-B"})
        result = verify(self.svc, created["fact"]["fact_id"])
        self.assertEqual(result["fact"]["status"], CONFLICT)
        self.assertEqual(len(result["conflicts"]), 1)
        drill = capacity(self.svc, date(2026, 6, 15), drill=True)
        self.assertEqual(staffed_orgs(drill), {"ORG-A"})
        self.assertEqual(len(drill["conflicts"]), 1)
        resolved = verify(self.svc, created["fact"]["fact_id"], resolution="更正既有")
        self.assertEqual(resolved["fact"]["status"], ACTIVE)
        self.assertEqual(staffed_orgs(capacity(self.svc, date(2026, 6, 15), drill=True)),
                         {"ORG-B"})
        self.assertEqual(staffed_orgs(capacity(self.svc, date(2026, 5, 15), drill=True)),
                         {"ORG-A"})
        self.assertEqual(capacity(self.svc, date(2026, 6, 15), drill=True)["conflicts"], [])

    def test_cert_expiry_bounds_counting_from_effective_date(self):
        hire(self.svc, "P1", "ORG-A", expires="2026-09-30")
        self.assertEqual(capacity(self.svc, date(2026, 9, 30))["headcount"]["执业医师"], 1)
        self.assertEqual(capacity(self.svc, date(2026, 10, 1))["headcount"]["执业医师"], 0)

    def test_leave_cancels_that_weeks_availability_without_touching_headcount(self):
        hire(self.svc, "P1", "ORG-A")
        report_and_verify(self.svc, {"person_id": "P1", "kind": "availability",
                                     "payload": {"type": "leave", "reason": "休假"},
                                     "valid_from": "2026-09-21", "valid_to": "2026-09-25",
                                     "source_org": "ORG-A"})
        on_leave = capacity(self.svc, date(2026, 9, 22))
        self.assertEqual(on_leave["headcount"]["执业医师"], 1)
        self.assertEqual(on_leave["available_week"]["执业医师"], 0)
        self.assertEqual(capacity(self.svc, date(2026, 9, 28))["available_week"]["执业医师"], 1)

    def test_training_status_stops_counting_from_effective_date(self):
        hire(self.svc, "P1", "ORG-A")
        self.clock.set(datetime(2026, 9, 5, 9, tzinfo=UTC))
        emp_id = [f for f in self.svc.store.facts.values()
                  if f.kind == "employment"][0].fact_id
        correction = report(self.svc, {"person_id": "P1", "kind": "employment",
                                       "payload": {"org": "ORG-A", "relation": "在编",
                                                   "status": "进修"},
                                       "valid_from": "2026-10-01", "source_org": "ORG-A",
                                       "corrects": emp_id})
        verify(self.svc, correction["fact"]["fact_id"])
        self.assertEqual(capacity(self.svc, date(2026, 9, 15))["headcount"]["执业医师"], 1)
        self.assertEqual(capacity(self.svc, date(2026, 10, 15))["headcount"]["执业医师"], 0)
        earlier = capacity(self.svc, date(2026, 10, 15), as_of=T0 + timedelta(hours=1))
        self.assertEqual(earlier["headcount"]["执业医师"], 1)

    def test_late_correction_preserves_published_baseline(self):
        hire(self.svc, "P1", "ORG-A", extra_regs=[("ORG-B", "多点执业")],
             slot_orgs=["ORG-A", "ORG-B"])
        published_at = self.clock()
        _, baseline = self.svc.publish_baseline({"county": "青山县", "date": "2026-06-01"})
        self.assertEqual(baseline["capacity"]["headcount"]["执业医师"], 1)
        self.assertEqual(staffed_orgs(baseline["capacity"]), {"ORG-A"})
        # 迟到更正：调动自 2026-05-01 生效，09-10 才核验入账
        self.clock.set(datetime(2026, 9, 10, 9, tzinfo=UTC))
        emp_id = [f for f in self.svc.store.facts.values()
                  if f.kind == "employment"][0].fact_id
        correction = report(self.svc, {"person_id": "P1", "kind": "employment",
                                       "payload": {"org": "ORG-B", "relation": "在编",
                                                   "status": "在职"},
                                       "valid_from": "2026-05-01", "source_org": "ORG-B",
                                       "corrects": emp_id})
        verify(self.svc, correction["fact"]["fact_id"])
        self.assertEqual(staffed_orgs(capacity(self.svc, date(2026, 6, 1), drill=True)),
                         {"ORG-B"})
        # 当时发布的基线不受倒改：按发布时刻重放结果一致
        replay = capacity(self.svc, date(2026, 6, 1), as_of=published_at, drill=True)
        self.assertEqual(replay, baseline["capacity"])
        _, fetched = self.svc.get_baseline(baseline["baseline_id"])
        self.assertEqual(fetched, baseline)
        _, as_of_view = self.svc.baseline_as_of("青山县", datetime(2026, 9, 5, tzinfo=UTC))
        self.assertEqual(as_of_view["baseline_id"], baseline["baseline_id"])
        # 更正前的历史区间在新口径下仍然完整
        self.assertEqual(staffed_orgs(capacity(self.svc, date(2026, 3, 1), drill=True)),
                         {"ORG-A"})

    def test_scenario_projection_never_touches_official_facts(self):
        hire(self.svc, "P1", "ORG-A")
        hire(self.svc, "P2", "ORG-A")
        hire(self.svc, "P3", "ORG-A", category="执业助理医师")
        hire(self.svc, "P4", "ORG-A", category="注册护士")
        hire(self.svc, "P5", "ORG-A", category="注册护士")
        _, baseline = self.svc.publish_baseline({"county": "青山县", "date": "2026-09-01"})
        facts_before = len(self.svc.store.facts)
        official_before = capacity(self.svc, date(2026, 9, 1))
        _, scenario = self.svc.create_scenario({
            "name": "2030扩容", "county": "青山县",
            "adjustments": [
                {"type": "新增岗位", "category": "执业医师", "count": 2, "specialty": "儿科"},
                {"type": "培训完成", "from_category": "执业助理医师",
                 "to_category": "执业医师", "count": 1},
                {"type": "人员流失", "category": "注册护士", "count": 1}]})
        _, projection = self.svc.project_scenario(scenario["scenario_id"],
                                                  day=date(2026, 9, 1))
        self.assertTrue(projection["is_projection"])
        self.assertEqual(projection["headcount"],
                         {"执业医师": 5, "执业助理医师": 0, "注册护士": 1})
        self.assertEqual(projection["available_week"]["执业医师"], 5)
        self.assertEqual(projection["specialty_availability"]["儿科"], 2)
        self.assertEqual(capacity(self.svc, date(2026, 9, 1)), official_before)
        self.assertEqual(len(self.svc.store.facts), facts_before)
        _, fetched = self.svc.get_baseline(baseline["baseline_id"])
        self.assertEqual(fetched, baseline)

    def test_county_drilldown_shows_freshness_conflicts_and_shortage(self):
        hire(self.svc, "P1", "ORG-A", specialty="全科医学")
        hire(self.svc, "P2", "ORG-B", specialty="儿科")
        created = report(self.svc, {"person_id": "P2", "kind": "employment",
                                    "payload": {"org": "ORG-A", "relation": "在编",
                                                "status": "在职"},
                                    "valid_from": "2026-06-01", "source_org": "ORG-A"})
        verify(self.svc, created["fact"]["fact_id"])  # 双在编 → 存在冲突
        self.clock.set(datetime(2026, 12, 20, 9, tzinfo=UTC))  # 距上次核验超过 90 天
        drill = capacity(self.svc, date(2026, 12, 21), drill=True)
        self.assertTrue(drill["freshness"]["stale"])
        institutions = {i["org"]: i for i in drill["institutions"]}
        self.assertEqual(institutions["ORG-A"]["headcount"]["执业医师"], 1)
        self.assertEqual(institutions["ORG-B"]["headcount"]["执业医师"], 1)
        self.assertEqual(institutions["ORG-B"]["persons"][0]["specialty"], "儿科")
        self.assertEqual(len(drill["conflicts"]), 1)
        self.assertEqual(drill["conflicts"][0]["person_id"], "P2")
        shortage = {s["specialty"] for s in drill["shortage_specialties"]}
        self.assertIn("全科医学", shortage)  # 阈值 2，仅 1 人
        self.assertIn("精神科", shortage)
        self.assertNotIn("儿科", shortage)  # 已有 1 人达到阈值

    def test_rejection_allows_fresh_resubmission(self):
        self.svc.add_person({"person_id": "P1", "name": "医生甲"})
        body = {"person_id": "P1", "kind": "qualification",
                "payload": {"category": "执业医师", "cert_no": "C-1"},
                "valid_from": "2026-01-01", "source_org": "ORG-A"}
        created = report(self.svc, body)
        rejected = verify(self.svc, created["fact"]["fact_id"], decision="驳回")
        self.assertEqual(rejected["fact"]["status"], RETIRED)
        again = report(self.svc, body)
        self.assertFalse(again["deduplicated"])
        self.assertNotEqual(again["fact"]["fact_id"], created["fact"]["fact_id"])

    def test_registration_at_attributed_org_required_for_counting(self):
        self.svc.add_person({"person_id": "P1", "name": "医生甲"})
        report_and_verify(self.svc, {"person_id": "P1", "kind": "qualification",
                                     "payload": {"category": "执业医师", "cert_no": "C-1"},
                                     "valid_from": "2026-01-01", "source_org": "ORG-A"})
        report_and_verify(self.svc, {"person_id": "P1", "kind": "employment",
                                     "payload": {"org": "ORG-A", "relation": "在编",
                                                 "status": "在职"},
                                     "valid_from": "2026-01-01", "source_org": "ORG-A"})
        self.assertEqual(capacity(self.svc, date(2026, 9, 1))["headcount"]["执业医师"], 0)
        report_and_verify(self.svc, {"person_id": "P1", "kind": "registration",
                                     "payload": {"org": "ORG-A", "type": "主执业点",
                                                 "specialty": "全科医学"},
                                     "valid_from": "2026-01-01", "source_org": "ORG-A"})
        self.assertEqual(capacity(self.svc, date(2026, 9, 1))["headcount"]["执业医师"], 1)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.clock = FakeClock()
        cls.svc = make_service(cls.clock)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def post(self, path, body):
        request = Request(f"{self.base}{path}", data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=2) as response:
            return response.status, json.load(response)

    def get(self, path, params=None):
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        with urlopen(url, timeout=2) as response:
            return response.status, json.load(response)

    def post_error(self, path, body):
        with self.assertRaises(HTTPError) as ctx:
            self.post(path, body)
        return ctx.exception

    def hire_over_http(self, pid, org="ORG-A"):
        status, _ = self.post("/persons", {"person_id": pid, "name": f"员工{pid}"})
        self.assertEqual(status, 201)
        bodies = [
            {"person_id": pid, "kind": "qualification",
             "payload": {"category": "执业医师", "cert_no": f"CERT-{pid}"},
             "valid_from": "2026-01-01", "source_org": org},
            {"person_id": pid, "kind": "employment",
             "payload": {"org": org, "relation": "在编", "status": "在职"},
             "valid_from": "2026-01-01", "source_org": org},
            {"person_id": pid, "kind": "registration",
             "payload": {"org": org, "type": "主执业点", "specialty": "全科医学"},
             "valid_from": "2026-01-01", "source_org": org},
        ] + [
            {"person_id": pid, "kind": "availability",
             "payload": {"type": "slot", "org": org, "weekday": weekday,
                         "period": "全天"},
             "valid_from": "2026-01-01", "source_org": org}
            for weekday in range(5)
        ]
        for body in bodies:
            status, created = self.post("/reports", body)
            self.assertEqual(status, 201)
            status, done = self.post("/verifications", {
                "fact_id": created["fact"]["fact_id"], "verifier_id": "V-QS",
                "decision": "通过"})
            self.assertEqual(done["fact"]["status"], "当前有效")

    def test_end_to_end_planning_flow(self):
        self.hire_over_http("P1")
        self.hire_over_http("P2", org="ORG-B")  # 县内两家机构均有上报，县级新鲜度才不滞后
        county = quote("青山县")
        status, cap = self.get(f"/capacity/county/{county}",
                               {"date": "2026-09-01", "drill": "1"})
        self.assertEqual(cap["headcount"]["执业医师"], 2)
        self.assertEqual(cap["available_week"]["执业医师"], 2)
        self.assertIn("institutions", cap)
        self.assertFalse(cap["freshness"]["stale"])
        status, baseline = self.post("/baselines", {"county": "青山县", "date": "2026-09-01"})
        self.assertEqual(status, 201)
        # 情景演算与正式现状严格分离
        status, scenario = self.post("/scenarios", {
            "name": "扩容", "county": "青山县",
            "adjustments": [{"type": "新增岗位", "category": "执业医师", "count": 3}]})
        self.assertEqual(status, 201)
        status, projection = self.get(
            f"/scenarios/{scenario['scenario_id']}/projection", {"date": "2026-09-01"})
        self.assertTrue(projection["is_projection"])
        self.assertEqual(projection["headcount"]["执业医师"], 5)
        status, cap_after = self.get(f"/capacity/county/{county}", {"date": "2026-09-01"})
        self.assertEqual(cap_after["headcount"]["执业医师"], 2)
        # 按历史日期复现当时发布的能力基线
        status, replay = self.get("/baselines", {"county": "青山县",
                                                 "as_of": baseline["published_at"]})
        self.assertEqual(replay["baseline_id"], baseline["baseline_id"])
        status, fetched = self.get(f"/baselines/{baseline['baseline_id']}")
        self.assertEqual(fetched["capacity"], baseline["capacity"])
        status, conflicts = self.get("/conflicts", {"county": "青山县"})
        self.assertEqual(conflicts["conflicts"], [])
        status, person = self.get("/persons/P1")
        self.assertEqual(len(person["facts"]), 8)

    def test_api_error_mapping(self):
        error = self.post_error("/reports", {"person_id": "NOPE", "kind": "employment",
                                             "payload": {"org": "ORG-A", "relation": "在编",
                                                         "status": "在职"},
                                             "valid_from": "2026-01-01",
                                             "source_org": "ORG-A"})
        self.assertEqual(error.code, 404)
        error.close()
        self.post("/persons", {"person_id": "P9", "name": "医生丙"})
        _, created = self.post("/reports", {"person_id": "P9", "kind": "employment",
                                            "payload": {"org": "ORG-A", "relation": "在编",
                                                        "status": "在职"},
                                            "valid_from": "2026-01-01",
                                            "source_org": "ORG-A"})
        error = self.post_error("/verifications", {
            "fact_id": created["fact"]["fact_id"], "verifier_id": "V-LX",
            "decision": "通过"})
        self.assertEqual(error.code, 403)
        error.close()
        error = self.post_error("/reports", {"person_id": "P9", "kind": "unknown",
                                             "payload": {}, "valid_from": "2026-01-01",
                                             "source_org": "ORG-A"})
        self.assertEqual(error.code, 400)
        error.close()
        with self.assertRaises(HTTPError) as ctx:
            self.get("/no-such-route")
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
