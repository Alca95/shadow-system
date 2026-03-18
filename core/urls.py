from django.contrib import admin
from django.urls import path
from core import views

urlpatterns = [
    # =========================
    # 🔐 ADMIN DJANGO
    # =========================
    path('admin/', admin.site.urls),

    # =========================
    # 🔑 AUTENTICACIÓN
    # =========================
    path('', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # =========================
    # 🏠 DASHBOARDS POR ROL
    # =========================
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('dashboard/funcionario/', views.dashboard_funcionario_view, name='dashboard_funcionario'),
    path('dashboard/contratista/', views.dashboard_contratista_view, name='dashboard_contratista'),

    # =========================
    # 📁 CARPETAS
    # =========================
    path('carpetas/', views.carpetas_view, name='carpetas'),
    path('carpetas/crear/', views.crear_carpeta_view, name='crear_carpeta'),
    path('carpetas/<int:carpeta_id>/', views.detalle_carpeta_view, name='detalle_carpeta'),

    # =========================
    # 📄 PLANOS
    # =========================
    path('planos/subir/<int:carpeta_id>/', views.subir_plano_view, name='subir_plano'),
    path('planos/<int:plano_id>/', views.detalle_plano_view, name='detalle_plano'),
    path('planos/subir/', views.subir_plano_view, name='subir_plano'),
    path('planos/subir/<int:carpeta_id>/', views.subir_plano_view, name='subir_plano_carpeta'),

    # =========================
    # 🔍 OCR Y VALIDACIÓN
    # =========================
    path('planos/<int:plano_id>/procesar/', views.procesar_plano_view, name='procesar_ocr'),
    #path('planos/<int:plano_id>/validar/', views.validar_plano_view, name='validar_plano'),

    # =========================
    # 👥 GESTIÓN DE USUARIOS (ADMIN)
    # =========================
    path('usuarios/', views.gestion_usuarios_view, name='gestion_usuarios'),
    path('usuarios/crear/', views.crear_usuario_view, name='crear_usuario'),
    path('usuarios/<int:user_id>/editar/', views.editar_usuario_view, name='editar_usuario'),
    path('usuarios/<int:user_id>/toggle/', views.toggle_usuario_activo_view, name='toggle_usuario'),

]