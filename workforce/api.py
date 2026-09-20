"""HTTP 路由：把请求映射到领域操作，统一错误格式与参数校验。"""
import re

from . import capacity as cap
from . import scenarios as scen
from .store import StoreError

_DATE = r"\d{4}-\d{2}-\d{2}"
_MOMENT = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?([+-]\d{2}:\d{2}|Z)?"


class Api:
    """无状态路由器；状态全部在 Store 中。"""

    def __init__(self, store, contract):
        self.store = store
        self.contract = contract

    # ---- 参数解析 -----------------------------------------------------------
    def _day(self, params, name="date"):
        value = params.get(name, [None])[0] or self.store.now()[:10]
        if not re.fullmatch(_DATE, value):
            raise StoreError(400, "BAD_DATE", f"{name} 应为 YYYY-MM-DD")
        return value

    def _moment(self, params):
        raw = params.get("as_of", [None])[0]
        if raw is None:
            return self.store.now()
        if re.fullmatch(_DATE, raw):
            return f"{raw}T23:59:59+00:00"
        if re.fullmatch(_MOMENT, raw):
            return raw.replace("Z", "+00:00")
        raise StoreError(400, "BAD_AS_OF", "as_of 应为 YYYY-MM-DD 或 ISO 时刻")

    # ---- 路由 ---------------------------------------------------------------
    def dispatch(self, method, path, params, body):
        routes = [
            ("POST", r"^/institutions$", self.create_institution),
            ("GET", r"^/institutions$", self.list_institutions),
            ("POST", r"^/counties/(?P<county>[\w-]+)/verifiers$", self.add_verifier),
            ("POST", r"^/changes$", self.submit_change),
            ("GET", r"^/changes$", self.list_changes),
            ("GET", r"^/changes/(?P<change_id>[\w-]+)$", self.get_change),
            ("POST", r"^/changes/(?P<change_id>[\w-]+)/verify$", self.verify_change),
            ("GET", r"^/persons/(?P<person_id>[\w-]+)$", self.get_person),
            ("GET", r"^/counties/(?P<county>[\w-]+)/capacity$", self.county_capacity),
            ("GET", r"^/institutions/(?P<institution_id>[\w-]+)/capacity$",
             self.institution_capacity),
            ("POST", r"^/baselines$", self.publish_baseline),
            ("GET", r"^/baselines/(?P<baseline_id>[\w-]+)$", self.get_baseline),
            ("GET", r"^/counties/(?P<county>[\w-]+)/baselines$", self.list_baselines),
            ("POST", r"^/scenarios$", self.create_scenario),
            ("GET", r"^/scenarios/(?P<scenario_id>[\w-]+)$", self.get_scenario),
            ("GET", r"^/scenarios/(?P<scenario_id>[\w-]+)/projection$",
             self.scenario_projection),
            ("GET", r"^/counties/(?P<county>[\w-]+)/scenarios$", self.list_scenarios),
        ]
        for route_method, pattern, handler in routes:
            if route_method != method:
                continue
            match = re.match(pattern, path)
            if match:
                return handler(body, params, **match.groupdict())
        raise StoreError(404, "NOT_FOUND", "接口不存在")

    # ---- 基础数据 -----------------------------------------------------------
    def create_institution(self, body, _params):
        for field in ("institution_id", "name", "county_code", "tier"):
            if not body.get(field):
                raise StoreError(400, "MISSING_FIELD", f"缺少必填字段: {field}")
        institution = self.store.upsert_institution(
            body["institution_id"], body["name"], body["county_code"], body["tier"])
        return 201, {"institution": institution}

    def list_institutions(self, _body, params):
        county = params.get("county_code", [None])[0]
        return 200, {"institutions": self.store.institutions(county)}

    def add_verifier(self, body, _params, county):
        if not body.get("verifier"):
            raise StoreError(400, "MISSING_FIELD", "缺少必填字段: verifier")
        return 201, self.store.add_verifier(county, body["verifier"])

    # ---- 变更与核验 -----------------------------------------------------------
    def submit_change(self, body, _params):
        for field in ("institution_id", "change_type", "payload"):
            if field not in body:
                raise StoreError(400, "MISSING_FIELD", f"缺少必填字段: {field}")
        change, deduplicated = self.store.submit_change(
            body["institution_id"], body["change_type"], body["payload"],
            submitted_by=body.get("submitted_by"),
            idempotency_key=body.get("idempotency_key"),
        )
        return 201, {"change": change, "deduplicated": deduplicated}

    def list_changes(self, _body, params):
        return 200, {"changes": self.store.list_changes(
            county_code=params.get("county_code", [None])[0],
            status=params.get("status", [None])[0],
        )}

    def get_change(self, _body, _params, change_id):
        change = self.store.get_change(change_id)
        if not change:
            raise StoreError(404, "UNKNOWN_CHANGE", f"变更 {change_id} 不存在")
        return 200, {"change": change}

    def verify_change(self, body, _params, change_id):
        for field in ("verifier", "decision"):
            if not body.get(field):
                raise StoreError(400, "MISSING_FIELD", f"缺少必填字段: {field}")
        change = self.store.verify_change(
            change_id, body["verifier"], body["decision"], note=body.get("note"))
        return 200, {"change": change}

    # ---- 人员档案 -----------------------------------------------------------
    def get_person(self, _body, params, person_id):
        person = self.store.person(person_id)
        if not person:
            raise StoreError(404, "UNKNOWN_PERSON", f"人员 {person_id} 不存在")
        moment = self._moment(params)
        return 200, {
            "person": person,
            "as_of": moment,
            "facts": self.store.person_facts(person_id, moment),
        }

    # ---- 能力汇总 -----------------------------------------------------------
    def county_capacity(self, _body, params, county):
        day = self._day(params)
        moment = self._moment(params)
        return 200, cap.county_capacity(self.store, self.contract, county, day, moment)

    def institution_capacity(self, _body, params, institution_id):
        day = self._day(params)
        moment = self._moment(params)
        return 200, cap.institution_capacity(
            self.store, self.contract, institution_id, day, moment)

    # ---- 历史基线 -----------------------------------------------------------
    def publish_baseline(self, body, _params):
        for field in ("county_code", "date"):
            if not body.get(field):
                raise StoreError(400, "MISSING_FIELD", f"缺少必填字段: {field}")
        if not re.fullmatch(_DATE, body["date"]):
            raise StoreError(400, "BAD_DATE", "date 应为 YYYY-MM-DD")
        snapshot = cap.county_capacity(
            self.store, self.contract, body["county_code"], body["date"], self.store.now())
        baseline = self.store.publish_baseline(body["county_code"], body["date"], snapshot)
        return 201, {"baseline": baseline}

    def get_baseline(self, _body, _params, baseline_id):
        baseline = self.store.get_baseline(baseline_id)
        if not baseline:
            raise StoreError(404, "UNKNOWN_BASELINE", f"基线 {baseline_id} 不存在")
        return 200, {"baseline": baseline}

    def list_baselines(self, _body, _params, county):
        return 200, {"baselines": self.store.list_baselines(county)}

    # ---- 规划情景 -----------------------------------------------------------
    def create_scenario(self, body, _params):
        for field in ("name", "county_code", "adjustments"):
            if field not in body:
                raise StoreError(400, "MISSING_FIELD", f"缺少必填字段: {field}")
        scenario = self.store.create_scenario(
            body["name"], body["county_code"], body["adjustments"],
            created_by=body.get("created_by"))
        return 201, {"scenario": scenario}

    def get_scenario(self, _body, _params, scenario_id):
        scenario = self.store.get_scenario(scenario_id)
        if not scenario:
            raise StoreError(404, "UNKNOWN_SCENARIO", f"情景 {scenario_id} 不存在")
        return 200, {"scenario": scenario}

    def list_scenarios(self, _body, _params, county):
        return 200, {"scenarios": self.store.list_scenarios(county)}

    def scenario_projection(self, _body, params, scenario_id):
        scenario = self.store.get_scenario(scenario_id)
        if not scenario:
            raise StoreError(404, "UNKNOWN_SCENARIO", f"情景 {scenario_id} 不存在")
        day = self._day(params)
        return 200, scen.project(self.store, self.contract, scenario, day)
