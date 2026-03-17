from __future__ import annotations
from typing import List, Optional

from rapidfuzz import fuzz

from .models import NRMateriales, Plano, ResultadoValidacionPlano


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


def compare_text_fuzzy(a: Optional[str], b: Optional[str], threshold: int = FUZZY_THRESHOLD) -> dict:
    """
    Compara dos textos de forma aproximada usando rapidfuzz.

    Retorna:
    {
        "comparable": bool,
        "matched": bool | None,
        "score": int | float | None,
        "reason": str | None,
    }
    """
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
    """
    Regla:
    fecha_trabajo >= fecha_reclamo
    """
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
        motivos.append("No se detectó ningún NR válido (existente en BD).")
        return ESTADO_EN_VERIFICACION, motivos

    if desconocidos:
        motivos.append(f"Se detectaron NR no existentes en BD: {', '.join(desconocidos)}")
        return ESTADO_EN_VERIFICACION, motivos

    return ESTADO_EN_REVISION, motivos


def crear_resultado_validacion_preliminar(plano: Plano, nr: str, nr_obj: Optional[NRMateriales]):
    if nr_obj:
        return ResultadoValidacionPlano.objects.create(
            plano=plano,
            nr_detectado=nr,
            nr_normalizado=nr,
            nr_materiales_encontrado=nr_obj,
            reclamo_encontrado=nr_obj.reclamo,
            estado_resultado=ESTADO_EN_VERIFICACION,  # preliminar, luego services.py define mejor
            ciudad_ok=False,
            zona_ok=False,
            fecha_ok=False,
            motivo_resultado="NR encontrado en base de datos. Pendiente de validación completa.",
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
        motivo_resultado="NR no encontrado en base de datos.",
    )


def validar_plano_contra_bd(plano: Plano, dias_repeticion: int = 90) -> dict:
    """
    Primera validación:
    - separa NR válidos y desconocidos
    - detecta duplicado de id_plano_deposito
    - deja estado preliminar
    - crea resultados básicos por NR detectado

    Esta función NO hace todavía la validación completa contra Reclamo.
    """
    nrs = parse_csv(plano.nr_detectados)

    validos = []
    desconocidos = []

    # Si el plano se reprocesa, limpiamos resultados previos
    plano.resultados_validacion.all().delete()

    # Búsqueda en lote para evitar múltiples consultas
    nr_objs = {
        obj.numero_nr: obj
        for obj in NRMateriales.objects.filter(numero_nr__in=nrs).select_related("reclamo")
    }

    for nr in nrs:
        nr_obj = nr_objs.get(nr)

        if nr_obj:
            validos.append(nr)
            crear_resultado_validacion_preliminar(plano, nr, nr_obj)
        else:
            desconocidos.append(nr)
            crear_resultado_validacion_preliminar(plano, nr, None)

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