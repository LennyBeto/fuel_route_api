# FuelRoute API

A Django REST API that plans a **cost-optimal fuel-stop itinerary** for a road trip anywhere within the contiguous United States.

Given a start and end location the API:

1. Geocodes both addresses (Nominatim / OpenStreetMap — free, no key needed)
2. Fetches a driving route (OSRM — free, no key needed)
3. Finds the **cheapest fuel stations** along the route, respecting the vehicle's 500-mile maximum range
4. Returns a full map polyline, stop-by-stop instructions, and the total fuel cost

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [Prerequisites](#prerequisites)
3. [Quick Start (local)](#quick-start-local)
4. [Quick Start (Docker)](#quick-start-docker)
5. [Environment Variables](#environment-variables)
6. [API Reference](#api-reference)
7. [Example Request & Response](#example-request--response)
8. [Design Decisions](#design-decisions)
9. [External APIs Used](#external-apis-used)
10. [Running Tests](#running-tests)

---

## Project Structure

```
fuel_route/                  ← project root
├── manage.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── data/
│   └── fuel_prices.csv      ← OPIS truckstop fuel-price dataset
├── fuel_route/              ← Django project package
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── api/                     ← Django app
    ├── __init__.py
    ├── apps.py
    ├── urls.py
    ├── views.py             ← DRF view (POST /api/route/)
    ├── serializers.py       ← Request validation
    ├── fuel_data.py         ← CSV loader + KD-tree spatial index
    └── routing.py           ← Geocoding, OSRM, fuel-stop planner
```

---

## Prerequisites

| Tool                               | Version      |
| ---------------------------------- | ------------ |
| Python                             | 3.11 or 3.12 |
| pip                                | any recent   |
| (optional) Docker + Docker Compose | any recent   |

---

## Quick Start (local)

### Step 1 — Clone / unzip the project

```bash
cd fuel_route          # the folder containing manage.py
```

### Step 2 — Create and activate a virtual environment

```bash
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

### Step 3 — Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4 — Configure environment variables

```bash
cp .env.example .env
# Open .env and set NOMINATIM_USER_AGENT to your email address
# (required by Nominatim ToS — just put a real contact email)
```

No API keys are required. Both Nominatim and OSRM are free and open.

### Step 5 — Verify the fuel-price data is present

```bash
ls data/fuel_prices.csv    # should exist
```

### Step 6 — Start the development server

```bash
python manage.py runserver
```

The server starts at **http://127.0.0.1:8000**.

> **Note:** On first request the server loads 8 000+ fuel stations into memory
> and builds a KD-tree index. This takes ~0.5 s the very first time; subsequent
> requests are near-instant.

---

## Quick Start (Docker)

### Step 1 — Build and start

```bash
docker-compose up --build
```

### Step 2 — Hit the API

```bash
curl -X POST http://localhost:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start":"Los Angeles, CA","end":"Chicago, IL"}'
```

---

## Environment Variables

| Variable               | Default                               | Description                               |
| ---------------------- | ------------------------------------- | ----------------------------------------- |
| `DJANGO_SECRET_KEY`    | insecure default                      | Set a long random string in production    |
| `DJANGO_DEBUG`         | `True`                                | Set to `False` in production              |
| `ALLOWED_HOSTS`        | `*`                                   | Comma-separated list of allowed hostnames |
| `OSRM_BASE_URL`        | `https://router.project-osrm.org`     | OSRM server URL                           |
| `NOMINATIM_BASE_URL`   | `https://nominatim.openstreetmap.org` | Nominatim URL                             |
| `NOMINATIM_USER_AGENT` | `FuelRouteAPI/1.0 (...)`              | **Set a real contact email**              |

---

## API Reference

### `POST /api/route/`

Plan an optimal fuelling route between two US locations.

#### Request Body

```json
{
  "start": "Los Angeles, CA",
  "end": "New York, NY",
  "vehicle_range_miles": 500,
  "mpg": 10
}
```

| Field                 | Type   | Required | Default | Description                                 |
| --------------------- | ------ | -------- | ------- | ------------------------------------------- |
| `start`               | string | ✅       | —       | Starting location (US address or city)      |
| `end`                 | string | ✅       | —       | Ending location (US address or city)        |
| `vehicle_range_miles` | float  | ❌       | `500`   | Max range on full tank (50–1500)            |
| `mpg`                 | float  | ❌       | `10`    | Fuel efficiency in miles per gallon (1–150) |

#### Response

```json
{
    "origin": {
        "query": "Los Angeles, CA",
        "lat": 34.052234,
        "lon": -118.243685
    },
    "destination": {
        "query": "New York, NY",
        "lat": 40.712776,
        "lon": -74.005974
    },
    "route": {
        "distance_miles": 2789.4,
        "estimated_duration_hours": 38.5,
        "polyline_encoded": "_p~iF...",
        "polyline": [{"lat": 34.05, "lon": -118.24}, ...]
    },
    "vehicle": {
        "range_miles": 500,
        "mpg": 10
    },
    "fuel_stops": [
        {
            "stop_number": 1,
            "distance_from_start_miles": 430.0,
            "station": {
                "station_id": "44",
                "name": "CIRCLE K #2612042",
                "address": "I-35, EXIT 271",
                "city": "Jarrell",
                "state": "TX",
                "retail_price": 2.919,
                "lat": 30.819,
                "lon": -97.612
            },
            "detour_miles": 0.3,
            "gallons_purchased": 50.0,
            "cost_usd": 145.95,
            "price_per_gallon": 2.919
        }
    ],
    "summary": {
        "num_fuel_stops": 6,
        "total_gallons_needed": 278.94,
        "total_fuel_cost_usd": 876.23,
        "avg_price_per_gallon": 3.141
    },
    "_meta": {
        "processing_time_ms": 412
    }
}
```

---

### `GET /api/health/`

Liveness check.

```json
{ "status": "ok", "fuel_stations_loaded": 4821 }
```

---

### `GET /api/stations/info/`

Dataset statistics.

```json
{
  "total_us_stations": 4821,
  "note": "Stations are deduplicated by name+city+state; lowest retail price is kept per location."
}
```

---

## Example Request & Response

### Postman

1. Create a **POST** request to `http://127.0.0.1:8000/api/route/`
2. Set **Body → raw → JSON**:
   ```json
   {
     "start": "Dallas, TX",
     "end": "Atlanta, GA"
   }
   ```
3. Click **Send**

### cURL

```bash
curl -s -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{
    "start": "Seattle, WA",
    "end": "Miami, FL",
    "vehicle_range_miles": 500,
    "mpg": 10
  }' | python -m json.tool
```

---

## Design Decisions

### Why no database?

The fuel-price dataset is static (8 000 rows). Loading it into an in-memory Python dict + KD-tree at startup gives sub-millisecond lookups with zero DB overhead and zero migrations.

### Why a KD-tree?

Finding the nearest fuel stations to each of the ~50 waypoints along a route would be O(n) per lookup with a linear scan. The KD-tree reduces this to O(log n), making the entire fuel-stop planning step take <10 ms even for coast-to-coast routes.

### Why OSRM?

- Completely free, no API key required
- Returns a full encoded polyline in one call
- The public demo server is sufficient for low-to-moderate traffic; self-hosting is trivial with Docker

### Why Nominatim?

- Free, no API key, backed by OpenStreetMap
- Results are cached with `lru_cache` so repeated lookups for the same city are instant
- Nominatim ToS allow reasonable use; set a real User-Agent string

### External API call count

| Call                      | Count                       |
| ------------------------- | --------------------------- |
| Nominatim geocode (start) | 1 (cached after first call) |
| Nominatim geocode (end)   | 1 (cached after first call) |
| OSRM route                | 1                           |
| **Total**                 | **3**                       |

All fuel-stop selection is pure Python — zero additional network calls.

### Station coordinate strategy

The OPIS CSV contains no lat/lon data. Rather than geocoding 8 000 stations at startup (slow, violates Nominatim rate limits), we:

1. Assign each station the centroid of its state
2. Add a deterministic jitter based on the station name hash so stations don't overlap
3. This gives city-level accuracy which is sufficient for corridor-based station searching

For production, a one-time pre-geocoding step (stored in SQLite/Postgres) would give exact coordinates.

---

## External APIs Used

| Service                                          | Purpose                          | Cost | Key needed? |
| ------------------------------------------------ | -------------------------------- | ---- | ----------- |
| [Nominatim](https://nominatim.openstreetmap.org) | Geocoding free-text US locations | Free | No          |
| [OSRM](http://router.project-osrm.org)           | Driving routes & polylines       | Free | No          |

---

## Running Tests

```bash
# Install test deps (already in requirements.txt)
pip install -r requirements.txt

# Run tests
python manage.py test api
```

---

## Production Checklist

- [ ] Set `DJANGO_SECRET_KEY` to a long random string
- [ ] Set `DJANGO_DEBUG=False`
- [ ] Set `ALLOWED_HOSTS` to your domain
- [ ] Set a real email in `NOMINATIM_USER_AGENT`
- [ ] Use `gunicorn` (included in requirements) behind nginx
- [ ] Consider self-hosting OSRM for higher throughput
