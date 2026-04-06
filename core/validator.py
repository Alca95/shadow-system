from __future__ import annotations
from typing import List, Optional

from rapidfuzz import fuzz

from .models import (
    ItemNRMateriales,
    MaterialDetectadoPlano,
    NRMateriales,
    Plano,
    ResultadoValidacionPlano,
    Reclamo,
)


FUZZY_THRESHOLD = 80

ESTADO_APROBADO = "APROBADO"
ESTADO_RECHAZADO = "RECHAZADO"
ESTADO_EN_VERIFICACION = "EN_VERIFICACION"
ESTADO_EN_REVISION = "EN_REVISION"


def deduplicate_keep_order(values: List[str]) -> List[str]:
    resultado = []
    vistos = set()

    for value in values:
        if value and value not in vistos:
            resultado.append(value)
            vistos.add(value)

    return resultado


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []

    items = [x.strip() for x in value.split(",") if x.strip()]
    return deduplicate_keep_order(items)


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""

    text = str(value).strip().lower()
    return " ".join(text.split())


def normalize_material_text(value: Optional[str]) -> str:
    text = normalize_text(value)
    if not text:
        return ""

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


def compare_text_fuzzy(a: Optional[str], b: Optional[str], threshold: int = FUZZY_THRESHOLD) -> dict:
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)

    if not a_norm or not b_norm:
        return {
            "comparable": False,
            "matched": None,
            "score": None,
            "reason": "Dato faltante en uno o ambos campos",
        }

    score = fuzz.token_sort_ratio(a_norm, b_norm)
    matched = score >= threshold

    return {
        "comparable": True,
        "matched": matched,
        "score": score,
        "reason": None if matched else f"No coincide (score={score}, threshold={threshold})",
    }


def compare_dates(fecha_trabajo, fecha_reclamo) -> dict:
    if not fecha_trabajo or not fecha_reclamo:
        return {
            "comparable": False,
            "matched": None,
            "reason": "Falta fecha_trabajo o fecha_reclamo",
        }

    matched = fecha_trabajo >= fecha_reclamo

    return {
        "comparable": True,
        "matched": matched,
        "reason": None if matched else "fecha_trabajo es anterior a fecha_reclamo",
    }


def build_preliminar_estado(nrs: List[str], validos: List[str], desconocidos: List[str]) -> tuple[str, List[str]]:
    motivos = []

    if not nrs:
        motivos.append("No se detectaron NR en el plano.")
        return ESTADO_EN_VERIFICACION, motivos

    if not validos:
        motivos.append("No se detectó ningún NR válido (existente en Reclamo).")
        return ESTADO_EN_VERIFICACION, motivos

    if desconocidos:
        motivos.append(f"Se detectaron NR no existentes en Reclamo: {', '.join(desconocidos)}")
        return ESTADO_EN_VERIFICACION, motivos

    return ESTADO_EN_REVISION, motivos


def crear_resultado_validacion_preliminar(
    plano: Plano,
    nr: str,
    reclamo_obj: Optional[Reclamo],
    nr_obj: Optional[NRMateriales] = None,
):
    if reclamo_obj:
        return ResultadoValidacionPlano.objects.create(
            plano=plano,
            nr_detectado=nr,
            nr_normalizado=nr,
            nr_materiales_encontrado=nr_obj,
            reclamo_encontrado=reclamo_obj,
            estado_resultado=ESTADO_EN_VERIFICACION,
            ciudad_ok=False,
            zona_ok=False,
            fecha_ok=False,
            materiales_ok=False,
            materiales_requieren_revision=False,
            motivo_resultado="NR encontrado en Reclamo. Pendiente de validación completa.",
        )

    return ResultadoValidacionPlano.objects.create(
        plano=plano,
        nr_detectado=nr,
        nr_normalizado=nr,
        nr_materiales_encontrado=None,
        reclamo_encontrado=None,
        estado_resultado=ESTADO_RECHAZADO,
        ciudad_ok=False,
        zona_ok=False,
        fecha_ok=False,
        materiales_ok=False,
        materiales_requieren_revision=False,
        motivo_resultado="NR no encontrado en Reclamo.",
    )


def _parse_single_material_entry(text: str) -> Optional[dict]:
    text = str(text).strip()
    if not text:
        return None

    import re

    match = re.match(r"^\s*(\d+(?:[\.,]\d+)?)\s+(.+)$", text)
    if match:
        cantidad = match.group(1).replace(",", ".").strip()
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


def _parse_materiales_plano(raw_value) -> List[dict]:
    if not raw_value:
        return []

    text = str(raw_value).strip()
    if not text or text.lower() in {"-", "none", "ninguno"}:
        return []

    normalized = (
        text.replace("\r", "\n")
        .replace("|", ",")
        .replace(";", ",")
        .replace("\n", ",")
    )
    parts = [p.strip(" -•\t") for p in normalized.split(",") if p.strip(" -•\t")]

    rows = []
    for idx, part in enumerate(parts, start=1):
        item = _parse_single_material_entry(part)
        if item:
            item["orden"] = idx
            rows.append(item)
    return rows


def _get_materiales_bd_rows(nr_obj: Optional[NRMateriales]) -> List[dict]:
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
    orden = 1

    for related_name in related_names:
        related_manager = getattr(nr_obj, related_name, None)
        if related_manager is None:
            continue

        try:
            iterable = related_manager.all() if hasattr(related_manager, "all") else related_manager
        except Exception:
            continue

        try:
            for item in iterable:
                cantidad = getattr(item, "cantidad", None)
                unidad = (
                    getattr(item, "unidad_medida", None)
                    or getattr(item, "unidad", None)
                    or "unidad"
                )
                descripcion = (
                    getattr(item, "descripcion", None)
                    or getattr(item, "material", None)
                    or getattr(item, "nombre", None)
                    or getattr(item, "detalle", None)
                    or getattr(item, "observacion", None)
                    or "-"
                )

                row = {
                    "orden": orden,
                    "cantidad": str(cantidad).rstrip("0").rstrip(".") if cantidad is not None else "-",
                    "unidad": unidad or "unidad",
                    "descripcion": descripcion or "-",
                }

                signature = (
                    normalize_material_text(row["cantidad"]),
                    normalize_material_text(row["unidad"]),
                    normalize_material_text(row["descripcion"]),
                )

                if signature not in seen:
                    seen.add(signature)
                    rows.append(row)
                    orden += 1

        except Exception:
            continue

    return rows


def _material_signature(cantidad, unidad, descripcion):
    return normalize_material_text(descripcion)


def _materiales_coinciden(materiales_plano: List[dict], materiales_bd: List[dict]) -> tuple[bool, List[str]]:
    if not materiales_bd and not materiales_plano:
        return True, ["No hay materiales en plano ni en BD."]

    if not materiales_plano and materiales_bd:
        return False, ["No se detectaron materiales en el plano, pero sí existen materiales en la BD."]

    if materiales_plano and not materiales_bd:
        return False, ["Se detectaron materiales en el plano, pero no existen materiales cargados en la BD para ese NR."]

    bd_signatures = [
        _material_signature(item.get("cantidad"), item.get("unidad"), item.get("descripcion"))
        for item in materiales_bd
    ]
    plano_signatures = [
        _material_signature(item.get("cantidad"), item.get("unidad"), item.get("descripcion"))
        for item in materiales_plano
    ]

    faltantes_en_plano = [sig for sig in bd_signatures if sig not in plano_signatures]
    sobrantes_en_plano = [sig for sig in plano_signatures if sig not in bd_signatures]

    motivos = []
    if faltantes_en_plano:
        motivos.append("Existen materiales de BD que no coinciden con los detectados en el plano.")
    if sobrantes_en_plano:
        motivos.append("Existen materiales detectados en el plano que no coinciden con la BD.")

    return not (faltantes_en_plano or sobrantes_en_plano), motivos


def _sincronizar_materiales_detectados(resultado: ResultadoValidacionPlano, materiales_plano_rows: List[dict]) -> None:
    existentes = list(resultado.materiales_detectados.all().order_by("orden", "id"))

    if not existentes:
        nuevos = []
        for idx, row in enumerate(materiales_plano_rows, start=1):
            nuevos.append(
                MaterialDetectadoPlano(
                    resultado_validacion=resultado,
                    orden=idx,
                    cantidad_original=row.get("cantidad"),
                    unidad_original=row.get("unidad"),
                    descripcion_original=row.get("descripcion"),
                )
            )
        if nuevos:
            MaterialDetectadoPlano.objects.bulk_create(nuevos)
        return

    # Solo sincroniza originales si aún no fueron editados manualmente
    for idx, row in enumerate(materiales_plano_rows, start=1):
        if idx <= len(existentes):
            item = existentes[idx - 1]
            updates = []
            if not item.fue_editado:
                if item.cantidad_original != row.get("cantidad"):
                    item.cantidad_original = row.get("cantidad")
                    updates.append("cantidad_original")
                if item.unidad_original != row.get("unidad"):
                    item.unidad_original = row.get("unidad")
                    updates.append("unidad_original")
                if item.descripcion_original != row.get("descripcion"):
                    item.descripcion_original = row.get("descripcion")
                    updates.append("descripcion_original")
            if item.orden != idx:
                item.orden = idx
                updates.append("orden")
            if updates:
                item.save(update_fields=updates)

        else:
            MaterialDetectadoPlano.objects.create(
                resultado_validacion=resultado,
                orden=idx,
                cantidad_original=row.get("cantidad"),
                unidad_original=row.get("unidad"),
                descripcion_original=row.get("descripcion"),
            )


def evaluar_resultado_nr(
    resultado: ResultadoValidacionPlano,
    ciudad_plano: Optional[str],
    zona_plano: Optional[str],
    fecha_plano,
    materiales_plano_texto: Optional[str],
) -> ResultadoValidacionPlano:
    reclamo_obj = resultado.reclamo_encontrado
    nr_obj = resultado.nr_materiales_encontrado

    if not reclamo_obj:
        resultado.estado_resultado = ESTADO_RECHAZADO
        resultado.ciudad_ok = False
        resultado.zona_ok = False
        resultado.fecha_ok = False
        resultado.materiales_ok = False
        resultado.materiales_requieren_revision = False
        resultado.motivo_resultado = "NR no encontrado en Reclamo."
        resultado.save(
            update_fields=[
                "estado_resultado",
                "ciudad_ok",
                "zona_ok",
                "fecha_ok",
                "materiales_ok",
                "materiales_requieren_revision",
                "motivo_resultado",
            ]
        )
        return resultado

    ciudad_cmp = compare_text_fuzzy(ciudad_plano, reclamo_obj.ciudad)
    zona_cmp = compare_text_fuzzy(zona_plano, reclamo_obj.zona)
    fecha_cmp = compare_dates(fecha_plano, reclamo_obj.fecha_reclamo)

    ciudad_ok = bool(ciudad_cmp["comparable"] and ciudad_cmp["matched"])
    zona_ok = bool(zona_cmp["comparable"] and zona_cmp["matched"])
    fecha_ok = bool(fecha_cmp["comparable"] and fecha_cmp["matched"])

    materiales_plano_rows = _parse_materiales_plano(materiales_plano_texto)

    if not nr_obj:
        nr_obj = NRMateriales.objects.filter(numero_nr=resultado.nr_detectado).select_related("reclamo").first()
        if nr_obj:
            resultado.nr_materiales_encontrado = nr_obj

    materiales_bd_rows = _get_materiales_bd_rows(nr_obj)
    materiales_ok, materiales_motivos = _materiales_coinciden(materiales_plano_rows, materiales_bd_rows)

    _sincronizar_materiales_detectados(resultado, materiales_plano_rows)

    motivos = []

    if ciudad_ok:
        motivos.append(f"Ciudad correcta (Plano: {ciudad_plano} | Reclamo: {reclamo_obj.ciudad})")
    else:
        motivos.append(f"Ciudad no coincide (Plano: {ciudad_plano or '-'} | Reclamo: {reclamo_obj.ciudad})")

    if zona_ok:
        motivos.append(f"Zona correcta (Plano/NR: {zona_plano} | Reclamo: {reclamo_obj.zona})")
    else:
        motivos.append(f"Zona no coincide (Plano/NR: {zona_plano or '-'} | Reclamo: {reclamo_obj.zona})")

    if fecha_ok:
        motivos.append(f"Fecha correcta (NR: {fecha_plano} | Reclamo: {reclamo_obj.fecha_reclamo})")
    else:
        motivos.append(f"Fecha no coincide (NR: {fecha_plano or '-'} | Reclamo: {reclamo_obj.fecha_reclamo})")

    if materiales_plano_rows:
        materiales_debug = ", ".join(
            [
                f"{m.get('cantidad', '-')} {m.get('unidad', 'unidad')} {m.get('descripcion', '-')}".strip()
                for m in materiales_plano_rows
            ]
        )
        motivos.append(f"Materiales OCR detectados: {materiales_debug}")

    if not ciudad_ok or not zona_ok or not fecha_ok:
        estado = ESTADO_RECHAZADO
        materiales_requieren_revision = bool(materiales_bd_rows or materiales_plano_rows) and not materiales_ok
        if materiales_motivos:
            motivos.extend(materiales_motivos)
    else:
        if materiales_ok:
            estado = ESTADO_APROBADO
            materiales_requieren_revision = False
        else:
            estado = ESTADO_EN_VERIFICACION
            materiales_requieren_revision = True
            motivos.extend(materiales_motivos)

    resultado.estado_resultado = estado
    resultado.ciudad_ok = ciudad_ok
    resultado.zona_ok = zona_ok
    resultado.fecha_ok = fecha_ok
    resultado.materiales_ok = materiales_ok
    resultado.materiales_requieren_revision = materiales_requieren_revision
    resultado.motivo_resultado = " | ".join(motivos)

    resultado.save(
        update_fields=[
            "nr_materiales_encontrado",
            "estado_resultado",
            "ciudad_ok",
            "zona_ok",
            "fecha_ok",
            "materiales_ok",
            "materiales_requieren_revision",
            "motivo_resultado",
        ]
    )
    return resultado


def validar_plano_contra_bd(plano: Plano, dias_repeticion: int = 90) -> dict:
    nrs = parse_csv(plano.nr_detectados)

    validos = []
    desconocidos = []

    plano.resultados_validacion.all().delete()

    reclamos_map = {
        obj.numero_reclamo: obj
        for obj in Reclamo.objects.filter(numero_reclamo__in=nrs)
    }

    for nr in nrs:
        reclamo_obj = reclamos_map.get(nr)
        nr_obj = NRMateriales.objects.filter(numero_nr=nr).select_related("reclamo").first()

        if reclamo_obj:
            validos.append(nr)
            crear_resultado_validacion_preliminar(plano, nr, reclamo_obj, nr_obj)
        else:
            desconocidos.append(nr)
            crear_resultado_validacion_preliminar(plano, nr, None, None)

    estado, motivos = build_preliminar_estado(nrs, validos, desconocidos)

    dup = (
        Plano.objects.filter(id_plano_deposito=plano.id_plano_deposito)
        .exclude(pk=plano.pk)
        .exclude(eliminado=True)
        .exists()
    )

    if dup:
        motivos.append("ID de plano duplicado.")
        estado = ESTADO_RECHAZADO

    plano.nr_validos = ",".join(validos) if validos else None
    plano.nr_desconocidos = ",".join(desconocidos) if desconocidos else None
    plano.nr_detectados_total = len(nrs)
    plano.nr_validos_total = len(validos)
    plano.nr_desconocidos_total = len(desconocidos)
    plano.motivo = "\n".join(motivos) if motivos else None
    plano.estado = estado
    plano.save(
        update_fields=[
            "nr_validos",
            "nr_desconocidos",
            "nr_detectados_total",
            "nr_validos_total",
            "nr_desconocidos_total",
            "motivo",
            "estado",
        ]
    )

    return {
        "estado": estado,
        "validos": validos,
        "desconocidos": desconocidos,
        "motivos": motivos,
    }


def recalcular_estado_plano_desde_resultados(plano: Plano) -> str:
    resultados = list(plano.resultados_validacion.all())

    if not resultados:
        plano.estado = ESTADO_EN_VERIFICACION
        plano.save(update_fields=["estado"])
        return plano.estado

    estados_finales = [r.estado_resultado_final for r in resultados]

    if any(estado == ESTADO_RECHAZADO for estado in estados_finales):
        plano.estado = ESTADO_RECHAZADO
    elif any(estado == ESTADO_EN_VERIFICACION for estado in estados_finales):
        plano.estado = ESTADO_EN_VERIFICACION
    elif all(estado == ESTADO_APROBADO for estado in estados_finales):
        plano.estado = ESTADO_APROBADO
    else:
        plano.estado = ESTADO_EN_VERIFICACION

    plano.save(update_fields=["estado"])
    return plano.estado
