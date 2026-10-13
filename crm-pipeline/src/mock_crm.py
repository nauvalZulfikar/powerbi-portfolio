"""A local CRM REST API shaped like HubSpot's CRM v3, serving the Maven CRM dataset as it stood on a given day.

    uv run python src/mock_crm.py            # http://127.0.0.1:8765, token pat-local-demo

Why a stand-in: there is no public CRM with real pipeline data to pull from. This server speaks the
parts of the protocol an extractor has to get right — bearer auth, cursor paging, a modified-since
search, a rolling rate limit (429 + Retry-After) and the odd transient 502 — so the extractor is
tested against the same failure modes a real CRM throws.

Time travel: POST /_mock/clock {"date": "2017-03-31"} sets "today". Deals that did not exist yet are
hidden, and every deal shows the stage, amount and close date it had on that day. The source only
dates two events per deal (engaged, closed), so "created" = engaged date; the 500 deals still in
Prospecting carry no date at all and first appear on the export date, 2017-12-31.

Records are served as the source has them, typos included (product "GTXPro", industry "technolgy"):
cleaning is the warehouse's job, not the API's.
"""
import csv
import json
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
TOKEN = "pat-local-demo"
EXPORT_DATE = date(2017, 12, 31)
RATE_LIMIT, RATE_WINDOW = 100, 10.0  # requests per rolling window (seconds)
FAULT_EVERY = 53  # every Nth request fails once with a 502, deterministically
MAX_PAGE = 100


def iso(d):
    return f"{d.isoformat()}T00:00:00.000Z" if d else None


def ms(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def read(name):
    with open(RAW / f"{name}.csv", newline="") as f:
        return list(csv.DictReader(f))


def day(s):
    return date.fromisoformat(s) if s else None


class CRM:
    """The dataset, plus the state that makes it behave like a live service."""

    def __init__(self):
        self.today = EXPORT_DATE
        self.lock = threading.Lock()
        self.hits, self.count = deque(), 0
        teams = read("sales_teams")
        self.owners = [{
            "id": str(100 + i), "email": f"{t['sales_agent'].lower().replace(' ', '.')}@maven-crm.example",
            "firstName": t["sales_agent"].split(" ", 1)[0], "lastName": t["sales_agent"].split(" ", 1)[1],
            "archived": False, "teams": [{"id": f"team-{t['manager'].split()[0].lower()}", "primary": True,
                                         "name": f"{t['regional_office']} / {t['manager']}"}],
        } for i, t in enumerate(teams)]
        owner_id = {f"{o['firstName']} {o['lastName']}": o["id"] for o in self.owners}
        accounts = sorted(read("accounts"), key=lambda a: a["account"])
        self.companies = [{
            "id": str(5000 + i), "properties": {
                "name": a["account"], "industry": a["sector"], "founded_year": a["year_established"],
                "annualrevenue": str(round(float(a["revenue"]) * 1e6)), "numberofemployees": a["employees"],
                "country": a["office_location"], "parent_company": a["subsidiary_of"] or None},
            "createdAt": iso(date(2016, 10, 1)), "updatedAt": iso(date(2016, 10, 1)), "archived": False,
        } for i, a in enumerate(accounts)]
        company_id = {c["properties"]["name"]: c["id"] for c in self.companies}
        self.products = [{
            "id": str(900 + i), "properties": {"name": p["product"], "price": p["sales_price"],
                                               "product_series": p["series"]},
            "createdAt": iso(date(2016, 10, 1)), "updatedAt": iso(date(2016, 10, 1)), "archived": False,
        } for i, p in enumerate(read("products"))]
        self.deals = [{
            "id": d["opportunity_id"], "owner": owner_id[d["sales_agent"]], "company": company_id.get(d["account"]),
            "account": d["account"], "product": d["product"], "stage": d["deal_stage"],
            "engaged": day(d["engage_date"]), "closed": day(d["close_date"]),
            "value": d["close_value"] or None,
        } for d in read("sales_pipeline")]

    # ------------------------------------------------------------ deal as of today
    def deal(self, d):
        created = d["engaged"] or EXPORT_DATE
        if created > self.today:
            return None
        closed = d["closed"] if d["closed"] and d["closed"] <= self.today else None
        stage = ("closedwon" if d["stage"] == "Won" else "closedlost") if closed else \
            "engaging" if d["engaged"] else "prospecting"
        modified = closed or created
        return {
            "id": d["id"], "archived": False, "createdAt": iso(created), "updatedAt": iso(modified),
            "properties": {
                "dealname": f"{d['account'] or 'Unassigned'} – {d['product']}",
                "pipeline": "default", "dealstage": stage,
                "amount": d["value"] if closed else None,
                "createdate": iso(created), "engaged_at": iso(d["engaged"]), "closedate": iso(closed),
                "hs_lastmodifieddate": iso(modified), "hubspot_owner_id": d["owner"],
                "company_id": d["company"], "product_name": d["product"],
            },
            "_modified_ms": ms(modified),
        }

    def search_deals(self, body):
        filters = [f for g in body.get("filterGroups", []) for f in g.get("filters", [])]
        ops = {"GT": lambda a, b: a > b, "GTE": lambda a, b: a >= b, "LT": lambda a, b: a < b,
               "LTE": lambda a, b: a <= b, "EQ": lambda a, b: a == b}
        out = []
        for d in self.deals:
            r = self.deal(d)
            if r is None:
                continue
            ok = True
            for f in filters:
                if f["propertyName"] != "hs_lastmodifieddate" or f["operator"] not in ops:
                    raise ValueError(f"unsupported filter {f}")
                ok &= ops[f["operator"]](r["_modified_ms"], int(f["value"]))
            if ok:
                out.append(r)
        out.sort(key=lambda r: (r["_modified_ms"], r["id"]))
        return out

    # --------------------------------------------------------------- throttling
    def throttle(self):
        """Returns (status, retry_after) when the request must fail, else None."""
        with self.lock:
            now = time.monotonic()
            while self.hits and now - self.hits[0] > RATE_WINDOW:
                self.hits.popleft()
            if len(self.hits) >= RATE_LIMIT:
                return 429, max(1, round(RATE_WINDOW - (now - self.hits[0]) + 0.5))
            self.hits.append(now)
            self.count += 1
            if self.count % FAULT_EVERY == 0:
                return 502, None
        return None


def page(items, query_or_body):
    limit = min(int(query_or_body.get("limit", 10)), MAX_PAGE)
    after = int(query_or_body.get("after") or 0)
    chunk = [{k: v for k, v in r.items() if not k.startswith("_")} for r in items[after:after + limit]]
    out = {"results": chunk}
    if after + limit < len(items):
        out["paging"] = {"next": {"after": str(after + limit)}}
    if "total" in query_or_body:
        out["total"] = len(items)
    return out


def make_handler(crm):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body=None, headers=None):
            data = json.dumps(body if body is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def guard(self):
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send(401, {"status": "error", "category": "INVALID_AUTHENTICATION",
                                "message": "Authentication credentials not found."})
                return False
            if fail := crm.throttle():
                status, retry = fail
                if status == 429:
                    self.send(429, {"status": "error", "category": "RATE_LIMITS",
                                    "message": f"You have reached your secondly limit ({RATE_LIMIT}/{RATE_WINDOW:.0f}s)."},
                              {"Retry-After": str(retry)})
                else:
                    self.send(502, {"status": "error", "message": "Bad gateway"})
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            routes = {"/crm/v3/owners": crm.owners, "/crm/v3/objects/companies": crm.companies,
                      "/crm/v3/objects/products": crm.products}
            if url.path in routes:
                return self.send(200, page(routes[url.path], q))
            self.send(404, {"status": "error", "message": f"no route {url.path}"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/_mock/clock":  # test harness only, no auth
                crm.today = date.fromisoformat(body["date"])
                return self.send(200, {"today": crm.today.isoformat()})
            if not self.guard():
                return
            if self.path == "/crm/v3/objects/deals/search":
                try:
                    return self.send(200, page(crm.search_deals(body), {**body, "total": True}))
                except ValueError as e:
                    return self.send(400, {"status": "error", "category": "VALIDATION_ERROR", "message": str(e)})
            self.send(404, {"status": "error", "message": f"no route {self.path}"})

    return Handler


def serve(port=8765):
    """Start in a background thread; returns the server (call .shutdown() when done)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(CRM()))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    srv = serve()
    print(f"mock CRM on http://127.0.0.1:{srv.server_port}  (token {TOKEN}) — Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
