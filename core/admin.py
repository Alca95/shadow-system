from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import (
    EmpresaContratista,
    PerfilUsuario,
    Carpeta,
    Reclamo,
    NRMateriales,
    ItemNRMateriales,
    Plano,
    ResultadoValidacionPlano,
    Auditoria,
    StockMaterial,
)
from .ocr_extract import ocr_text_from_file, extract_nrs, extract_fecha_plano
from .validator import validar_plano_contra_bd
from .services import validar_plano_completo


# =========================
# USER + PERFIL USUARIO
# =========================

class PerfilUsuarioInline(admin.StackedInline):
    model = PerfilUsuario
    can_delete = False
    extra = 0
    fk_name = "user"


class CustomUserAdmin(UserAdmin):
    inlines = [PerfilUsuarioInline]

    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "is_staff",
        "get_rol",
        "get_empresa",
    )

    def get_rol(self, obj):
        perfil = getattr(obj, "perfil", None)
        return perfil.rol if perfil else "-"
    get_rol.short_description = "Rol"

    def get_empresa(self, obj):
        perfil = getattr(obj, "perfil", None)
        return perfil.empresa if perfil and perfil.empresa else "-"
    get_empresa.short_description = "Empresa"


@admin.register(PerfilUsuario)
class PerfilUsuarioAdmin(admin.ModelAdmin):
    list_display = ("user", "rol", "empresa", "activo")
    list_filter = ("rol", "activo")
    search_fields = ("user__username", "user__email")


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


# =========================
# ACCIONES DE ADMIN PARA PLANOS
# =========================

@admin.action(description="Procesar OCR del plano (extraer NR y fecha)")
def procesar_ocr(modeladmin, request, queryset):
    for plano in queryset:
        try:
            file_path = plano.archivo.path
            text = ocr_text_from_file(file_path)

            nrs = extract_nrs(text)
            fecha = extract_fecha_plano(text)

            plano.texto_ocr = text
            plano.nr_detectados = ",".join(nrs) if nrs else None
            plano.fecha_plano = fecha
            plano.procesado = True
            plano.procesado_por = str(request.user)
            plano.save()

            Auditoria.objects.create(
                plano=plano,
                carpeta=plano.carpeta,
                usuario=str(request.user),
                accion="PROCESAR_OCR",
                descripcion=f"OCR OK -> nr_detectados={plano.nr_detectados} fecha_plano={plano.fecha_plano}",
                entidad="Plano",
                entidad_id=str(plano.id),
            )

        except Exception as e:
            plano.estado = "EN_ESPERA"
            plano.save()

            Auditoria.objects.create(
                plano=plano,
                carpeta=plano.carpeta,
                usuario=str(request.user),
                accion="PROCESAR_OCR_ERROR",
                descripcion=f"OCR ERROR: {e}",
                entidad="Plano",
                entidad_id=str(plano.id),
            )


@admin.action(description="Validar NR detectados contra la BD (marca válidos/desconocidos)")
def validar_nrs(modeladmin, request, queryset):
    for plano in queryset:
        res = validar_plano_contra_bd(plano)

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="VALIDAR_NR_BD",
            descripcion=f"VALIDACIÓN NR -> estado={res['estado']} validos={res['validos']} desconocidos={res['desconocidos']}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )


@admin.action(description="Validar plano completo contra Reclamo")
def validar_plano_completo_admin(modeladmin, request, queryset):
    for plano in queryset:
        res = validar_plano_completo(plano)

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="VALIDAR_PLANO_COMPLETO",
            descripcion=f"VALIDACIÓN COMPLETA -> estado={res['estado']} motivos={res['motivos']}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )


# =========================
# INLINES
# =========================

class ItemNRMaterialesInline(admin.TabularInline):
    model = ItemNRMateriales
    extra = 1


class ResultadoValidacionPlanoInline(admin.TabularInline):
    model = ResultadoValidacionPlano
    extra = 0
    readonly_fields = (
        "nr_detectado",
        "nr_normalizado",
        "nr_materiales_encontrado",
        "reclamo_encontrado",
        "estado_resultado",
        "ciudad_ok",
        "zona_ok",
        "fecha_ok",
        "motivo_resultado",
        "observacion",
    )
    can_delete = False


# =========================
# MODELOS DEL SISTEMA
# =========================

@admin.register(EmpresaContratista)
class EmpresaContratistaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "ruc", "telefono", "email", "activo", "fecha_creacion")
    search_fields = ("nombre", "ruc", "contacto", "telefono", "email")
    list_filter = ("activo",)
    ordering = ("nombre",)


@admin.register(Carpeta)
class CarpetaAdmin(admin.ModelAdmin):
    list_display = (
        "codigo_carpeta",
        "mes",
        "anio",
        "empresa",
        "estado",
        "creada_por",
        "eliminada",
        "fecha_creacion",
    )
    search_fields = ("codigo_carpeta", "empresa__nombre", "creada_por")
    list_filter = ("estado", "anio", "mes", "empresa", "eliminada")
    readonly_fields = ("codigo_carpeta", "fecha_creacion", "fecha_eliminacion")
    ordering = ("-anio", "-mes", "-fecha_creacion")


@admin.register(Reclamo)
class ReclamoAdmin(admin.ModelAdmin):
    list_display = (
        "numero_reclamo",
        "nombre_cliente",
        "ciudad",
        "zona",
        "fecha_reclamo",
        "empresa",
    )
    search_fields = (
        "numero_reclamo",
        "nombre_cliente",
        "ciudad",
        "zona",
    )
    list_filter = ("ciudad", "zona", "empresa", "fecha_reclamo")
    ordering = ("-fecha_reclamo",)


@admin.register(NRMateriales)
class NRMaterialesAdmin(admin.ModelAdmin):
    list_display = (
        "numero_nr",
        "reclamo",
        "ciudad",
        "zona",
        "fecha_trabajo",
    )
    search_fields = (
        "numero_nr",
        "reclamo__numero_reclamo",
        "ciudad",
        "zona",
    )
    list_filter = ("ciudad", "zona", "fecha_trabajo")
    inlines = [ItemNRMaterialesInline]
    ordering = ("-fecha_trabajo",)


@admin.register(ItemNRMateriales)
class ItemNRMaterialesAdmin(admin.ModelAdmin):
    list_display = ("nr", "descripcion", "cantidad", "unidad_medida")
    search_fields = ("descripcion", "nr__numero_nr")


@admin.register(Plano)
class PlanoAdmin(admin.ModelAdmin):
    list_display = (
        "id_plano_deposito",
        "carpeta",
        "estado",
        "procesado",
        "fecha_plano",
        "nr_detectados_total",
        "nr_validos_total",
        "nr_desconocidos_total",
        "eliminado",
        "fecha_carga",
    )

    search_fields = (
        "id_plano_deposito",
        "carpeta__codigo_carpeta",
        "carpeta__empresa__nombre",
        "nr_detectados",
        "nr_validos",
        "nr_desconocidos",
    )

    list_filter = (
        "estado",
        "procesado",
        "eliminado",
        "fecha_carga",
        "fecha_plano",
        "carpeta__empresa",
    )

    readonly_fields = (
        "texto_ocr",
        "nr_detectados",
        "nr_validos",
        "nr_desconocidos",
        "motivo",
        "fecha_plano",
        "fecha_carga",
        "fecha_procesamiento",
        "nr_detectados_total",
        "nr_validos_total",
        "nr_desconocidos_total",
    )

    inlines = [ResultadoValidacionPlanoInline]

    actions = [
        procesar_ocr,
        validar_nrs,
        validar_plano_completo_admin,
    ]

    ordering = ("-fecha_carga",)


@admin.register(ResultadoValidacionPlano)
class ResultadoValidacionPlanoAdmin(admin.ModelAdmin):
    list_display = (
        "plano",
        "nr_detectado",
        "nr_normalizado",
        "estado_resultado",
        "ciudad_ok",
        "zona_ok",
        "fecha_ok",
    )
    search_fields = (
        "nr_detectado",
        "nr_normalizado",
        "plano__id_plano_deposito",
    )
    list_filter = (
        "estado_resultado",
        "ciudad_ok",
        "zona_ok",
        "fecha_ok",
    )
    ordering = ("plano", "id")


@admin.register(Auditoria)
class AuditoriaAdmin(admin.ModelAdmin):
    list_display = (
        "fecha",
        "usuario",
        "accion",
        "entidad",
        "entidad_id",
        "plano",
        "carpeta",
    )
    search_fields = (
        "usuario",
        "accion",
        "descripcion",
        "entidad",
        "entidad_id",
    )
    list_filter = ("accion", "entidad", "fecha")
    readonly_fields = (
        "fecha",
        "usuario",
        "accion",
        "descripcion",
        "entidad",
        "entidad_id",
        "plano",
        "carpeta",
    )
    ordering = ("-fecha",)


@admin.register(StockMaterial)
class StockMaterialAdmin(admin.ModelAdmin):
    list_display = (
        "nombre_material",
        "codigo_material",
        "cantidad_disponible",
        "unidad",
        "stock_minimo",
        "activo",
        "fecha_actualizacion",
    )
    search_fields = ("nombre_material", "codigo_material")
    list_filter = ("activo", "unidad")
    ordering = ("nombre_material",)