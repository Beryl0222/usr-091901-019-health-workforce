"""区域医护能力规划台账的服务入口。"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from workforce.api import Api
from workforce.store import Store, StoreError

SERVICE_ID = "health-workforce"
SERVICE_NAME = "区域医护能力规划台账"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查、契约读取与区域医护能力业务接口。"""

    api = None  # 由 configure() 注入；未注入时业务接口一律 404

    @classmethod
    def configure(cls, store, contract):
        cls.api = Api(store, contract)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, health_payload())
            return
        if parsed.path == "/contract":
            self._send_json(200, load_contract())
            return
        self._dispatch("GET", parsed)

    def do_POST(self):
        self._dispatch("POST", urlparse(self.path))

    def _dispatch(self, method, parsed):
        if Handler.api is None:
            self._send_json(404, {"error": "NOT_FOUND", "detail": "接口不存在"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._send_json(400, {"error": "BAD_JSON", "detail": "请求体不是合法 JSON"})
                return
        try:
            status, payload = Handler.api.dispatch(
                method, parsed.path, parse_qs(parsed.query), body)
        except StoreError as error:
            status, payload = error.status, {"error": error.code, "detail": error.detail}
        except Exception as error:  # 兜底，保证错误响应同样是 JSON
            status, payload = 500, {"error": "INTERNAL", "detail": str(error)}
        self._send_json(status, payload)

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def check():
    """基础检查：契约结构完整且事实库可建表。"""
    contract = load_contract()
    assert contract["states"] and contract["invariants"]
    assert contract["calibers"]["attribution"] and contract["shortage_specialties"]
    Store(":memory:").close()
    print("基础检查通过")


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--db", default="workforce.db", help="SQLite 事实库路径")
    parser.add_argument("--seed", action="store_true", help="载入演示数据")
    args = parser.parse_args()
    if args.check:
        check()
        return
    contract = load_contract()
    store = Store(args.db)
    if args.seed:
        from workforce.seed import load_demo_data
        load_demo_data(store)
    Handler.configure(store, contract)
    print(f"{SERVICE_NAME} 正在监听 :{args.port}")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
