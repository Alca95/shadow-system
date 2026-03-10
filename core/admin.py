from django.contrib import admin
from .models import Carpeta, Reclamo, NRMateriales, ItemNRMateriales, Plano, Auditoria
from .ocr_extract import ocr_text_from_file, extract_nrs, extract_fecha_plano
from .validator import validar_plano_contra_bd
from .services import validar_plano_completo


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
            plano.save()

            Auditoria.objects.create(
                plano=plano,
                usuario=str(request.user),
                accion=f"OCR OK -> nr_detectados={plano.nr_detectados} fecha_plano={plano.fecha_plano}"
            )

        except Exception as e:
            plano.estado = "EN_ESPERA"
            plano.save()
            Auditoria.objects.create(
                plano=plano,
                usuario=str(request.user),
                accion=f"OCR ERROR: {e}"
            )


@admin.action(description="Validar NR detectados contra la BD (marca válidos/desconocidos)")
def validar_nrs(modeladmin, request, queryset):
    for plano in queryset:
        res = validar_plano_contra_bd(plano)
        Auditoria.objects.create(
            plano=plano,
            usuario=str(request.user),
            accion=f"VALIDACIÓN NR -> estado={res['estado']} validos={res['validos']} desconocidos={res['desconocidos']}"
        )


@admin.action(description="Validar plano completo contra Reclamo")
def validar_plano_completo_admin(modeladmin, request, queryset):
    for plano in queryset:
        res = validar_plano_completo(plano)
        Auditoria.objects.create(
            plano=plano,
            usuario=str(request.user),
            accion=f"VALIDACIÓN COMPLETA -> estado={res['estado']} motivos={res['motivos']}"
        )


@admin.register(Plano)
class PlanoAdmin(admin.ModelAdmin):
    list_display = (
        "id_plano_deposito",
        "estado",
        "fecha_plano",
        "nr_detectados",
        "nr_validos",
        "nr_desconocidos",
        "carpeta",
    )

    readonly_fields = (
        "texto_ocr",
        "nr_detectados",
        "nr_validos",
        "nr_desconocidos",
        "motivo",
        "fecha_plano",
    )

    actions = [
        procesar_ocr,
        validar_nrs,
        validar_plano_completo_admin,
    ]


admin.site.register(Carpeta)
admin.site.register(Reclamo)
admin.site.register(NRMateriales)
admin.site.register(ItemNRMateriales)
admin.site.register(Auditoria)