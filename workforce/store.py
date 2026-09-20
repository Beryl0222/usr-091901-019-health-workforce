"""SQLite 事实库：追加式双时态存储、变更提交与属地核验。

每条事实同时携带两个时间轴：
- 业务时间(effective_from/effective_to)：事实在现实世界的生效区间；
- 知识时间(recorded_at/expired_at)：事实何时被系统确认、何时被更正取代。

事实只新增或作废，绝不原地改写，因此任意历史知识时刻都能复现当时口径。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import date as date_type
from datetime import datetime, timedelta, timezone

from . import domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
  institution_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  county_code TEXT NOT NULL,
  tier TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS county_verifiers (
  county_code TEXT NOT NULL,
  verifier TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY (county_code, verifier)
);
CREATE TABLE IF NOT EXISTS persons (
  person_id TEXT PRIMARY KEY,
  id_number TEXT NOT NULL,
  name TEXT NOT NULL,
  staff_type TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qualifications (
  qual_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  cert_no TEXT NOT NULL,
  qual_type TEXT NOT NULL,
  specialty TEXT,
  issued_on TEXT,
  expires_on TEXT,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  recorded_at TEXT NOT NULL,
  expired_at TEXT
);
CREATE TABLE IF NOT EXISTS registrations (
  reg_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  institution_id TEXT NOT NULL,
  reg_kind TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  recorded_at TEXT NOT NULL,
  expired_at TEXT
);
CREATE TABLE IF NOT EXISTS employments (
  emp_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  institution_id TEXT NOT NULL,
  relation_kind TEXT NOT NULL,
  is_primary INTEGER NOT NULL DEFAULT 0,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  recorded_at TEXT NOT NULL,
  expired_at TEXT
);
CREATE TABLE IF NOT EXISTS availability (
  avail_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT,
  recorded_at TEXT NOT NULL,
  expired_at TEXT
);
CREATE TABLE IF NOT EXISTS changes (
  change_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  institution_id TEXT NOT NULL,
  county_code TEXT NOT NULL,
  change_type TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL,
  duplicate_of TEXT,
  submitted_by TEXT,
  submitted_at TEXT NOT NULL,
  verified_by TEXT,
  verified_at TEXT,
  verify_note TEXT,
  result TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_county ON changes (county_code, status);
CREATE INDEX IF NOT EXISTS idx_changes_fingerprint ON changes (fingerprint, status);
CREATE TABLE IF NOT EXISTS scenarios (
  scenario_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  county_code TEXT NOT NULL,
  adjustments TEXT NOT NULL,
  created_by TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS baselines (
  baseline_id TEXT PRIMARY KEY,
  county_code TEXT NOT NULL,
  baseline_date TEXT NOT NULL,
  published_at TEXT NOT NULL,
  payload TEXT NOT NULL
);
"""

FACT_TABLES = ("qualifications", "registrations", "employments", "availability")

CHANGE_TYPES = (
    "PERSON_REGISTER",
    "QUALIFICATION_ADD",
    "QUALIFICATION_RENEW",
    "REGISTRATION_ADD",
    "EMPLOYMENT_START",
    "EMPLOYMENT_END",
    "TRANSFER",
    "AVAILABILITY_SET",
)

SCENARIO_ADJUSTMENTS = ("ADD_POST", "TRAINING_COMPLETE", "ATTRITION")


class StoreError(Exception):
    """存储层业务错误，携带 HTTP 状态码与错误码。"""

    def __init__(self, status, code, detail):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _require(payload, *keys):
    missing = [key for key in keys if payload.get(key) in (None, "")]
    if missing:
        raise StoreError(400, "MISSING_FIELD", f"缺少必填字段: {', '.join(missing)}")


class Store:
    """追加式事实库。clock 可注入以便测试，返回带时区的 ISO 时刻。"""

    def __init__(self, path=":memory:", clock=None):
        self._clock = clock or _utcnow
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def now(self):
        return self._clock()

    def close(self):
        self._conn.close()

    # ---- 基础数据 ---------------------------------------------------------
    def upsert_institution(self, institution_id, name, county_code, tier):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO institutions (institution_id,name,county_code,tier,recorded_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(institution_id) DO UPDATE SET name=excluded.name,"
                " county_code=excluded.county_code, tier=excluded.tier",
                (institution_id, name, county_code, tier, self.now()),
            )
        return self.institution(institution_id)

    def institution(self, institution_id):
        row = self._conn.execute(
            "SELECT * FROM institutions WHERE institution_id=?", (institution_id,)
        ).fetchone()
        return dict(row) if row else None

    def institutions(self, county_code=None):
        if county_code:
            rows = self._conn.execute(
                "SELECT * FROM institutions WHERE county_code=? ORDER BY institution_id",
                (county_code,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM institutions ORDER BY institution_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_verifier(self, county_code, verifier):
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO county_verifiers (county_code,verifier,recorded_at)"
                " VALUES (?,?,?)",
                (county_code, verifier, self.now()),
            )
        return {"county_code": county_code, "verifier": verifier}

    def verifiers(self, county_code):
        rows = self._conn.execute(
            "SELECT verifier FROM county_verifiers WHERE county_code=?", (county_code,)
        ).fetchall()
        return [r["verifier"] for r in rows]

    # ---- 人员档案 ---------------------------------------------------------
    def person(self, person_id):
        row = self._conn.execute(
            "SELECT * FROM persons WHERE person_id=?", (person_id,)
        ).fetchone()
        return dict(row) if row else None

    def persons_by_id_number(self, id_number):
        rows = self._conn.execute(
            "SELECT * FROM persons WHERE id_number=?", (id_number,)
        ).fetchall()
        return [dict(r) for r in rows]

    def persons_as_of(self, moment):
        rows = self._conn.execute(
            "SELECT * FROM persons WHERE recorded_at<=? ORDER BY person_id", (moment,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 双时态事实读取 ---------------------------------------------------
    def facts_as_of(self, moment):
        """知识时刻 moment 可见的全部事实，按人员归组。"""
        facts = {table: {} for table in FACT_TABLES}
        for table in FACT_TABLES:
            rows = self._conn.execute(
                f"SELECT * FROM {table}"
                " WHERE recorded_at<=? AND (expired_at IS NULL OR expired_at>?)"
                " ORDER BY effective_from, recorded_at",
                (moment, moment),
            ).fetchall()
            for row in rows:
                facts[table].setdefault(row["person_id"], []).append(dict(row))
        return facts

    def person_facts(self, person_id, moment):
        facts = {}
        for table in FACT_TABLES:
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE person_id=?"
                " AND recorded_at<=? AND (expired_at IS NULL OR expired_at>?)"
                " ORDER BY effective_from, recorded_at",
                (person_id, moment, moment),
            ).fetchall()
            facts[table] = [dict(r) for r in rows]
        return facts

    def _visible_facts(self, table, person_id, moment):
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE person_id=?"
            " AND recorded_at<=? AND (expired_at IS NULL OR expired_at>?)",
            (person_id, moment, moment),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 变更提交(幂等) ---------------------------------------------------
    def submit_change(self, institution_id, change_type, payload, submitted_by=None,
                      idempotency_key=None):
        """机构提交变更，进入待核验。

        幂等规则：同一幂等键重复提交返回原变更；内容指纹相同的重复上报
        只形成一次有效事实(标记 DUPLICATE 并指向原变更)。
        """
        institution = self.institution(institution_id)
        if not institution:
            raise StoreError(404, "UNKNOWN_INSTITUTION", f"机构 {institution_id} 不存在")
        if change_type not in CHANGE_TYPES:
            raise StoreError(400, "UNKNOWN_CHANGE_TYPE", f"不支持的变更类型 {change_type}")
        if not isinstance(payload, dict):
            raise StoreError(400, "BAD_PAYLOAD", "payload 应为对象")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"type": change_type, "institution": institution_id, "payload": payload},
                sort_keys=True, ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        key = idempotency_key or fingerprint
        with self._lock, self._conn:
            same_key = self._conn.execute(
                "SELECT * FROM changes WHERE idempotency_key=?", (key,)
            ).fetchone()
            if same_key:
                if same_key["fingerprint"] != fingerprint:
                    raise StoreError(409, "IDEMPOTENCY_KEY_REUSED", "同一幂等键提交了不同内容")
                return self.get_change(same_key["change_id"]), True
            twin = self._conn.execute(
                "SELECT * FROM changes WHERE fingerprint=? AND status IN ('PENDING','VERIFIED')",
                (fingerprint,),
            ).fetchone()
            now = self.now()
            if twin:
                change_id = _new_id("chg")
                self._conn.execute(
                    "INSERT INTO changes (change_id,idempotency_key,fingerprint,institution_id,"
                    " county_code,change_type,payload,status,duplicate_of,submitted_by,submitted_at)"
                    " VALUES (?,?,?,?,?,?,?,'DUPLICATE',?,?,?)",
                    (change_id, key, fingerprint, institution_id, institution["county_code"],
                     change_type, self._dump(payload), twin["change_id"], submitted_by, now),
                )
                return self.get_change(change_id), True
            change_id = _new_id("chg")
            self._conn.execute(
                "INSERT INTO changes (change_id,idempotency_key,fingerprint,institution_id,"
                " county_code,change_type,payload,status,submitted_by,submitted_at)"
                " VALUES (?,?,?,?,?,?,?,'PENDING',?,?)",
                (change_id, key, fingerprint, institution_id, institution["county_code"],
                 change_type, self._dump(payload), submitted_by, now),
            )
            return self.get_change(change_id), False

    @staticmethod
    def _dump(payload):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def get_change(self, change_id):
        row = self._conn.execute(
            "SELECT * FROM changes WHERE change_id=?", (change_id,)
        ).fetchone()
        return self._change_dict(row) if row else None

    def list_changes(self, county_code=None, status=None):
        sql, args, conditions = "SELECT * FROM changes", [], []
        if county_code:
            conditions.append("county_code=?")
            args.append(county_code)
        if status:
            conditions.append("status=?")
            args.append(status)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY submitted_at, change_id"
        return [self._change_dict(r) for r in self._conn.execute(sql, args).fetchall()]

    @staticmethod
    def _change_dict(row):
        change = dict(row)
        change["payload"] = json.loads(change["payload"])
        if change.get("result"):
            change["result"] = json.loads(change["result"])
        return change

    # ---- 属地核验 ---------------------------------------------------------
    def verify_change(self, change_id, verifier, decision, note=None):
        """属地核验员核验变更；通过后事实自生效日起入账。"""
        with self._lock, self._conn:
            change = self.get_change(change_id)
            if not change:
                raise StoreError(404, "UNKNOWN_CHANGE", f"变更 {change_id} 不存在")
            if change["status"] != "PENDING":
                raise StoreError(
                    409, "ALREADY_SETTLED", f"变更当前状态为 {change['status']}，不可重复核验")
            if verifier not in self.verifiers(change["county_code"]):
                raise StoreError(403, "NOT_LOCAL_VERIFIER", "核验员不属于该机构所属属地")
            if decision not in ("APPROVE", "REJECT"):
                raise StoreError(400, "UNKNOWN_DECISION", "核验结论仅支持 APPROVE 或 REJECT")
            now = self.now()
            result = None
            if decision == "APPROVE":
                result = self._apply(change, now)
                status = "VERIFIED"
            else:
                status = "REJECTED"
            self._conn.execute(
                "UPDATE changes SET status=?, verified_by=?, verified_at=?, verify_note=?,"
                " result=? WHERE change_id=?",
                (status, verifier, now, note,
                 self._dump(result) if result is not None else None, change_id),
            )
            return self.get_change(change_id)

    def _apply(self, change, now):
        handler = getattr(self, f"_apply_{change['change_type'].lower()}")
        return handler(change["payload"], now)

    # ---- 事实写入辅助 -----------------------------------------------------
    def _supersede(self, table, id_column, row_id, moment):
        """作废旧事实(知识时间轴)，历史查询仍可见作废前的版本。"""
        self._conn.execute(
            f"UPDATE {table} SET expired_at=? WHERE {id_column}=?", (moment, row_id))

    def _close_facts(self, table, id_column, person_id, moment, predicate, close_before):
        """把匹配事实的生效区间截止到 close_before 前一日(作废旧行+补登截止版)。"""
        inserter = getattr(self, f"_insert_{table}")
        for row in self._visible_facts(table, person_id, moment):
            if not predicate(row):
                continue
            self._supersede(table, id_column, row[id_column], moment)
            close_on = (date_type.fromisoformat(close_before) - timedelta(days=1)).isoformat()
            if row["effective_from"] <= close_on:
                closed = {
                    key: value for key, value in row.items()
                    if key not in (id_column, "recorded_at", "expired_at")
                }
                closed["effective_to"] = close_on
                inserter(closed, moment)

    def _require_person(self, person_id):
        if not self.person(person_id):
            raise StoreError(404, "UNKNOWN_PERSON", f"人员 {person_id} 不存在")
        return person_id

    def _insert_qualifications(self, qual, moment):
        self._conn.execute(
            "INSERT INTO qualifications (qual_id,person_id,cert_no,qual_type,specialty,"
            " issued_on,expires_on,effective_from,effective_to,recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_new_id("qua"), qual["person_id"], qual["cert_no"], qual["qual_type"],
             qual.get("specialty"), qual.get("issued_on"), qual.get("expires_on"),
             qual["effective_from"], qual.get("effective_to"), moment),
        )

    def _insert_registrations(self, reg, moment):
        self._conn.execute(
            "INSERT INTO registrations (reg_id,person_id,institution_id,reg_kind,"
            " effective_from,effective_to,recorded_at) VALUES (?,?,?,?,?,?,?)",
            (_new_id("reg"), reg["person_id"], reg["institution_id"], reg["reg_kind"],
             reg["effective_from"], reg.get("effective_to"), moment),
        )

    def _insert_employments(self, emp, moment):
        self._conn.execute(
            "INSERT INTO employments (emp_id,person_id,institution_id,relation_kind,"
            " is_primary,effective_from,effective_to,recorded_at) VALUES (?,?,?,?,?,?,?,?)",
            (_new_id("emp"), emp["person_id"], emp["institution_id"], emp["relation_kind"],
             emp["is_primary"], emp["effective_from"], emp.get("effective_to"), moment),
        )

    def _insert_availability(self, avail, moment):
        self._conn.execute(
            "INSERT INTO availability (avail_id,person_id,kind,effective_from,effective_to,"
            " recorded_at) VALUES (?,?,?,?,?,?)",
            (_new_id("avl"), avail["person_id"], avail["kind"], avail["effective_from"],
             avail.get("effective_to"), moment),
        )

    # ---- 各类变更的落账逻辑 -------------------------------------------------
    def _apply_person_register(self, payload, now):
        person = payload.get("person") or {}
        _require(person, "id_number", "name", "staff_type")
        if person["staff_type"] not in domain.STAFF_TYPES:
            raise StoreError(400, "UNKNOWN_STAFF_TYPE", "未知人员类别")
        if self.persons_by_id_number(person["id_number"]):
            raise StoreError(409, "DUPLICATE_IDENTITY", "证件号码已建档，重复建档被拒绝")
        person_id = _new_id("per")
        self._conn.execute(
            "INSERT INTO persons (person_id,id_number,name,staff_type,recorded_at)"
            " VALUES (?,?,?,?,?)",
            (person_id, person["id_number"], person["name"], person["staff_type"], now),
        )
        qual = payload.get("qualification")
        if qual:
            _require(qual, "cert_no", "qual_type", "effective_from")
            expected = domain.LICENSE_FOR[person["staff_type"]]
            if qual["qual_type"] != expected:
                raise StoreError(
                    400, "LICENSE_MISMATCH",
                    f"{domain.STAFF_TYPES[person['staff_type']]}应提交 {expected}")
            self._insert_qualifications({"person_id": person_id, **qual}, now)
        registration = payload.get("registration")
        if registration:
            _require(registration, "institution_id", "reg_kind", "effective_from")
            self._insert_registrations({"person_id": person_id, **registration}, now)
        employment = payload.get("employment")
        if employment:
            _require(employment, "institution_id", "effective_from")
            self._insert_employments({
                "person_id": person_id,
                "relation_kind": employment.get("relation_kind", domain.REL_REGULAR),
                "is_primary": 1 if employment.get("is_primary", True) else 0,
                **{k: v for k, v in employment.items()
                   if k in ("institution_id", "effective_from", "effective_to")},
            }, now)
        return {"person_id": person_id}

    def _apply_qualification_add(self, payload, now):
        _require(payload, "person_id", "cert_no", "qual_type", "effective_from")
        person_id = self._require_person(payload["person_id"])
        for row in self._visible_facts("qualifications", person_id, now):
            if row["cert_no"] == payload["cert_no"]:
                self._supersede("qualifications", "qual_id", row["qual_id"], now)
        self._insert_qualifications(payload, now)
        return {"applied": "QUALIFICATION_ADD"}

    def _apply_qualification_renew(self, payload, now):
        _require(payload, "person_id", "cert_no", "new_expires_on")
        person_id = self._require_person(payload["person_id"])
        targets = [
            row for row in self._visible_facts("qualifications", person_id, now)
            if row["cert_no"] == payload["cert_no"]
        ]
        if not targets:
            raise StoreError(404, "UNKNOWN_CERTIFICATE", "未找到待续期的证书")
        latest = max(targets, key=lambda r: r["effective_from"])
        # 续期是新增一段接续的有效期，旧证书事实保留，其原本有效的期间不受影响
        renewed = {
            "person_id": person_id,
            "cert_no": latest["cert_no"],
            "qual_type": latest["qual_type"],
            "specialty": latest["specialty"],
            "issued_on": latest["issued_on"],
            "expires_on": payload["new_expires_on"],
            "effective_from": payload.get("effective_from") or now[:10],
            "effective_to": latest["effective_to"],
        }
        self._insert_qualifications(renewed, now)
        return {"applied": "QUALIFICATION_RENEW"}

    def _apply_registration_add(self, payload, now):
        _require(payload, "person_id", "institution_id", "reg_kind", "effective_from")
        person_id = self._require_person(payload["person_id"])
        if not self.institution(payload["institution_id"]):
            raise StoreError(404, "UNKNOWN_INSTITUTION", "注册机构不存在")
        if payload["reg_kind"] not in (domain.REG_PRIMARY, domain.REG_MULTI_SITE):
            raise StoreError(400, "UNKNOWN_REG_KIND", "未知注册类型")
        if payload["reg_kind"] == domain.REG_MULTI_SITE:
            # 同机构同类备案的重复上报只保留最新一次事实
            for row in self._visible_facts("registrations", person_id, now):
                if (row["reg_kind"] == domain.REG_MULTI_SITE
                        and row["institution_id"] == payload["institution_id"]):
                    self._supersede("registrations", "reg_id", row["reg_id"], now)
        self._insert_registrations(payload, now)
        return {"applied": "REGISTRATION_ADD"}

    def _apply_employment_start(self, payload, now):
        _require(payload, "person_id", "institution_id", "relation_kind", "effective_from")
        person_id = self._require_person(payload["person_id"])
        if not self.institution(payload["institution_id"]):
            raise StoreError(404, "UNKNOWN_INSTITUTION", "劳动关系机构不存在")
        relation = payload["relation_kind"]
        if relation not in (domain.REL_REGULAR, domain.REL_SECONDMENT, domain.REL_TRAINING):
            raise StoreError(400, "UNKNOWN_RELATION", "未知劳动关系类型")
        is_primary = 1 if relation == domain.REL_REGULAR and payload.get("is_primary", True) else 0
        self._insert_employments({
            "person_id": person_id,
            "institution_id": payload["institution_id"],
            "relation_kind": relation,
            "is_primary": is_primary,
            "effective_from": payload["effective_from"],
            "effective_to": payload.get("effective_to"),
        }, now)
        return {"applied": "EMPLOYMENT_START"}

    def _apply_employment_end(self, payload, now):
        _require(payload, "person_id", "effective_from")
        person_id = self._require_person(payload["person_id"])

        def matches(row):
            if payload.get("institution_id"):
                return row["institution_id"] == payload["institution_id"]
            return row["is_primary"] and row["relation_kind"] == domain.REL_REGULAR

        ended = [
            row for row in self._visible_facts("employments", person_id, now)
            if matches(row)
        ]
        if not ended:
            raise StoreError(404, "NO_ACTIVE_EMPLOYMENT", "未找到可终止的劳动关系")
        self._close_facts("employments", "emp_id", person_id, now, matches,
                          payload["effective_from"])
        # 主要劳动关系终止时，同一机构的主执业注册同步失效，避免注册兜底口径继续归属
        primary_institutions = {
            row["institution_id"] for row in ended
            if row["is_primary"] and row["relation_kind"] == domain.REL_REGULAR
        }
        if primary_institutions:
            self._close_facts(
                "registrations", "reg_id", person_id, now,
                lambda row: row["reg_kind"] == domain.REG_PRIMARY
                and row["institution_id"] in primary_institutions,
                payload["effective_from"])
        return {"applied": "EMPLOYMENT_END"}

    def _apply_transfer(self, payload, now):
        _require(payload, "person_id", "to_institution_id", "effective_from")
        person_id = self._require_person(payload["person_id"])
        if not self.institution(payload["to_institution_id"]):
            raise StoreError(404, "UNKNOWN_INSTITUTION", "调入机构不存在")
        start = payload["effective_from"]
        self._close_facts(
            "employments", "emp_id", person_id, now,
            lambda row: row["is_primary"] and row["relation_kind"] == domain.REL_REGULAR,
            start)
        self._insert_employments({
            "person_id": person_id,
            "institution_id": payload["to_institution_id"],
            "relation_kind": domain.REL_REGULAR,
            "is_primary": 1,
            "effective_from": start,
            "effective_to": None,
        }, now)
        # 主执业注册随调动一并迁移，保持归属口径一致
        self._close_facts(
            "registrations", "reg_id", person_id, now,
            lambda row: row["reg_kind"] == domain.REG_PRIMARY, start)
        self._insert_registrations({
            "person_id": person_id,
            "institution_id": payload["to_institution_id"],
            "reg_kind": domain.REG_PRIMARY,
            "effective_from": start,
            "effective_to": None,
        }, now)
        return {"applied": "TRANSFER"}

    def _apply_availability_set(self, payload, now):
        _require(payload, "person_id", "kind", "effective_from")
        person_id = self._require_person(payload["person_id"])
        if payload["kind"] not in (domain.AV_WORK, domain.AV_LEAVE, domain.AV_LONG_LEAVE):
            raise StoreError(400, "UNKNOWN_AVAILABILITY", "未知可服务时段类型")
        self._insert_availability(payload, now)
        return {"applied": "AVAILABILITY_SET"}

    # ---- 数据新鲜度 ---------------------------------------------------------
    def institution_freshness(self, county_code, moment, stale_before):
        """各机构在知识时刻的新鲜度：最近核验时间与当时待核验积压。"""
        entries = []
        for institution in self.institutions(county_code):
            row = self._conn.execute(
                "SELECT MAX(verified_at) AS last FROM changes"
                " WHERE institution_id=? AND status='VERIFIED' AND verified_at<=?",
                (institution["institution_id"], moment),
            ).fetchone()
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM changes WHERE institution_id=?"
                " AND submitted_at<=? AND (verified_at IS NULL OR verified_at>?)",
                (institution["institution_id"], moment, moment),
            ).fetchone()
            last = row["last"]
            entries.append({
                "institution_id": institution["institution_id"],
                "name": institution["name"],
                "last_verified_at": last,
                "pending": pending["n"],
                "stale": last is None or last[:10] < stale_before,
            })
        return entries

    # ---- 历史基线 -----------------------------------------------------------
    def publish_baseline(self, county_code, baseline_date, payload):
        """把当前口径下的县级能力固化为不可变基线。"""
        baseline_id = _new_id("bl")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO baselines (baseline_id,county_code,baseline_date,published_at,payload)"
                " VALUES (?,?,?,?,?)",
                (baseline_id, county_code, baseline_date, self.now(), self._dump(payload)),
            )
        return self.get_baseline(baseline_id)

    def get_baseline(self, baseline_id):
        row = self._conn.execute(
            "SELECT * FROM baselines WHERE baseline_id=?", (baseline_id,)
        ).fetchone()
        if not row:
            return None
        baseline = dict(row)
        baseline["snapshot"] = json.loads(baseline.pop("payload"))
        return baseline

    def list_baselines(self, county_code):
        rows = self._conn.execute(
            "SELECT baseline_id,county_code,baseline_date,published_at FROM baselines"
            " WHERE county_code=? ORDER BY published_at",
            (county_code,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 规划情景 -----------------------------------------------------------
    def create_scenario(self, name, county_code, adjustments, created_by=None):
        if not isinstance(adjustments, list) or not adjustments:
            raise StoreError(400, "BAD_ADJUSTMENTS", "情景至少包含一个调整动作")
        for adjustment in adjustments:
            if adjustment.get("type") not in SCENARIO_ADJUSTMENTS:
                raise StoreError(
                    400, "UNKNOWN_ADJUSTMENT",
                    f"未知情景动作 {adjustment.get('type')}，可选: {', '.join(SCENARIO_ADJUSTMENTS)}")
        scenario_id = _new_id("scn")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO scenarios (scenario_id,name,county_code,adjustments,created_by,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (scenario_id, name, county_code, self._dump(adjustments), created_by, self.now()),
            )
        return self.get_scenario(scenario_id)

    def get_scenario(self, scenario_id):
        row = self._conn.execute(
            "SELECT * FROM scenarios WHERE scenario_id=?", (scenario_id,)
        ).fetchone()
        if not row:
            return None
        scenario = dict(row)
        scenario["adjustments"] = json.loads(scenario["adjustments"])
        return scenario

    def list_scenarios(self, county_code):
        rows = self._conn.execute(
            "SELECT scenario_id,name,county_code,created_by,created_at FROM scenarios"
            " WHERE county_code=? ORDER BY created_at",
            (county_code,),
        ).fetchall()
        return [dict(r) for r in rows]
