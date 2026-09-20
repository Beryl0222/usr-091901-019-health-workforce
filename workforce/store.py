"""台账存储：基础档案、双时间事实版本、基线快照与规划情景。

事实记录不可变，状态迁移以 replace 生成新记录；更正通过「原版本退出 +
按生效日切出闭合历史版本 + 新版本生效」完成，任何事务时间截点下同一
事实链在生效时间轴上互不重叠，保证重复上报与迟到更正只形成一次有效事实。
"""
from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from typing import Callable, Optional

from .model import (
    ACTIVE, CONFLICT, DECISION_APPROVE, DECISION_REJECT, DomainError, Fact,
    PENDING, RETIRED, RESOLUTION_SUPERSEDE, facts_conflict, new_fact_id,
    submission_key, utcnow,
)


class WorkforceStore:
    """内存台账：人员、机构、核验员、事实版本、基线快照与规划情景。"""

    def __init__(self, clock: Callable = utcnow):
        self._clock = clock
        self._lock = threading.RLock()
        self.persons: dict[str, dict] = {}
        self.orgs: dict[str, dict] = {}
        self.verifiers: dict[str, dict] = {}
        self.facts: dict[str, Fact] = {}
        self._keys: dict[str, str] = {}
        self.baselines: dict[str, dict] = {}
        self.scenarios: dict[str, dict] = {}

    # ---- 基础档案 ----
    def add_org(self, org_id: str, name: str, county: str):
        with self._lock:
            existing = self.orgs.get(org_id)
            if existing:
                if existing["name"] == name and existing["county"] == county:
                    return existing, False
                raise DomainError(f"机构编号已存在且信息不一致：{org_id}", 409)
            org = {"org_id": org_id, "name": name, "county": county}
            self.orgs[org_id] = org
            return org, True

    def add_verifier(self, verifier_id: str, county: str):
        with self._lock:
            existing = self.verifiers.get(verifier_id)
            if existing:
                if existing["county"] == county:
                    return existing, False
                raise DomainError(f"核验员编号已存在且属地不一致：{verifier_id}", 409)
            verifier = {"verifier_id": verifier_id, "county": county}
            self.verifiers[verifier_id] = verifier
            return verifier, True

    def add_person(self, person_id: str, name: str):
        with self._lock:
            existing = self.persons.get(person_id)
            if existing:
                if existing["name"] == name:
                    return existing, False
                raise DomainError(f"人员编号已存在且姓名不一致：{person_id}", 409)
            person = {"person_id": person_id, "name": name}
            self.persons[person_id] = person
            return person, True

    # ---- 变更上报（幂等） ----
    def submit_report(self, *, person_id, kind, payload, valid_from, valid_to,
                      source_org, corrects=None):
        with self._lock:
            if person_id not in self.persons:
                raise DomainError(f"人员不存在：{person_id}", 404)
            if source_org not in self.orgs:
                raise DomainError(f"上报机构不存在：{source_org}", 404)
            if corrects is not None and corrects not in self.facts:
                raise DomainError(f"更正对象不存在：{corrects}", 404)
            key = submission_key(person_id, kind, valid_from, payload, corrects)
            existing_id = self._keys.get(key)
            if existing_id:
                existing = self.facts[existing_id]
                if existing.status != RETIRED:
                    return existing, True  # 重复上报：只形成一次有效事实
            fact = Fact(
                fact_id=new_fact_id(), person_id=person_id, kind=kind, payload=payload,
                valid_from=valid_from, valid_to=valid_to, status=PENDING,
                source_org=source_org, county=self.orgs[source_org]["county"],
                submitted_at=self._clock(), verified_at=None, verified_by=None,
                activated_at=None, key=key, supersedes=corrects, superseded_at=None,
            )
            self.facts[fact.fact_id] = fact
            self._keys[key] = fact.fact_id
            return fact, False

    # ---- 属地核验 ----
    def verify(self, *, fact_id, verifier_id, decision, resolution=None):
        with self._lock:
            fact = self._fact_or_404(fact_id)
            verifier = self.verifiers.get(verifier_id)
            if verifier is None:
                raise DomainError(f"核验员不存在：{verifier_id}", 404)
            if verifier["county"] != fact.county:
                raise DomainError("核验员属地与上报机构属地不一致，须由属地核验", 403)
            if fact.status not in (PENDING, CONFLICT):
                raise DomainError(f"事实 {fact_id} 当前状态为「{fact.status}」，不可核验", 409)
            now = self._clock()
            if decision == DECISION_REJECT:
                return self._update(replace(
                    fact, status=RETIRED, verified_at=fact.verified_at or now,
                    verified_by=verifier_id, superseded_at=now)), []
            if decision != DECISION_APPROVE:
                raise DomainError(f"decision 须为 {DECISION_APPROVE}/{DECISION_REJECT}")
            conflicts: list[Fact] = []
            if fact.supersedes:
                target = self._fact_or_404(fact.supersedes)
                if target.person_id != fact.person_id or target.kind != fact.kind:
                    raise DomainError("更正对象与上报事实须属于同一人员同一事类", 409)
                self._close_version(target, fact.valid_from, now, verifier_id)
            else:
                conflicts = self._find_conflicts(fact)
                if conflicts and resolution != RESOLUTION_SUPERSEDE:
                    return self._update(replace(
                        fact, status=CONFLICT, verified_at=fact.verified_at or now,
                        verified_by=verifier_id)), conflicts
                for other in conflicts:
                    self._close_version(other, fact.valid_from, now, verifier_id)
            return self._update(replace(
                fact, status=ACTIVE, verified_at=fact.verified_at or now,
                verified_by=fact.verified_by or verifier_id,
                activated_at=fact.activated_at or now)), conflicts

    def _close_version(self, target: Fact, cutoff, now, verifier_id):
        """更正：原版本在事务时间退出，并按更正生效日切出闭合的历史版本。"""
        if target.status != ACTIVE or target.superseded_at is not None:
            raise DomainError(f"事实 {target.fact_id} 不是当前有效版本，无法更正", 409)
        self._update(replace(target, superseded_at=now))
        last_day = cutoff - timedelta(days=1)
        if target.valid_to is not None and target.valid_to < last_day:
            last_day = target.valid_to
        if last_day < target.valid_from:
            return None  # 原事实被完全取代，无历史区间可保留
        closure = Fact(
            fact_id=new_fact_id(), person_id=target.person_id, kind=target.kind,
            payload=dict(target.payload), valid_from=target.valid_from, valid_to=last_day,
            status=ACTIVE, source_org=target.source_org, county=target.county,
            submitted_at=target.submitted_at, verified_at=now, verified_by=verifier_id,
            activated_at=now, key=f"{target.key}#closure:{now.isoformat()}",
            supersedes=target.fact_id, superseded_at=None,
        )
        self.facts[closure.fact_id] = closure
        return closure

    def _find_conflicts(self, fact: Fact) -> list[Fact]:
        return [f for f in self.facts.values()
                if f.fact_id != fact.fact_id and f.status == ACTIVE
                and f.superseded_at is None and facts_conflict(f, fact)]

    def _fact_or_404(self, fact_id: str) -> Fact:
        fact = self.facts.get(fact_id)
        if fact is None:
            raise DomainError(f"事实不存在：{fact_id}", 404)
        return fact

    def _update(self, fact: Fact) -> Fact:
        self.facts[fact.fact_id] = fact
        return fact
