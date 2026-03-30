from django.utils import timezone
import re
from datetime import date

from .models import NRMateriales, Plano, Auditoria, Reclamo
from .validator import (
    parse_csv,
    compare_text_fuzzy,
    compare_dates,
    validar_plano_contra_bd,
)
from .ocr_extract import (
    ocr_text_from_file,
    extract_nrs,
    extract_fecha_plano,
    extract_detalles_por_nr,
)


ESTADO_APROBADO = "APROBADO"
ESTADO_RECHAZADO = "RECHAZADO"
ESTADO_EN_VERIFICACION = "EN_VERIFICACION"
ESTADO_EN_ESPERA = "EN_ESPERA"

RE_FECHA_NUMERICA = re.compile(r"(?<!\d)(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})(?!\d)")


def deduplicate_keep_order(values):
    resultado = []
    vistos = set()

    for value in values:
        if value and value not in vistos:
            resultado.append(value)
            vistos.add(value)

    return resultado


def normalize_nr_list(values):
    """
    Limpieza defensiva de lista de NR.
    No altera el formato si ya viene normalizado desde extract_nrs,
    solo elimina vacíos, espacios extra y duplicados.
    """
    limpios = []

    for value in values or []:
        if not value:
            continue

        value = str(value).strip()
        if not value:
            continue

        limpios.append(value)

    return deduplicate_keep_order(limpios)


def normalize_for_contains(value):
    if not value:
        return ""

    value = str(value).strip().lower()
    value = re.sub(r"[^a-záéíóúñ0-9\s]", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def get_candidate_location_lines(text):
    """
    Devuelve líneas OCR candidatas a contener ciudad/localidad.
    Evita líneas de ruido típicas del plano.
    """
    if not text:
        return []

    lineas = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        line_norm = normalize_for_contains(line)

        if not line_norm:
            continue

        if len(line_norm) > 45:
            continue

        if sum(ch.isdigit() for ch in line_norm) >= 4:
            continue

        if any(
            ruido in line_norm
            for ruido in [
                "oem",
                "lpn",
                "item",
                "mantenimientos",
                "rep tecnico",
                "fiscal",
                "influencia",
                "dist coronel oviedo",
                "x ",
                "y ",
                "r n",
                "fecha",
                "lampara",
                "ignitor",
                "limpieza",
                "ife",
                "blc",
                "bcc",
                "bca",
                "py08",
            ]
        ):
            continue

        lineas.append(line.strip())

    return deduplicate_keep_order(lineas)


def detectar_ciudad_desde_ocr(texto_ocr, ciudad_reclamo):
    """
    Intenta validar la ciudad/localidad del plano a partir del OCR.
    Estrategia:
    1) coincidencia exacta por inclusión en el texto OCR
    2) mejor coincidencia fuzzy sobre líneas cortas candidatas
    """
    if not texto_ocr or not ciudad_reclamo:
        return {
            "comparable": False,
            "matched": None,
            "score": None,
            "ciudad_detectada": None,
            "reason": "Falta texto OCR o ciudad del reclamo",
        }

    texto_norm = normalize_for_contains(texto_ocr)
    ciudad_norm = normalize_for_contains(ciudad_reclamo)

    if not texto_norm or not ciudad_norm:
        return {
            "comparable": False,
            "matched": None,
            "score": None,
            "ciudad_detectada": None,
            "reason": "No se pudo normalizar el texto para comparar ciudad",
        }

    if ciudad_norm in texto_norm:
        return {
            "comparable": True,
            "matched": True,
            "score": 100,
            "ciudad_detectada": ciudad_reclamo,
            "reason": None,
        }

    lineas_candidatas = get_candidate_location_lines(texto_ocr)

    mejor_score = -1
    mejor_linea = None

    for linea in lineas_candidatas:
        resultado = compare_text_fuzzy(linea, ciudad_reclamo)
        score = resultado.get("score") or 0

        if score > mejor_score:
            mejor_score = score
            mejor_linea = linea

    if mejor_linea is None:
        return {
            "comparable": False,
            "matched": None,
            "score": None,
            "ciudad_detectada": None,
            "reason": "No se detectaron líneas candidatas para ciudad",
        }

    matched = mejor_score >= 80

    return {
        "comparable": True,
        "matched": matched,
        "score": mejor_score,
        "ciudad_detectada": mejor_linea,
        "reason": None if matched else f"No coincide (score={mejor_score}, threshold=80)",
    }


def build_estado_from_checks(resultado_ciudad, resultado_zona, resultado_fecha):
    hay_contradiccion = (
        (resultado_ciudad["comparable"] and resultado_ciudad["matched"] is False)
        or (resultado_zona["comparable"] and resultado_zona["matched"] is False)
        or (resultado_fecha["comparable"] and resultado_fecha["matched"] is False)
    )

    hay_pendientes = (
        not resultado_ciudad["comparable"]
        or not resultado_zona["comparable"]
        or not resultado_fecha["comparable"]
    )

    if hay_contradiccion:
        return ESTADO_RECHAZADO

    if hay_pendientes:
        return ESTADO_EN_VERIFICACION

    return ESTADO_APROBADO


def build_resultado_pendiente_zona():
    return {
        "comparable": False,
        "matched": None,
        "reason": "Zona no detectada en el bloque OCR del NR",
    }


def build_resultado_pendiente_fecha():
    return {
        "comparable": False,
        "matched": None,
        "reason": "Fecha no detectada en el bloque OCR del NR",
        "fecha_usada": None,
        "fecha_original": None,
        "fecha_corregida": False,
    }


def parse_numeric_date_candidates_from_lines(lines):
    """
    Extrae candidatos de fechas numéricas crudas para poder corregir años OCR
    extraños como 2020/2028 cuando en realidad era 2026.
    """
    candidatos = []

    for line in lines or []:
        if not line:
            continue

        for m in RE_FECHA_NUMERICA.finditer(str(line)):
            try:
                dia = int(m.group(1))
                mes = int(m.group(2))
                anio = int(m.group(3))
            except Exception:
                continue

            if anio < 100:
                anio += 2000

            candidatos.append(
                {
                    "raw": m.group(0),
                    "day": dia,
                    "month": mes,
                    "year": anio,
                }
            )

    return candidatos


def corregir_fecha_ocr_con_contexto(fecha_detectada, lines, fecha_reclamo):
    """
    Ajusta fechas OCR cuando el día/mes parecen correctos pero el año sale mal.

    Regla:
    - Si el año detectado está muy lejos del año del reclamo, lo consideramos
      sospechoso.
    - En ese caso reconstruimos la fecha usando el mismo día/mes y un año
      plausible respecto al reclamo:
        * mismo año del reclamo, si el mes/día es posterior o igual
        * año del reclamo + 1, si el mes/día es anterior
    """
    if not fecha_detectada or not fecha_reclamo:
        return {
            "fecha": fecha_detectada,
            "original": fecha_detectada,
            "corregida": False,
        }

    year_diff = abs(fecha_detectada.year - fecha_reclamo.year)

    if year_diff <= 1:
        return {
            "fecha": fecha_detectada,
            "original": fecha_detectada,
            "corregida": False,
        }

    candidatos = parse_numeric_date_candidates_from_lines(lines)

    if candidatos:
        base = candidatos[0]
        dia = base["day"]
        mes = base["month"]
    else:
        dia = fecha_detectada.day
        mes = fecha_detectada.month

    try:
        if (mes, dia) >= (fecha_reclamo.month, fecha_reclamo.day):
            anio_plausible = fecha_reclamo.year
        else:
            anio_plausible = fecha_reclamo.year + 1

        fecha_corregida = date(anio_plausible, mes, dia)

        return {
            "fecha": fecha_corregida,
            "original": fecha_detectada,
            "corregida": True,
        }
    except Exception:
        return {
            "fecha": fecha_detectada,
            "original": fecha_detectada,
            "corregida": False,
        }


def validar_nr_contra_reclamo(plano, reclamo, nr_obj=None, detalle_ocr=None):
    """
    Valida un NR detectado en el plano contra su Reclamo asociado.

    Reglas nuevas:
    - ciudad: se valida desde el texto OCR general del plano vs reclamo.ciudad
    - zona: se valida desde la zona detectada en el bloque OCR del NR vs reclamo.zona
    - fecha: se valida desde la fecha detectada en el bloque OCR del NR >= reclamo.fecha_reclamo

    Nota:
    - la fecha general del plano se conserva solo como dato auxiliar,
      pero la comparación principal usa la fecha por cada NR.
    """
    detalles = []

    if not reclamo:
        return {
            "nr": None,
            "estado": ESTADO_EN_VERIFICACION,
            "detalles": ["Sin reclamo asociado"],
            "ciudad_ok": False,
            "zona_ok": False,
            "fecha_ok": False,
            "motivo_resultado": "Sin reclamo asociado",
        }

    detalle_ocr = detalle_ocr or {}
    zona_ocr = detalle_ocr.get("zona")
    fecha_ocr = detalle_ocr.get("fecha")
    materiales_ocr = detalle_ocr.get("materiales", [])
    lineas_ocr = detalle_ocr.get("lineas", [])

    resultado_ciudad = detectar_ciudad_desde_ocr(plano.texto_ocr, reclamo.ciudad)

    if zona_ocr:
        resultado_zona = compare_text_fuzzy(zona_ocr, reclamo.zona)
    else:
        resultado_zona = build_resultado_pendiente_zona()

    if fecha_ocr:
        ajuste_fecha = corregir_fecha_ocr_con_contexto(
            fecha_detectada=fecha_ocr,
            lines=lineas_ocr,
            fecha_reclamo=reclamo.fecha_reclamo,
        )
        fecha_usada = ajuste_fecha["fecha"]
        fecha_original = ajuste_fecha["original"]
        fecha_corregida = ajuste_fecha["corregida"]

        resultado_fecha = compare_dates(fecha_usada, reclamo.fecha_reclamo)
        resultado_fecha["fecha_usada"] = fecha_usada
        resultado_fecha["fecha_original"] = fecha_original
        resultado_fecha["fecha_corregida"] = fecha_corregida
    else:
        resultado_fecha = build_resultado_pendiente_fecha()

    if resultado_ciudad["comparable"]:
        ciudad_detectada = resultado_ciudad.get("ciudad_detectada") or "No detectada"
        if resultado_ciudad["matched"]:
            detalles.append(
                f"Ciudad correcta (Plano: {ciudad_detectada} | Reclamo: {reclamo.ciudad})"
            )
        else:
            detalles.append(
                f"Ciudad no coincide (Plano: {ciudad_detectada} | Reclamo: {reclamo.ciudad})"
            )
    else:
        detalles.append(f"Ciudad pendiente (Reclamo: {reclamo.ciudad})")

    if resultado_zona["comparable"]:
        if resultado_zona["matched"]:
            detalles.append(
                f"Zona correcta (Plano/NR: {zona_ocr} | Reclamo: {reclamo.zona})"
            )
        else:
            detalles.append(
                f"Zona no coincide (Plano/NR: {zona_ocr} | Reclamo: {reclamo.zona})"
            )
    else:
        detalles.append(f"Zona pendiente (Reclamo: {reclamo.zona})")

    if resultado_fecha["comparable"]:
        fecha_usada = resultado_fecha.get("fecha_usada")
        fecha_original = resultado_fecha.get("fecha_original")
        fecha_corregida = resultado_fecha.get("fecha_corregida", False)

        if resultado_fecha["matched"]:
            if fecha_corregida and fecha_original and fecha_usada:
                detalles.append(
                    f"Fecha correcta (OCR: {fecha_original} ajustada a {fecha_usada} >= Reclamo: {reclamo.fecha_reclamo})"
                )
            else:
                detalles.append(
                    f"Fecha correcta (NR: {fecha_usada} >= Reclamo: {reclamo.fecha_reclamo})"
                )
        else:
            if fecha_corregida and fecha_original and fecha_usada:
                detalles.append(
                    f"Fecha no válida (OCR: {fecha_original} ajustada a {fecha_usada} < Reclamo: {reclamo.fecha_reclamo})"
                )
            else:
                detalles.append(
                    f"Fecha no válida (NR: {fecha_usada} < Reclamo: {reclamo.fecha_reclamo})"
                )
    else:
        detalles.append(f"Fecha pendiente (Reclamo: {reclamo.fecha_reclamo})")

    if materiales_ocr:
        resumen_materiales = ", ".join(
            f"{m.get('cantidad_mostrar') or m.get('cantidad') or '-'} {m.get('descripcion', '')}".strip()
            for m in materiales_ocr[:4]
        )
        if len(materiales_ocr) > 4:
            resumen_materiales += ", ..."
        detalles.append(f"Materiales OCR detectados: {resumen_materiales}")
    else:
        detalles.append("Materiales OCR detectados: ninguno")

    estado = build_estado_from_checks(
        resultado_ciudad,
        resultado_zona,
        resultado_fecha,
    )

    return {
        "nr": reclamo.numero_reclamo,
        "estado": estado,
        "detalles": detalles,
        "ciudad_ok": bool(resultado_ciudad["comparable"] and resultado_ciudad["matched"]),
        "zona_ok": bool(resultado_zona["comparable"] and resultado_zona["matched"]),
        "fecha_ok": bool(resultado_fecha["comparable"] and resultado_fecha["matched"]),
        "motivo_resultado": " | ".join(detalles),
    }


def validar_plano_completo(plano: Plano):
    nr_validos = normalize_nr_list(parse_csv(plano.nr_validos))
    nr_desconocidos = normalize_nr_list(parse_csv(plano.nr_desconocidos))
    detalles_ocr_por_nr = extract_detalles_por_nr(plano.texto_ocr or "")

    detalles_nr = []
    resumen_general = []
    estado_final = ESTADO_APROBADO

    if not nr_validos:
        estado_final = ESTADO_EN_VERIFICACION
        resumen_general.append("No hay NR válidos para validar contra Reclamo.")
    else:
        reclamos = list(Reclamo.objects.filter(numero_reclamo__in=nr_validos))
        reclamos_map = {obj.numero_reclamo: obj for obj in reclamos}

        nr_materiales_qs = list(
            NRMateriales.objects.select_related("reclamo").filter(
                numero_nr__in=nr_validos
            )
        )
        nr_materiales_map = {obj.numero_nr: obj for obj in nr_materiales_qs}

        faltantes = [nr for nr in nr_validos if nr not in reclamos_map]
        for nr_faltante in faltantes:
            detalles_nr.append(f"NR {nr_faltante} -> {ESTADO_EN_VERIFICACION}")
            detalles_nr.append(" - NR válido detectado, pero no localizado en Reclamo al validar.")

            resultado_plano = plano.resultados_validacion.filter(
                nr_detectado=nr_faltante
            ).first()

            if resultado_plano:
                resultado_plano.estado_resultado = ESTADO_EN_VERIFICACION
                resultado_plano.ciudad_ok = False
                resultado_plano.zona_ok = False
                resultado_plano.fecha_ok = False
                resultado_plano.motivo_resultado = (
                    "NR válido detectado, pero no localizado en Reclamo al validar."
                )
                resultado_plano.save()

            if estado_final != ESTADO_RECHAZADO:
                estado_final = ESTADO_EN_VERIFICACION

        for nr in nr_validos:
            reclamo = reclamos_map.get(nr)
            if not reclamo:
                continue

            nr_obj = nr_materiales_map.get(nr)
            detalle_ocr = detalles_ocr_por_nr.get(nr, {})
            resultado = validar_nr_contra_reclamo(
                plano=plano,
                reclamo=reclamo,
                nr_obj=nr_obj,
                detalle_ocr=detalle_ocr,
            )

            detalles_nr.append(f"NR {resultado['nr']} -> {resultado['estado']}")
            for detalle in resultado["detalles"]:
                detalles_nr.append(f" - {detalle}")

            resultado_plano = plano.resultados_validacion.filter(
                nr_detectado=resultado["nr"]
            ).first()

            if resultado_plano:
                resultado_plano.estado_resultado = resultado["estado"]
                resultado_plano.ciudad_ok = resultado["ciudad_ok"]
                resultado_plano.zona_ok = resultado["zona_ok"]
                resultado_plano.fecha_ok = resultado["fecha_ok"]
                resultado_plano.motivo_resultado = resultado["motivo_resultado"]
                resultado_plano.reclamo_encontrado = reclamo
                resultado_plano.nr_materiales_encontrado = nr_obj
                resultado_plano.save()

            if resultado["estado"] == ESTADO_RECHAZADO:
                estado_final = ESTADO_RECHAZADO
            elif (
                resultado["estado"] == ESTADO_EN_VERIFICACION
                and estado_final != ESTADO_RECHAZADO
            ):
                estado_final = ESTADO_EN_VERIFICACION

    if nr_desconocidos:
        resumen_general.append("Se detectaron NR desconocidos en el OCR.")
        if estado_final == ESTADO_APROBADO:
            estado_final = ESTADO_EN_VERIFICACION

    motivo_lineas = [
        f"RESULTADO FINAL DEL PLANO: {estado_final}",
        "",
    ]

    if resumen_general:
        motivo_lineas.append("Motivo general:")
        for motivo in resumen_general:
            motivo_lineas.append(f" - {motivo}")
        motivo_lineas.append("")

    if detalles_nr:
        motivo_lineas.extend(detalles_nr)
        motivo_lineas.append("")

    if nr_desconocidos:
        motivo_lineas.append("NR desconocidos detectados:")
        for nr in nr_desconocidos:
            motivo_lineas.append(f" - {nr}")

    plano.estado = estado_final
    plano.motivo = "\n".join(motivo_lineas).strip()
    plano.save(update_fields=["estado", "motivo"])

    return {
        "estado": estado_final,
        "motivos": motivo_lineas,
    }


def procesar_plano_completo(plano: Plano, usuario: str = "sistema"):
    """
    Ejecuta todo el flujo del plano:
    1. OCR
    2. extracción de NR y fecha general auxiliar
    3. validación contra BD
    4. validación completa contra Reclamo
    """
    try:
        file_path = plano.archivo.path
        text = ocr_text_from_file(file_path)

        nrs = normalize_nr_list(extract_nrs(text))
        fecha = extract_fecha_plano(text)

        plano.texto_ocr = text
        plano.nr_detectados = ",".join(nrs) if nrs else None
        plano.fecha_plano = fecha
        plano.procesado = True
        plano.procesado_por = usuario
        plano.fecha_procesamiento = timezone.now()
        plano.save()

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="PROCESAR_OCR",
            descripcion=f"OCR OK -> nr_detectados={plano.nr_detectados} fecha_plano={plano.fecha_plano}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        res_bd = validar_plano_contra_bd(plano)
        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="VALIDAR_NR_BD",
            descripcion=(
                f"VALIDACIÓN NR -> estado={res_bd['estado']} "
                f"validos={res_bd['validos']} "
                f"desconocidos={res_bd['desconocidos']}"
            ),
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        res_final = validar_plano_completo(plano)
        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="VALIDAR_PLANO_COMPLETO",
            descripcion=f"VALIDACIÓN COMPLETA -> estado={res_final['estado']} motivos={res_final['motivos']}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        return {
            "ok": True,
            "estado": plano.estado,
            "plano_id": plano.id,
            "motivo": plano.motivo,
        }

    except Exception as e:
        plano.estado = ESTADO_EN_ESPERA
        plano.save(update_fields=["estado"])

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="PROCESO_COMPLETO_ERROR",
            descripcion=str(e),
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        return {
            "ok": False,
            "estado": plano.estado,
            "error": str(e),
        }
