
from datetime import date

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fast_flights import (
    FlightQuery,
    Passengers,
    create_query,
    get_flights,
)

app = FastAPI(
    title="Minha API de Voos",
    description="Pesquisa de voos baseada no Google Flights",
    version="1.0.0",
)


class FlightSearch(BaseModel):
    origin: str
    destination: str
    departure_date: date
    adults: int = 1
    currency: str = "EUR"


@app.get("/")
def home():
    return {
        "status": "online",
        "message": "API de voos funcionando",
        "docs": "/docs",
    }


@app.post("/search")
def search_flights(search: FlightSearch):
    origin = search.origin.strip().upper()
    destination = search.destination.strip().upper()

    if len(origin) != 3 or len(destination) != 3:
        raise HTTPException(
            status_code=400,
            detail="Use códigos IATA de aeroportos, como GRU e MXP."
        )

    if search.adults < 1 or search.adults > 9:
        raise HTTPException(
            status_code=400,
            detail="O número de adultos deve estar entre 1 e 9."
        )

    try:
        query = create_query(
            flights=[
                FlightQuery(
                    date=search.departure_date.isoformat(),
                    from_airport=origin,
                    to_airport=destination,
                )
            ],
            seat="economy",
            trip="one-way",
            passengers=Passengers(adults=search.adults),
            currency=search.currency.upper(),
            language="en",
        )

        result = get_flights(query)

        return {
            "origin": origin,
            "destination": destination,
            "departure_date": search.departure_date.isoformat(),
            "currency": search.currency.upper(),
            "results": str(result),
        }

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Não foi possível pesquisar os voos: {str(exc)}"
        )
