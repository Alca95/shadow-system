from django.contrib import admin
from django.urls import path
from core import views
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.auth import views as auth_views

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
    # 🔑 RESET
    # =========================
    path(
        'password-reset/',
        auth_views.PasswordResetView.as_view(
            template_name='registration/password_reset_form.html',
            email_template_name='registration/password_reset_email.html',
            subject_template_name='registration/password_reset_subject.txt',
            success_url='/password-reset/done/'
        ),
        name='password_reset'
    ),

    path(
        'password-reset/done/',
        auth_views.PasswordResetDoneView.as_view(
            template_name='registration/password_reset_done.html'
        ),
        name='password_reset_done'
    ),

    path(
        'reset/<uidb64>/<token>/',
        auth_views.PasswordResetConfirmView.as_view(
            template_name='registration/password_reset_confirm.html',
            success_url='/reset/done/'
        ),
        name='password_reset_confirm'
    ),

    path(
        'reset/done/',
        auth_views.PasswordResetCompleteView.as_view(
            template_name='registration/password_reset_complete.html'
        ),
        name='password_reset_complete'
    ),
    # =========================
    # 🏠 DASHBOARDS POR ROL
    # =========================
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('bandeja-revision/', views.bandeja_revision_view, name='bandeja_revision'),
    path(
        'bandeja-revision/plano/<int:plano_id>/cerrar/',
        views.cerrar_revision_plano_view,
        name='cerrar_revision_plano'
    ),
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
    
    path('planos/subir/', views.subir_plano_view, name='subir_plano'),
    path('planos/subir/<int:carpeta_id>/', views.subir_plano_view, name='subir_plano_carpeta'),
    path('planos/<int:plano_id>/', views.detalle_plano_view, name='detalle_plano'),
    path('planos/<int:plano_id>/editar-referencia/', views.editar_referencia_plano_view, name='editar_referencia_plano'),

    # ✅ ELIMINACIÓN LÓGICA DE PLANO
    path('planos/<int:plano_id>/cancelar/', views.cancelar_plano_view, name='cancelar_plano'),
    path('planos/<int:plano_id>/guardar/', views.guardar_plano_view, name='guardar_plano'),
    path('planos/<int:plano_id>/resumen/', views.resumen_plano_view, name='resumen_plano'),

    # =========================
    # 🔍 OCR Y VALIDACIÓN
    # =========================
    path('planos/<int:plano_id>/procesar/', views.procesar_plano_view, name='procesar_ocr'),

    path(
        'materiales-detectados/<int:material_id>/editar/',
        views.editar_material_detectado_view,
        name='editar_material_detectado'
    ),
    path(
        'resultados-validacion/<int:resultado_id>/estado-manual/',
        views.cambiar_estado_nr_manual_view,
        name='cambiar_estado_nr_manual'
    ),

    # =========================
    # 🏢 EMPRESAS CONTRATISTAS
    # =========================
    path('empresas-contratistas/', views.empresas_contratistas_view, name='empresas_contratistas'),

    # =========================
    # 📄 REPORTES
    # =========================
    path('reportes/', views.reportes_view, name='reportes'),


    # =========================
    # 🔍 BÚSQUEDA GLOBAL
    # =========================
    path('buscar/', views.buscar_nr_global_view, name='buscar_nr_global'),

    # =========================
    # 📈 ESTADÍSTICAS
    # =========================
    path('estadisticas/', views.estadisticas_view, name='estadisticas'),

    # =========================
    # RESULTADO VALIDACIONES
    # =========================
    path(
        'resultados-validacion/<int:resultado_id>/editar-datos/',
        views.editar_datos_nr_view,
        name='editar_datos_nr'
    ),

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

    path(
    "reportes/print/tiempos/",
    views.reporte_tiempos_print_view,
    name="reporte_tiempos_print"
    ),
    path(
        "reportes/print/materiales/",
        views.reporte_materiales_print_view,
        name="reporte_materiales_print"
    ),

    path(
        "reportes/print/rendimiento/",
        views.reporte_rendimiento_print_view,
        name="reporte_rendimiento_print"
    ),   

    path(
        "reportes/print/efectividad-mensual/",
        views.reporte_efectividad_mensual_print_view,
        name="reporte_efectividad_mensual_print"
    ),

    path(
        "reportes/print/rechazos/",
        views.reporte_rechazos_print_view,
        name="reporte_rechazos_print"
    ), 
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)