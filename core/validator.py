from __future__ import annotations
from typing import List, Optional

from rapidfuzz import fuzz

from .models import NRMateriales, Plano


FUZZY_THRESHOLD = 80


def parse_csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(str(value).strip().lower().split())


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


def validar_plano_contra_bd(plano: Plano, dias_repeticion: int = 90) -> dict:
    """
    Primera validación:
    - separa NR válidos y desconocidos
    - detecta duplicado de id_plano_deposito
    - deja estado preliminar

    Esta función NO hace todavía la validación completa contra Reclamo.
    """
    nrs = parse_csv(plano.nr_detectados)

    validos = []
    desconocidos = []
    motivos = []

    for nr in nrs:
        if NRMateriales.objects.filter(numero_nr=nr).exists():
            validos.append(nr)
        else:
            desconocidos.append(nr)

    if not validos:
        motivos.append("No se detectó ningún NR válido (existente en BD).")
        estado = "EN_VERIFICACION"
    elif desconocidos:
        motivos.append(f"Se detectaron NR no existentes en BD: {', '.join(desconocidos)}")
        estado = "EN_VERIFICACION"
    else:
        estado = "EN_REVISION"

    dup = Plano.objects.filter(id_plano_deposito=plano.id_plano_deposito).exclude(pk=plano.pk).exists()
    if dup:
        motivos.append("ID de plano duplicado.")
        estado = "RECHAZADO"

    plano.nr_validos = ",".join(validos) if validos else None
    plano.nr_desconocidos = ",".join(desconocidos) if desconocidos else None
    plano.motivo = "\n".join(motivos) if motivos else None
    plano.estado = estado
    plano.save()

    return {
        "estado": estado,
        "validos": validos,
        "desconocidos": desconocidos,
        "motivos": motivos,
    }