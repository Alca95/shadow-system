from django.urls import path
from .views import (
    login_view,
    dashboard_view,
    dashboard_contratista_view,
    logout_view,
    carpetas_view,
    crear_carpeta_view,
    detalle_carpeta_view,
    eliminar_carpeta_view,
    subir_plano_view,
    detalle_plano_view,
    procesar_plano_view,
    cancelar_plano_view,
)

urlpatterns = [
    # Autenticación
    path("", login_view, name="login"),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),

    # Inicio
    path("dashboard/", dashboard_view, name="dashboard"),
    path("dashboard-contratista/", dashboard_contratista_view, name="dashboard_contratista"),

    # Carpetas
    path("carpetas/", carpetas_view, name="carpetas"),
    path("carpetas/crear/", crear_carpeta_view, name="crear_carpeta"),
    path("carpetas/<int:carpeta_id>/", detalle_carpeta_view, name="detalle_carpeta"),
    path("carpetas/<int:carpeta_id>/eliminar/", eliminar_carpeta_view, name="eliminar_carpeta"),

    # Planos
    path("planos/subir/", subir_plano_view, name="subir_plano"),
    path("carpetas/<int:carpeta_id>/subir-plano/", subir_plano_view, name="subir_plano_carpeta"),
    path("planos/<int:plano_id>/", detalle_plano_view, name="detalle_plano"),
    path("planos/<int:plano_id>/procesar/", procesar_plano_view, name="procesar_plano"),
    path("planos/<int:plano_id>/cancelar/", cancelar_plano_view, name="cancelar_plano"),
]