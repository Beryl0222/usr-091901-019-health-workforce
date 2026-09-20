"""HTTP 接口层：路由、JSON 编解码与错误映射。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlsplit

from .core import WorkforceService
from .model import DomainError, parse_date, parse_time


def _q1(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default


def _day(query: dict):
    raw = _q1(query, "date")
    return parse_date(raw, "date") if raw else None


def _knowledge(query: dict):
    raw = _q1(query, "as_of")
    return parse_time(raw, "as_of") if raw else None


def _county_capacity(svc, match, query, _body):
    return svc.county_capacity(match.group("county"), day=_day(query),
                               knowledge=_knowledge(query),
                               drill=_q1(query, "drill") in ("1", "true"))


def _org_capacity(svc, match, query, _body):
    return svc.org_capacity(match.group("org_id"), day=_day(query),
                            knowledge=_knowledge(query))


def _baseline_as_of(svc, _match, query, _body):
    county = _q1(query, "county")
    if not county:
        raise DomainError("缺少查询参数：county")
    return svc.baseline_as_of(county, _knowledge(query) or svc.clock())


def _list_reports(svc, _match, query, _body):
    return svc.list_reports(county=_q1(query, "county"), status=_q1(query, "status"))


def _list_conflicts(svc, _match, query, _body):
    county = _q1(query, "county")
    if not county:
        raise DomainError("缺少查询参数：county")
    return svc.list_conflicts(county, knowledge=_knowledge(query))


def _projection(svc, match, query, _body):
    return svc.project_scenario(match.group("scenario_id"), day=_day(query))


ROUTES = [
    ("POST", r"^/orgs$", lambda svc, m, q, b: svc.add_org(b)),
    ("POST", r"^/verifiers$", lambda svc, m, q, b: svc.add_verifier(b)),
    ("POST", r"^/persons$", lambda svc, m, q, b: svc.add_person(b)),
    ("GET", r"^/persons/(?P<person_id>[^/]+)$",
     lambda svc, m, q, b: svc.person_view(m.group("person_id"))),
    ("POST", r"^/reports$", lambda svc, m, q, b: svc.submit_report(b)),
    ("GET", r"^/reports$", _list_reports),
    ("POST", r"^/verifications$", lambda svc, m, q, b: svc.verify(b)),
    ("GET", r"^/capacity/county/(?P<county>[^/]+)$", _county_capacity),
    ("GET", r"^/capacity/institution/(?P<org_id>[^/]+)$", _org_capacity),
    ("POST", r"^/baselines$", lambda svc, m, q, b: svc.publish_baseline(b)),
    ("GET", r"^/baselines$", _baseline_as_of),
    ("GET", r"^/baselines/(?P<baseline_id>[^/]+)$",
     lambda svc, m, q, b: svc.get_baseline(m.group("baseline_id"))),
    ("POST", r"^/scenarios$", lambda svc, m, q, b: svc.create_scenario(b)),
    ("GET", r"^/scenarios/(?P<scenario_id>[^/]+)/projection$", _projection),
    ("GET", r"^/conflicts$", _list_conflicts),
]


class WorkforceRequestHandler(BaseHTTPRequestHandler):
    """区域医护能力台账 HTTP 入口；service 为类级注入的领域服务。"""

    service: WorkforceService = None  # 由子类或 make_handler 注入

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = parse_qs(split.query)
        for route_method, pattern, handler in ROUTES:
            match = re.match(pattern, path)
            if route_method != method or not match:
                continue
            try:
                body = self._read_body() if method == "POST" else None
                status, payload = handler(self.service, match, query, body)
            except DomainError as error:
                self._send_json({"error": str(error)}, error.status)
                return
            self._send_json(payload, status)
            return
        self.send_error(404)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DomainError("请求体须为合法 JSON")
        if not isinstance(body, dict):
            raise DomainError("请求体须为 JSON 对象")
        return body

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def make_handler(service: WorkforceService):
    """生成绑定指定领域服务实例的处理器，便于测试隔离。"""
    class BoundHandler(WorkforceRequestHandler):
        pass

    BoundHandler.service = service
    return BoundHandler
