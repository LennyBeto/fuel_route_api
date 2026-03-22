"""
views.py
========
DRF views for the Fuel Route API.

Endpoints
---------
POST /api/route/
    Body: { "start": "...", "end": "...", "vehicle_range_miles": 500, "mpg": 10 }
    Returns a full route with optimal fuel stops and cost breakdown.

GET  /api/health/
    Quick liveness check.

GET  /api/stations/info/
    Returns stats about the loaded fuel station dataset.
"""

import logging
import time

import requests
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from .fuel_data import get_registry
from .routing import build_route_response
from .serializers import RouteRequestSerializer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@api_view(["GET"])
def health(request: Request) -> Response:
    """Simple liveness / readiness probe."""
    registry = get_registry()
    return Response(
        {
            "status": "ok",
            "fuel_stations_loaded": registry.count,
        }
    )


# ---------------------------------------------------------------------------
# Dataset info
# ---------------------------------------------------------------------------

@api_view(["GET"])
def stations_info(request: Request) -> Response:
    """Return metadata about the loaded fuel price dataset."""
    registry = get_registry()
    return Response(
        {
            "total_us_stations": registry.count,
            "note": (
                "Stations are deduplicated by name+city+state; "
                "lowest retail price is kept per location."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Main route endpoint
# ---------------------------------------------------------------------------

@api_view(["POST"])
def get_route(request: Request) -> Response:
    """
    Plan an optimal fuel-stop route between two US locations.

    Request body (JSON)
    -------------------
    {
        "start": "Los Angeles, CA",
        "end":   "New York, NY",
        "vehicle_range_miles": 500,   // optional, default 500
        "mpg": 10                     // optional, default 10
    }

    Response (JSON)
    ---------------
    {
        "origin":      { "query", "lat", "lon" },
        "destination": { "query", "lat", "lon" },
        "route": {
            "distance_miles",
            "estimated_duration_hours",
            "polyline_encoded",    // Google-encoded polyline for map libs
            "polyline": [{"lat", "lon"}, ...]
        },
        "vehicle":   { "range_miles", "mpg" },
        "fuel_stops": [
            {
                "stop_number",
                "distance_from_start_miles",
                "station": { "name", "city", "state", "address",
                             "retail_price", "lat", "lon" },
                "detour_miles",
                "gallons_purchased",
                "cost_usd",
                "price_per_gallon"
            }, ...
        ],
        "summary": {
            "num_fuel_stops",
            "total_gallons_needed",
            "total_fuel_cost_usd",
            "avg_price_per_gallon"
        }
    }
    """
    serializer = RouteRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(
            {"errors": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    validated = serializer.validated_data
    t0 = time.perf_counter()

    try:
        result = build_route_response(
            start=validated["start"],
            end=validated["end"],
            vehicle_range_miles=validated["vehicle_range_miles"],
            mpg=validated["mpg"],
        )
    except ValueError as exc:
        return Response(
            {"error": str(exc)},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    except requests.exceptions.ConnectionError:
        return Response(
            {
                "error": (
                    "Could not connect to geocoding / routing service. "
                    "Check your internet connection and try again."
                )
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except requests.exceptions.Timeout:
        return Response(
            {"error": "External routing service timed out. Please try again."},
            status=status.HTTP_504_GATEWAY_TIMEOUT,
        )
    except Exception as exc:
        logger.exception("Unexpected error in get_route: %s", exc)
        return Response(
            {"error": "An unexpected error occurred. See server logs for details."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    elapsed_ms = round((time.perf_counter() - t0) * 1000)
    result["_meta"] = {"processing_time_ms": elapsed_ms}

    return Response(result, status=status.HTTP_200_OK)