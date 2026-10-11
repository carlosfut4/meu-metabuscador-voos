import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fli.models import (
    Airline,
    Airport,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TripType,
)
from fli.search import SearchDates, SearchFlights

VERSION = 4
API_KEY = os.environ.get("API_KEY", "")
MAX_BODY = 20000
CACHE_TTL = int(os.environ.get("CACHE_TTL", "10800"))
_cache = {}
_cache_lock = threading.Lock()

STOPS = {"any": MaxStops.ANY, "0": MaxStops.NON_STOP, "1": MaxStops.ONE_STOP_OR_FEWER, "2": MaxStops.TWO_OR_FEWER_STOPS}
CABINS = {"economy": SeatType.ECONOMY, "premium": SeatType.PREMIUM_ECONOMY, "business": SeatType.BUSINESS, "first": SeatType.FIRST}
SORTS = {"cheapest": SortBy.CHEAPEST, "best": SortBy.BEST, "duration": SortBy.DURATION}


class BadRequest(Exception):
    pass


class Busy(Exception):
    pass


def cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        _cache.pop(key, None)
    return None


def cache_put(key, value):
    with _cache_lock:
        if len(_cache) > 2000:
            _cache.clear()
        _cache[key] = (time.time() + CACHE_TTL, value)


def airport(code):
    code = str(code or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise BadRequest("Aeroporto inválido: " + code)
    try:
        return Airport[code]
    except KeyError:
        raise BadRequest("Aeroporto desconhecido: " + code)


def airlines(codes):
    out = []
    for c in (codes or [])[:10]:
        c = str(c).strip().upper()
        for name in (c, "_" + c):
            try:
                out.append(Airline[name])
                break
            except KeyError:
                continue
    return out or None


def day(value, field):
    try:
        d = datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        raise BadRequest(field + " inválida (use AAAA-MM-DD).")
    if d < date.today() - timedelta(days=1):
        raise BadRequest(field + " já passou.")
    if d > date.today() + timedelta(days=330):
        raise BadRequest(field + " muito distante (até 11 meses).")
    return d


def passengers(body):
    try:
        p = PassengerInfo(
            adults=max(0, min(9, int(body.get("adults", 1)))),
            children=max(0, min(8, int(body.get("children", 0)))),
            infants_in_seat=max(0, min(4, int(body.get("infants_seat", 0)))),
            infants_on_lap=max(0, min(4, int(body.get("infants_lap", 0)))),
        )
    except Exception as e:
        raise BadRequest("Passageiros inválidos: total de 1 a 9 e um adulto por bebê de colo.")
    return p


def code_of(enum_value):
    return getattr(enum_value, "name", str(enum_value)).lstrip("_")


def name_of(enum_value):
    return getattr(enum_value, "value", str(enum_value))


def segments_of(body):
    raw = body.get("segments")
    if not raw:
        raw = [{"origin": body.get("origin"), "destination": body.get("destination"), "date": body.get("departure_date")}]
        if body.get("return_date"):
            raw.append({"origin": body.get("return_from") or body.get("destination"), "destination": body.get("origin"), "date": body.get("return_date")})
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise BadRequest("Informe de 1 a 5 trechos.")
    segs = []
    last = None
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise BadRequest("Trecho inválido.")
        o = airport(s.get("origin"))
        d = airport(s.get("destination"))
        if o == d:
            raise BadRequest("Origem e destino iguais no trecho " + str(i + 1) + ".")
        dt = day(s.get("date"), "Data do trecho " + str(i + 1))
        if last and dt < last:
            raise BadRequest("As datas dos trechos precisam estar em ordem.")
        last = dt
        segs.append((o, d, dt))
    return segs


def trip_type(segs):
    if len(segs) == 1:
        return TripType.ONE_WAY
    if len(segs) == 2 and segs[0][0] == segs[1][1] and segs[0][1] == segs[1][0]:
        return TripType.ROUND_TRIP
    return TripType.MULTI_CITY


def slice_json(f, currency):
    legs = [{
        "airline": name_of(l.airline),
        "airline_code": code_of(l.airline),
        "number": l.flight_number,
        "from": code_of(l.departure_airport),
        "to": code_of(l.arrival_airport),
        "departure": l.departure_datetime.isoformat(),
        "arrival": l.arrival_datetime.isoformat(),
        "minutes": l.duration,
        "operated_by": name_of(l.operating_airline) if getattr(l, "operating_airline", None) else None,
    } for l in f.legs]
    names = []
    for l in legs:
        if l["airline"] not in names:
            names.append(l["airline"])
    return {
        "airline": ", ".join(names),
        "airline_codes": sorted({l["airline_code"] for l in legs}),
        "from": legs[0]["from"] if legs else None,
        "to": legs[-1]["to"] if legs else None,
        "departure": legs[0]["departure"] if legs else None,
        "arrival": legs[-1]["arrival"] if legs else None,
        "minutes": f.duration,
        "stops": f.stops,
        "layovers": [{"airport": code_of(x.airport), "minutes": x.duration, "overnight": bool(getattr(x, "overnight", False)), "change_airport": bool(getattr(x, "change_of_airport", False))} for x in (f.layovers or [])],
        "self_transfer": bool(getattr(f, "self_transfer", False)),
        "emissions": getattr(f, "emissions_tag", None),
        "legs": legs,
    }


def flight_json(f, currency):
    out = slice_json(f, currency)
    out.update({"price": f.price, "currency": f.currency or currency, "airline_direct": None})
    return out


def filters_for(body, segs):
    return FlightSearchFilters(
        trip_type=trip_type(segs),
        passenger_info=passengers(body),
        flight_segments=[FlightSegment(departure_airport=[[o, 0]], arrival_airport=[[d, 0]], travel_date=dt.isoformat()) for o, d, dt in segs],
        stops=STOPS.get(str(body.get("stops", "any")), MaxStops.ANY),
        seat_type=CABINS.get(str(body.get("cabin", "economy")), SeatType.ECONOMY),
        sort_by=SORTS.get(str(body.get("sort", "cheapest")), SortBy.CHEAPEST),
        airlines=airlines(body.get("airlines")),
    )


def itinerary_key(parts):
    return "|".join(l.airline.name + l.flight_number + l.departure_datetime.isoformat() for p in parts for l in p.legs)


def trips(body):
    segs = segments_of(body)
    currency = str(body.get("currency", "EUR")).upper()[:3]
    direct = bool(body.get("airline_direct", False))
    limit = max(1, min(80, int(body.get("max_results", 40))))
    key = json.dumps(["t", body], sort_keys=True)
    hit = cache_get(key)
    if hit is not None:
        return hit
    filters = filters_for(body, segs)
    n = len(segs)
    top = max(1, min(8, int(body.get("expand", 4 if n <= 2 else (3 if n == 3 else 2)))))
    client = SearchFlights()
    results = client.search(filters, top_n=top, currency=currency, language="pt-BR", country="BR") or []
    seen = set()
    rows = []
    for r in results:
        parts = r if isinstance(r, tuple) else (r,)
        k = itinerary_key(parts)
        if k in seen:
            continue
        seen.add(k)
        price = parts[-1].price if parts[-1].price is not None else parts[0].price
        rows.append((price, parts, r))
    rows.sort(key=lambda x: (x[0] is None, x[0] or 0))
    rows = rows[:limit]
    out = []
    for price, parts, raw in rows:
        out.append({
            "price": price,
            "currency": parts[-1].currency or currency,
            "slices": [slice_json(p, currency) for p in parts],
            "airline_direct": None,
            "_raw": raw,
        })
    if direct:
        checks = max(1, min(12, int(body.get("direct_checks", 6))))

        def check(item):
            try:
                opts = client.get_booking_options(item["_raw"], filters, currency=currency, language="pt-BR", country="BR") or []
            except Exception:
                opts = []
            airline_opts = [o for o in opts if o.is_airline_direct and o.price is not None]
            if airline_opts:
                best = min(airline_opts, key=lambda o: o.price)
                item.update(airline_direct=True, direct_price=best.price, vendor=best.vendor_name, booking_url=best.booking_url or best.google_click_url)
            elif opts:
                item["airline_direct"] = False
                others = [o for o in opts if o.price is not None]
                if others:
                    item["vendor"] = min(others, key=lambda o: o.price).vendor_name

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(check, [x for x in out if x["price"] is not None][:checks]))
    for item in out:
        item.pop("_raw", None)
    data = {
        "ok": True,
        "version": VERSION,
        "trip": trip_type(segs).name.lower(),
        "segments": [{"origin": code_of(o), "destination": code_of(d), "date": dt.isoformat()} for o, d, dt in segs],
        "itineraries": out,
        "sparse": bool(getattr(client, "sparse_passenger_mix", False)) and not out,
    }
    cache_put(key, data)
    return data


def search(body):
    data = trips(dict(body, max_results=body.get("max_results", 20)))
    flights = []
    for it in data["itineraries"]:
        item = dict(it["slices"][0])
        item.update(price=it["price"], currency=it["currency"], airline_direct=it.get("airline_direct"))
        for k in ("direct_price", "vendor", "booking_url"):
            if k in it:
                item[k] = it[k]
        if len(it["slices"]) > 1:
            item["return"] = it["slices"][-1]
        flights.append(item)
    return dict(data, flights=flights, query={k: body.get(k) for k in ("origin", "destination", "departure_date", "return_date")})


def price_pairs(body, origin, dest, pairs, on_row=None):
    kw = dict(
        passenger_info=passengers(body),
        stops=STOPS.get(str(body.get("stops", "any")), MaxStops.ANY),
        seat_type=CABINS.get(str(body.get("cabin", "economy")), SeatType.ECONOMY),
        airlines=airlines(body.get("airlines")),
    )
    currency = str(body.get("currency", "EUR")).upper()[:3]

    def one(p):
        dep, ret = p
        segs = [FlightSegment(departure_airport=[[origin, 0]], arrival_airport=[[dest, 0]], travel_date=dep.isoformat())]
        k = dict(kw)
        if ret:
            segs.append(FlightSegment(departure_airport=[[dest, 0]], arrival_airport=[[origin, 0]], travel_date=ret.isoformat()))
            k.update(trip_type=TripType.ROUND_TRIP, duration=(ret - dep).days)
        else:
            k.update(trip_type=TripType.ONE_WAY)
        ck = json.dumps(["p", code_of(origin), code_of(dest), dep.isoformat(), ret.isoformat() if ret else None, currency, body.get("adults"), body.get("children"), body.get("infants_seat"), body.get("infants_lap"), body.get("stops"), body.get("cabin"), body.get("airlines")])
        hit = cache_get(ck)
        if hit is not None:
            return hit
        f = DateSearchFilters(flight_segments=segs, from_date=dep.isoformat(), to_date=dep.isoformat(), **k)
        try:
            res = SearchDates().search(f, currency=currency, language="pt-BR", country="BR") or []
        except Exception:
            return {"date": dep.isoformat(), "return_date": ret.isoformat() if ret else None, "price": None, "error": True}
        price = min([r.price for r in res if r.price], default=None)
        row = {"date": dep.isoformat(), "return_date": ret.isoformat() if ret else None, "price": price, "currency": (res[0].currency if res and res[0].currency else currency)}
        cache_put(ck, row)
        return row

    def run(p):
        row = one(p)
        if on_row:
            on_row(row)
        return row

    with ThreadPoolExecutor(max_workers=max(1, min(10, int(os.environ.get("WORKERS", "6"))))) as ex:
        return list(ex.map(run, pairs))


def parse_pairs(body, cap=None):
    raw = body.get("pairs")
    if not isinstance(raw, list) or not raw:
        raise BadRequest("Informe as datas (pares ida/volta).")
    cap = cap or max(1, min(120, int(os.environ.get("MAX_PAIRS", "60"))))
    if len(raw) > cap:
        raise BadRequest("Muitas datas numa busca só (máximo " + str(cap) + ").")
    out = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or not 1 <= len(p) <= 2:
            raise BadRequest("Par de datas inválido.")
        dep = day(p[0], "Data de ida")
        ret = day(p[1], "Data de volta") if len(p) > 1 and p[1] else None
        if ret and ret <= dep:
            raise BadRequest("A volta precisa ser depois da ida.")
        if ret and (ret - dep).days > 60:
            raise BadRequest("Viagem de no máximo 60 dias.")
        out.append((dep, ret))
    return out


def prices(body):
    origin = airport(body.get("origin"))
    dest = airport(body.get("destination"))
    if origin == dest:
        raise BadRequest("Origem e destino iguais.")
    rows = price_pairs(body, origin, dest, parse_pairs(body))
    return {"ok": True, "version": VERSION, "prices": rows}


def date_prices(body, origin, dest, start, end, trip_days):
    pairs = []
    d = start
    while d <= end and len(pairs) < 93:
        pairs.append((d, d + timedelta(days=trip_days) if trip_days else None))
        d += timedelta(days=1)
    return [r for r in price_pairs(body, origin, dest, pairs) if r["price"]]


def dates(body):
    origin = airport(body.get("origin"))
    dest = airport(body.get("destination"))
    start = day(body.get("from_date"), "Data inicial")
    end = day(body.get("to_date"), "Data final")
    if end < start:
        raise BadRequest("A data final é antes da inicial.")
    if (end - start).days > 92:
        raise BadRequest("Período máximo de 93 dias por busca.")
    trip = int(body.get("trip_days") or 0)
    trip = max(1, min(60, trip)) if trip else None
    rows = date_prices(body, origin, dest, start, end, trip)
    rows.sort(key=lambda r: r["date"])
    return {"ok": True, "version": VERSION, "dates": rows, "cheapest": sorted(rows, key=lambda r: r["price"])[:10]}


def explore(body, on_progress=None):
    origin = airport(body.get("origin"))
    dests = []
    for c in (body.get("destinations") or [])[:30]:
        try:
            a = airport(c)
            if a != origin and a not in dests:
                dests.append(a)
        except BadRequest:
            continue
    if not dests:
        raise BadRequest("Informe os destinos.")
    start = day(body.get("from_date"), "Data inicial")
    end = day(body.get("to_date"), "Data final")
    if end < start:
        raise BadRequest("A data final é antes da inicial.")
    trip = int(body.get("trip_days") or 0)
    trip = max(1, min(60, trip)) if trip else None
    budget = max(10, min(240, int(os.environ.get("MAX_EXPLORE", "120"))))
    per = max(1, budget // len(dests))
    span = (end - start).days + 1
    step = max(1, -(-span // per))
    step = max(step, max(1, min(30, int(body.get("sample_every", 1)))))
    samples = []
    d = start
    while d <= end and len(samples) < per:
        samples.append(d)
        d += timedelta(days=step)
    pairs_by_dest = {dest: [(s, s + timedelta(days=trip) if trip else None) for s in samples] for dest in dests}

    def one(dest):
        rows = [r for r in price_pairs(body, origin, dest, pairs_by_dest[dest]) if r["price"]]
        best = min(rows, key=lambda r: r["price"]) if rows else None
        if on_progress:
            on_progress(len(pairs_by_dest[dest]))
        return {"destination": code_of(dest), "best": best, "samples": sorted(rows, key=lambda r: r["date"])}

    with ThreadPoolExecutor(max_workers=2) as ex:
        res = list(ex.map(one, dests))
    res.sort(key=lambda r: (r["best"] is None, r["best"]["price"] if r["best"] else 0))
    return {"ok": True, "version": VERSION, "results": res, "sampled_dates": [s.isoformat() for s in samples], "step": step}


JOBS = {}
_jobs_lock = threading.Lock()
MAX_RUNNING = int(os.environ.get("MAX_JOBS", "4"))


def _job_set(jid, **kw):
    with _jobs_lock:
        if jid in JOBS:
            JOBS[jid].update(kw)


def start_job(body):
    kind = str(body.get("kind", ""))
    req = body.get("body")
    if kind not in ("search", "prices", "explore") or not isinstance(req, dict):
        raise BadRequest("Tipo de busca inválido.")
    now = time.time()
    with _jobs_lock:
        for k in [k for k, j in JOBS.items() if j["created"] < now - 1800]:
            JOBS.pop(k, None)
        running = sum(1 for j in JOBS.values() if j["status"] == "running")
    if kind == "search":
        segments_of(req)
        hit = cache_get(json.dumps(["t", req], sort_keys=True))
        if hit is not None:
            return {"ok": True, "version": VERSION, "id": None, "status": "done", "result": hit}
        total = 1
    elif kind == "prices":
        airport(req.get("origin"))
        airport(req.get("destination"))
        total = len(parse_pairs(req, cap=150))
    else:
        airport(req.get("origin"))
        total = max(1, len(req.get("destinations") or []))
    if running >= MAX_RUNNING:
        raise Busy("O metabuscador está ocupado com outras buscas. Tente de novo em 1 minuto.")
    jid = secrets.token_urlsafe(18)
    with _jobs_lock:
        JOBS[jid] = {"status": "running", "created": now, "kind": kind, "done": 0, "total": total, "partial": [], "result": None, "error": None}

    def work():
        try:
            if kind == "search":
                res = trips(req)
                _job_set(jid, done=1)
            elif kind == "prices":
                o = airport(req.get("origin"))
                d = airport(req.get("destination"))

                def on_row(row):
                    with _jobs_lock:
                        j = JOBS.get(jid)
                        if j:
                            j["done"] += 1
                            if row.get("price"):
                                j["partial"].append(row)
                rows = price_pairs(req, o, d, parse_pairs(req, cap=150), on_row)
                res = {"ok": True, "version": VERSION, "prices": rows}
            else:
                def on_progress(n):
                    with _jobs_lock:
                        j = JOBS.get(jid)
                        if j:
                            j["done"] += 1
                res = explore(req, on_progress)
            _job_set(jid, status="done", result=res)
        except (BadRequest, ValueError) as e:
            _job_set(jid, status="error", error=str(e)[:300])
        except Exception as e:
            _job_set(jid, status="error", error="O Google não respondeu como esperado: " + type(e).__name__ + ": " + str(e)[:200])

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "version": VERSION, "id": jid, "status": "running", "done": 0, "total": total}


def get_job(body):
    jid = str(body.get("id", ""))
    since = max(0, int(body.get("since", 0) or 0))
    with _jobs_lock:
        j = JOBS.get(jid)
        if not j:
            raise BadRequest("Busca não encontrada (o servidor pode ter reiniciado). Busque de novo.")
        out = {"ok": True, "version": VERSION, "id": jid, "status": j["status"], "done": j["done"], "total": j["total"], "partial": j["partial"][since:], "partial_count": len(j["partial"])}
        if j["status"] == "done":
            out["result"] = j["result"]
        if j["status"] == "error":
            out["error"] = j["error"]
    return out


ROUTES = {"/search": search, "/v2/search": search, "/v2/dates": dates, "/v2/explore": explore, "/v3/search": trips, "/v3/prices": prices, "/v3/explore": explore, "/v4/jobs": start_job, "/v4/job": get_job}


INFO = {"status": "online", "message": "API de voos funcionando", "version": VERSION, "features": ["jobs", "search", "roundtrip", "multicity", "prices", "dates", "explore", "children", "airline_direct"]}


def handle(method, path, key, raw):
    path = path.split("?")[0]
    if method in ("GET", "HEAD"):
        if path in ("/", "/health"):
            return 200, INFO
        return 404, {"ok": False, "error": "Não encontrado."}
    if method != "POST":
        return 405, {"ok": False, "error": "Método não permitido."}
    fn = ROUTES.get(path)
    if fn is None:
        return 404, {"ok": False, "error": "Não encontrado."}
    if API_KEY and key != API_KEY:
        return 401, {"ok": False, "error": "Chave inválida."}
    if raw is None or len(raw) == 0 or len(raw) > MAX_BODY:
        return 413, {"ok": False, "error": "Corpo inválido."}
    try:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError()
    except Exception:
        return 400, {"ok": False, "error": "JSON inválido."}
    try:
        return 200, fn(body)
    except Busy as e:
        return 429, {"ok": False, "error": str(e)}
    except (BadRequest, ValueError) as e:
        return 422, {"ok": False, "error": str(e)[:300]}
    except Exception as e:
        return 502, {"ok": False, "error": "O Google não respondeu como esperado: " + type(e).__name__ + ": " + str(e)[:300]}


def encode(data):
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "metabuscador/2"

    def run(self, method):
        raw = None
        if method == "POST":
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if 0 < n <= MAX_BODY else b""
        code, data = handle(method, self.path, self.headers.get("X-API-Key", ""), raw)
        out = encode(data)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(out)

    def do_GET(self):
        self.run("GET")

    def do_HEAD(self):
        self.run("HEAD")

    def do_POST(self):
        self.run("POST")

    def log_message(self, fmt, *args):
        if os.environ.get("LOG_REQUESTS"):
            super().log_message(fmt, *args)


_pool = ThreadPoolExecutor(max_workers=8)


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    import asyncio
    raw = b""
    more = True
    while more:
        msg = await receive()
        raw += msg.get("body", b"")
        more = msg.get("more_body", False)
        if len(raw) > MAX_BODY:
            break
    key = ""
    for k, v in scope.get("headers", []):
        if k.lower() == b"x-api-key":
            key = v.decode("latin-1")
    method = scope.get("method", "GET")
    code, data = await asyncio.get_running_loop().run_in_executor(_pool, handle, method, scope.get("path", "/"), key, raw if method == "POST" else None)
    out = encode(data)
    await send({"type": "http.response.start", "status": code, "headers": [(b"content-type", b"application/json; charset=utf-8"), (b"content-length", str(len(out)).encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": b"" if method == "HEAD" else out})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
