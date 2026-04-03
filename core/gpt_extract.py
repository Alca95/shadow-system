from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Dict, List, Optional

from openai import OpenAI


MODEL_NAME = "gpt-5.4"


def _get_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "No se encontró OPENAI_API_KEY en las variables de entorno."
        )
    return OpenAI(api_key=api_key)


def _encode_file_base64(file_path: str) -> str:
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _build_prompt() -> str:
    return """
Eres un extractor experto de planos de mantenimiento de alumbrado público.

Tu tarea:
1. Detectar TODOS los bloques de trabajo visibles en la hoja.
2. Cada bloque normalmente contiene:
   - NR
   - Zona
   - Fecha
   - Lista de materiales
3. Puede haber 1, 2, 3 o 4 bloques en la misma hoja.
4. NO mezclar materiales entre bloques distintos.
5. Si una línea de material no muestra cantidad explícita pero claramente corresponde al bloque, asumir cantidad 1.
6. Si una fecha se ve dudosa, devuelve la mejor lectura visible del documento en formato DD/MM/YYYY.
7. Si una zona no se distingue con seguridad, devuelve null.
8. Si un bloque no tiene materiales legibles, devuelve lista vacía.
9. No inventes NR ni materiales.

Devuelve SOLO JSON válido, sin comentarios, sin markdown y sin texto extra.

Formato exacto esperado:

{
  "nrs": ["5072455-24"],
  "detalles": {
    "5072455-24": {
      "zona": "Centro",
      "fecha": "22/03/2026",
      "materiales": [
        {"cantidad": 1, "descripcion": "IFE"},
        {"cantidad": 1, "descripcion": "LAMPARA DE 250W-NA"}
      ]
    }
  }
}
"""


def _normalize_nr_value(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    match = re.search(r"(\d{6,8})\s*[-/\.]?\s*(\d{2,4})", text)
    if not match:
        return text

    a = match.group(1)
    b = match.group(2)
    if len(b) == 4:
        b = b[-2:]
    return f"{a}-{b}"


def _normalize_fecha_value(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    m = re.search(r"(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})", text)
    if not m:
        return text

    dd = m.group(1).zfill(2)
    mm = m.group(2).zfill(2)
    yyyy = m.group(3)
    if len(yyyy) == 2:
        yyyy = f"20{yyyy}"
    return f"{dd}/{mm}/{yyyy}"


def _normalize_materiales(materiales: Any) -> List[Dict[str, Any]]:
    if not isinstance(materiales, list):
        return []

    salida: List[Dict[str, Any]] = []
    for item in materiales:
        if not isinstance(item, dict):
            continue

        cantidad = item.get("cantidad")
        descripcion = str(item.get("descripcion") or "").strip()

        if not descripcion:
            continue

        salida.append({
            "cantidad": cantidad,
            "descripcion": descripcion,
        })

    return salida


def _normalize_response_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_nrs = payload.get("nrs", [])
    raw_detalles = payload.get("detalles", {})

    nrs_normalizados: List[str] = []
    detalles_normalizados: Dict[str, Dict[str, Any]] = {}

    if not isinstance(raw_nrs, list):
        raw_nrs = []

    if not isinstance(raw_detalles, dict):
        raw_detalles = {}

    for nr in raw_nrs:
        nr_norm = _normalize_nr_value(nr)
        if nr_norm and nr_norm not in nrs_normalizados:
            nrs_normalizados.append(nr_norm)

    for raw_nr, detalle in raw_detalles.items():
        nr_norm = _normalize_nr_value(raw_nr)
        if not nr_norm:
            continue

        if not isinstance(detalle, dict):
            detalle = {}

        zona = detalle.get("zona")
        fecha = _normalize_fecha_value(detalle.get("fecha"))
        materiales = _normalize_materiales(detalle.get("materiales"))

        detalles_normalizados[nr_norm] = {
            "zona": str(zona).strip() if zona not in (None, "") else None,
            "fecha": fecha,
            "materiales": materiales,
        }

        if nr_norm not in nrs_normalizados:
            nrs_normalizados.append(nr_norm)

    return {
        "nrs": nrs_normalizados,
        "detalles": detalles_normalizados,
    }


def _extract_output_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if output_text:
        return output_text

    try:
        return response.model_dump_json(indent=2)
    except Exception:
        return str(response)


def _try_parse_json_from_text(text: str) -> Dict[str, Any]:
    text = text.strip()

    # intento directo
    try:
        return json.loads(text)
    except Exception:
        pass

    # intenta encontrar el primer bloque {...}
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        fragment = text[start:end + 1]
        try:
            return json.loads(fragment)
        except Exception:
            pass

    raise RuntimeError(
        f"No se pudo parsear JSON válido desde la respuesta de GPT. Respuesta cruda: {text[:1500]}"
    )


def extract_with_gpt(file_path: str) -> Dict[str, Any]:
    client = _get_client()
    image_b64 = _encode_file_base64(file_path)

    response = client.responses.create(
        model=MODEL_NAME,
        input=[
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": "Responde únicamente con JSON válido. No uses markdown."
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": _build_prompt(),
                    },
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_b64}",
                        "detail": "high",
                    },
                ],
            },
        ],
    )

    output_text = _extract_output_text(response).strip()
    if not output_text:
        raise RuntimeError("GPT no devolvió texto en la respuesta.")

    payload = _try_parse_json_from_text(output_text)
    normalized = _normalize_response_payload(payload)

    if not normalized["nrs"]:
        raise RuntimeError(
            f"GPT respondió pero no detectó NR. Respuesta: {json.dumps(payload, ensure_ascii=False)[:1500]}"
        )

    return normalized