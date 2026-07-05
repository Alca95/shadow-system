from urllib import request

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Count
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone
from django.db import transaction
from datetime import datetime
from collections import Counter
from django.db.models.functions import TruncMonth
import re
import json
from collections import Counter, defaultdict
from datetime import datetime
from collections import defaultdict, Counter

from .forms import UsuarioCrearForm, UsuarioEditarForm
from .models import (
    Carpeta,
    EmpresaContratista,
    Plano,
    Auditoria,
    PerfilUsuario,
    MaterialDetectadoPlano,
    ResultadoValidacionPlano,
)
from .services import procesar_plano_completo
from .validator import (
    recalcular_estado_plano_desde_resultados,
    compare_text_fuzzy,
    compare_dates,
)

from core.models import (
    Plano,
    Carpeta,
    ResultadoValidacionPlano,
    Reclamo,
    MaterialDetectadoPlano,
    NRMateriales, 
)

def get_or_create_perfil(user):
    if user.is_superuser:
        return None
    perfil, _ = PerfilUsuario.objects.get_or_create(user=user)
    return perfil


def user_is_admin(user):
    return user.is_authenticated and user.is_superuser


def user_is_funcionario(user):
    if user.is_superuser:
        return False
    perfil = get_or_create_perfil(user)
    return bool(perfil and perfil.rol == "FUNCIONARIO" and perfil.activo)


def user_is_contratista(user):
    if user.is_superuser:
        return False
    perfil = get_or_create_perfil(user)
    return bool(perfil and perfil.rol == "CONTRATISTA" and perfil.activo)


def require_admin_or_funcionario(request):
    if request.user.is_superuser:
        return
    if user_is_funcionario(request.user):
        return
    raise PermissionDenied("No tienes permisos para realizar esta acción.")


def _plano_en_edicion_session_key(plano_id):
    return f"plano_en_edicion_{plano_id}"


def _plano_en_edicion(request, plano_id):
    return bool(request.session.get(_plano_en_edicion_session_key(plano_id), False))


def _set_plano_en_edicion(request, plano_id, value):
    key = _plano_en_edicion_session_key(plano_id)
    if value:
        request.session[key] = True
    else:
        request.session.pop(key, None)
    request.session.modified = True


def _user_can_edit_materiales(user):
    return user_is_admin(user) or user_is_funcionario(user)

def _user_can_edit_datos_nr(user):
    return user_is_admin(user) or user_is_funcionario(user)

def _user_can_change_nr_estado(user):
    return user_is_admin(user)


# =========================================================
# HELPERS DETALLE PLANO
# =========================================================
def _safe_getattr(obj, attr_name, default=None):
    try:
        return getattr(obj, attr_name, default)
    except Exception:
        return default


def _first_value(obj, attr_names, default=None):
    if not obj:
        return default
    for attr_name in attr_names:
        value = _safe_getattr(obj, attr_name, None)
        if value not in (None, "", [], (), {}):
            return value
    return default


def _format_value(value):
    if value in (None, "", [], (), {}):
        return "-"
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%d/%m/%Y")
        except Exception:
            return str(value)
    return str(value)


def _normalize_decimal_text(value):
    text = str(value).strip()
    if text.endswith(".0"):
        return text[:-2]
    return text


def _split_material_items(raw_text):
    if not raw_text:
        return []

    text = str(raw_text).strip()
    if not text or text.lower() in {"ninguno", "-", "none"}:
        return []

    text = text.replace("\n", ",")
    parts = [p.strip(" -•\t") for p in re.split(r",|;|\|", text) if p.strip(" -•\t")]
    return parts


def _parse_single_material_entry(text):
    text = str(text).strip()
    if not text:
        return None

    match = re.match(r"^\s*(\d+(?:[\.,]\d+)?)\s+(.+)$", text)
    if match:
        cantidad = _normalize_decimal_text(match.group(1).replace(",", "."))
        descripcion = match.group(2).strip()
        return {
            "cantidad": cantidad,
            "unidad": "unidad",
            "descripcion": descripcion,
        }

    return {
        "cantidad": "-",
        "unidad": "unidad",
        "descripcion": text,
    }


def _parse_material_text_to_rows(raw_text):
    rows = []
    for part in _split_material_items(raw_text):
        item = _parse_single_material_entry(part)
        if item:
            rows.append(item)
    return rows


def _extract_parenthetical_value(text, label):
    pattern = rf"{label}\s*:\s*([^|)\n]+)"
    match = re.search(pattern, str(text), re.IGNORECASE)
    return match.group(1).strip() if match else None


def _extract_ocr_data_from_motivo(motivo):
    data = {
        "ciudad_plano": None,
        "ciudad_reclamo": None,
        "zona_plano": None,
        "zona_reclamo": None,
        "fecha_nr": None,
        "fecha_reclamo": None,
        "materiales_ocr": None,
    }

    if not motivo:
        return data

    text = str(motivo)

    ciudad_line = re.search(r"Ciudad\s+(?:correcta|no coincide)\s*\((.*?)\)", text, re.IGNORECASE)
    if ciudad_line:
        fragment = ciudad_line.group(1)
        data["ciudad_plano"] = _extract_parenthetical_value(fragment, "Plano")
        data["ciudad_reclamo"] = _extract_parenthetical_value(fragment, "Reclamo")

    zona_line = re.search(r"Zona\s+(?:correcta|no coincide)\s*\((.*?)\)", text, re.IGNORECASE)
    if zona_line:
        fragment = zona_line.group(1)
        data["zona_plano"] = _extract_parenthetical_value(fragment, "Plano/NR") or _extract_parenthetical_value(fragment, "Plano")
        data["zona_reclamo"] = _extract_parenthetical_value(fragment, "Reclamo")

    fecha_line = re.search(r"Fecha\s+correcta\s*\((.*?)\)", text, re.IGNORECASE)
    if fecha_line:
        fragment = fecha_line.group(1)
        nr_match = re.search(r"NR\s*:\s*([0-9\-\/]+)", fragment, re.IGNORECASE)
        reclamo_match = re.search(r"Reclamo\s*:\s*([0-9\-\/]+)", fragment, re.IGNORECASE)
        if nr_match:
            data["fecha_nr"] = nr_match.group(1).strip()
        if reclamo_match:
            data["fecha_reclamo"] = reclamo_match.group(1).strip()

    materiales_match = re.search(r"Materiales OCR detectados:\s*(.+)$", text, re.IGNORECASE)
    if materiales_match:
        data["materiales_ocr"] = materiales_match.group(1).strip()

    return data


def _clean_motivo_text(motivo):
    if not motivo:
        return "-"

    text = str(motivo).strip()
    text = re.sub(r"\s*\|\s*Materiales OCR detectados:.*$", "", text).strip()
    return text or "-"


def _extract_material_rows_from_related(nr_obj):
    if not nr_obj:
        return []

    related_names = [
        "items",
        "itemnrmateriales_set",
        "item_nr_materiales",
        "itemes",
        "detalles",
        "detalle_items",
        "materiales",
    ]

    rows = []
    seen = set()

    for related_name in related_names:
        related_manager = _safe_getattr(nr_obj, related_name, None)
        if related_manager is None:
            continue

        try:
            iterable = related_manager.all() if hasattr(related_manager, "all") else related_manager
        except Exception:
            continue

        try:
            for item in iterable:
                cantidad = _first_value(item, ["cantidad", "cant"], None)
                unidad = _first_value(item, ["unidad"], None)
                descripcion = _first_value(
                    item,
                    ["descripcion", "material", "nombre", "detalle", "observacion"],
                    None
                )

                if cantidad in (None, "", [], (), {}) and descripcion in (None, "", [], (), {}):
                    continue

                row = {
                    "cantidad": _normalize_decimal_text(_format_value(cantidad)),
                    "unidad": _format_value(unidad) if unidad not in (None, "") else "unidad",
                    "descripcion": _format_value(descripcion),
                }

                signature = (
                    row["cantidad"].lower(),
                    row["unidad"].lower(),
                    row["descripcion"].lower(),
                )

                if signature not in seen:
                    seen.add(signature)
                    rows.append(row)
        except Exception:
            continue

    return rows


def _merge_material_rows(*groups):
    rows = []
    seen = set()

    for group in groups:
        for item in group or []:
            row = {
                "cantidad": _normalize_decimal_text(item.get("cantidad", "-")),
                "unidad": item.get("unidad", "unidad"),
                "descripcion": item.get("descripcion", "-"),
            }
            signature = (
                str(row["cantidad"]).strip().lower(),
                str(row["unidad"]).strip().lower(),
                str(row["descripcion"]).strip().lower(),
            )
            if signature not in seen:
                seen.add(signature)
                rows.append(row)

    return rows

def _normalize_ui_material_text(value):
    text = str(value or "").strip().lower()
    text = " ".join(text.split())

    replacements = {
        "á": "a",
        "é": "e",
        "í": "i",
        "ó": "o",
        "ú": "u",
        "ü": "u",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)

    return text


def _material_matches_for_ui(descripcion_plano, materiales_bd_tabla):
    desc_plano = _normalize_ui_material_text(descripcion_plano)

    if not desc_plano:
        return False

    for item_bd in materiales_bd_tabla or []:
        desc_bd = _normalize_ui_material_text(item_bd.get("descripcion", ""))

        if not desc_bd:
            continue

        if desc_plano == desc_bd:
            return True

        if desc_plano in desc_bd or desc_bd in desc_plano:
            return True

    return False

def _material_quantity_equal(a, b):
    try:
        return float(str(a).replace(",", ".")) == float(str(b).replace(",", "."))
    except Exception:
        return str(a or "").strip().lower() == str(b or "").strip().lower()


def _material_unit_equal(a, b):
    a_norm = _normalize_ui_material_text(a)
    b_norm = _normalize_ui_material_text(b)

    metros = {"m", "mt", "mts", "metro", "metros"}
    unidad = {"u", "un", "und", "unidad", "unidades"}

    if a_norm in metros and b_norm in metros:
        return True

    if a_norm in unidad and b_norm in unidad:
        return True

    return a_norm == b_norm


def _material_desc_equal(a, b):
    a_norm = _normalize_ui_material_text(a).replace("mm2", "").strip()
    b_norm = _normalize_ui_material_text(b).replace("mm2", "").strip()

    return a_norm == b_norm or a_norm in b_norm or b_norm in a_norm


def _material_row_matches(plano_item, bd_item):
    return (
        _material_quantity_equal(plano_item.get("cantidad"), bd_item.get("cantidad"))
        and _material_unit_equal(plano_item.get("unidad"), bd_item.get("unidad"))
        and _material_desc_equal(plano_item.get("descripcion"), bd_item.get("descripcion"))
    )


def _ordenar_materiales_plano_por_bd(materiales_plano, materiales_bd):
    materiales_plano = list(materiales_plano or [])
    materiales_bd = list(materiales_bd or [])

    usados = set()
    ordenados = []

    for bd_item in materiales_bd:
        mejor_idx = None

        for idx, plano_item in enumerate(materiales_plano):
            if idx in usados:
                continue

            if _material_desc_equal(plano_item.get("descripcion"), bd_item.get("descripcion")):
                mejor_idx = idx
                break

        if mejor_idx is not None:
            item = materiales_plano[mejor_idx]
            item["ui_match"] = _material_row_matches(item, bd_item)
            ordenados.append(item)
            usados.add(mejor_idx)

    for idx, item in enumerate(materiales_plano):
        if idx not in usados:
            item["ui_match"] = False
            ordenados.append(item)

    return ordenados

def _build_observacion_administrativa(r):
    estado_final = r.get("estado_resultado_final")
    fue_editado = r.get("fue_editado") or r.get("fue_editado_manual")
    inconsistencias = []

    if not r.get("ciudad_ok"):
        inconsistencias.append("ciudad")
    if not r.get("zona_ok"):
        inconsistencias.append("zona")
    if not r.get("fecha_ok"):
        inconsistencias.append("fecha")
    if not r.get("materiales_ok"):
        inconsistencias.append("materiales")

    if estado_final == "APROBADO":
        if fue_editado:
            return (
                "El reclamo fue aprobado posterior a una corrección manual registrada "
                "durante el proceso de validación administrativa."
            )
        return (
            "El reclamo cumple con las validaciones operativas y administrativas "
            "definidas por el sistema."
        )

    if estado_final == "RECHAZADO":
        if inconsistencias:
            return (
                "El reclamo fue rechazado debido a inconsistencias detectadas en "
                + ", ".join(inconsistencias)
                + " respecto a la base de datos institucional."
            )
        return "El reclamo fue rechazado por inconsistencias detectadas durante la validación."

    if fue_editado:
        return (
            "El reclamo permanece en estado EN VERIFICACIÓN debido a modificaciones "
            "manuales pendientes de aprobación administrativa final."
        )

    return (
        "El reclamo permanece en estado EN VERIFICACIÓN porque requiere revisión "
        "administrativa antes de su aprobación final."
    )

def _build_resultado_detalle(resultado):
    nr_obj = _safe_getattr(resultado, "nr_materiales_encontrado", None)
    reclamo_obj = _safe_getattr(resultado, "reclamo_encontrado", None)

    motivo_resultado = _format_value(
        _first_value(resultado, ["motivo_resultado", "diagnostico", "detalle"], None)
    )
    parsed = _extract_ocr_data_from_motivo(motivo_resultado)

    valores_detectados = _resolver_valores_detectados_resultado(resultado)

    ciudad_plano = valores_detectados["ciudad"]
    zona_plano = valores_detectados["zona"]
    fecha_plano = valores_detectados["fecha"]

    if ciudad_plano in (None, ""):
        ciudad_plano = _first_value(nr_obj, ["ciudad"], None)

    if zona_plano in (None, ""):
        zona_plano = _first_value(resultado, ["zona"], None) or _first_value(nr_obj, ["zona"], None)

    if fecha_plano in (None, ""):
        fecha_plano = _first_value(nr_obj, ["fecha_trabajo"], None)

    ciudad_bd = _first_value(reclamo_obj, ["ciudad"], None) or parsed["ciudad_reclamo"] or _first_value(nr_obj, ["ciudad"], None)
    zona_bd = _first_value(reclamo_obj, ["zona"], None) or parsed["zona_reclamo"] or _first_value(nr_obj, ["zona"], None)
    fecha_bd = _first_value(reclamo_obj, ["fecha_reclamo"], None) or parsed["fecha_reclamo"] or _first_value(nr_obj, ["fecha_trabajo"], None)

    materiales_plano_texto = (
        _first_value(resultado, ["materiales_plano", "materiales_detectados", "materiales_extraidos"], None)
        or parsed["materiales_ocr"]
    )

    materiales_bd_texto = (
        _first_value(resultado, ["materiales_bd", "materiales_registrados"], None)
        or _first_value(nr_obj, ["observacion"], None)
    )

    materiales_plano_tabla = _parse_material_text_to_rows(materiales_plano_texto)

    materiales_editables = []
    materiales_detectados_qs = []
    try:
        materiales_detectados_qs = resultado.materiales_detectados.all().order_by("orden", "id")
        for material in materiales_detectados_qs:
            materiales_editables.append({
                "id": material.id,
                "cantidad": material.cantidad_final or "-",
                "unidad": material.unidad_final or "unidad",
                "descripcion": material.descripcion_final or "-",
                "cantidad_original": material.cantidad_original or "-",
                "unidad_original": material.unidad_original or "unidad",
                "descripcion_original": material.descripcion_original or "-",
                "fue_editado": material.fue_editado,
                "coincide_con_bd": material.coincide_con_bd,
            })
    except Exception:
        materiales_editables = []
        materiales_detectados_qs = []

    if materiales_editables:
        materiales_plano_tabla = [
            {
                "id": item["id"],
                "cantidad": item["cantidad"],
                "unidad": item["unidad"],
                "descripcion": item["descripcion"],
                "cantidad_original": item["cantidad_original"],
                "unidad_original": item["unidad_original"],
                "descripcion_original": item["descripcion_original"],
                "fue_editado": item["fue_editado"],
                "coincide_con_bd": item["coincide_con_bd"],
            }
            for item in materiales_editables
        ]

    materiales_bd_tabla_rel = _extract_material_rows_from_related(nr_obj)
    materiales_bd_tabla_text = _parse_material_text_to_rows(materiales_bd_texto)
    materiales_bd_tabla = _merge_material_rows(materiales_bd_tabla_rel, materiales_bd_tabla_text)

    materiales_plano_tabla = _ordenar_materiales_plano_por_bd(
        materiales_plano_tabla,
        materiales_bd_tabla,
    )

    estado_resultado = _first_value(resultado, ["estado_resultado"], "EN_VERIFICACION")
    estado_resultado_final = _first_value(resultado, ["estado_resultado_final"], None) or _first_value(resultado, ["estado_resultado_manual"], None) or estado_resultado

    return {
        "obj": resultado,
        "id_resultado": resultado.id,
        "nr_detectado": _first_value(
            resultado,
            ["nr_detectado", "numero_nr", "nr", "codigo_nr"],
            None,
        ) or _first_value(nr_obj, ["numero_nr"], "NR no identificado"),
        "estado_resultado": estado_resultado,
        "estado_resultado_final": estado_resultado_final,
        "estado_resultado_manual": _first_value(resultado, ["estado_resultado_manual"], None),
        "fue_revisado_manual": bool(_first_value(resultado, ["fue_revisado_manual"], False)),
        "motivo_revision_manual": _format_value(_first_value(resultado, ["motivo_revision_manual"], None)),
        "ciudad_ok": bool(_first_value(resultado, ["ciudad_ok"], False)),
        "zona_ok": bool(_first_value(resultado, ["zona_ok"], False)),
        "fecha_ok": bool(_first_value(resultado, ["fecha_ok"], False)),
        "materiales_ok": bool(_first_value(resultado, ["materiales_ok"], False)),
        "materiales_requieren_revision": bool(_first_value(resultado, ["materiales_requieren_revision"], False)),
        "ciudad_plano": _format_value(ciudad_plano),
        "zona_plano": _format_value(zona_plano),
        "fecha_plano": _format_value(fecha_plano),
        "ciudad_reclamo": _format_value(ciudad_bd),
        "zona_reclamo": _format_value(zona_bd),
        "fecha_reclamo": _format_value(fecha_bd),
        "resultado_ocr": _format_value(
            _first_value(resultado, ["resultado_ocr", "observacion_ocr"], None)
        ),
        "observacion": _format_value(
            _first_value(resultado, ["observacion", "detalle", "diagnostico"], None)
        ),
        "motivo_resultado": motivo_resultado,
        "motivo_resultado_limpio": _clean_motivo_text(motivo_resultado),
        "observacion_administrativa": _build_observacion_administrativa({
            "estado_resultado_final": estado_resultado_final,
            "ciudad_ok": bool(_first_value(resultado, ["ciudad_ok"], False)),
            "zona_ok": bool(_first_value(resultado, ["zona_ok"], False)),
            "fecha_ok": bool(_first_value(resultado, ["fecha_ok"], False)),
            "materiales_ok": bool(_first_value(resultado, ["materiales_ok"], False)),
            "fue_editado": any(m.fue_editado for m in materiales_detectados_qs),
            "fue_editado_manual": bool(_first_value(resultado, ["fue_editado_manual"], False)),
        }),
        "materiales_plano": None if str(materiales_plano_texto).strip().lower() in {"ninguno", "-", "none", ""} else materiales_plano_texto,
        "materiales_plano_tabla": materiales_plano_tabla,
        "materiales_bd": None if str(materiales_bd_texto).strip().lower() in {"ninguno", "-", "none", ""} else materiales_bd_texto,
        "materiales_bd_tabla": materiales_bd_tabla,
        "fue_editado": any(m.fue_editado for m in materiales_detectados_qs),
                "ciudad_plano_original": _format_value(_first_value(resultado, ["ciudad_plano_original"], None)),
        "zona_plano_original": _format_value(_first_value(resultado, ["zona_plano_original"], None)),
        "fecha_plano_original": _format_value(_first_value(resultado, ["fecha_plano_original"], None)),
        "ciudad_plano_editada": _format_value(_first_value(resultado, ["ciudad_plano_editada"], None)),
        "zona_plano_editada": _format_value(_first_value(resultado, ["zona_plano_editada"], None)),
        "fecha_plano_editada": _format_value(_first_value(resultado, ["fecha_plano_editada"], None)),
        "fue_editado_manual": bool(_first_value(resultado, ["fue_editado_manual"], False)),
        "motivo_edicion_manual": _format_value(_first_value(resultado, ["motivo_edicion_manual"], None)),
    }

def _parse_input_date(value):
    text = str(value or "").strip()
    if not text:
        return None

    formatos = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y")
    for fmt in formatos:
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue
    return None


def _resolver_valores_detectados_resultado(resultado):
    motivo_resultado = _format_value(
        _first_value(resultado, ["motivo_resultado", "diagnostico", "detalle"], None)
    )
    parsed = _extract_ocr_data_from_motivo(motivo_resultado)

    nr_obj = _safe_getattr(resultado, "nr_materiales_encontrado", None)

    ciudad = (
        _first_value(resultado, ["ciudad_plano_editada"], None)
        or _first_value(resultado, ["ciudad_plano_original"], None)
        or _first_value(resultado, ["ciudad_plano", "ciudad_detectada", "ciudad_extraida", "ciudad_ocr"], None)
        or parsed["ciudad_plano"]
        or _first_value(nr_obj, ["ciudad"], None)
    )

    zona = (
        _first_value(resultado, ["zona_plano_editada"], None)
        or _first_value(resultado, ["zona_plano_original"], None)
        or _first_value(resultado, ["zona_plano", "zona_detectada", "zona_extraida", "zona_ocr", "zona"], None)
        or parsed["zona_plano"]
        or _first_value(nr_obj, ["zona"], None)
    )

    fecha = (
        _first_value(resultado, ["fecha_plano_editada"], None)
        or _first_value(resultado, ["fecha_plano_original"], None)
        or _first_value(resultado, ["fecha_plano", "fecha_detectada", "fecha_extraida", "fecha_ocr"], None)
        or parsed["fecha_nr"]
        or _first_value(nr_obj, ["fecha_trabajo"], None)
    )

    if isinstance(fecha, str):
        fecha = _parse_input_date(fecha)

    return {
        "ciudad": ciudad,
        "zona": zona,
        "fecha": fecha,
    }


def _normalize_material_text_simple(value):
    text = str(value or "").strip().lower()
    text = " ".join(text.split())
    replacements = {
        "í": "i",
        "ó": "o",
        "á": "a",
        "é": "e",
        "ú": "u",
        "ü": "u",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)

    equivalencias = {
        "ife": "ife",
        "1fe": "ife",
        "fe": "ife",
        "1f": "ife",
    }
    return equivalencias.get(text, text)


def _material_signature_final(cantidad, unidad, descripcion):
    return (
        _normalize_material_text_simple(cantidad),
        _normalize_material_text_simple(unidad),
        _normalize_material_text_simple(descripcion),
    )


def _materiales_finales_resultado(resultado):
    rows = []
    try:
        for item in resultado.materiales_detectados.all().order_by("orden", "id"):
            rows.append({
                "cantidad": item.cantidad_final or "-",
                "unidad": item.unidad_final or "unidad",
                "descripcion": item.descripcion_final or "-",
                "obj": item,
            })
    except Exception:
        pass
    return rows


def _materiales_bd_resultado(resultado):
    nr_obj = resultado.nr_materiales_encontrado
    if not nr_obj:
        return []

    rows = _extract_material_rows_from_related(nr_obj)

    normalizados = []
    for item in rows:
        normalizados.append({
            "cantidad": _normalize_decimal_text(item.get("cantidad", "-")),
            "unidad": item.get("unidad", "unidad") or "unidad",
            "descripcion": item.get("descripcion", "-") or "-",
        })

    return normalizados


def _comparar_materiales_resultado(resultado):
    materiales_plano = _materiales_finales_resultado(resultado)
    materiales_bd = _materiales_bd_resultado(resultado)

    if not materiales_plano and not materiales_bd:
        return True, ["No hay materiales en plano ni en BD."]

    if not materiales_plano and materiales_bd:
        return False, ["No se detectaron materiales en el plano, pero sí existen materiales en la BD."]

    if materiales_plano and not materiales_bd:
        return False, ["Se detectaron materiales en el plano, pero no existen materiales cargados en la BD para ese NR."]

    plano_signatures = [
        _material_signature_final(x["cantidad"], x["unidad"], x["descripcion"])
        for x in materiales_plano
    ]
    bd_signatures = [
        _material_signature_final(x["cantidad"], x["unidad"], x["descripcion"])
        for x in materiales_bd
    ]

    faltantes_en_plano = [sig for sig in bd_signatures if sig not in plano_signatures]
    sobrantes_en_plano = [sig for sig in plano_signatures if sig not in bd_signatures]

    motivos = []
    if faltantes_en_plano:
        motivos.append("Existen materiales de BD que no coinciden con los detectados/corregidos del plano.")
    if sobrantes_en_plano:
        motivos.append("Existen materiales detectados/corregidos del plano que no coinciden con la BD.")

    coincide = not (faltantes_en_plano or sobrantes_en_plano)

    for material in materiales_plano:
        material["obj"].coincide_con_bd = _material_signature_final(
            material["cantidad"], material["unidad"], material["descripcion"]
        ) in bd_signatures
        material["obj"].save(update_fields=["coincide_con_bd"])

    return coincide, motivos

def _recalcular_resultado_por_materiales(resultado):
    materiales_ok, motivos_materiales = _comparar_materiales_resultado(resultado)

    resultado.materiales_ok = materiales_ok
    resultado.materiales_requieren_revision = not materiales_ok

    hubo_edicion_materiales = resultado.materiales_detectados.filter(fue_editado=True).exists()
    hubo_edicion_datos = bool(resultado.fue_editado_manual)
    hubo_edicion_manual = hubo_edicion_materiales or hubo_edicion_datos

    if hubo_edicion_manual:
        resultado.estado_resultado = "EN_VERIFICACION"
    else:
        if not resultado.ciudad_ok or not resultado.zona_ok or not resultado.fecha_ok:
            resultado.estado_resultado = "RECHAZADO"
        else:
            if materiales_ok:
                resultado.estado_resultado = "APROBADO"
            else:
                resultado.estado_resultado = "EN_VERIFICACION"

    resultado.save(update_fields=[
        "estado_resultado",
        "materiales_ok",
        "materiales_requieren_revision",
    ])

    recalcular_estado_plano_desde_resultados(resultado.plano)
    return resultado

def _recalcular_resultado_por_datos(resultado):
    reclamo = resultado.reclamo_encontrado
    if not reclamo:
        resultado.estado_resultado = "EN_VERIFICACION"
        resultado.ciudad_ok = False
        resultado.zona_ok = False
        resultado.fecha_ok = False
        resultado.save(update_fields=["estado_resultado", "ciudad_ok", "zona_ok", "fecha_ok"])
        recalcular_estado_plano_desde_resultados(resultado.plano)
        return resultado

    valores = _resolver_valores_detectados_resultado(resultado)

    ciudad_cmp = compare_text_fuzzy(valores["ciudad"], reclamo.ciudad)
    zona_cmp = compare_text_fuzzy(valores["zona"], reclamo.zona)
    fecha_cmp = compare_dates(valores["fecha"], reclamo.fecha_reclamo)

    resultado.ciudad_ok = bool(ciudad_cmp["comparable"] and ciudad_cmp["matched"])
    resultado.zona_ok = bool(zona_cmp["comparable"] and zona_cmp["matched"])
    resultado.fecha_ok = bool(fecha_cmp["comparable"] and fecha_cmp["matched"])

    hubo_edicion_materiales = resultado.materiales_detectados.filter(fue_editado=True).exists()
    hubo_edicion_datos = bool(resultado.fue_editado_manual)
    hubo_edicion_manual = hubo_edicion_materiales or hubo_edicion_datos

    if hubo_edicion_manual:
        resultado.estado_resultado = "EN_VERIFICACION"
    else:
        if not resultado.ciudad_ok or not resultado.zona_ok or not resultado.fecha_ok:
            resultado.estado_resultado = "RECHAZADO"
        else:
            if resultado.materiales_ok:
                resultado.estado_resultado = "APROBADO"
            else:
                resultado.estado_resultado = "EN_VERIFICACION"

    resultado.save(update_fields=["ciudad_ok", "zona_ok", "fecha_ok", "estado_resultado"])
    recalcular_estado_plano_desde_resultados(resultado.plano)
    return resultado

def login_view(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect("dashboard")

        perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

        if perfil.rol == "CONTRATISTA":
            return redirect("dashboard_contratista")
        if perfil.rol == "FUNCIONARIO":
            return redirect("dashboard_funcionario")

        return redirect("dashboard")

    error = None

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if user.is_superuser:
                login(request, user)
                return redirect("dashboard")

            perfil, _ = PerfilUsuario.objects.get_or_create(user=user)

            if not perfil.activo:
                error = "Tu perfil de usuario está inactivo."
            else:
                login(request, user)

                if perfil.rol == "CONTRATISTA":
                    return redirect("dashboard_contratista")
                if perfil.rol == "FUNCIONARIO":
                    return redirect("dashboard_funcionario")

                return redirect("dashboard")
        else:
            error = "Usuario o contraseña incorrectos."

    return render(request, "login.html", {
        "error": error,
    })


@login_required
def dashboard_view(request):
    carpetas_recientes = (
        Carpeta.objects
        .filter(eliminada=False)
        .select_related("empresa")
        .order_by("-fecha_creacion")[:5]
    )

    if request.user.is_superuser:
        return render(request, "dashboard.html", {
            "app_name": "Shadow",
            "titulo_pantalla": "Inicio administrador",
            "carpetas_recientes": carpetas_recientes,
        })

    perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

    if perfil.rol == "FUNCIONARIO":
        return render(request, "dashboard_funcionario.html", {
            "app_name": "Shadow",
            "titulo_pantalla": "Inicio funcionario",
            "carpetas_recientes": carpetas_recientes,
        })

    return render(request, "dashboard.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Inicio",
        "carpetas_recientes": carpetas_recientes,
    })


@login_required
def dashboard_funcionario_view(request):
    if request.user.is_superuser:
        return redirect("dashboard")

    perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

    if perfil.rol != "FUNCIONARIO" or not perfil.activo:
        return redirect("dashboard")

    carpetas_recientes = (
        Carpeta.objects
        .filter(eliminada=False)
        .select_related("empresa")
        .order_by("-fecha_creacion")[:5]
    )

    return render(request, "dashboard_funcionario.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Inicio funcionario",
        "carpetas_recientes": carpetas_recientes,
    })


@login_required
def dashboard_contratista_view(request):
    perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

    if perfil.rol != "CONTRATISTA":
        return redirect("dashboard")

    carpetas_recientes = (
        Carpeta.objects
        .filter(
            empresa=perfil.empresa,
            eliminada=False
        )
        .select_related("empresa")
        .order_by("-fecha_creacion")[:5]
    )

    return render(request, "dashboard_contratista.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Inicio contratista",
        "carpetas_recientes": carpetas_recientes,
    })


@login_required
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def carpetas_view(request):
    query = request.GET.get("q", "").strip()
    estado = request.GET.get("estado", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    mes = request.GET.get("mes", "").strip()
    anio = request.GET.get("anio", "").strip()
    nr_busqueda = request.GET.get("nr", "").strip()
    filtro_nivel_respuesta = request.GET.get("nivel_respuesta", "").strip()
    respuesta_fecha_reclamo = request.GET.get("respuesta_fecha_reclamo", "").strip()
    respuesta_fecha_trabajo = request.GET.get("respuesta_fecha_trabajo", "").strip()
    respuesta_dias_max = request.GET.get("respuesta_dias_max", "").strip()
    respuesta_orden = request.GET.get("respuesta_orden", "dias_desc").strip()

    carpetas = Carpeta.objects.filter(eliminada=False).select_related("empresa")

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        carpetas = carpetas.filter(empresa=perfil.empresa)

    if query:
        carpetas = carpetas.filter(codigo_carpeta__icontains=query)

    if estado:
        carpetas = carpetas.filter(estado=estado)

    if empresa_id and not user_is_contratista(request.user):
        carpetas = carpetas.filter(empresa_id=empresa_id)

    if mes:
        carpetas = carpetas.filter(mes=mes)

    if anio:
        carpetas = carpetas.filter(anio=anio)

    carpetas = carpetas.order_by("-anio", "-mes", "-fecha_creacion")
    empresas = EmpresaContratista.objects.filter(activo=True).order_by("nombre")
    meses_choices = [
        (1, "Enero"),
        (2, "Febrero"),
        (3, "Marzo"),
        (4, "Abril"),
        (5, "Mayo"),
        (6, "Junio"),
        (7, "Julio"),
        (8, "Agosto"),
        (9, "Septiembre"),
        (10, "Octubre"),
        (11, "Noviembre"),
        (12, "Diciembre"),
    ]

    anio_actual = timezone.localdate().year

    anios_disponibles = list(range(anio_actual + 1, anio_actual - 4, -1))
    meses_dict = dict(meses_choices)

    carpetas = list(carpetas)

    for carpeta in carpetas:
        carpeta.mes_nombre = meses_dict.get(carpeta.mes, carpeta.mes)
    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Carpetas",
        "carpetas": carpetas,
        "empresas": empresas,
        "filtros": {
            "q": query,
            "estado": estado,
            "empresa": "" if user_is_contratista(request.user) else empresa_id,
            "mes": mes,
            "anio": anio,
            "nr": nr_busqueda,
        },
        "estado_choices": Carpeta.ESTADO_CHOICES,
        "meses_choices": meses_choices,
        "anios_disponibles": anios_disponibles,
    }

    if user_is_contratista(request.user):
        return render(request, "carpetas_contratista.html", contexto)

    return render(request, "carpetas.html", contexto)


@login_required
def crear_carpeta_view(request):
    require_admin_or_funcionario(request)

    empresas = EmpresaContratista.objects.filter(activo=True).order_by("nombre")
    error = None
    meses_choices = [
        (1, "Enero"),
        (2, "Febrero"),
        (3, "Marzo"),
        (4, "Abril"),
        (5, "Mayo"),
        (6, "Junio"),
        (7, "Julio"),
        (8, "Agosto"),
        (9, "Septiembre"),
        (10, "Octubre"),
        (11, "Noviembre"),
        (12, "Diciembre"),
    ]

    anio_actual = timezone.localdate().year
    anios_disponibles = list(range(anio_actual + 1, anio_actual - 4, -1))

    valores_form = {
        "empresa": "",
        "mes": "",
        "anio": str(anio_actual),
        "observacion_general": "",
    }


    if request.method == "POST":
        mes = request.POST.get("mes")
        anio = request.POST.get("anio")
        empresa_id = request.POST.get("empresa")
        observacion_general = request.POST.get("observacion_general", "").strip()
        valores_form = {
            "empresa": empresa_id or "",
            "mes": mes or "",
            "anio": anio or "",
            "observacion_general": observacion_general,
        }

        if not mes or not anio or not empresa_id:
            error = "Debes completar mes, año y empresa."
        else:
            try:
                empresa = EmpresaContratista.objects.get(id=empresa_id, activo=True)

                nombre_usuario = (
                    request.user.get_full_name().strip()
                    if request.user.get_full_name()
                    else request.user.username
                )

                carpeta = Carpeta.objects.create(
                    mes=int(mes),
                    anio=int(anio),
                    empresa=empresa,
                    observacion_general=observacion_general or None,
                    creada_por=nombre_usuario,
                )

                Auditoria.objects.create(
                    carpeta=carpeta,
                    usuario=str(request.user),
                    accion="CREAR_CARPETA",
                    descripcion=f"Se creó la carpeta {carpeta.codigo_carpeta}",
                    entidad="Carpeta",
                    entidad_id=str(carpeta.id),
                )

                return redirect("detalle_carpeta", carpeta_id=carpeta.id)

            except EmpresaContratista.DoesNotExist:
                error = "La empresa seleccionada no existe."
            except Exception as e:
                error = f"No se pudo crear la carpeta: {e}"

    return render(request, "crear_carpeta.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Crear carpeta",
        "empresas": empresas,
        "error": error,
        "meses_choices": meses_choices,
        "anios_disponibles": anios_disponibles,
        "valores_form": valores_form,
    })


@login_required
def detalle_carpeta_view(request, carpeta_id):
    carpeta = get_object_or_404(
        Carpeta.objects.select_related("empresa"),
        id=carpeta_id,
        eliminada=False,
    )

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        if carpeta.empresa_id != perfil.empresa_id:
            raise PermissionDenied("No puedes acceder a esta carpeta.")

    estado_plano = request.GET.get("estado", "").strip()

    planos_base = carpeta.planos.filter(eliminado=False)

    total_planos = planos_base.count()
    planos_aprobados = planos_base.filter(estado="APROBADO").count()
    planos_en_revision = planos_base.filter(estado="EN_REVISION").count()
    planos_en_verificacion = planos_base.filter(estado="EN_VERIFICACION").count()
    planos_rechazados = planos_base.filter(estado="RECHAZADO").count()

    planos = planos_base
    if estado_plano:
        planos = planos.filter(estado=estado_plano)

    planos = planos.order_by("-fecha_carga")

    meses_dict = {
        1: "Enero",
        2: "Febrero",
        3: "Marzo",
        4: "Abril",
        5: "Mayo",
        6: "Junio",
        7: "Julio",
        8: "Agosto",
        9: "Septiembre",
        10: "Octubre",
        11: "Noviembre",
        12: "Diciembre",
    }

    carpeta.mes_nombre = meses_dict.get(carpeta.mes, carpeta.mes)

    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Detalle de carpeta",
        "carpeta": carpeta,
        "planos": planos,
        "estado_plano": estado_plano,
        "estado_choices_plano": Plano.ESTADO_CHOICES,
        "total_planos": total_planos,
        "planos_aprobados": planos_aprobados,
        "planos_en_revision": planos_en_revision,
        "planos_en_verificacion": planos_en_verificacion,
        "planos_rechazados": planos_rechazados,
    }

    if user_is_contratista(request.user):
        return render(request, "detalle_carpeta_contratista.html", contexto)

    return render(request, "detalle_carpeta.html", contexto)


@login_required
def eliminar_carpeta_view(request, carpeta_id):
    require_admin_or_funcionario(request)

    carpeta = get_object_or_404(Carpeta, id=carpeta_id, eliminada=False)

    if request.method == "POST":
        carpeta.eliminada = True
        carpeta.fecha_eliminacion = timezone.now()
        carpeta.save()

        Auditoria.objects.create(
            carpeta=carpeta,
            usuario=str(request.user),
            accion="ELIMINAR_CARPETA",
            descripcion=f"Eliminación lógica de carpeta {carpeta.codigo_carpeta}",
            entidad="Carpeta",
            entidad_id=str(carpeta.id),
        )

        return redirect("carpetas")

    return redirect("detalle_carpeta", carpeta_id=carpeta.id)


@login_required
def subir_plano_view(request, carpeta_id=None):
    require_admin_or_funcionario(request)

    error = None
    carpeta = None

    if carpeta_id is not None:
        carpeta = get_object_or_404(
            Carpeta.objects.select_related("empresa"),
            id=carpeta_id,
            eliminada=False,
        )

    carpetas = (
        Carpeta.objects
        .filter(eliminada=False)
        .select_related("empresa")
        .order_by("-anio", "-mes", "empresa__nombre")
    )

    if request.method == "POST":
        carpeta_id_post = request.POST.get("carpeta")
        id_plano_deposito = request.POST.get("id_plano_deposito")
        archivo = request.FILES.get("archivo")

        if not carpeta_id_post or not id_plano_deposito or not archivo:
            error = "Debes completar carpeta, ID del plano y archivo."
        else:
            try:
                carpeta_obj = Carpeta.objects.get(id=carpeta_id_post, eliminada=False)

                plano = Plano.objects.create(
                    carpeta=carpeta_obj,
                    id_plano_deposito=id_plano_deposito,
                    archivo=archivo,
                    estado="EN_ESPERA",
                )

                Auditoria.objects.create(
                    plano=plano,
                    carpeta=carpeta_obj,
                    usuario=str(request.user),
                    accion="SUBIR_PLANO",
                    descripcion=f"Se cargó el plano {plano.id_plano_deposito}",
                    entidad="Plano",
                    entidad_id=str(plano.id),
                )

                return redirect("detalle_plano", plano_id=plano.id)

            except Carpeta.DoesNotExist:
                error = "La carpeta seleccionada no existe."
            except Exception as e:
                error = f"No se pudo guardar el plano: {e}"

    return render(request, "subir_plano.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Subir plano",
        "carpeta_actual": carpeta,
        "carpetas": carpetas,
        "error": error,
    })


@login_required
def detalle_plano_view(request, plano_id):
    plano = get_object_or_404(
        Plano.objects.select_related("carpeta", "carpeta__empresa"),
        id=plano_id,
        eliminado=False,
    )

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        if plano.carpeta.empresa_id != perfil.empresa_id:
            raise PermissionDenied("No puedes acceder a este plano.")

    resultados_qs = (
        plano.resultados_validacion
        .select_related("nr_materiales_encontrado", "reclamo_encontrado")
        .prefetch_related("materiales_detectados")
        .all()
    )

    resultados = [_build_resultado_detalle(r) for r in resultados_qs]

    total_nr = len(resultados)
    total_aprobados = sum(1 for r in resultados if r["estado_resultado_final"] == "APROBADO")
    total_rechazados = sum(1 for r in resultados if r["estado_resultado_final"] == "RECHAZADO")
    total_en_verificacion = sum(1 for r in resultados if r["estado_resultado_final"] == "EN_VERIFICACION")
    plano_en_edicion = _plano_en_edicion(request, plano.id)
    puede_guardar_plano = bool(plano.procesado and plano_en_edicion and not user_is_contratista(request.user))
    puede_ver_resumen = bool(plano.procesado and not plano_en_edicion)

    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Detalle del plano",
        "plano": plano,
        "resultados": resultados,
        "total_nr": total_nr,
        "total_aprobados": total_aprobados,
        "total_rechazados": total_rechazados,
        "total_en_verificacion": total_en_verificacion,
        "plano_en_edicion": plano_en_edicion,
        "puede_guardar_plano": puede_guardar_plano,
        "puede_ver_resumen": puede_ver_resumen,
        "puede_editar_materiales": _user_can_edit_materiales(request.user),
        "puede_cambiar_estado_nr": _user_can_change_nr_estado(request.user),
    }

    if user_is_contratista(request.user):
        return render(request, "detalle_plano_contratista.html", contexto)

    return render(request, "detalle_plano.html", contexto)


@login_required
def editar_material_detectado_view(request, material_id):
    if not _user_can_edit_materiales(request.user):
        raise PermissionDenied("No tienes permisos para editar materiales detectados.")

    material = get_object_or_404(
        MaterialDetectadoPlano.objects.select_related(
            "resultado_validacion",
            "resultado_validacion__plano",
            "resultado_validacion__plano__carpeta",
        ),
        id=material_id,
    )

    resultado = material.resultado_validacion
    plano = resultado.plano

    if request.method == "POST":
        material.cantidad_editada = (request.POST.get("cantidad") or "").strip() or None
        material.unidad_editada = (request.POST.get("unidad") or "").strip() or None
        material.descripcion_editada = (request.POST.get("descripcion") or "").strip() or None
        material.fue_editado = True
        material.editado_por = str(request.user)
        material.fecha_edicion = timezone.now()
        material.save(
            update_fields=[
                "cantidad_editada",
                "unidad_editada",
                "descripcion_editada",
                "fue_editado",
                "editado_por",
                "fecha_edicion",
            ]
        )
        _recalcular_resultado_por_datos(resultado)
        _recalcular_resultado_por_materiales(resultado)
        

        if "Corrección manual aplicada en materiales" not in str(resultado.motivo_resultado or ""):
            resultado.motivo_resultado = (
                (resultado.motivo_resultado or "").strip() +
                " | Corrección manual aplicada en materiales"
            ).strip(" |")
            resultado.save(update_fields=["motivo_resultado"])

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="EDITAR_MATERIAL_DETECTADO",
            descripcion=(
                f"Se editó material detectado del NR {resultado.nr_detectado} "
                f"en el plano {plano.id_plano_deposito}"
            ),
            entidad="MaterialDetectadoPlano",
            entidad_id=str(material.id),
        )

        messages.success(request, "Material detectado actualizado correctamente.")

    return redirect("detalle_plano", plano_id=plano.id)

@login_required
def editar_datos_nr_view(request, resultado_id):
    if not _user_can_edit_datos_nr(request.user):
        raise PermissionDenied("No tienes permisos para editar los datos detectados del NR.")

    resultado = get_object_or_404(
        ResultadoValidacionPlano.objects.select_related(
            "plano",
            "plano__carpeta",
            "reclamo_encontrado",
            "nr_materiales_encontrado",
        ),
        id=resultado_id,
    )

    if request.method == "POST":
        valores_actuales = _resolver_valores_detectados_resultado(resultado)

        if not resultado.ciudad_plano_original and valores_actuales["ciudad"] not in (None, "", "-"):
            resultado.ciudad_plano_original = valores_actuales["ciudad"]

        if not resultado.zona_plano_original and valores_actuales["zona"] not in (None, "", "-"):
            resultado.zona_plano_original = valores_actuales["zona"]

        if not resultado.fecha_plano_original and valores_actuales["fecha"]:
            resultado.fecha_plano_original = valores_actuales["fecha"]

        ciudad_editada = (request.POST.get("ciudad") or "").strip() or None
        zona_editada = (request.POST.get("zona") or "").strip() or None
        fecha_editada = _parse_input_date(request.POST.get("fecha"))

        resultado.ciudad_plano_editada = ciudad_editada
        resultado.zona_plano_editada = zona_editada
        resultado.fecha_plano_editada = fecha_editada
        resultado.fue_editado_manual = True
        resultado.editado_manual_por = str(request.user)
        resultado.fecha_edicion_manual = timezone.now()
        resultado.motivo_edicion_manual = "Corrección manual aplicada en ciudad, zona o fecha."

        resultado.save(
            update_fields=[
                "ciudad_plano_original",
                "zona_plano_original",
                "fecha_plano_original",
                "ciudad_plano_editada",
                "zona_plano_editada",
                "fecha_plano_editada",
                "fue_editado_manual",
                "editado_manual_por",
                "fecha_edicion_manual",
                "motivo_edicion_manual",
            ]
        )

        _recalcular_resultado_por_datos(resultado)

        resultado.estado_resultado = "EN_VERIFICACION"
        resultado.motivo_resultado = (
            (resultado.motivo_resultado or "") +
            " | Corrección manual aplicada en ciudad/zona/fecha"
        )
        resultado.save(update_fields=["estado_resultado", "motivo_resultado"])

        Auditoria.objects.create(
            plano=resultado.plano,
            carpeta=resultado.plano.carpeta,
            usuario=str(request.user),
            accion="EDITAR_DATOS_NR",
            descripcion=(
                f"Se editaron ciudad/zona/fecha del NR {resultado.nr_detectado} "
                f"en el plano {resultado.plano.id_plano_deposito}"
            ),
            entidad="ResultadoValidacionPlano",
            entidad_id=str(resultado.id),
        )

        messages.success(request, "Datos del NR actualizados correctamente.")

    return redirect("detalle_plano", plano_id=resultado.plano.id)

@login_required
def cambiar_estado_nr_manual_view(request, resultado_id):
    if not _user_can_change_nr_estado(request.user):
        raise PermissionDenied("Solo el administrador puede cambiar manualmente el estado del NR.")

    resultado = get_object_or_404(
        ResultadoValidacionPlano.objects.select_related("plano", "plano__carpeta"),
        id=resultado_id,
    )

    if request.method == "POST":
        nuevo_estado = (request.POST.get("estado_resultado_manual") or "").strip().upper()
        motivo = (request.POST.get("motivo_revision_manual") or "").strip()

        estados_validos = {"APROBADO", "RECHAZADO", "EN_VERIFICACION", "AUTO"}

        if nuevo_estado not in estados_validos:
            messages.error(request, "Estado manual inválido.")
            return redirect("detalle_plano", plano_id=resultado.plano.id)

        if nuevo_estado == "AUTO":
            resultado.estado_resultado_manual = None
            resultado.fue_revisado_manual = False
            resultado.revisado_por = None
            resultado.fecha_revision_manual = None
            resultado.motivo_revision_manual = None
            accion = "LIMPIAR_ESTADO_MANUAL_NR"
            descripcion = (
                f"Se eliminó el estado manual del NR {resultado.nr_detectado} "
                f"del plano {resultado.plano.id_plano_deposito}"
            )
        else:
            resultado.estado_resultado_manual = nuevo_estado
            resultado.fue_revisado_manual = True
            resultado.revisado_por = str(request.user)
            resultado.fecha_revision_manual = timezone.now()
            if motivo:
                resultado.motivo_revision_manual = motivo
            else:
                resultado.motivo_revision_manual = (
                    f"El administrador cambió manualmente el estado del NR a {nuevo_estado}."
                )
            accion = "CAMBIAR_ESTADO_MANUAL_NR"
            descripcion = (
                f"Se cambió manualmente el estado del NR {resultado.nr_detectado} "
                f"a {nuevo_estado} en el plano {resultado.plano.id_plano_deposito}"
            )

        resultado.save(
            update_fields=[
                "estado_resultado_manual",
                "fue_revisado_manual",
                "revisado_por",
                "fecha_revision_manual",
                "motivo_revision_manual",
            ]
        )

        recalcular_estado_plano_desde_resultados(resultado.plano)

        Auditoria.objects.create(
            plano=resultado.plano,
            carpeta=resultado.plano.carpeta,
            usuario=str(request.user),
            accion=accion,
            descripcion=descripcion,
            entidad="ResultadoValidacionPlano",
            entidad_id=str(resultado.id),
        )

        messages.success(request, "Estado del NR actualizado correctamente.")

    return redirect("detalle_plano", plano_id=resultado.plano.id)

@login_required
def procesar_plano_view(request, plano_id):
    require_admin_or_funcionario(request)

    plano = get_object_or_404(Plano, id=plano_id, eliminado=False)

    if request.method == "POST":
        es_reproceso = bool(plano.procesado)

        extractor = "gpt"

        accion_base = "REPROCESAR_PLANO" if es_reproceso else "INICIAR_PROCESAMIENTO_PLANO"
        accion = f"{accion_base}_{extractor.upper()}"

        descripcion = (
            f"Se solicitó reprocesar el plano {plano.id_plano_deposito} usando {extractor.upper()}"
            if es_reproceso
            else f"Se solicitó procesar el plano {plano.id_plano_deposito} usando {extractor.upper()}"
        )

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion=accion,
            descripcion=descripcion,
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        procesar_plano_completo(
            plano,
            usuario=str(request.user),
            extractor=extractor,
        )
        _set_plano_en_edicion(request, plano.id, True)

    return redirect("detalle_plano", plano_id=plano.id)


@login_required
def guardar_plano_view(request, plano_id):
    require_admin_or_funcionario(request)

    plano = get_object_or_404(Plano, id=plano_id, eliminado=False)

    if request.method == "POST":
        _set_plano_en_edicion(request, plano.id, False)

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="GUARDAR_PLANO",
            descripcion=f"Se confirmó y guardó el plano {plano.id_plano_deposito}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )

    return redirect("detalle_plano", plano_id=plano.id)


@login_required
def resumen_plano_view(request, plano_id):
    plano = get_object_or_404(
        Plano.objects.select_related("carpeta", "carpeta__empresa"),
        id=plano_id,
        eliminado=False,
    )

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        if plano.carpeta.empresa_id != perfil.empresa_id:
            raise PermissionDenied("No puedes acceder a este plano.")

    if not plano.procesado or _plano_en_edicion(request, plano.id):
        return redirect("detalle_plano", plano_id=plano.id)

    resultados_qs = (
        plano.resultados_validacion
        .select_related("nr_materiales_encontrado", "reclamo_encontrado")
        .prefetch_related("materiales_detectados")
        .all()
    )

    resultados = [_build_resultado_detalle(r) for r in resultados_qs]

    total_nr = len(resultados)
    total_aprobados = sum(1 for r in resultados if r["estado_resultado_final"] == "APROBADO")
    total_rechazados = sum(1 for r in resultados if r["estado_resultado_final"] == "RECHAZADO")
    total_en_verificacion = sum(1 for r in resultados if r["estado_resultado_final"] == "EN_VERIFICACION")

    return render(request, "resumen_plano.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Resumen del plano",
        "plano": plano,
        "resultados": resultados,
        "total_nr": total_nr,
        "total_aprobados": total_aprobados,
        "total_rechazados": total_rechazados,
        "total_en_verificacion": total_en_verificacion,
    })

@login_required
@transaction.atomic
def cancelar_plano_view(request, plano_id):
    require_admin_or_funcionario(request)

    plano = get_object_or_404(
        Plano,
        id=plano_id,
        eliminado=False
    )

    if request.method == "POST":

        carpeta_id = plano.carpeta.id
        plano_codigo = plano.id_plano_deposito

        _set_plano_en_edicion(request, plano.id, False)

        # Eliminar archivo físico del storage
        if plano.archivo:
            plano.archivo.delete(save=False)

        # Auditoría previa a eliminación
        Auditoria.objects.create(
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="ELIMINAR_PLANO_FISICO",
            descripcion=f"Plano eliminado físicamente: {plano_codigo}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        # Eliminación física completa
        plano.delete()

        return redirect(
            "detalle_carpeta",
            carpeta_id=carpeta_id
        )

    return redirect(
        "detalle_plano",
        plano_id=plano.id
    )


@login_required
@user_passes_test(user_is_admin)
def gestion_usuarios_view(request):
    usuarios = User.objects.select_related("perfil").all().order_by("username")

    return render(request, "gestion_usuarios/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Gestión de usuarios",
        "usuarios": usuarios,
    })


@login_required
@user_passes_test(user_is_admin)
def crear_usuario_view(request):
    if request.method == "POST":
        form = UsuarioCrearForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Usuario creado correctamente.")
            return redirect("gestion_usuarios")
    else:
        form = UsuarioCrearForm()

    return render(request, "gestion_usuarios/form.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Crear usuario",
        "form": form,
        "modo": "crear",
    })


@login_required
@user_passes_test(user_is_admin)
def editar_usuario_view(request, user_id):
    usuario = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        form = UsuarioEditarForm(request.POST, user_instance=usuario)
        if form.is_valid():
            form.save()
            messages.success(request, "Usuario actualizado correctamente.")
            return redirect("gestion_usuarios")
    else:
        form = UsuarioEditarForm(user_instance=usuario)

    return render(request, "gestion_usuarios/form.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Editar usuario",
        "form": form,
        "modo": "editar",
        "usuario_obj": usuario,
    })


@login_required
@user_passes_test(user_is_admin)
def toggle_usuario_activo_view(request, user_id):
    usuario = get_object_or_404(User, pk=user_id)

    if usuario == request.user:
        messages.warning(request, "No puedes desactivar tu propio usuario.")
        return redirect("gestion_usuarios")

    usuario.is_active = not usuario.is_active
    usuario.save()

    perfil, _ = PerfilUsuario.objects.get_or_create(user=usuario)
    perfil.activo = usuario.is_active
    perfil.save()

    estado = "activado" if usuario.is_active else "desactivado"
    messages.success(request, f"Usuario {estado} correctamente.")

    return redirect("gestion_usuarios")


@login_required
@user_passes_test(user_is_admin)
def empresas_contratistas_view(request):
    q = request.GET.get("q", "").strip()

    empresas = EmpresaContratista.objects.all()

    if q:
        empresas = empresas.filter(
            Q(nombre__icontains=q) |
            Q(ruc__icontains=q)
        )

    empresas = empresas.order_by("-activo", "nombre")

    total_empresas = empresas.count()
    activas = empresas.filter(activo=True).count()

    return render(request, "empresas_contratistas/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Empresas contratistas",
        "empresas": empresas,
        "filtros": {"q": q},
        "total_empresas": total_empresas,
        "empresas_activas": activas,
    })


@login_required
def auditoria_view(request):
    require_admin_or_funcionario(request)

    query = request.GET.get("q", "").strip()
    accion = request.GET.get("accion", "").strip()
    entidad = request.GET.get("entidad", "").strip()
    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()

    auditorias = Auditoria.objects.all().order_by("-fecha")

    if query:
        auditorias = auditorias.filter(
            Q(usuario__icontains=query) |
            Q(accion__icontains=query) |
            Q(descripcion__icontains=query) |
            Q(entidad__icontains=query) |
            Q(entidad_id__icontains=query)
        )

    if accion:
        auditorias = auditorias.filter(accion=accion)

    if entidad:
        auditorias = auditorias.filter(entidad=entidad)

    if fecha_desde:
        auditorias = auditorias.filter(fecha__date__gte=fecha_desde)

    if fecha_hasta:
        auditorias = auditorias.filter(fecha__date__lte=fecha_hasta)

    acciones = (
        Auditoria.objects
        .exclude(accion__isnull=True)
        .exclude(accion__exact="")
        .values_list("accion", flat=True)
        .distinct()
        .order_by("accion")
    )

    entidades = (
        Auditoria.objects
        .exclude(entidad__isnull=True)
        .exclude(entidad__exact="")
        .values_list("entidad", flat=True)
        .distinct()
        .order_by("entidad")
    )

    return render(request, "auditoria/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Auditoría",
        "auditorias": auditorias,
        "acciones": acciones,
        "entidades": entidades,
        "filtros": {
            "q": query,
            "accion": accion,
            "entidad": entidad,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
        },
    })


@login_required
def estadisticas_view(request):
    require_admin_or_funcionario(request)

    total_carpetas = Carpeta.objects.filter(eliminada=False).count()
    total_planos = Plano.objects.filter(eliminado=False).count()
    total_planos_procesados = Plano.objects.filter(eliminado=False, procesado=True).count()
    total_planos_pendientes = Plano.objects.filter(eliminado=False, procesado=False).count()
    total_usuarios_activos = User.objects.filter(is_active=True).count()

    carpetas_por_estado_qs = (
        Carpeta.objects
        .filter(eliminada=False)
        .values("estado")
        .order_by("estado")
        .annotate(total=Count("id"))
    )

    planos_por_estado_qs = (
        Plano.objects
        .filter(eliminado=False)
        .values("estado")
        .order_by("estado")
        .annotate(total=Count("id"))
    )

    usuarios_por_rol_qs = (
        PerfilUsuario.objects
        .values("rol")
        .order_by("rol")
        .annotate(total=Count("id"))
    )

    planos_por_empresa_qs = (
        Plano.objects
        .filter(eliminado=False)
        .values("carpeta__empresa__nombre")
        .annotate(total=Count("id"))
        .order_by("-total", "carpeta__empresa__nombre")
    )

    planos_por_empresa = []
    for item in planos_por_empresa_qs:
        nombre_empresa = item.get("carpeta__empresa__nombre") or "Sin empresa"
        planos_por_empresa.append({
            "empresa": nombre_empresa,
            "total": item["total"],
        })

    return render(request, "estadisticas/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Estadísticas",
        "resumen": {
            "total_carpetas": total_carpetas,
            "total_planos": total_planos,
            "total_planos_procesados": total_planos_procesados,
            "total_planos_pendientes": total_planos_pendientes,
            "total_usuarios_activos": total_usuarios_activos,
        },
        "carpetas_por_estado": carpetas_por_estado_qs,
        "planos_por_estado": planos_por_estado_qs,
        "usuarios_por_rol": usuarios_por_rol_qs,
        "planos_por_empresa": planos_por_empresa,
        "estadisticas_v2_pendiente": True,
    })


@login_required
def reportes_view(request):
    require_admin_or_funcionario(request)

    export = request.GET.get("export")
    tab = request.GET.get("tab", "general").strip() or "general"

    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    estado = request.GET.get("estado", "").strip()
    nr_busqueda = request.GET.get("nr", "").strip()
    filtro_nivel_respuesta = request.GET.get("nivel_respuesta", "").strip()
    respuesta_fecha_reclamo = request.GET.get("respuesta_fecha_reclamo", "").strip()
    respuesta_fecha_trabajo = request.GET.get("respuesta_fecha_trabajo", "").strip()
    respuesta_dias_min = request.GET.get("respuesta_dias_min", "").strip()
    respuesta_dias_max = request.GET.get("respuesta_dias_max", "").strip()
    respuesta_orden = request.GET.get("respuesta_orden", "dias_desc").strip()
    material_busqueda = request.GET.get("material", "").strip()
    tipo_material = request.GET.get("tipo_material", "contiene").strip()
    nivel_consumo = request.GET.get("nivel_consumo", "").strip()
    tipo_rechazo_busqueda = request.GET.get("tipo_rechazo", "").strip()
    coincidencia_rechazo = request.GET.get("coincidencia_rechazo", "contiene").strip()
    nivel_impacto_rechazo = request.GET.get("nivel_impacto_rechazo", "").strip()
    orden_rechazo = request.GET.get("orden_rechazo", "mayor").strip()
    carpeta_id = request.GET.get("carpeta", "").strip()
    localidad = request.GET.get("localidad", "").strip()
    zona = request.GET.get("zona", "").strip()
    respuesta_fecha_reclamo = request.GET.get("respuesta_fecha_reclamo", "").strip()
    respuesta_fecha_trabajo = request.GET.get("respuesta_fecha_trabajo", "").strip()
    respuesta_dias_max = request.GET.get("respuesta_dias_max", "").strip()
    respuesta_orden = request.GET.get("respuesta_orden", "dias_desc").strip()
    metrica = request.GET.get("metrica", "").strip()
    estado_operativo = request.GET.get("estado_operativo", "").strip()
    orden = request.GET.get("orden", "").strip()
    metrica_efectividad = request.GET.get("metrica_efectividad", "").strip()
    nivel_rendimiento = request.GET.get("nivel_rendimiento", "").strip()
    vista_efectividad = request.GET.get("vista_efectividad", "").strip()
    efectividad_mensual_anio = request.GET.get("efectividad_mensual_anio", "").strip()
    efectividad_mensual_mes = request.GET.get("efectividad_mensual_mes", "").strip()

    if efectividad_mensual_mes:
        efectividad_mensual_mes = efectividad_mensual_mes.zfill(2)
    efectividad_mensual_nivel = request.GET.get("efectividad_mensual_nivel", "").strip()
    efectividad_mensual_orden = request.GET.get("efectividad_mensual_orden", "efectividad_desc").strip()

    carpetas = Carpeta.objects.filter(eliminada=False).select_related("empresa")
    planos = Plano.objects.filter(eliminado=False).select_related("carpeta", "carpeta__empresa")

    resultados = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa",
        "reclamo_encontrado",
        "nr_materiales_encontrado",
    ).prefetch_related("materiales_detectados")

    if fecha_desde:
        planos = planos.filter(fecha_carga__date__gte=fecha_desde)
        resultados = resultados.filter(plano__fecha_carga__date__gte=fecha_desde)
        carpetas = carpetas.filter(fecha_creacion__date__gte=fecha_desde)

    if fecha_hasta:
        planos = planos.filter(fecha_carga__date__lte=fecha_hasta)
        resultados = resultados.filter(plano__fecha_carga__date__lte=fecha_hasta)
        carpetas = carpetas.filter(fecha_creacion__date__lte=fecha_hasta)

    if empresa_id:
        planos = planos.filter(carpeta__empresa_id=empresa_id)
        resultados = resultados.filter(plano__carpeta__empresa_id=empresa_id)
        carpetas = carpetas.filter(empresa_id=empresa_id)

    if carpeta_id:
        planos = planos.filter(carpeta_id=carpeta_id)
        resultados = resultados.filter(plano__carpeta_id=carpeta_id)

    if estado:
        planos = planos.filter(estado=estado)
        resultados = resultados.filter(estado_resultado=estado)

    if localidad:
        resultados = resultados.filter(reclamo_encontrado__ciudad__iexact=localidad)

    if zona:
        resultados = resultados.filter(reclamo_encontrado__zona__iexact=zona)

    empresas = EmpresaContratista.objects.all().order_by("nombre")

    total_planos = planos.count()
    total_nr = resultados.count()
    total_aprobados = 0
    total_rechazados = 0
    total_verificacion = 0

    for resultado in resultados:
        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final == "APROBADO":
            total_aprobados += 1
        elif estado_final == "RECHAZADO":
            total_rechazados += 1
        elif estado_final == "EN_VERIFICACION":
            total_verificacion += 1

    efectividad_general = round((total_aprobados / total_nr) * 100, 2) if total_nr else 0

    # =========================
    # AISLAMIENTO DE FILTROS POR MODAL
    # =========================

    if tab != "materiales":
        carpeta_id = ""

    if tab not in ["reclamos", "estados", "respuesta"]:
        localidad = ""
        zona = ""

    if tab != "trabajos":
        metrica = ""
        orden = ""

    if tab != "estados":
        estado_operativo = ""

    if tab != "efectividad":
        metrica_efectividad = ""
        nivel_rendimiento = ""
        vista_efectividad = ""

    if tab != "rechazos":
        tipo_rechazo_busqueda = ""
        coincidencia_rechazo = "contiene"
        nivel_impacto_rechazo = ""
        orden_rechazo = "mayor"

    # =========================
    # MATERIALES POR EMPRESA / CARPETA
    # =========================
    materiales_map = defaultdict(lambda: {
        "empresa": "",
        "carpeta": "",
        "material": "",
        "unidad": "",
        "cantidad_total": 0,
        "apariciones": 0,
    })

    if nr_busqueda:
        resultados = resultados.filter(
            nr_detectado__icontains=nr_busqueda
        )

    for resultado in resultados:
        empresa = resultado.plano.carpeta.empresa.nombre if resultado.plano.carpeta.empresa else "Sin empresa"
        carpeta = resultado.plano.carpeta.codigo_carpeta

        for material in resultado.materiales_detectados.all():
            descripcion = material.descripcion_final or "Sin descripción"
            unidad = material.unidad_final or "unidad"
            cantidad_raw = material.cantidad_final or "0"

            try:
                cantidad = float(str(cantidad_raw).replace(",", "."))
            except Exception:
                cantidad = 0

            key = (empresa, carpeta, descripcion, unidad)

            materiales_map[key]["empresa"] = empresa
            materiales_map[key]["carpeta"] = carpeta
            materiales_map[key]["material"] = descripcion
            materiales_map[key]["unidad"] = unidad
            materiales_map[key]["cantidad_total"] += cantidad
            materiales_map[key]["apariciones"] += 1

    reporte_materiales = sorted(
        materiales_map.values(),
        key=lambda x: x["cantidad_total"],
        reverse=True
    )
    if material_busqueda:
        material_query = material_busqueda.lower()

        if tipo_material == "exacta":
            reporte_materiales = [
                item for item in reporte_materiales
                if item["material"].lower() == material_query
            ]

        elif tipo_material == "inicia":
            reporte_materiales = [
                item for item in reporte_materiales
                if item["material"].lower().startswith(material_query)
            ]

        else:
            reporte_materiales = [
                item for item in reporte_materiales
                if material_query in item["material"].lower()
            ]

    if nivel_consumo:
        def resolver_nivel_consumo(cantidad):
            if cantidad > 20:
                return "critico"
            if cantidad > 10:
                return "alto"
            if cantidad >= 5:
                return "medio"
            return "bajo"

        reporte_materiales = [
            item for item in reporte_materiales
            if resolver_nivel_consumo(item["cantidad_total"]) == nivel_consumo
        ]

    material_top = reporte_materiales[0] if reporte_materiales else None

    total_materiales_operativos = sum(
        item["cantidad_total"]
        for item in reporte_materiales
    )

    empresa_top = None
    carpeta_top = None

    if reporte_materiales:

        empresa_consumo = {}
        carpeta_consumo = {}

        for item in reporte_materiales:

            empresa = item["empresa"]
            carpeta = item["carpeta"]

            empresa_consumo[empresa] = (
                empresa_consumo.get(empresa, 0)
                + item["cantidad_total"]
            )

            carpeta_consumo[carpeta] = (
                carpeta_consumo.get(carpeta, 0)
                + item["cantidad_total"]
            )

        empresa_top = max(
            empresa_consumo.items(),
            key=lambda x: x[1]
        )

        carpeta_top = max(
            carpeta_consumo.items(),
            key=lambda x: x[1]
        )        
    # =========================
    # RECLAMOS POR LOCALIDAD / BARRIO / EMPRESA
    # =========================
    reclamos_map = defaultdict(lambda: {
        "ciudad": "",
        "zona": "",
        "empresa": "",
        "total": 0,
        "aprobados": 0,
        "rechazados": 0,
        "verificacion": 0,
    })

    for resultado in resultados:
        reclamo = resultado.reclamo_encontrado
        empresa = resultado.plano.carpeta.empresa.nombre if resultado.plano.carpeta.empresa else "Sin empresa"

        ciudad = reclamo.ciudad if reclamo else "Sin ciudad"
        zona_item = reclamo.zona if reclamo else "Sin zona"

        key = (ciudad, zona_item, empresa)

        reclamos_map[key]["ciudad"] = ciudad
        reclamos_map[key]["zona"] = zona_item
        reclamos_map[key]["empresa"] = empresa
        reclamos_map[key]["total"] += 1

        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final == "APROBADO":
            reclamos_map[key]["aprobados"] += 1
        elif estado_final == "RECHAZADO":
            reclamos_map[key]["rechazados"] += 1
        elif estado_final == "EN_VERIFICACION":
            reclamos_map[key]["verificacion"] += 1

    reporte_reclamos = sorted(
        reclamos_map.values(),
        key=lambda x: (-x["total"], x["ciudad"], x["zona"])
    )

    if estado_operativo == "aprobados":
        reporte_reclamos = [item for item in reporte_reclamos if item["aprobados"] > 0]
        reporte_reclamos = sorted(reporte_reclamos, key=lambda x: x["aprobados"], reverse=True)

    elif estado_operativo == "no_concretados":
        reporte_reclamos = [item for item in reporte_reclamos if item["rechazados"] > 0]
        reporte_reclamos = sorted(reporte_reclamos, key=lambda x: x["rechazados"], reverse=True)

    elif estado_operativo == "verificacion":
        reporte_reclamos = [item for item in reporte_reclamos if item["verificacion"] > 0]
        reporte_reclamos = sorted(reporte_reclamos, key=lambda x: x["verificacion"], reverse=True)

    # =========================
    # TRABAJOS / EFECTIVIDAD POR EMPRESA
    # =========================
    empresas_map = defaultdict(lambda: {
        "empresa": "",
        "total_carpetas": 0,
        "total_planos": 0,
        "total_nr": 0,
        "aprobados": 0,
        "rechazados": 0,
        "verificacion": 0,
        "efectividad": 0,
    })

    for carpeta in carpetas:
        empresa = carpeta.empresa.nombre if carpeta.empresa else "Sin empresa"

        empresas_map[empresa]["empresa"] = empresa
        empresas_map[empresa]["total_carpetas"] += 1

    for plano in planos:
        empresa = plano.carpeta.empresa.nombre if plano.carpeta.empresa else "Sin empresa"

        empresas_map[empresa]["empresa"] = empresa
        empresas_map[empresa]["total_planos"] += 1

    for resultado in resultados:
        empresa = resultado.plano.carpeta.empresa.nombre if resultado.plano.carpeta.empresa else "Sin empresa"

        empresas_map[empresa]["empresa"] = empresa
        empresas_map[empresa]["total_nr"] += 1

        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final == "APROBADO":
            empresas_map[empresa]["aprobados"] += 1
        elif estado_final == "RECHAZADO":
            empresas_map[empresa]["rechazados"] += 1
        elif estado_final == "EN_VERIFICACION":
            empresas_map[empresa]["verificacion"] += 1

    for item in empresas_map.values():
        item["efectividad"] = round((item["aprobados"] / item["total_nr"]) * 100, 2) if item["total_nr"] else 0


    mapa_metricas_efectividad = {
        "efectividad_desc": "efectividad_desc",
        "concretados_desc": "aprobados",
        "volumen": "total_nr",
        "rechazos": "rechazados",
        "pendientes": "verificacion",
        "carpetas_desc": "total_carpetas",
        "carpetas_asc": "carpetas_asc",
        "planos_desc": "total_planos",
        "planos_asc": "planos_asc",

    }

    criterio_efectividad = mapa_metricas_efectividad.get(metrica_efectividad, "")


    criterio_empresas = metrica or orden or criterio_efectividad

    if criterio_empresas == "empresa_az":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["empresa"])



    elif criterio_empresas == "total_planos":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["total_planos"], reverse=True)

    elif criterio_empresas == "total_carpetas":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["total_carpetas"], reverse=True)

    elif criterio_empresas == "carpetas_asc":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["total_carpetas"])

    elif criterio_empresas == "planos_asc":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["total_planos"])
        
    elif criterio_empresas == "total_nr":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["total_nr"], reverse=True)

    elif criterio_empresas == "aprobados":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["aprobados"], reverse=True)

    elif criterio_empresas == "efectividad_desc":
        reporte_empresas = sorted(
            empresas_map.values(),
            key=lambda x: (-x["efectividad"], -x["total_nr"], x["empresa"])
        )        

    elif criterio_empresas == "rechazados":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["rechazados"], reverse=True)

    elif criterio_empresas == "verificacion":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["verificacion"], reverse=True)

    else:
        reporte_empresas = sorted(
            empresas_map.values(),
            key=lambda x: (-x["efectividad"], -x["total_nr"], x["empresa"])
        )
    reporte_empresas_base = list(reporte_empresas)

    for index, item in enumerate(reporte_empresas_base, start=1):
        item["ranking"] = index

    if nivel_rendimiento == "alto":
        reporte_empresas = [
            item for item in reporte_empresas_base
            if item["efectividad"] >= 85
        ]

    elif nivel_rendimiento == "medio":
        reporte_empresas = [
            item for item in reporte_empresas_base
            if 60 <= item["efectividad"] < 85
        ]

    elif nivel_rendimiento == "bajo":
        reporte_empresas = [
            item for item in reporte_empresas_base
            if item["efectividad"] < 60
        ]

    else:
        reporte_empresas = reporte_empresas_base

    # =========================
    # EFECTIVIDAD MENSUAL POR EMPRESA
    # =========================
    efectividad_mensual_map = defaultdict(lambda: {
        "periodo": "",
        "anio": "",
        "mes": "",
        "empresa": "",
        "carpeta": "",
        "codigo_carpeta": "",
        "total_planos": 0,
        "total_nr": 0,
        "aprobados": 0,
        "rechazados": 0,
        "verificacion": 0,
        "efectividad": 0,
        "nivel": "",
        "_planos_ids": set(),
    })

    for resultado in resultados:
        plano = resultado.plano
        carpeta = plano.carpeta if plano else None

        if not carpeta:
            continue

        empresa = carpeta.empresa.nombre if carpeta.empresa else "Sin empresa"

        anio = str(carpeta.anio) if getattr(carpeta, "anio", None) else ""
        mes = str(carpeta.mes).zfill(2) if getattr(carpeta, "mes", None) else ""

        if not anio or not mes:
            fecha_base = plano.fecha_carga
            anio = str(fecha_base.year)
            mes = str(fecha_base.month).zfill(2)

        if efectividad_mensual_anio and anio != efectividad_mensual_anio:
            continue

        if efectividad_mensual_mes and mes != efectividad_mensual_mes.zfill(2):
            continue

        key = (anio, mes, empresa, carpeta.codigo_carpeta)

        item = efectividad_mensual_map[key]
        item["anio"] = anio
        item["mes"] = mes
        item["periodo"] = f"{mes}/{anio}"
        item["empresa"] = empresa
        item["carpeta"] = carpeta.codigo_carpeta
        item["codigo_carpeta"] = carpeta.codigo_carpeta
        item["_planos_ids"].add(plano.id)
        item["total_nr"] += 1

        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final == "APROBADO":
            item["aprobados"] += 1
        elif estado_final == "RECHAZADO":
            item["rechazados"] += 1
        elif estado_final == "EN_VERIFICACION":
            item["verificacion"] += 1

    for item in efectividad_mensual_map.values():
        item["total_planos"] = len(item["_planos_ids"])
        item["efectividad"] = round((item["aprobados"] / item["total_nr"]) * 100, 2) if item["total_nr"] else 0

        if item["efectividad"] >= 95:
            item["nivel"] = "EXCELENTE"
        elif item["efectividad"] >= 85:
            item["nivel"] = "OPTIMO"
        elif item["efectividad"] >= 70:
            item["nivel"] = "RIESGO"
        else:
            item["nivel"] = "CRITICO"

        item.pop("_planos_ids", None)

    reporte_efectividad_mensual = list(efectividad_mensual_map.values())

    if empresa_id:
        empresa_filtrada = EmpresaContratista.objects.filter(id=empresa_id).first()
        if empresa_filtrada:
            reporte_efectividad_mensual = [
                item for item in reporte_efectividad_mensual
                if item["empresa"] == empresa_filtrada.nombre
            ]

    if efectividad_mensual_nivel:
        reporte_efectividad_mensual = [
            item for item in reporte_efectividad_mensual
            if item["nivel"] == efectividad_mensual_nivel
        ]

    if efectividad_mensual_orden == "periodo_desc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: (x["anio"], x["mes"], x["empresa"]),
            reverse=True
        )
    elif efectividad_mensual_orden == "periodo_asc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: (x["anio"], x["mes"], x["empresa"])
        )
    elif efectividad_mensual_orden == "nr_desc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: x["total_nr"],
            reverse=True
        )
    elif efectividad_mensual_orden == "rechazos_desc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: x["rechazados"],
            reverse=True
        )
    else:
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: (-x["efectividad"], -x["total_nr"], x["empresa"])
        )

    resumen_efectividad_mensual = {
        "empresas": len({item["empresa"] for item in reporte_efectividad_mensual}),
        "carpetas": len({item["codigo_carpeta"] for item in reporte_efectividad_mensual}),
        "total_nr": sum(item["total_nr"] for item in reporte_efectividad_mensual),
        "efectividad_promedio": round(
            sum(item["efectividad"] for item in reporte_efectividad_mensual) / len(reporte_efectividad_mensual),
            2
        ) if reporte_efectividad_mensual else 0,
    }
        
    # =========================
    # TIPO DE RECHAZO MÁS FRECUENTE
    # =========================
    tipos_rechazo_counter = Counter()

    for resultado in resultados:
        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final != "RECHAZADO":
            continue

        if not resultado.ciudad_ok:
            tipos_rechazo_counter["Ciudad no coincide"] += 1
        if not resultado.zona_ok:
            tipos_rechazo_counter["Zona no coincide"] += 1
        if not resultado.fecha_ok:
            tipos_rechazo_counter["Fecha inválida"] += 1
        if resultado.materiales_requieren_revision:
            tipos_rechazo_counter["Materiales inconsistentes"] += 1

        motivo = str(resultado.motivo_resultado or "").lower()
        if "desconocido" in motivo:
            tipos_rechazo_counter["NR desconocido"] += 1

    reporte_tipos_rechazo = [
        {"tipo": tipo, "total": total}
        for tipo, total in tipos_rechazo_counter.most_common()
    ]

    if tipo_rechazo_busqueda:
        query = tipo_rechazo_busqueda.lower()

        if coincidencia_rechazo == "exacta":
            reporte_tipos_rechazo = [
                item for item in reporte_tipos_rechazo
                if item["tipo"].lower() == query
            ]

        elif coincidencia_rechazo == "inicia":
            reporte_tipos_rechazo = [
                item for item in reporte_tipos_rechazo
                if item["tipo"].lower().startswith(query)
            ]

        else:
            reporte_tipos_rechazo = [
                item for item in reporte_tipos_rechazo
                if query in item["tipo"].lower()
            ]


    def resolver_nivel_impacto_rechazo(total):
        if total >= 10:
            return "critico"
        if total >= 5:
            return "alto"
        if total >= 2:
            return "medio"
        return "bajo"


    if nivel_impacto_rechazo:
        reporte_tipos_rechazo = [
            item for item in reporte_tipos_rechazo
            if resolver_nivel_impacto_rechazo(item["total"]) == nivel_impacto_rechazo
        ]


    if orden_rechazo == "menor":
        reporte_tipos_rechazo = sorted(
            reporte_tipos_rechazo,
            key=lambda x: x["total"]
        )

    elif orden_rechazo == "tipo_az":
        reporte_tipos_rechazo = sorted(
            reporte_tipos_rechazo,
            key=lambda x: x["tipo"]
        )

    elif orden_rechazo == "tipo_za":
        reporte_tipos_rechazo = sorted(
            reporte_tipos_rechazo,
            key=lambda x: x["tipo"],
            reverse=True
        )

    else:
        reporte_tipos_rechazo = sorted(
            reporte_tipos_rechazo,
            key=lambda x: x["total"],
            reverse=True
        )

    # =========================
    # TIEMPO DE RESPUESTA
    # =========================
    tiempos = []

    if nr_busqueda:
        resultados = resultados.filter(
            nr_detectado__icontains=nr_busqueda
        )

    for resultado in resultados:
        reclamo = resultado.reclamo_encontrado
        nr = resultado.nr_materiales_encontrado

        valores_detectados = _resolver_valores_detectados_resultado(resultado)
        fecha_trabajo = valores_detectados.get("fecha") or (nr.fecha_trabajo if nr else None)

        if reclamo and fecha_trabajo and reclamo.fecha_reclamo:
            dias = max((fecha_trabajo - reclamo.fecha_reclamo).days, 0)

            if dias <= 3:
                nivel_operacional = "EXCELENTE"
            elif dias <= 7:
                nivel_operacional = "OPTIMO"
            elif dias <= 10:
                nivel_operacional = "ACEPTABLE"
            else:
                nivel_operacional = "CRITICO"

            if filtro_nivel_respuesta and nivel_operacional != filtro_nivel_respuesta:
                continue
            if localidad and reclamo.ciudad != localidad:
                continue

            if zona and reclamo.zona != zona:
                continue

            if respuesta_fecha_reclamo and str(reclamo.fecha_reclamo) != respuesta_fecha_reclamo:
                continue

            if respuesta_fecha_trabajo and str(fecha_trabajo) != respuesta_fecha_trabajo:
                continue

            if respuesta_dias_max:
                try:
                    if dias > int(respuesta_dias_max):
                        continue
                except ValueError:
                    pass
            tiempos.append({
                "nr": resultado.nr_detectado,
                "empresa": resultado.plano.carpeta.empresa.nombre if resultado.plano.carpeta.empresa else "Sin empresa",
                "ciudad": reclamo.ciudad,
                "zona": reclamo.zona,
                "fecha_reclamo": reclamo.fecha_reclamo,
                "fecha_trabajo": fecha_trabajo,
                "dias": dias,
                "nivel_respuesta": nivel_operacional,
            })

    promedio_respuesta = (
        round(sum(t["dias"] for t in tiempos) / len(tiempos), 2)
        if tiempos else 0
    )

    respuesta_rapida = min(
        [t["dias"] for t in tiempos],
        default=0
    )

    respuesta_lenta = max(
        [t["dias"] for t in tiempos],
        default=0
    )

    casos_criticos = len([
        t for t in tiempos
        if t["nivel_respuesta"] == "CRITICO"
    ])

    if respuesta_orden == "dias_asc":
        tiempos_respuesta = sorted(tiempos, key=lambda x: x["dias"])

    elif respuesta_orden == "nr_asc":
        tiempos_respuesta = sorted(tiempos, key=lambda x: x["nr"])

    elif respuesta_orden == "fecha_reclamo_desc":
        tiempos_respuesta = sorted(tiempos, key=lambda x: x["fecha_reclamo"], reverse=True)

    elif respuesta_orden == "fecha_trabajo_desc":
        tiempos_respuesta = sorted(tiempos, key=lambda x: x["fecha_trabajo"], reverse=True)

    else:
        tiempos_respuesta = sorted(tiempos, key=lambda x: x["dias"], reverse=True)


    localidades = sorted({
        item["ciudad"]
        for item in reporte_reclamos
        if item["ciudad"]
    })

    zonas = sorted({
        item["zona"]
        for item in reporte_reclamos
        if item["zona"]
    })

    zonas_territoriales = sorted({
        (item["ciudad"], item["zona"])
        for item in reporte_reclamos
        if item["ciudad"] and item["zona"]
    })

    return render(request, "reportes/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Reportes",
        "tab": tab,
        "empresas": empresas,
        "plano_estado_choices": Plano.ESTADO_CHOICES,
        "filtros": {
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
            "empresa": empresa_id,
            "estado": estado,
            "carpeta": carpeta_id,
            "localidad": localidad,
            "zona": zona,
            "metrica": metrica,
            "estado_operativo": estado_operativo,
            "orden": orden,
            "criterio_empresas": criterio_empresas,
            "metrica_efectividad": metrica_efectividad,
            "nivel_rendimiento": nivel_rendimiento,
            "vista_efectividad": vista_efectividad,
            "nr": nr_busqueda,
            "nivel_respuesta": filtro_nivel_respuesta,
            "material": material_busqueda,
            "tipo_material": tipo_material,
            "nivel_consumo": nivel_consumo,
            "tipo_rechazo": tipo_rechazo_busqueda,
            "coincidencia_rechazo": coincidencia_rechazo,
            "nivel_impacto_rechazo": nivel_impacto_rechazo,
            "orden_rechazo": orden_rechazo,
            "respuesta_fecha_reclamo": respuesta_fecha_reclamo,
            "respuesta_fecha_trabajo": respuesta_fecha_trabajo,
            "respuesta_dias_max": respuesta_dias_max,
            "respuesta_orden": respuesta_orden,
            "efectividad_mensual_anio": efectividad_mensual_anio,
            "efectividad_mensual_mes": efectividad_mensual_mes,
            "efectividad_mensual_nivel": efectividad_mensual_nivel,
            "efectividad_mensual_orden": efectividad_mensual_orden,

        },
        "resumen": {
            "total_planos": total_planos,
            "total_nr": total_nr,
            "total_aprobados": total_aprobados,
            "total_rechazados": total_rechazados,
            "total_verificacion": total_verificacion,
            "efectividad_general": efectividad_general,
            "promedio_respuesta": promedio_respuesta,
            "respuesta_rapida": respuesta_rapida,
            "respuesta_lenta": respuesta_lenta,
            "casos_criticos": casos_criticos,
            "material_top": material_top,
            "total_materiales_operativos": total_materiales_operativos,
            "empresa_top_consumo": empresa_top,
            "carpeta_top_consumo": carpeta_top,
        },
        "reporte_materiales": reporte_materiales,
        "reporte_reclamos": reporte_reclamos,
        "reporte_empresas": reporte_empresas,
        "reporte_efectividad_mensual": reporte_efectividad_mensual,
        "resumen_efectividad_mensual": resumen_efectividad_mensual,
        "reporte_tipos_rechazo": reporte_tipos_rechazo,
        "tiempos_respuesta": tiempos_respuesta,
        "carpetas": carpetas,
        "localidades": localidades,
        "zonas": zonas,
        "zonas_territoriales": zonas_territoriales,
    })

@login_required
def reporte_tiempos_print_view(request):
    require_admin_or_funcionario(request)

    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    carpeta_id = request.GET.get("carpeta", "").strip()
    nr_busqueda = request.GET.get("nr", "").strip()
    filtro_nivel_respuesta = request.GET.get("nivel_respuesta", "").strip()
    tipo_rechazo_busqueda = request.GET.get("tipo_rechazo", "").strip()
    coincidencia_rechazo = request.GET.get("coincidencia_rechazo", "contiene").strip()
    nivel_impacto_rechazo = request.GET.get("nivel_impacto_rechazo", "").strip()
    orden_rechazo = request.GET.get("orden_rechazo", "mayor").strip()

    resultados = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa",
        "reclamo_encontrado",
        "nr_materiales_encontrado",
    )

    if fecha_desde:
        resultados = resultados.filter(plano__fecha_carga__date__gte=fecha_desde)

    if fecha_hasta:
        resultados = resultados.filter(plano__fecha_carga__date__lte=fecha_hasta)

    if empresa_id:
        resultados = resultados.filter(plano__carpeta__empresa_id=empresa_id)

    if carpeta_id:
        resultados = resultados.filter(plano__carpeta_id=carpeta_id)

    if nr_busqueda:
        nr_limpio = (
            nr_busqueda.upper()
            .replace("NR", "")
            .replace("N°", "")
            .replace("Nº", "")
            .strip()
        )

        resultados = resultados.filter(
            Q(nr_detectado__icontains=nr_limpio) |
            Q(nr_normalizado__icontains=nr_limpio)
        )

    tiempos = []

    for resultado in resultados:
        reclamo = resultado.reclamo_encontrado
        nr = resultado.nr_materiales_encontrado

        valores_detectados = _resolver_valores_detectados_resultado(resultado)
        fecha_trabajo = valores_detectados.get("fecha") or (nr.fecha_trabajo if nr else None)

        if reclamo and fecha_trabajo and reclamo.fecha_reclamo:
            dias = max((fecha_trabajo - reclamo.fecha_reclamo).days, 0)

            if dias <= 3:
                nivel_operacional = "EXCELENTE"
            elif dias <= 7:
                nivel_operacional = "OPTIMO"
            elif dias <= 10:
                nivel_operacional = "ACEPTABLE"
            else:
                nivel_operacional = "CRITICO"

            if filtro_nivel_respuesta and nivel_operacional != filtro_nivel_respuesta:
                continue

            tiempos.append({
                "nr": resultado.nr_detectado,
                "empresa": resultado.plano.carpeta.empresa.nombre if resultado.plano.carpeta.empresa else "Sin empresa",
                "ciudad": reclamo.ciudad,
                "zona": reclamo.zona,
                "fecha_reclamo": reclamo.fecha_reclamo,
                "fecha_trabajo": fecha_trabajo,
                "dias": dias,
                "nivel_respuesta": nivel_operacional,
            })

    tiempos_respuesta = sorted(tiempos, key=lambda x: x["dias"], reverse=True)

    resumen = {
        "promedio_respuesta": round(sum(t["dias"] for t in tiempos) / len(tiempos), 2) if tiempos else 0,
        "respuesta_rapida": min([t["dias"] for t in tiempos], default=0),
        "respuesta_lenta": max([t["dias"] for t in tiempos], default=0),
        "casos_criticos": len([t for t in tiempos if t["nivel_respuesta"] == "CRITICO"]),
    }

    empresa_nombre = ""
    carpeta_codigo = ""

    if empresa_id:
        empresa = EmpresaContratista.objects.filter(id=empresa_id).first()
        empresa_nombre = empresa.nombre if empresa else ""

    if carpeta_id:
        carpeta = Carpeta.objects.filter(id=carpeta_id).first()
        carpeta_codigo = carpeta.codigo_carpeta if carpeta else ""

    filtros = {
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "empresa_nombre": empresa_nombre,
        "carpeta_codigo": carpeta_codigo,
        "nr": nr_busqueda,
        "nivel_respuesta": filtro_nivel_respuesta,
    }

    return render(request, "reportes/print/tiempos_print.html", {
        "resumen": resumen,
        "tiempos_respuesta": tiempos_respuesta,
        "filtros": filtros,
    })

@login_required
def reporte_materiales_print_view(request):
    require_admin_or_funcionario(request)

    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    carpeta_id = request.GET.get("carpeta", "").strip()
    material_busqueda = request.GET.get("material", "").strip()
    tipo_material = request.GET.get("tipo_material", "contiene").strip()
    nivel_consumo = request.GET.get("nivel_consumo", "").strip()

    resultados = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa",
    ).prefetch_related("materiales_detectados")

    if fecha_desde:
        resultados = resultados.filter(plano__fecha_carga__date__gte=fecha_desde)

    if fecha_hasta:
        resultados = resultados.filter(plano__fecha_carga__date__lte=fecha_hasta)

    if empresa_id:
        resultados = resultados.filter(plano__carpeta__empresa_id=empresa_id)

    if carpeta_id:
        resultados = resultados.filter(plano__carpeta_id=carpeta_id)

    materiales_map = defaultdict(lambda: {
        "empresa": "",
        "carpeta": "",
        "material": "",
        "unidad": "",
        "cantidad_total": 0,
    })

    for resultado in resultados:
        empresa = (
            resultado.plano.carpeta.empresa.nombre
            if resultado.plano.carpeta.empresa
            else "Sin empresa"
        )
        carpeta = resultado.plano.carpeta.codigo_carpeta

        for material in resultado.materiales_detectados.all():
            descripcion = material.descripcion_final or "Sin descripción"
            unidad = material.unidad_final or "unidad"
            cantidad_raw = material.cantidad_final or "0"

            try:
                cantidad = float(str(cantidad_raw).replace(",", "."))
            except Exception:
                cantidad = 0

            key = (empresa, carpeta, descripcion, unidad)

            materiales_map[key]["empresa"] = empresa
            materiales_map[key]["carpeta"] = carpeta
            materiales_map[key]["material"] = descripcion
            materiales_map[key]["unidad"] = unidad
            materiales_map[key]["cantidad_total"] += cantidad

    reporte_materiales = sorted(
        materiales_map.values(),
        key=lambda x: x["cantidad_total"],
        reverse=True
    )

    if material_busqueda:
        material_query = material_busqueda.lower()

        if tipo_material == "exacta":
            reporte_materiales = [
                item for item in reporte_materiales
                if item["material"].lower() == material_query
            ]

        elif tipo_material == "inicia":
            reporte_materiales = [
                item for item in reporte_materiales
                if item["material"].lower().startswith(material_query)
            ]

        else:
            reporte_materiales = [
                item for item in reporte_materiales
                if material_query in item["material"].lower()
            ]
            
    if nivel_consumo:
        def resolver_nivel_consumo(cantidad):
            if cantidad > 20:
                return "critico"
            if cantidad > 10:
                return "alto"
            if cantidad >= 5:
                return "medio"
            return "bajo"

        reporte_materiales = [
            item for item in reporte_materiales
            if resolver_nivel_consumo(item["cantidad_total"]) == nivel_consumo
        ]


    material_top = reporte_materiales[0] if reporte_materiales else None

    total_materiales_operativos = sum(
        item["cantidad_total"]
        for item in reporte_materiales
    )

    empresa_top = None
    carpeta_top = None

    if reporte_materiales:
        empresa_consumo = {}
        carpeta_consumo = {}

        for item in reporte_materiales:
            empresa_consumo[item["empresa"]] = (
                empresa_consumo.get(item["empresa"], 0)
                + item["cantidad_total"]
            )

            carpeta_consumo[item["carpeta"]] = (
                carpeta_consumo.get(item["carpeta"], 0)
                + item["cantidad_total"]
            )

        empresa_top = max(empresa_consumo.items(), key=lambda x: x[1])
        carpeta_top = max(carpeta_consumo.items(), key=lambda x: x[1])

    empresa_nombre = ""
    carpeta_codigo = ""

    if empresa_id:
        empresa = EmpresaContratista.objects.filter(id=empresa_id).first()
        empresa_nombre = empresa.nombre if empresa else ""

    if carpeta_id:
        carpeta = Carpeta.objects.filter(id=carpeta_id).first()
        carpeta_codigo = carpeta.codigo_carpeta if carpeta else ""

    resumen = {
        "material_top": material_top,
        "total_materiales_operativos": total_materiales_operativos,
        "empresa_top_consumo": empresa_top,
        "carpeta_top_consumo": carpeta_top,
    }

    filtros = {
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "empresa_nombre": empresa_nombre,
        "carpeta_codigo": carpeta_codigo,
        "material": material_busqueda,
        "tipo_material": tipo_material,
        "nivel_consumo": nivel_consumo,
    }

    return render(request, "reportes/print/materiales_print.html", {
        "resumen": resumen,
        "reporte_materiales": reporte_materiales,
        "filtros": filtros,
    })

@login_required
def reporte_rendimiento_print_view(request):
    require_admin_or_funcionario(request)

    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    metrica_efectividad = request.GET.get("metrica_efectividad", "").strip()
    nivel_rendimiento = request.GET.get("nivel_rendimiento", "").strip()

    planos = Plano.objects.filter(eliminado=False).select_related(
        "carpeta",
        "carpeta__empresa"
    )

    resultados = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa",
    )

    if fecha_desde:
        planos = planos.filter(fecha_carga__date__gte=fecha_desde)
        resultados = resultados.filter(plano__fecha_carga__date__gte=fecha_desde)

    if fecha_hasta:
        planos = planos.filter(fecha_carga__date__lte=fecha_hasta)
        resultados = resultados.filter(plano__fecha_carga__date__lte=fecha_hasta)

    if empresa_id:
        planos = planos.filter(carpeta__empresa_id=empresa_id)
        resultados = resultados.filter(plano__carpeta__empresa_id=empresa_id)

    empresas_map = defaultdict(lambda: {
        "empresa": "",
        "total_planos": 0,
        "total_nr": 0,
        "aprobados": 0,
        "rechazados": 0,
        "verificacion": 0,
        "efectividad": 0,
    })

    for plano in planos:
        empresa = plano.carpeta.empresa.nombre if plano.carpeta.empresa else "Sin empresa"
        empresas_map[empresa]["empresa"] = empresa
        empresas_map[empresa]["total_planos"] += 1

    for resultado in resultados:
        empresa = resultado.plano.carpeta.empresa.nombre if resultado.plano.carpeta.empresa else "Sin empresa"

        empresas_map[empresa]["empresa"] = empresa
        empresas_map[empresa]["total_nr"] += 1

        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final == "APROBADO":
            empresas_map[empresa]["aprobados"] += 1
        elif estado_final == "RECHAZADO":
            empresas_map[empresa]["rechazados"] += 1
        elif estado_final == "EN_VERIFICACION":
            empresas_map[empresa]["verificacion"] += 1

    for item in empresas_map.values():
        item["efectividad"] = (
            round((item["aprobados"] / item["total_nr"]) * 100, 2)
            if item["total_nr"]
            else 0
        )

    mapa_metricas = {
        "efectividad": "efectividad",
        "volumen": "total_nr",
        "rechazos": "rechazados",
        "pendientes": "verificacion",
    }

    criterio = mapa_metricas.get(metrica_efectividad, "efectividad")

    if criterio == "total_nr":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["total_nr"], reverse=True)
    elif criterio == "rechazados":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["rechazados"], reverse=True)
    elif criterio == "verificacion":
        reporte_empresas = sorted(empresas_map.values(), key=lambda x: x["verificacion"], reverse=True)
    else:
        reporte_empresas = sorted(
            empresas_map.values(),
            key=lambda x: (-x["efectividad"], -x["total_nr"], x["empresa"])
        )

    reporte_empresas_base = list(reporte_empresas)

    for index, item in enumerate(reporte_empresas_base, start=1):
        item["ranking"] = index

    if nivel_rendimiento == "alto":
        reporte_empresas = [
            item for item in reporte_empresas_base
            if item["efectividad"] >= 85
        ]
    elif nivel_rendimiento == "medio":
        reporte_empresas = [
            item for item in reporte_empresas_base
            if 60 <= item["efectividad"] < 85
        ]
    elif nivel_rendimiento == "bajo":
        reporte_empresas = [
            item for item in reporte_empresas_base
            if item["efectividad"] < 60
        ]
    else:
        reporte_empresas = reporte_empresas_base

    total_planos = planos.count()
    total_nr = sum(item["total_nr"] for item in reporte_empresas)
    total_aprobados = sum(item["aprobados"] for item in reporte_empresas)
    total_rechazados = sum(item["rechazados"] for item in reporte_empresas)
    total_verificacion = sum(item["verificacion"] for item in reporte_empresas)

    efectividad_general = (
        round((total_aprobados / total_nr) * 100, 2)
        if total_nr
        else 0
    )

    empresa_nombre = ""

    if empresa_id:
        empresa = EmpresaContratista.objects.filter(id=empresa_id).first()
        empresa_nombre = empresa.nombre if empresa else ""

    resumen = {
        "total_planos": total_planos,
        "total_nr": total_nr,
        "total_aprobados": total_aprobados,
        "total_rechazados": total_rechazados,
        "total_verificacion": total_verificacion,
        "efectividad_general": efectividad_general,
        "empresas_evaluadas": len(reporte_empresas),
    }

    filtros = {
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "empresa_nombre": empresa_nombre,
        "metrica_efectividad": metrica_efectividad,
        "nivel_rendimiento": nivel_rendimiento,
    }

    return render(request, "reportes/print/rendimiento_print.html", {
        "resumen": resumen,
        "reporte_empresas": reporte_empresas,
        "filtros": filtros,
    })

@login_required
def reporte_efectividad_mensual_print_view(request):

    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()

    efectividad_mensual_anio = request.GET.get("efectividad_mensual_anio", "").strip()
    efectividad_mensual_mes = request.GET.get("efectividad_mensual_mes", "").strip()
    efectividad_mensual_nivel = request.GET.get("efectividad_mensual_nivel", "").strip()
    efectividad_mensual_orden = request.GET.get("efectividad_mensual_orden", "efectividad_desc").strip()

    resultados = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa"
    )

    if fecha_desde:
        resultados = resultados.filter(
            plano__fecha_carga__date__gte=fecha_desde
        )

    if fecha_hasta:
        resultados = resultados.filter(
            plano__fecha_carga__date__lte=fecha_hasta
        )

    if empresa_id:
        resultados = resultados.filter(
            plano__carpeta__empresa_id=empresa_id
        )

    efectividad_mensual_map = defaultdict(lambda: {
        "periodo": "",
        "empresa": "",
        "carpeta": "",
        "total_planos": 0,
        "total_nr": 0,
        "aprobados": 0,
        "rechazados": 0,
        "verificacion": 0,
        "efectividad": 0,
        "nivel": "",
        "_planos_ids": set(),
    })

    for resultado in resultados:

        plano = resultado.plano
        carpeta = plano.carpeta if plano else None

        if not carpeta:
            continue

        empresa = carpeta.empresa.nombre if carpeta.empresa else "Sin empresa"

        anio = str(carpeta.anio) if getattr(carpeta, "anio", None) else ""
        mes = str(carpeta.mes).zfill(2) if getattr(carpeta, "mes", None) else ""

        if not anio or not mes:
            fecha_base = plano.fecha_carga
            anio = str(fecha_base.year)
            mes = str(fecha_base.month).zfill(2)

        if efectividad_mensual_anio and anio != efectividad_mensual_anio:
            continue

        if efectividad_mensual_mes and mes != efectividad_mensual_mes.zfill(2):
            continue

        key = (anio, mes, empresa, carpeta.codigo_carpeta)

        item = efectividad_mensual_map[key]

        item["anio"] = anio
        item["mes"] = mes
        item["periodo"] = f"{mes}/{anio}"
        item["empresa"] = empresa
        item["carpeta"] = carpeta.codigo_carpeta

        item["_planos_ids"].add(plano.id)

        item["total_nr"] += 1

        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final == "APROBADO":
            item["aprobados"] += 1

        elif estado_final == "RECHAZADO":
            item["rechazados"] += 1

        elif estado_final == "EN_VERIFICACION":
            item["verificacion"] += 1

    for item in efectividad_mensual_map.values():

        item["total_planos"] = len(item["_planos_ids"])

        item["efectividad"] = round(
            (item["aprobados"] / item["total_nr"]) * 100,
            2
        ) if item["total_nr"] else 0

        if item["efectividad"] >= 95:
            item["nivel"] = "EXCELENTE"

        elif item["efectividad"] >= 85:
            item["nivel"] = "OPTIMO"

        elif item["efectividad"] >= 70:
            item["nivel"] = "RIESGO"

        else:
            item["nivel"] = "CRITICO"

        item.pop("_planos_ids", None)

    reporte_efectividad_mensual = list(efectividad_mensual_map.values())

    if efectividad_mensual_nivel:
        reporte_efectividad_mensual = [
            item for item in reporte_efectividad_mensual
            if item["nivel"] == efectividad_mensual_nivel
        ]

    if efectividad_mensual_orden == "periodo_desc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: x["periodo"],
            reverse=True
        )

    elif efectividad_mensual_orden == "periodo_asc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: x["periodo"]
        )

    elif efectividad_mensual_orden == "nr_desc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: x["total_nr"],
            reverse=True
        )

    elif efectividad_mensual_orden == "rechazos_desc":
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: x["rechazados"],
            reverse=True
        )

    else:
        reporte_efectividad_mensual = sorted(
            reporte_efectividad_mensual,
            key=lambda x: (-x["efectividad"], -x["total_nr"])
        )

    if efectividad_mensual_mes and not efectividad_mensual_anio and reporte_efectividad_mensual:
        efectividad_mensual_anio = reporte_efectividad_mensual[0].get("anio", "")

    resumen = {
        "empresas": len({item["empresa"] for item in reporte_efectividad_mensual}),
        "carpetas": len({item["carpeta"] for item in reporte_efectividad_mensual}),
        "nr": sum(item["total_nr"] for item in reporte_efectividad_mensual),
        "efectividad": round(
            sum(item["efectividad"] for item in reporte_efectividad_mensual) / len(reporte_efectividad_mensual),
            2
        ) if reporte_efectividad_mensual else 0
    }

    meses_label = {
        "01": "Enero",
        "02": "Febrero",
        "03": "Marzo",
        "04": "Abril",
        "05": "Mayo",
        "06": "Junio",
        "07": "Julio",
        "08": "Agosto",
        "09": "Septiembre",
        "10": "Octubre",
        "11": "Noviembre",
        "12": "Diciembre",
    }

    periodo_label = "Periodo completo"

    if efectividad_mensual_mes:
        efectividad_mensual_mes = efectividad_mensual_mes.zfill(2)

    if efectividad_mensual_mes and efectividad_mensual_anio:
        periodo_label = f"{meses_label.get(efectividad_mensual_mes, efectividad_mensual_mes)} {efectividad_mensual_anio}"

    elif efectividad_mensual_anio:
        periodo_label = f"Año {efectividad_mensual_anio}"


    context = {
        "reporte": reporte_efectividad_mensual,
        "resumen": resumen,
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "periodo_label": periodo_label,
    }

    return render(
        request,
        "reportes/print/efectividad_mensual_print.html",
        context
    )

@login_required
def reporte_rechazos_print_view(request):
    require_admin_or_funcionario(request)

    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    tipo_rechazo_busqueda = request.GET.get("tipo_rechazo", "").strip()
    coincidencia_rechazo = request.GET.get("coincidencia_rechazo", "contiene").strip()
    nivel_impacto_rechazo = request.GET.get("nivel_impacto_rechazo", "").strip()
    orden_rechazo = request.GET.get("orden_rechazo", "mayor").strip()

    resultados = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa",
    )

    if fecha_desde:
        resultados = resultados.filter(plano__fecha_carga__date__gte=fecha_desde)

    if fecha_hasta:
        resultados = resultados.filter(plano__fecha_carga__date__lte=fecha_hasta)

    if empresa_id:
        resultados = resultados.filter(plano__carpeta__empresa_id=empresa_id)

    tipos_rechazo_counter = Counter()

    for resultado in resultados:
        estado_final = resultado.estado_resultado_manual or resultado.estado_resultado

        if estado_final != "RECHAZADO":
            continue

        if not resultado.ciudad_ok:
            tipos_rechazo_counter["Ciudad no coincide"] += 1

        if not resultado.zona_ok:
            tipos_rechazo_counter["Zona no coincide"] += 1

        if not resultado.fecha_ok:
            tipos_rechazo_counter["Fecha inválida"] += 1

        if resultado.materiales_requieren_revision:
            tipos_rechazo_counter["Materiales inconsistentes"] += 1

        motivo = str(resultado.motivo_resultado or "").lower()

        if "desconocido" in motivo:
            tipos_rechazo_counter["NR desconocido"] += 1

    reporte_tipos_rechazo = [
        {"tipo": tipo, "total": total}
        for tipo, total in tipos_rechazo_counter.most_common()
    ]

    if tipo_rechazo_busqueda:
        query = tipo_rechazo_busqueda.lower()

        if coincidencia_rechazo == "exacta":
            reporte_tipos_rechazo = [
                item for item in reporte_tipos_rechazo
                if item["tipo"].lower() == query
            ]

        elif coincidencia_rechazo == "inicia":
            reporte_tipos_rechazo = [
                item for item in reporte_tipos_rechazo
                if item["tipo"].lower().startswith(query)
            ]

        else:
            reporte_tipos_rechazo = [
                item for item in reporte_tipos_rechazo
                if query in item["tipo"].lower()
            ]

    def resolver_nivel_impacto(total):
        if total >= 10:
            return "critico"
        if total >= 5:
            return "alto"
        if total >= 2:
            return "medio"
        return "bajo"

    if nivel_impacto_rechazo:
        reporte_tipos_rechazo = [
            item for item in reporte_tipos_rechazo
            if resolver_nivel_impacto(item["total"]) == nivel_impacto_rechazo
        ]

    if orden_rechazo == "menor":
        reporte_tipos_rechazo = sorted(reporte_tipos_rechazo, key=lambda x: x["total"])

    elif orden_rechazo == "tipo_az":
        reporte_tipos_rechazo = sorted(reporte_tipos_rechazo, key=lambda x: x["tipo"])

    elif orden_rechazo == "tipo_za":
        reporte_tipos_rechazo = sorted(reporte_tipos_rechazo, key=lambda x: x["tipo"], reverse=True)

    else:
        reporte_tipos_rechazo = sorted(reporte_tipos_rechazo, key=lambda x: x["total"], reverse=True)

    empresa_nombre = ""

    if empresa_id:
        empresa = EmpresaContratista.objects.filter(id=empresa_id).first()
        empresa_nombre = empresa.nombre if empresa else ""

    total_inconsistencias = sum(item["total"] for item in reporte_tipos_rechazo)

    resumen = {
        "total_inconsistencias": total_inconsistencias,
        "tipos_detectados": len(reporte_tipos_rechazo),
        "tipo_predominante": reporte_tipos_rechazo[0]["tipo"] if reporte_tipos_rechazo else "",
    }

    filtros = {
        "fecha_desde": fecha_desde,
        "fecha_hasta": fecha_hasta,
        "empresa_nombre": empresa_nombre,
        "tipo_rechazo": tipo_rechazo_busqueda,
        "coincidencia_rechazo": coincidencia_rechazo,
        "nivel_impacto_rechazo": nivel_impacto_rechazo,
        "orden_rechazo": orden_rechazo,
    }

    return render(request, "reportes/print/rechazos_print.html", {
        "resumen": resumen,
        "reporte_tipos_rechazo": reporte_tipos_rechazo,
        "filtros": filtros,
    })

@login_required
def buscar_nr_global_view(request):
    query = request.GET.get("q", "").strip()
    filtro = request.GET.get("filtro", "").strip()

    resultados = []

    if query:
        resultados = ResultadoValidacionPlano.objects.select_related(
            "plano",
            "plano__carpeta",
            "plano__carpeta__empresa"
        ).filter(
            Q(nr_detectado__icontains=query)
        )

        # Filtros
        if filtro == "aprobado":
            resultados = resultados.filter(estado_resultado="APROBADO")

        elif filtro == "rechazado":
            resultados = resultados.filter(estado_resultado="RECHAZADO")

        elif filtro == "verificacion":
            resultados = resultados.filter(estado_resultado="EN_VERIFICACION")

        elif filtro == "empresa":
            resultados = resultados.filter(
                plano__carpeta__empresa__nombre__icontains=query
            )

        elif filtro == "carpeta":
            resultados = resultados.filter(
                plano__carpeta__codigo_carpeta__icontains=query
            )

        resultados = resultados.order_by("-id")

    return render(request, "busqueda_global.html", {
        "query": query,
        "filtro": filtro,
        "resultados": resultados,
    })
@login_required
def estadisticas_view(request):
    require_admin_or_funcionario(request)

    # =========================
    # BASES
    # =========================
    planos_qs = Plano.objects.filter(eliminado=False)
    carpetas_qs = Carpeta.objects.filter(eliminada=False)
    resultados_qs = ResultadoValidacionPlano.objects.select_related(
        "plano",
        "plano__carpeta",
        "plano__carpeta__empresa",
        "reclamo_encontrado",
    )
    reclamos_qs = Reclamo.objects.all()

    # =========================
    # KPIs PRINCIPALES
    # =========================
    total_carpetas = carpetas_qs.count()
    total_planos = planos_qs.count()
    total_planos_procesados = planos_qs.filter(procesado=True).count()
    total_planos_aprobados = planos_qs.filter(estado="APROBADO").count()
    total_planos_rechazados = planos_qs.filter(estado="RECHAZADO").count()
    total_planos_en_verificacion = planos_qs.filter(estado="EN_VERIFICACION").count()
    total_nr_analizados = resultados_qs.count()
    total_usuarios_activos = User.objects.filter(is_active=True).count()

    # =========================
    # PLANOS POR ESTADO
    # =========================
    estados_orden = [
        "APROBADO",
        "RECHAZADO",
        "EN_VERIFICACION",
        "EN_REVISION",
        "EN_ESPERA",
    ]

    planos_estado_map = {
        item["estado"]: item["total"]
        for item in (
            planos_qs
            .values("estado")
            .annotate(total=Count("id"))
        )
    }

    planos_por_estado_labels = []
    planos_por_estado_values = []

    for estado in estados_orden:
        if estado in planos_estado_map:
            planos_por_estado_labels.append(estado.replace("_", " ").title())
            planos_por_estado_values.append(planos_estado_map[estado])

    # =========================
    # PLANOS POR EMPRESA
    # =========================
    planos_por_empresa_qs = (
        planos_qs
        .values("carpeta__empresa__nombre")
        .annotate(total=Count("id"))
        .order_by("-total", "carpeta__empresa__nombre")
    )

    planos_por_empresa = []
    empresas_labels = []
    empresas_values = []

    for item in planos_por_empresa_qs:
        empresa = item["carpeta__empresa__nombre"] or "Sin empresa"
        total = item["total"]

        planos_por_empresa.append({
            "empresa": empresa,
            "total": total,
        })
        empresas_labels.append(empresa)
        empresas_values.append(total)

    # =========================
    # RENDIMIENTO POR EMPRESA
    # =========================
    empresas_rendimiento = []
    empresas_base_qs = (
        planos_qs
        .exclude(carpeta__empresa__isnull=True)
        .values("carpeta__empresa__id", "carpeta__empresa__nombre")
        .annotate(total=Count("id"))
        .order_by("carpeta__empresa__nombre")
    )

    for item in empresas_base_qs:
        empresa_id = item["carpeta__empresa__id"]
        empresa_nombre = item["carpeta__empresa__nombre"] or "Sin empresa"
        total = item["total"]

        aprobados = planos_qs.filter(carpeta__empresa_id=empresa_id, estado="APROBADO").count()
        rechazados = planos_qs.filter(carpeta__empresa_id=empresa_id, estado="RECHAZADO").count()
        verificacion = planos_qs.filter(carpeta__empresa_id=empresa_id, estado="EN_VERIFICACION").count()

        efectividad = round((aprobados / total) * 100, 2) if total > 0 else 0

        empresas_rendimiento.append({
            "empresa": empresa_nombre,
            "total": total,
            "aprobados": aprobados,
            "rechazados": rechazados,
            "verificacion": verificacion,
            "efectividad": efectividad,
        })

    empresas_rendimiento = sorted(
        empresas_rendimiento,
        key=lambda x: (-x["efectividad"], -x["aprobados"], x["empresa"])
    )

    mejor_empresa = empresas_rendimiento[0] if empresas_rendimiento else None
    empresa_mas_rechazos = (
        max(empresas_rendimiento, key=lambda x: x["rechazados"])
        if empresas_rendimiento else None
    )

    # =========================
    # TRABAJOS POR MES
    # =========================
    trabajos_por_mes_qs = (
        planos_qs
        .annotate(mes=TruncMonth("fecha_carga"))
        .values("mes")
        .annotate(total=Count("id"))
        .order_by("mes")
    )

    meses_labels = []
    meses_values = []
    meses_nombres = {
        1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr",
        5: "May", 6: "Jun", 7: "Jul", 8: "Ago",
        9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
    }

    for item in trabajos_por_mes_qs:
        mes = item["mes"]
        total = item["total"]
        if mes:
            etiqueta = f"{meses_nombres.get(mes.month, mes.month)} {mes.year}"
            meses_labels.append(etiqueta)
            meses_values.append(total)

    mes_mas_activo = None
    if meses_labels and meses_values:
        idx_mes_max = meses_values.index(max(meses_values))
        mes_mas_activo = {
            "label": meses_labels[idx_mes_max],
            "total": meses_values[idx_mes_max],
        }

    # =========================
    # ZONAS CON MÁS RECLAMOS
    # =========================
    zonas_qs = (
        reclamos_qs
        .exclude(zona__isnull=True)
        .exclude(zona__exact="")
        .values("zona")
        .annotate(total=Count("id"))
        .order_by("-total", "zona")[:10]
    )

    zonas_labels = [item["zona"] for item in zonas_qs]
    zonas_values = [item["total"] for item in zonas_qs]
    top_zonas = [{"zona": item["zona"], "total": item["total"]} for item in zonas_qs]
    zona_mas_reclamos = top_zonas[0] if top_zonas else None

    # =========================
    # CIUDADES CON MÁS RECLAMOS
    # =========================
    ciudades_qs = (
        reclamos_qs
        .exclude(ciudad__isnull=True)
        .exclude(ciudad__exact="")
        .values("ciudad")
        .annotate(total=Count("id"))
        .order_by("-total", "ciudad")[:10]
    )

    top_ciudades = [{"ciudad": item["ciudad"], "total": item["total"]} for item in ciudades_qs]

    # =========================
    # MATERIALES MÁS UTILIZADOS
    # =========================
    materiales_counter = Counter()

    materiales_qs = MaterialDetectadoPlano.objects.select_related("resultado_validacion").all()

    for material in materiales_qs:
        descripcion = (
            material.descripcion_editada
            if material.descripcion_editada not in (None, "")
            else material.descripcion_original
        )

        if descripcion:
            clave = str(descripcion).strip()
            if clave:
                materiales_counter[clave] += 1

    materiales_top = [
        {"material": nombre, "total": total}
        for nombre, total in materiales_counter.most_common(10)
    ]

    materiales_labels = [item["material"] for item in materiales_top]
    materiales_values = [item["total"] for item in materiales_top]
    material_mas_utilizado = materiales_top[0] if materiales_top else None

    # =========================
    # MOTIVOS FRECUENTES DE RECHAZO / VERIFICACIÓN
    # =========================
    motivos_counter = Counter()

    resultados_motivos_qs = resultados_qs.exclude(motivo_resultado__isnull=True).exclude(motivo_resultado__exact="")

    for resultado in resultados_motivos_qs:
        texto = str(resultado.motivo_resultado or "").lower()

        if "ciudad" in texto and ("no coincide" in texto or "incorrecta" in texto):
            motivos_counter["Ciudad no coincide"] += 1

        if "zona" in texto and ("no coincide" in texto or "incorrecta" in texto):
            motivos_counter["Zona no coincide"] += 1

        if "fecha" in texto and ("no coincide" in texto or "incorrecta" in texto or "invalida" in texto):
            motivos_counter["Fecha inconsistente"] += 1

        if "material" in texto:
            motivos_counter["Materiales en revisión"] += 1

        if "desconocido" in texto:
            motivos_counter["NR desconocido"] += 1

        if "corrección manual" in texto or "correccion manual" in texto:
            motivos_counter["Edición manual"] += 1

    motivos_top = [
        {"motivo": nombre, "total": total}
        for nombre, total in motivos_counter.most_common(8)
    ]

    motivos_labels = [item["motivo"] for item in motivos_top]
    motivos_values = [item["total"] for item in motivos_top]

    # =========================
    # ANÁLISIS INTELIGENTE (REGLAS)
    # =========================
    analisis_inteligente = []

    analisis_inteligente.append(
        f"El sistema registra {total_planos_procesados} planos procesados y {total_nr_analizados} NR analizados en total."
    )

    if mejor_empresa:
        analisis_inteligente.append(
            f"La empresa con mejor rendimiento actual es {mejor_empresa['empresa']}, con una efectividad de {mejor_empresa['efectividad']}%."
        )

    if empresa_mas_rechazos and empresa_mas_rechazos["rechazados"] > 0:
        analisis_inteligente.append(
            f"La empresa con mayor cantidad de rechazos es {empresa_mas_rechazos['empresa']}, con {empresa_mas_rechazos['rechazados']} planos rechazados."
        )

    if material_mas_utilizado:
        analisis_inteligente.append(
            f"El material más utilizado es {material_mas_utilizado['material']}, con {material_mas_utilizado['total']} apariciones registradas."
        )

    if zona_mas_reclamos:
        analisis_inteligente.append(
            f"La zona con mayor índice de reclamos es {zona_mas_reclamos['zona']}, con {zona_mas_reclamos['total']} registros."
        )

    if mes_mas_activo:
        analisis_inteligente.append(
            f"El período con mayor actividad fue {mes_mas_activo['label']}, con {mes_mas_activo['total']} planos cargados."
        )

    if not analisis_inteligente:
        analisis_inteligente.append(
            "Aún no hay suficientes datos para generar conclusiones automáticas."
        )

    # =========================
    # CONTEXTO
    # =========================
    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Estadísticas",
        "resumen": {
            "total_carpetas": total_carpetas,
            "total_planos": total_planos,
            "total_planos_procesados": total_planos_procesados,
            "total_planos_aprobados": total_planos_aprobados,
            "total_planos_rechazados": total_planos_rechazados,
            "total_planos_en_verificacion": total_planos_en_verificacion,
            "total_nr_analizados": total_nr_analizados,
            "total_usuarios_activos": total_usuarios_activos,
        },
        "planos_por_empresa": planos_por_empresa,
        "empresas_rendimiento": empresas_rendimiento[:10],
        "top_zonas": top_zonas,
        "top_ciudades": top_ciudades,
        "materiales_top": materiales_top,
        "motivos_top": motivos_top,
        "mejor_empresa": mejor_empresa,
        "empresa_mas_rechazos": empresa_mas_rechazos,
        "material_mas_utilizado": material_mas_utilizado,
        "zona_mas_reclamos": zona_mas_reclamos,
        "mes_mas_activo": mes_mas_activo,
        "analisis_inteligente": analisis_inteligente,

        # Datos para gráficos
        "planos_por_estado": json.dumps(planos_por_estado_labels),
        "planos_por_estado_values": json.dumps(planos_por_estado_values),
        "empresas_labels": json.dumps(empresas_labels[:10]),
        "empresas_values": json.dumps(empresas_values[:10]),
        "meses_labels": json.dumps(meses_labels),
        "meses_values": json.dumps(meses_values),
        "zonas_labels": json.dumps(zonas_labels),
        "zonas_values": json.dumps(zonas_values),
        "materiales_labels": json.dumps(materiales_labels),
        "materiales_values": json.dumps(materiales_values),
        "motivos_labels": json.dumps(motivos_labels),
        "motivos_values": json.dumps(motivos_values),
    }

    return render(request, "estadisticas/listado.html", contexto)

