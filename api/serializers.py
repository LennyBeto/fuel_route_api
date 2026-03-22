"""
serializers.py
==============
DRF serializers for validating the incoming request and shaping the output.
"""

from rest_framework import serializers
from django.conf import settings


class RouteRequestSerializer(serializers.Serializer):
    """Validates the POST body for the /api/route/ endpoint."""

    start = serializers.CharField(
        max_length=200,
        help_text=(
            "Starting location within the USA. "
            "E.g. 'Los Angeles, CA' or '350 Fifth Ave, New York, NY'."
        ),
    )
    end = serializers.CharField(
        max_length=200,
        help_text=(
            "Ending location within the USA. "
            "E.g. 'Chicago, IL' or 'Houston, TX'."
        ),
    )
    vehicle_range_miles = serializers.FloatField(
        required=False,
        default=500.0,
        min_value=50.0,
        max_value=1500.0,
        help_text="Maximum range of the vehicle on a full tank (default 500).",
    )
    mpg = serializers.FloatField(
        required=False,
        default=10.0,
        min_value=1.0,
        max_value=150.0,
        help_text="Fuel efficiency in miles per gallon (default 10).",
    )

    def validate_start(self, value: str) -> str:
        return value.strip()

    def validate_end(self, value: str) -> str:
        return value.strip()

    def validate(self, data: dict) -> dict:
        if data["start"].lower() == data["end"].lower():
            raise serializers.ValidationError(
                "Start and end locations must be different."
            )
        return data