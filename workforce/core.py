"""领域服务：建档、上报核验、能力汇总、基线发布与规划情景。

所有方法返回 (HTTP 状态码, 响应负载)，由接口层直接序列化。
"""
from __future__ import annotations

import uuid
from typing import Callable, Optional

from .model import (
    AVAIL_SLOT, AVAIL_TYPES, CATEGORIES, CONFLICT, DomainError, EMP_STATUSES,
    FACT_KINDS, KIND_AVAILABILITY, KIND_EMPLOYMENT, KIND_QUALIFICATION,
    KIND_REGISTRATION, REG_TYPES, RELATIONS, parse_date, parse_time,
    status_as_of, utcnow,
)
from .rules import CapacityConfig, build_person_rows, summarize
from .scenarios import project, validate_adjustment
from .store import WorkforceStore


def _required(body: dict, field: str):
    value = body.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainError(f"缺少必填字段：{field}")
    return value


class WorkforceService:
    def __init__(self, clock: Callable = utcnow, config: Optional[CapacityConfig] = None):
        self.clock = clock
        self.config = config or CapacityConfig()
        self.store = WorkforceStore(clock)

    # ---- 基础档案 ----
    def add_org(self, body: dict):
        org, created = self.store.add_org(
            _required(body, "org_id"), _required(body, "name"), _required(body, "county"))
        return (201 if created else 200), org

    def add_verifier(self, body: dict):
        verifier, created = self.store.add_verifier(
            _required(body, "verifier_id"), _required(body, "county"))
        return (201 if created else 200), verifier

    def add_person(self, body: dict):
        person, created = self.store.add_person(
            _required(body, "person_id"), _required(body, "name"))
        return (201 if created else 200), person

    def person_view(self, person_id: str):
        person = self.store.persons.get(person_id)
        if person is None:
            raise DomainError(f"人员不存在：{person_id}", 404)
        facts = sorted((f for f in self.store.facts.values() if f.person_id == person_id),
                       key=lambda f: (f.submitted_at, f.fact_id))
        return 200, {"person": person, "facts": [f.to_dict() for f in facts]}

    # ---- 变更上报与属地核验 ----
    def submit_report(self, body: dict):
        person_id = _required(body, "person_id")
        kind = _required(body, "kind")
        if kind not in FACT_KINDS:
            raise DomainError(f"kind 须为 {list(FACT_KINDS)} 之一")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise DomainError("payload 须为对象")
        valid_from = parse_date(_required(body, "valid_from"), "valid_from")
        valid_to = parse_date(body["valid_to"], "valid_to") if body.get("valid_to") else None
        if valid_to and valid_to < valid_from:
            raise DomainError("valid_to 不得早于 valid_from")
        self._validate_payload(kind, payload)
        fact, deduplicated = self.store.submit_report(
            person_id=person_id, kind=kind, payload=payload, valid_from=valid_from,
            valid_to=valid_to, source_org=_required(body, "source_org"),
            corrects=body.get("corrects"))
        return (200 if deduplicated else 201), {"fact": fact.to_dict(),
                                                "deduplicated": deduplicated}

    def list_reports(self, county=None, status=None):
        items = [f.to_dict() for f in sorted(
            self.store.facts.values(), key=lambda f: (f.submitted_at, f.fact_id))
            if (county is None or f.county == county) and (status is None or f.status == status)]
        return 200, {"reports": items}

    def verify(self, body: dict):
        fact, conflicts = self.store.verify(
            fact_id=_required(body, "fact_id"), verifier_id=_required(body, "verifier_id"),
            decision=_required(body, "decision"), resolution=body.get("resolution"))
        return 200, {"fact": fact.to_dict(),
                     "conflicts": [c.to_dict() for c in conflicts]}

    def _validate_payload(self, kind: str, payload: dict):
        if kind == KIND_QUALIFICATION:
            if payload.get("category") not in CATEGORIES:
                raise DomainError(f"资质类别须为 {list(CATEGORIES)} 之一")
            _required(payload, "cert_no")
            if payload.get("expires"):
                parse_date(payload["expires"], "expires")
        elif kind == KIND_REGISTRATION:
            self._org_or_404(_required(payload, "org"))
            if payload.get("type") not in REG_TYPES:
                raise DomainError(f"注册类型须为 {list(REG_TYPES)} 之一")
        elif kind == KIND_EMPLOYMENT:
            self._org_or_404(_required(payload, "org"))
            if payload.get("relation") not in RELATIONS:
                raise DomainError(f"劳动关系类型须为 {list(RELATIONS)} 之一")
            if payload.get("status") not in EMP_STATUSES:
                raise DomainError(f"劳动状态须为 {list(EMP_STATUSES)} 之一")
        elif kind == KIND_AVAILABILITY:
            avail_type = payload.get("type")
            if avail_type not in AVAIL_TYPES:
                raise DomainError(f"可服务时段类型须为 {list(AVAIL_TYPES)} 之一")
            if avail_type == AVAIL_SLOT:
                self._org_or_404(_required(payload, "org"))
                weekday = payload.get("weekday")
                if not isinstance(weekday, int) or isinstance(weekday, bool) \
                        or not 0 <= weekday <= 6:
                    raise DomainError("weekday 须为 0-6 的整数")
                _required(payload, "period")
            else:
                _required(payload, "reason")

    def _org_or_404(self, org_id: str):
        org = self.store.orgs.get(org_id)
        if org is None:
            raise DomainError(f"机构不存在：{org_id}", 404)
        return org

    # ---- 能力汇总 ----
    def county_capacity(self, county: str, day=None, knowledge=None, drill=False):
        day = day or self.clock().date()
        knowledge = knowledge or self.clock()
        org_county = {oid: o["county"] for oid, o in self.store.orgs.items()}
        rows = [r for r in build_person_rows(
            self.store.persons, list(self.store.facts.values()), day, knowledge)
            if org_county.get(r["org"]) == county]
        payload = {"county": county, "date": day.isoformat(),
                   "as_of": knowledge.isoformat(),
                   **summarize(rows, self.config),
                   "freshness": self._county_freshness(county, knowledge),
                   "conflicts": self._conflict_items(county, knowledge)}
        if drill:
            payload["institutions"] = [
                {"org": oid, "name": org["name"],
                 **summarize([r for r in rows if r["org"] == oid], self.config),
                 "freshness": self._org_freshness(oid, knowledge),
                 "persons": [r for r in rows if r["org"] == oid]}
                for oid, org in sorted(self.store.orgs.items()) if org["county"] == county
            ]
        return 200, payload

    def org_capacity(self, org_id: str, day=None, knowledge=None):
        org = self._org_or_404(org_id)
        day = day or self.clock().date()
        knowledge = knowledge or self.clock()
        rows = [r for r in build_person_rows(
            self.store.persons, list(self.store.facts.values()), day, knowledge)
            if r["org"] == org_id]
        return 200, {"org": org_id, "name": org["name"], "county": org["county"],
                     "date": day.isoformat(), "as_of": knowledge.isoformat(),
                     **summarize(rows, self.config),
                     "freshness": self._org_freshness(org_id, knowledge),
                     "persons": rows}

    def _org_freshness(self, org_id: str, knowledge):
        times = [f.verified_at for f in self.store.facts.values()
                 if f.source_org == org_id and f.verified_at is not None
                 and f.verified_at <= knowledge]
        if not times:
            return {"last_verified_at": None, "days_since": None, "stale": True}
        last = max(times)
        days = (knowledge - last).days
        return {"last_verified_at": last.isoformat(), "days_since": days,
                "stale": days > self.config.stale_days}

    def _county_freshness(self, county: str, knowledge):
        per_org = [self._org_freshness(oid, knowledge)
                   for oid, o in self.store.orgs.items() if o["county"] == county]
        if not per_org:
            return {"last_verified_at": None, "days_since": None, "stale": True}
        known = [f for f in per_org if f["days_since"] is not None]
        return {"last_verified_at": max((f["last_verified_at"] for f in known), default=None),
                "days_since": max((f["days_since"] for f in known), default=None),
                "stale": any(f["stale"] for f in per_org)}

    def _conflict_items(self, county: str, knowledge):
        items = []
        for fact in sorted(self.store.facts.values(), key=lambda f: f.fact_id):
            if fact.county != county or status_as_of(fact, knowledge) != CONFLICT:
                continue
            items.append({"fact_id": fact.fact_id, "person_id": fact.person_id,
                          "kind": fact.kind, "source_org": fact.source_org,
                          "payload": fact.payload,
                          "submitted_at": fact.submitted_at.isoformat()})
        return items

    def list_conflicts(self, county: str, knowledge=None):
        knowledge = knowledge or self.clock()
        return 200, {"county": county, "as_of": knowledge.isoformat(),
                     "conflicts": self._conflict_items(county, knowledge)}

    # ---- 能力基线 ----
    def publish_baseline(self, body: dict):
        county = _required(body, "county")
        day = parse_date(_required(body, "date"), "date")
        knowledge = self.clock()
        _, capacity = self.county_capacity(county, day=day, knowledge=knowledge, drill=True)
        snapshot = {"baseline_id": f"B{uuid.uuid4().hex[:12]}", "county": county,
                    "valid_date": day.isoformat(), "published_at": knowledge.isoformat(),
                    "capacity": capacity}
        self.store.baselines[snapshot["baseline_id"]] = snapshot
        return 201, snapshot

    def get_baseline(self, baseline_id: str):
        snapshot = self.store.baselines.get(baseline_id)
        if snapshot is None:
            raise DomainError(f"基线不存在：{baseline_id}", 404)
        return 200, snapshot

    def baseline_as_of(self, county: str, knowledge):
        published = [b for b in self.store.baselines.values()
                     if b["county"] == county
                     and parse_time(b["published_at"], "published_at") <= knowledge]
        if not published:
            raise DomainError("该日期之前本县无已发布基线", 404)
        return 200, max(published, key=lambda b: b["published_at"])

    # ---- 规划情景 ----
    def create_scenario(self, body: dict):
        name = _required(body, "name")
        county = _required(body, "county")
        adjustments = body.get("adjustments")
        if not isinstance(adjustments, list) or not adjustments:
            raise DomainError("adjustments 须为非空数组")
        validated = [validate_adjustment(a) for a in adjustments]
        scenario = {"scenario_id": f"S{uuid.uuid4().hex[:12]}", "name": name,
                    "county": county, "adjustments": validated,
                    "created_at": self.clock().isoformat()}
        self.store.scenarios[scenario["scenario_id"]] = scenario
        return 201, scenario

    def project_scenario(self, scenario_id: str, day=None):
        scenario = self.store.scenarios.get(scenario_id)
        if scenario is None:
            raise DomainError(f"情景不存在：{scenario_id}", 404)
        day = day or self.clock().date()
        _, base = self.county_capacity(scenario["county"], day=day, knowledge=self.clock())
        return 200, project(base, scenario["adjustments"], self.config,
                            scenario_id=scenario_id, scenario_name=scenario["name"])
