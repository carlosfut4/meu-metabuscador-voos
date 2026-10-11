import json
import os
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

VERSION = 2
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


def flight_json(f, currency):
    legs = [{
        "airline": name_of(l.airline),
        "airline_code": code_of(l.airline),
        "number": l.flight_number,
        "from": code_of(l.departure_airport),
        "to": code_of(l.arrival_airport),
        "departure": l.departure_datetime.isoformat(),
        "arrival": l.arrival_datetime.isoformat(),
        "minutes": l.duration,
    } for l in f.legs]
    names = []
    for l in legs:
        if l["airline"] not in names:
            names.append(l["airline"])
    return {
        "airline": ", ".join(names),
        "airline_codes": sorted({l["airline_code"] for l in legs}),
        "departure": legs[0]["departure"] if legs else None,
        "arrival": legs[-1]["arrival"] if legs else None,
        "minutes": f.duration,
        "stops": f.stops,
        "layovers": [{"airport": code_of(x.airport), "minutes": x.duration} for x in (f.layovers or [])],
        "price": f.price,
        "currency": f.currency or currency,
        "legs": legs,
        "self_transfer": bool(getattr(f, "self_transfer", False)),
        "airline_direct": None,
    }


def search(body):
    origin = airport(body.get("origin"))
    dest = airport(body.get("destination"))
    if origin == dest:
        raise BadRequest("Origem e destino iguais.")
    dep = day(body.get("departure_date"), "Data de ida")
    ret = body.get("return_date")
    ret = day(ret, "Data de volta") if ret else None
    currency = str(body.get("currency", "EUR")).upper()[:3]
    direct = bool(body.get("airline_direct", False))
    limit = max(1, min(40, int(body.get("max_results", 20))))
    key = json.dumps(["s", body], sort_keys=True)
    hit = cache_get(key)
    if hit is not None:
        return dict(hit, cached=True)
    segs = [FlightSegment(departure_airport=[[origin, 0]], arrival_airport=[[dest, 0]], travel_date=dep.isoformat())]
    if ret:
        segs.append(FlightSegment(departure_airport=[[dest, 0]], arrival_airport=[[origin, 0]], travel_date=ret.isoformat()))
    filters = FlightSearchFilters(
        trip_type=TripType.ROUND_TRIP if ret else TripType.ONE_WAY,
        passenger_info=passengers(body),
        flight_segments=segs,
        stops=STOPS.get(str(body.get("stops", "any")), MaxStops.ANY),
        seat_type=CABINS.get(str(body.get("cabin", "economy")), SeatType.ECONOMY),
        sort_by=SORTS.get(str(body.get("sort", "cheapest")), SortBy.CHEAPEST),
        airlines=airlines(body.get("airlines")),
    )
    client = SearchFlights()
    results = client.search(filters, top_n=5, currency=currency, language="pt-BR", country="BR") or []
    out = []
    for r in results[: limit]:
        if isinstance(r, tuple):
            item = flight_json(r[0], currency)
            item["return"] = flight_json(r[-1], currency)
            item["price"] = r[-1].price if r[-1].price is not None else r[0].price
            item["_raw"] = r
        else:
            item = flight_json(r, currency)
            item["_raw"] = r
        out.append(item)
    if direct:
        priced = sorted([x for x in out if x["price"] is not None], key=lambda x: x["price"])[: int(body.get("direct_checks", 6))]
        for item in priced:
            try:
                opts = client.get_booking_options(item["_raw"], filters, currency=currency, language="pt-BR", country="BR") or []
            except Exception:
                opts = []
            airline_opts = [o for o in opts if o.is_airline_direct and o.price is not None]
            if airline_opts:
                best = min(airline_opts, key=lambda o: o.price)
                item["airline_direct"] = True
                item["direct_price"] = best.price
                item["vendor"] = best.vendor_name
                item["booking_url"] = best.booking_url or best.google_click_url
            elif opts:
                item["airline_direct"] = False
    for item in out:
        item.pop("_raw", None)
    data = {"ok": True, "version": VERSION, "flights": out, "query": {k: body.get(k) for k in ("origin", "destination", "departure_date", "return_date")}}
    cache_put(key, data)
    return data


def date_prices(body, origin, dest, start, end, trip_days):
    filters_kw = dict(
        passenger_info=passengers(body),
        stops=STOPS.get(str(body.get("stops", "any")), MaxStops.ANY),
        seat_type=CABINS.get(str(body.get("cabin", "economy")), SeatType.ECONOMY),
        airlines=airlines(body.get("airlines")),
    )
    currency = str(body.get("currency", "EUR")).upper()[:3]
    out = []
    cur = start
    while cur <= end:
        chunk_end = min(end, cur + timedelta(days=90))
        segs = [FlightSegment(departure_airport=[[origin, 0]], arrival_airport=[[dest, 0]], travel_date=cur.isoformat())]
        kw = dict(filters_kw)
        if trip_days:
            segs.append(FlightSegment(departure_airport=[[dest, 0]], arrival_airport=[[origin, 0]], travel_date=(cur + timedelta(days=trip_days)).isoformat()))
            kw.update(trip_type=TripType.ROUND_TRIP, duration=trip_days)
        else:
            kw.update(trip_type=TripType.ONE_WAY)
        f = DateSearchFilters(flight_segments=segs, from_date=cur.isoformat(), to_date=chunk_end.isoformat(), **kw)
        try:
            res = SearchDates().search(f, currency=currency, language="pt-BR", country="BR") or []
        except Exception as e:
            if not out and chunk_end >= end:
                raise
            res = []
        for r in res:
            out.append({
                "date": r.date[0].date().isoformat(),
                "return_date": r.date[1].date().isoformat() if len(r.date) > 1 else None,
                "price": r.price,
                "currency": r.currency or currency,
            })
        cur = chunk_end + timedelta(days=1)
    return out


def dates(body):
    origin = airport(body.get("origin"))
    dest = airport(body.get("destination"))
    start = day(body.get("from_date"), "Data inicial")
    end = day(body.get("to_date"), "Data final")
    if end < start:
        raise BadRequest("A data final é antes da inicial.")
    if (end - start).days > 185:
        raise BadRequest("Período máximo de 6 meses por busca.")
    trip = int(body.get("trip_days") or 0)
    trip = max(1, min(60, trip)) if trip else None
    key = json.dumps(["d", body], sort_keys=True)
    hit = cache_get(key)
    if hit is not None:
        return dict(hit, cached=True)
    rows = date_prices(body, origin, dest, start, end, trip)
    rows.sort(key=lambda r: r["date"])
    data = {"ok": True, "version": VERSION, "dates": rows, "cheapest": sorted(rows, key=lambda r: r["price"])[:10]}
    cache_put(key, data)
    return data


def explore(body):
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
    step = max(1, min(30, int(body.get("sample_every", 7))))
    trip = int(body.get("trip_days") or 0)
    trip = max(1, min(60, trip)) if trip else None
    samples = []
    d = start
    while d <= end and len(samples) < 26:
        samples.append(d)
        d += timedelta(days=step)
    if len(dests) * len(samples) > 240:
        raise BadRequest("Busca grande demais: diminua os destinos ou aumente o intervalo entre datas.")
    key = json.dumps(["e", body], sort_keys=True)
    hit = cache_get(key)
    if hit is not None:
        return dict(hit, cached=True)

    def one(dest):
        rows = []
        for s in samples:
            try:
                rows += date_prices(body, origin, dest, s, s, trip)
            except Exception:
                continue
        rows = [r for r in rows if r["price"]]
        best = min(rows, key=lambda r: r["price"]) if rows else None
        return {"destination": code_of(dest), "best": best, "samples": sorted(rows, key=lambda r: r["date"])}

    with ThreadPoolExecutor(max_workers=int(os.environ.get("WORKERS", "6"))) as ex:
        res = list(ex.map(one, dests))
    res.sort(key=lambda r: (r["best"] is None, r["best"]["price"] if r["best"] else 0))
    data = {"ok": True, "version": VERSION, "results": res, "sampled_dates": [s.isoformat() for s in samples]}
    cache_put(key, data)
    return data


ROUTES = {"/search": search, "/v2/search": search, "/v2/dates": dates, "/v2/explore": explore}


class Handler(BaseHTTPRequestHandler):
    server_version = "metabuscador/2"

    def send(self, code, data):
        raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/health"):
            self.send(200, {"status": "online", "message": "API de voos funcionando", "version": VERSION, "features": ["search", "dates", "explore", "children", "airline_direct"]})
        else:
            self.send(404, {"ok": False, "error": "Não encontrado."})

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        fn = ROUTES.get(self.path.split("?")[0])
        if fn is None:
            return self.send(404, {"ok": False, "error": "Não encontrado."})
        if API_KEY and self.headers.get("X-API-Key", "") != API_KEY:
            return self.send(401, {"ok": False, "error": "Chave inválida."})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self.send(413, {"ok": False, "error": "Corpo inválido."})
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError()
        except Exception:
            return self.send(400, {"ok": False, "error": "JSON inválido."})
        try:
            self.send(200, fn(body))
        except (BadRequest, ValueError) as e:
            self.send(422, {"ok": False, "error": str(e)[:300]})
        except Exception as e:
            self.send(502, {"ok": False, "error": "O Google não respondeu como esperado: " + type(e).__name__ + ": " + str(e)[:300]})

    def log_message(self, fmt, *args):
        if os.environ.get("LOG_REQUESTS"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
