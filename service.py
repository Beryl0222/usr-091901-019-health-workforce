"""区域医护能力规划台账的基础服务入口。"""
import argparse
import json
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from workforce.api import WorkforceRequestHandler
from workforce.core import WorkforceService

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


class Handler(WorkforceRequestHandler):
    """在领域接口之外提供健康检查和领域契约读取接口。"""

    service = WorkforceService()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            self._send_json(health_payload())
            return
        if path == "/contract":
            self._send_json(load_contract())
            return
        super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
