from django.urls import path
from . import views

urlpatterns = [
    path("route/",         views.get_route,      name="get_route"),
    path("health/",        views.health,          name="health"),
    path("stations/info/", views.stations_info,   name="stations_info"),
]