from django.urls import path
from .views import (
    login_view,
    dashboard_view,
    logout_view,
    subir_plano_view,
    detalle_plano_view,
    procesar_plano_view,
    cancelar_plano_view,
)

urlpatterns = [
    path("", login_view, name="login"),
    path("dashboard/", dashboard_view, name="dashboard"),
    path("logout/", logout_view, name="logout"),

    path("planos/subir/", subir_plano_view, name="subir_plano"),
    path("planos/<int:plano_id>/", detalle_plano_view, name="detalle_plano"),
    path("planos/<int:plano_id>/procesar/", procesar_plano_view, name="procesar_plano"),
    path("planos/<int:plano_id>/cancelar/", cancelar_plano_view, name="cancelar_plano"),
]