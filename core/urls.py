from django.contrib import admin
from django.urls import path
from core import views
from django.conf import settings
from django.conf.urls.static import static

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
    path('carpetas/<int:carpeta_id>/eliminar/', views.eliminar_carpeta_view, name='eliminar_carpeta'),

    # =========================
    # 📄 PLANOS
    # =========================
    path('planos/subir/<int:carpeta_id>/', views.subir_plano_view, name='subir_plano'),
    path('planos/<int:plano_id>/', views.detalle_plano_view, name='detalle_plano'),
    path('planos/subir/', views.subir_plano_view, name='subir_plano'),
    path('planos/subir/<int:carpeta_id>/', views.subir_plano_view, name='subir_plano_carpeta'),

    # ✅ ELIMINACIÓN LÓGICA DE PLANO
    path('planos/<int:plano_id>/cancelar/', views.cancelar_plano_view, name='cancelar_plano'),
    path('planos/<int:plano_id>/guardar/', views.guardar_plano_view, name='guardar_plano'),
    path('planos/<int:plano_id>/resumen/', views.resumen_plano_view, name='resumen_plano'),

    # =========================
    # 🔍 OCR Y VALIDACIÓN
    # =========================
    path('planos/<int:plano_id>/procesar/', views.procesar_plano_view, name='procesar_ocr'),
    # path('planos/<int:plano_id>/validar/', views.validar_plano_view, name='validar_plano'),


    # =========================
    # 🏢 EMPRESAS CONTRATISTAS
    # =========================
    path('empresas-contratistas/', views.empresas_contratistas_view, name='empresas_contratistas'),

    # =========================
    # 📄 REPORTES
    # =========================
    path('reportes/', views.reportes_view, name='reportes'),

    # =========================
    # 📈 ESTADÍSTICAS
    # =========================
    path('estadisticas/', views.estadisticas_view, name='estadisticas'),

    # =========================
    # 🛡️ AUDITORÍA
    # =========================
    path('auditoria/', views.auditoria_view, name='auditoria'),

    # =========================
    # 👥 GESTIÓN DE USUARIOS (ADMIN)
    # =========================
    path('usuarios/', views.gestion_usuarios_view, name='gestion_usuarios'),
    path('usuarios/crear/', views.crear_usuario_view, name='crear_usuario'),
    path('usuarios/<int:user_id>/editar/', views.editar_usuario_view, name='editar_usuario'),
    path('usuarios/<int:user_id>/toggle/', views.toggle_usuario_activo_view, name='toggle_usuario'),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)