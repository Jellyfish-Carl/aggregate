from __future__ import annotations

import errno
import json
import mimetypes
from time import perf_counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .simulator import simulate, summary
PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web"


def _nullable_float_array(params: dict, key: str) -> list:
    raw = params.get(key, [""])[0]
    if not raw:
        return []
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or len(parsed) != 96:
        raise ValueError("%s 必须是包含96项的JSON数组" % key)
    return [None if value is None else float(value) for value in parsed]


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "AggregateMpcDemo/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._json({"status": "ok", "service": "aggregate-mpc-demo"})
                return
            if parsed.path == "/api/summary":
                params = parse_qs(parsed.query)
                diagnostic_mode = params.get("diagnostic_mode", ["false"])[0].lower() == "true"
                self._json(summary(diagnostic_mode=diagnostic_mode))
                return
            if parsed.path == "/api/simulate":
                params = parse_qs(parsed.query)
                event = params.get("event", ["D-1"])[0]
                risk = float(params.get("risk", ["0.25"])[0])
                volatility = params.get("volatility", ["high"])[0]
                request_started = perf_counter()
                request_label = "event=%s rt=%s risk=%.2f" % (
                    event,
                    params.get("rt_period", ["76"])[0],
                    risk,
                )
                print("[mpc-demo] simulate start %s" % request_label, flush=True)
                self._json(
                    simulate(
                        event=event,
                        risk=risk,
                        cvar_enabled=params.get("cvar_enabled", ["true"])[0].lower() == "true",
                        volatility=volatility,
                        annual_price=float(params.get("annual_price", ["405"])[0]),
                        monthly_price=float(params.get("monthly_price", ["416"])[0]),
                        ten_day_price=float(params.get("ten_day_price", ["424"])[0]),
                        annual_coverage=float(params.get("annual_coverage", ["0.80"])[0]),
                        monthly_coverage=float(params.get("monthly_coverage", ["0.95"])[0]),
                        ten_day_coverage=float(params.get("ten_day_coverage", ["0.98"])[0]),
                        d3_coverage=float(params.get("d3_coverage", ["1.00"])[0]),
                        d2_coverage=float(params.get("d2_coverage", ["1.00"])[0]),
                        target_quantile=params.get("target_quantile", ["P50"])[0],
                        minimum_edge=float(params.get("minimum_edge", ["2"])[0]),
                        deadband_ratio=float(params.get("deadband_ratio", ["0.005"])[0]),
                        maximum_adjustment=float(params.get("maximum_adjustment", ["8000"])[0]),
                        allow_sell=params.get("allow_sell", ["true"])[0].lower() == "true",
                        rolling_interval_limit_ratio=float(params.get("rolling_interval_limit_ratio", ["0.20"])[0]),
                        rolling_daily_limit_ratio=float(params.get("rolling_daily_limit_ratio", ["0.05"])[0]),
                        rolling_sell_basis=params.get("rolling_sell_basis", ["P50"])[0],
                        rolling_user_buy_ceiling=float(params.get("rolling_user_buy_ceiling", ["1000"])[0]),
                        rolling_user_sell_floor=float(params.get("rolling_user_sell_floor", ["0"])[0]),
                        rolling_price_edge=float(params.get("rolling_price_edge", ["100"])[0]),
                        rolling_price_edge_lower=float(params.get("rolling_price_edge_lower", ["100"])[0]),
                        rolling_price_edge_upper=float(params.get("rolling_price_edge_upper", ["100"])[0]),
                        rolling_min_fill_ratio=float(params.get("rolling_min_fill_ratio", ["0.10"])[0]),
                        rolling_max_fill_ratio=float(params.get("rolling_max_fill_ratio", ["0.20"])[0]),
                        retail_pricing_source=params.get("retail_pricing_source", ["MOCK_ASSUMPTION"])[0],
                        package_type=params.get("package_type", ["LINKED"])[0],
                        cap_price=float(params.get("cap_price", ["490"])[0]),
                        service_fee=float(params.get("service_fee", ["18"])[0]),
                        fixed_price=float(params.get("fixed_price", ["455"])[0]),
                        share_ratio=float(params.get("share_ratio", ["0.50"])[0]),
                        retail_annual_weight=float(params.get("retail_annual_weight", ["0.70"])[0]),
                        retail_monthly_weight=float(params.get("retail_monthly_weight", ["0.20"])[0]),
                        retail_spot_weight=float(params.get("retail_spot_weight", ["0.10"])[0]),
                        rt_period=int(params.get("rt_period", ["76"])[0]),
                        require_milp=params.get("require_milp", ["true"])[0].lower() == "true",
                        diagnostic_mode=params.get("diagnostic_mode", ["false"])[0].lower() == "true",
                        load_scenario_seed=int(params.get("load_scenario_seed", ["2026091501"])[0]),
                        price_scenario_seed=int(params.get("price_scenario_seed", ["2026091502"])[0]),
                        actual_load_overrides=_nullable_float_array(params, "actual_load_overrides"),
                        actual_real_time_price_overrides=_nullable_float_array(
                            params, "actual_real_time_price_overrides"
                        ),
                        progress_logger=lambda message: print(
                            "[mpc-demo] timing " + message, flush=True
                        ),
                    )
                )
                print(
                    "[mpc-demo] simulate done %s %.3fs"
                    % (request_label, perf_counter() - request_started),
                    flush=True,
                )
                return
            self._static(parsed.path)
        except (ValueError, KeyError) as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - process boundary
            self._json({"error": "INTERNAL_ERROR", "detail": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else unquote(request_path.lstrip("/"))
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN.value)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND.value)
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        print("[mpc-demo] " + format_string % args)


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), DemoHandler)
    return server


def find_available_port(host: str = "127.0.0.1", preferred_port: int = 8765, scan_limit: int = 50) -> int:
    for port in range(preferred_port, preferred_port + scan_limit + 1):
        try:
            server = create_server(host, port)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                continue
            raise
        server.server_close()
        return port
    raise OSError(errno.EADDRINUSE, "no available port found")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(host, port)
    print("储售集合体 Demo: http://%s:%d" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
