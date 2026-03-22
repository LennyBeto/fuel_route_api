"""
routing.py
==========
Handles all communication with external APIs and the core route/fuel logic.

External API calls made per request
------------------------------------
1. Nominatim geocode  (start location)   → 1 call
2. Nominatim geocode  (end location)     → 1 call   [skipped if same city]
3. OSRM route         (full driving route) → 1 call
                                           ─────────
                                           3 calls total (2 geocode + 1 route)

The geocode calls are cached with an LRU cache so repeated queries for the
same city are free.  OSRM returns the full polyline in a single call which
we then sample locally — no extra route API calls.
"""

from __future__ import annotations

import math
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.conf import settings

from .fuel_data import FuelStation, get_registry, haversine_miles

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": settings.NOMINATIM_USER_AGENT})


def _get(url: str, params: dict, timeout: int = 10) -> Any:
    resp = _SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Geocoding (Nominatim)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=512)
def geocode(location: str) -> Tuple[float, float]:
    """
    Return (lat, lon) for a free-text US location string.
    Raises ValueError if not found.
    Cached so repeated calls for the same string are free.
    """
    params = {
        "q": location,
        "format": "json",
        "limit": 1,
        "countrycodes": "us",
    }
    url = f"{settings.NOMINATIM_BASE_URL}/search"

    # Nominatim ToS: max 1 req/second
    time.sleep(0.1)

    data = _get(url, params)
    if not data:
        raise ValueError(f"Could not geocode location: '{location}'. "
                         "Try a more specific address, e.g. 'Chicago, IL'.")
    return float(data[0]["lat"]), float(data[0]["lon"])


# ---------------------------------------------------------------------------
# OSRM routing
# ---------------------------------------------------------------------------

def _decode_polyline(encoded: str) -> List[Tuple[float, float]]:
    """Decode a Google-style encoded polyline into [(lat, lon), ...] pairs."""
    coords: List[Tuple[float, float]] = []
    index = 0
    lat = lon = 0
    while index < len(encoded):
        for is_lon in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / 1e5, lon / 1e5))
    return coords


def fetch_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> Dict[str, Any]:
    """
    Call OSRM and return a normalised dict with:
        distance_miles, duration_seconds, polyline [(lat,lon),...], steps
    Single external call.
    """
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    url = f"{settings.OSRM_BASE_URL}/route/v1/driving/{coords}"
    params = {
        "overview": "full",
        "geometries": "polyline",
        "steps": "false",
        "annotations": "false",
    }
    data = _get(url, params)

    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(
            f"OSRM could not find a route: {data.get('message', data.get('code', 'unknown error'))}"
        )

    route = data["routes"][0]
    distance_meters = route["distance"]
    duration_seconds = route["duration"]
    polyline_encoded = route["geometry"]
    polyline = _decode_polyline(polyline_encoded)

    return {
        "distance_miles": distance_meters / 1609.344,
        "duration_seconds": duration_seconds,
        "polyline": polyline,  # list of (lat, lon)
        "polyline_encoded": polyline_encoded,
    }


# ---------------------------------------------------------------------------
# Route waypoint sampling
# ---------------------------------------------------------------------------

def _sample_waypoints(
    polyline: List[Tuple[float, float]],
    interval_miles: float = 50.0,
) -> List[Tuple[float, float]]:
    """
    Walk the polyline and return points every `interval_miles`.
    This gives us candidate locations at which to search for fuel stations.
    """
    if not polyline:
        return []

    waypoints = [polyline[0]]
    accumulated = 0.0
    prev = polyline[0]

    for pt in polyline[1:]:
        d = haversine_miles(prev[0], prev[1], pt[0], pt[1])
        accumulated += d
        if accumulated >= interval_miles:
            waypoints.append(pt)
            accumulated = 0.0
        prev = pt

    # Always include destination
    if waypoints[-1] != polyline[-1]:
        waypoints.append(polyline[-1])

    return waypoints


# ---------------------------------------------------------------------------
# Cumulative distance along polyline
# ---------------------------------------------------------------------------

def _cumulative_distances(polyline: List[Tuple[float, float]]) -> List[float]:
    """Return list of cumulative miles from start for each polyline point."""
    cum = [0.0]
    for i in range(1, len(polyline)):
        d = haversine_miles(
            polyline[i - 1][0], polyline[i - 1][1],
            polyline[i][0], polyline[i][1],
        )
        cum.append(cum[-1] + d)
    return cum


def _nearest_polyline_point(
    polyline: List[Tuple[float, float]],
    cum_dist: List[float],
    lat: float,
    lon: float,
) -> Tuple[int, float]:
    """Return (index, cumulative_distance) of the polyline point nearest to (lat, lon)."""
    best_i, best_d = 0, float("inf")
    for i, (plat, plon) in enumerate(polyline):
        d = haversine_miles(lat, lon, plat, plon)
        if d < best_d:
            best_d = d
            best_i = i
    return best_i, cum_dist[best_i]


# ---------------------------------------------------------------------------
# Core fuel-stop planning algorithm
# ---------------------------------------------------------------------------

def plan_fuel_stops(
    polyline: List[Tuple[float, float]],
    total_distance_miles: float,
    vehicle_range_miles: float = 500.0,
    mpg: float = 10.0,
    safety_factor: float = 0.90,
    search_radius_miles: float = 60.0,
) -> List[Dict[str, Any]]:
    """
    Greedy cheapest-fuel algorithm:

    Starting from the origin with a full tank, scan ahead for fuel windows
    and pick the cheapest station reachable before we'd run dry.

    Returns a list of fuel-stop dicts sorted by position along the route.
    Each dict contains station info + gallons_purchased + cost + distance_from_start.
    """
    registry = get_registry()
    effective_range = vehicle_range_miles * safety_factor  # miles per full tank (safety margin)
    tank_capacity_gallons = vehicle_range_miles / mpg  # e.g. 500/10 = 50 gal

    cum_dist = _cumulative_distances(polyline)
    stops: List[Dict[str, Any]] = []

    current_mile = 0.0        # how far along the route we are
    fuel_in_tank_gallons = tank_capacity_gallons  # start full

    # We'll work in terms of "miles remaining in tank"
    miles_remaining = effective_range  # usable range from start

    while True:
        # How far can we reach from current position?
        reach_mile = current_mile + miles_remaining

        # If we can reach the destination, we're done
        if reach_mile >= total_distance_miles:
            break

        # ── Find cheapest station in the window ──────────────────────────────
        # Window: from "must stop before running dry" back to current position.
        # We look for stations between current_mile + 1 mile and reach_mile.
        # Sample the polyline every 40 miles in that window and find candidates.

        window_start_mile = current_mile
        window_end_mile = reach_mile

        # Collect candidate stations along this window
        candidates: List[Tuple[float, float, FuelStation]] = []
        # (mile_on_route, detour_miles, station)

        sample_interval = 40.0
        sample_mile = window_start_mile + sample_interval
        while sample_mile <= window_end_mile:
            # Find polyline point nearest this mileage
            target_idx = _mile_to_polyline_index(cum_dist, sample_mile)
            pt = polyline[target_idx]

            nearby = registry.nearest(
                pt[0], pt[1],
                k=10,
                max_distance_miles=search_radius_miles,
            )
            for detour_dist, station in nearby:
                # Approximate route position of this station
                _, station_route_mile = _nearest_polyline_point(
                    polyline, cum_dist, station.lat, station.lon
                )
                if window_start_mile < station_route_mile <= window_end_mile:
                    candidates.append((station_route_mile, detour_dist, station))

            sample_mile += sample_interval

        if not candidates:
            # No station found in normal window — desperate search: use
            # whatever is closest to the furthest reachable point
            target_idx = _mile_to_polyline_index(cum_dist, min(reach_mile, total_distance_miles))
            pt = polyline[target_idx]
            hit = registry.cheapest_near(pt[0], pt[1], max_distance_miles=120.0)
            if hit:
                detour_dist, station = hit
                candidates.append((reach_mile - 10, detour_dist, station))
            else:
                # Give up — no station reachable
                break

        # Pick the cheapest station (by price, tie-break by proximity to route)
        best_candidate = min(candidates, key=lambda c: (c[2].retail_price, c[1]))
        stop_mile, detour_miles, station = best_candidate

        # How much fuel did we use to reach this stop?
        miles_driven = stop_mile - current_mile
        gallons_used = miles_driven / mpg
        fuel_in_tank_gallons = max(0.0, fuel_in_tank_gallons - gallons_used)

        # Fill up completely
        gallons_to_fill = tank_capacity_gallons - fuel_in_tank_gallons
        cost_at_stop = gallons_to_fill * station.retail_price

        stops.append({
            "stop_number": len(stops) + 1,
            "distance_from_start_miles": round(stop_mile, 1),
            "station": station.to_dict(),
            "detour_miles": round(detour_miles, 1),
            "gallons_purchased": round(gallons_to_fill, 2),
            "cost_usd": round(cost_at_stop, 2),
            "price_per_gallon": station.retail_price,
        })

        # Update state: full tank, new position
        fuel_in_tank_gallons = tank_capacity_gallons
        miles_remaining = effective_range
        current_mile = stop_mile

    return stops


def _mile_to_polyline_index(cum_dist: List[float], target_mile: float) -> int:
    """Binary search for the polyline index closest to target_mile."""
    lo, hi = 0, len(cum_dist) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if cum_dist[mid] < target_mile:
            lo = mid + 1
        else:
            hi = mid
    return lo


# ---------------------------------------------------------------------------
# High-level entry point
# ---------------------------------------------------------------------------

def build_route_response(
    start: str,
    end: str,
    vehicle_range_miles: float = 500.0,
    mpg: float = 10.0,
) -> Dict[str, Any]:
    """
    Full pipeline:
      1. Geocode start & end         (2 Nominatim calls, cached)
      2. Fetch OSRM driving route    (1 OSRM call)
      3. Plan optimal fuel stops     (pure Python, no network)
      4. Assemble response dict
    """
    # 1. Geocode
    start_lat, start_lon = geocode(start)
    end_lat, end_lon = geocode(end)

    # 2. Route
    route = fetch_route(start_lat, start_lon, end_lat, end_lon)
    polyline = route["polyline"]
    total_miles = route["distance_miles"]
    duration_seconds = route["duration_seconds"]

    # 3. Fuel stops
    safety_factor = getattr(settings, "RANGE_SAFETY_FACTOR", 0.90)
    stops = plan_fuel_stops(
        polyline=polyline,
        total_distance_miles=total_miles,
        vehicle_range_miles=vehicle_range_miles,
        mpg=mpg,
        safety_factor=safety_factor,
    )

    # 4. Cost summary
    total_miles_driven = total_miles
    total_gallons = total_miles_driven / mpg
    total_fuel_cost = sum(s["cost_usd"] for s in stops)

    # If stops don't cover the full trip (short trip, no stop needed):
    if not stops:
        # Single fill-up at origin assumed (but cost still calculated)
        total_fuel_cost = total_gallons * _estimate_origin_price(start_lat, start_lon)

    return {
        "origin": {
            "query": start,
            "lat": round(start_lat, 6),
            "lon": round(start_lon, 6),
        },
        "destination": {
            "query": end,
            "lat": round(end_lat, 6),
            "lon": round(end_lon, 6),
        },
        "route": {
            "distance_miles": round(total_miles, 1),
            "estimated_duration_hours": round(duration_seconds / 3600, 2),
            "polyline_encoded": route["polyline_encoded"],
            "polyline": [
                {"lat": lat, "lon": lon}
                for lat, lon in _decimate_polyline(polyline, max_points=200)
            ],
        },
        "vehicle": {
            "range_miles": vehicle_range_miles,
            "mpg": mpg,
        },
        "fuel_stops": stops,
        "summary": {
            "num_fuel_stops": len(stops),
            "total_gallons_needed": round(total_gallons, 2),
            "total_fuel_cost_usd": round(total_fuel_cost, 2),
            "avg_price_per_gallon": (
                round(total_fuel_cost / total_gallons, 3) if total_gallons > 0 else 0
            ),
        },
    }


def _estimate_origin_price(lat: float, lon: float) -> float:
    """Fallback: cheapest nearby station price for no-stop trips."""
    registry = get_registry()
    hit = registry.cheapest_near(lat, lon, max_distance_miles=100.0)
    return hit[1].retail_price if hit else 3.50


def _decimate_polyline(
    polyline: List[Tuple[float, float]],
    max_points: int = 200,
) -> List[Tuple[float, float]]:
    """Thin a polyline to at most max_points for the JSON response."""
    if len(polyline) <= max_points:
        return polyline
    step = len(polyline) / max_points
    return [polyline[int(i * step)] for i in range(max_points)] + [polyline[-1]]