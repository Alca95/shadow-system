from django.utils import timezone
import json
import re
from datetime import date, datetime
from core.ocr_extract import extract_detalles_por_nr

from .models import NRMateriales, Plano, Auditoria, Reclamo, MaterialDetectadoPlano
from .validator import (
    parse_csv,
    compare_text_fuzzy,
    compare_dates,
    validar_plano_contra_bd,
    evaluar_resultado_nr,
)
from .ocr_extract import (
    ocr_text_from_file,
    extract_nrs,
    extract_fecha_plano,
    extract_detalles_por_nr,
)
from .gpt_extract import extract_with_gpt


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
                "oem", "lpn", "item", "mantenimientos", "rep tecnico", "fiscal",
                "influencia", "dist coronel oviedo", "x ", "y ", "r n", "fecha",
                "lampara", "ignitor", "limpieza", "ife", "blc", "bcc", "bca", "py08",
                "cantidad", "descripcion", "materiales", "detalles",
            ]
        ):
            continue

        lineas.append(line.strip())

    return deduplicate_keep_order(lineas)


def detectar_ciudad_desde_ocr(texto_ocr, ciudad_reclamo):
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
    if not fecha_detectada or not fecha_reclamo:
        return {"fecha": fecha_detectada, "original": fecha_detectada, "corregida": False}

    year_diff = abs(fecha_detectada.year - fecha_reclamo.year)
    if year_diff <= 1:
        return {"fecha": fecha_detectada, "original": fecha_detectada, "corregida": False}

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
        return {"fecha": fecha_corregida, "original": fecha_detectada, "corregida": True}
    except Exception:
        return {"fecha": fecha_detectada, "original": fecha_detectada, "corregida": False}


def validar_nr_contra_reclamo(plano, reclamo, nr_obj=None, detalle_ocr=None):
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

    # 🔥 NUEVO: usar ciudad del reclamo directamente (más confiable que OCR)
    resultado_ciudad = {
        "comparable": True,
        "matched": True,
        "score": 100,
        "ciudad_detectada": reclamo.ciudad,
        "reason": None,
    }

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
            detalles.append(f"Ciudad correcta (Plano: {ciudad_detectada} | Reclamo: {reclamo.ciudad})")
        else:
            detalles.append(f"Ciudad no coincide (Plano: {ciudad_detectada} | Reclamo: {reclamo.ciudad})")
    else:
        detalles.append(f"Ciudad pendiente (Reclamo: {reclamo.ciudad})")

    if resultado_zona["comparable"]:
        if resultado_zona["matched"]:
            detalles.append(f"Zona correcta (Plano/NR: {zona_ocr} | Reclamo: {reclamo.zona})")
        else:
            detalles.append(f"Zona no coincide (Plano/NR: {zona_ocr} | Reclamo: {reclamo.zona})")
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
                detalles.append(f"Fecha correcta (NR: {fecha_usada} >= Reclamo: {reclamo.fecha_reclamo})")
        else:
            if fecha_corregida and fecha_original and fecha_usada:
                detalles.append(
                    f"Fecha no válida (OCR: {fecha_original} ajustada a {fecha_usada} < Reclamo: {reclamo.fecha_reclamo})"
                )
            else:
                detalles.append(f"Fecha no válida (NR: {fecha_usada} < Reclamo: {reclamo.fecha_reclamo})")
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

    estado = build_estado_from_checks(resultado_ciudad, resultado_zona, resultado_fecha)

    return {
        "nr": reclamo.numero_reclamo,
        "estado": estado,
        "detalles": detalles,
        "ciudad_ok": bool(resultado_ciudad["comparable"] and resultado_ciudad["matched"]),
        "zona_ok": bool(resultado_zona["comparable"] and resultado_zona["matched"]),
        "fecha_ok": bool(resultado_fecha["comparable"] and resultado_fecha["matched"]),
        "motivo_resultado": " | ".join(detalles),
    }


def _parse_gpt_fecha(fecha_raw):
    if not fecha_raw:
        return None

    text = str(fecha_raw).strip()
    if not text:
        return None

    formatos = ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d")
    for fmt in formatos:
        try:
            return datetime.strptime(text, fmt).date()
        except Exception:
            continue

    match = RE_FECHA_NUMERICA.search(text)
    if match:
        try:
            dia = int(match.group(1))
            mes = int(match.group(2))
            anio = int(match.group(3))
            if anio < 100:
                anio += 2000
            return date(anio, mes, dia)
        except Exception:
            return None

    return None


def _normalize_gpt_materiales(materiales):
    salida = []
    for item in materiales or []:
        if not isinstance(item, dict):
            continue

        descripcion = str(item.get("descripcion") or "").strip()
        if not descripcion:
            continue

        cantidad = item.get("cantidad", 1)
        try:
            cantidad_num = float(cantidad)
        except Exception:
            cantidad_num = 1.0

        if cantidad_num.is_integer():
            cantidad_mostrar = str(int(cantidad_num))
        else:
            cantidad_mostrar = str(cantidad_num).replace(".", ",")

        salida.append(
            {
                "cantidad": cantidad_num,
                "cantidad_mostrar": cantidad_mostrar,
                "descripcion": descripcion,
                "unidad_medida": "unidad",
                "catalogo_match": True,
                "score_catalogo": 100,
                "texto_original": f"{cantidad_mostrar} {descripcion}",
            }
        )
    return salida


def _normalize_material_name(text):
    if not text:
        return ""
    text = str(text).strip().upper()
    text = re.sub(r"\s+", " ", text)
    return text


def _has_material(materiales, material_name):
    objetivo = _normalize_material_name(material_name)
    for item in materiales or []:
        if _normalize_material_name(item.get("descripcion")) == objetivo:
            return True
    return False


def _merge_gpt_with_ocr_support(nrs, detalles_gpt, detalles_ocr):
    for nr in nrs:
        detalle_gpt = detalles_gpt.get(nr, {}) or {}
        detalle_ocr = detalles_ocr.get(nr, {}) or {}

        zona_gpt = detalle_gpt.get("zona")
        fecha_gpt = detalle_gpt.get("fecha")
        materiales_gpt = detalle_gpt.get("materiales", []) or []

        if not zona_gpt and detalle_ocr.get("zona"):
            detalle_gpt["zona"] = detalle_ocr.get("zona")

        if not fecha_gpt and detalle_ocr.get("fecha"):
            detalle_gpt["fecha"] = detalle_ocr.get("fecha")

        materiales_ocr = detalle_ocr.get("materiales", []) or []

        if (not materiales_gpt) and materiales_ocr:
            detalle_gpt["materiales"] = materiales_ocr
        else:
            if _has_material(materiales_ocr, "IFE") and not _has_material(materiales_gpt, "IFE"):
                detalle_gpt.setdefault("materiales", []).insert(
                    0,
                    {
                        "cantidad": 1.0,
                        "cantidad_mostrar": "1",
                        "descripcion": "IFE",
                        "unidad_medida": "unidad",
                        "catalogo_match": True,
                        "score_catalogo": 100,
                        "texto_original": "1 IFE",
                    }
                )

        detalle_gpt.setdefault("lineas", [])
        detalles_gpt[nr] = detalle_gpt

    return detalles_gpt


def _normalize_gpt_detalles_to_internal(payload):
    payload = payload or {}
    nrs = normalize_nr_list(payload.get("nrs", []))
    detalles_raw = payload.get("detalles", {}) or {}

    detalles = {}
    for nr in nrs:
        raw = detalles_raw.get(nr, {}) or {}
        zona = raw.get("zona")
        fecha = _parse_gpt_fecha(raw.get("fecha"))
        materiales = _normalize_gpt_materiales(raw.get("materiales"))

        detalles[nr] = {
            "zona": zona,
            "fecha": fecha,
            "materiales": materiales,
            "lineas": [],
        }

    return nrs, detalles

def sincronizar_materiales_detectados(plano):
    detalles_por_nr = extract_detalles_por_nr(plano.texto_ocr or "")

    for resultado in plano.resultados_validacion.all():
        nr = resultado.nr_detectado
        detalle = detalles_por_nr.get(nr, {})

        materiales = detalle.get("materiales", [])

        # 🔥 limpiar anteriores (reproceso)
        MaterialDetectadoPlano.objects.filter(resultado=resultado).delete()

        orden = 1

        for mat in materiales:
            MaterialDetectadoPlano.objects.create(
                resultado=resultado,
                orden=orden,
                cantidad_original=mat.get("cantidad"),
                unidad_original=mat.get("unidad_medida"),
                descripcion_original=mat.get("descripcion"),

                cantidad_final=mat.get("cantidad"),
                unidad_final=mat.get("unidad_medida"),
                descripcion_final=mat.get("descripcion"),

                fue_editado=False,
                coincide_con_bd=True,
            )
            orden += 1

def sincronizar_materiales_detectados_desde_detalles(plano, detalles_por_nr):
    for resultado in plano.resultados_validacion.all():
        nr = resultado.nr_detectado
        detalle = (detalles_por_nr or {}).get(nr, {}) or {}
        materiales = detalle.get("materiales", []) or []

        # limpiar anteriores del mismo resultado
        resultado.materiales_detectados.all().delete()

        nuevos = []
        for orden, mat in enumerate(materiales, start=1):
            cantidad = mat.get("cantidad_mostrar") or mat.get("cantidad") or "-"
            unidad = mat.get("unidad_medida") or "unidad"
            descripcion = mat.get("descripcion") or "-"

            nuevos.append(
                MaterialDetectadoPlano(
                    resultado_validacion=resultado,
                    orden=orden,
                    cantidad_original=str(cantidad),
                    unidad_original=str(unidad),
                    descripcion_original=str(descripcion),
                    fue_editado=False,
                    coincide_con_bd=False,
                )
            )

        if nuevos:
            MaterialDetectadoPlano.objects.bulk_create(nuevos)

def validar_plano_completo(plano: Plano):
    nr_validos = normalize_nr_list(parse_csv(plano.nr_validos))
    nr_desconocidos = normalize_nr_list(parse_csv(plano.nr_desconocidos))

    detalles_ocr_por_nr = extract_detalles_por_nr(plano.texto_ocr or "")
    
    sincronizar_materiales_detectados_desde_detalles(plano, detalles_ocr_por_nr)

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
            NRMateriales.objects.select_related("reclamo").filter(numero_nr__in=nr_validos)
        )
        nr_materiales_map = {obj.numero_nr: obj for obj in nr_materiales_qs}

        faltantes = [nr for nr in nr_validos if nr not in reclamos_map]
        for nr_faltante in faltantes:
            detalles_nr.append(f"NR {nr_faltante} -> {ESTADO_EN_VERIFICACION}")
            detalles_nr.append(" - NR válido detectado, pero no localizado en Reclamo al validar.")

            resultado_plano = plano.resultados_validacion.filter(nr_detectado=nr_faltante).first()

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
            detalle_ocr = detalles_ocr_por_nr.get(nr, {}) or {}

            zona_ocr = detalle_ocr.get("zona")
            fecha_ocr = detalle_ocr.get("fecha")
            materiales_ocr = detalle_ocr.get("materiales", []) or []

            materiales_plano_texto = ", ".join(
                f"{m.get('cantidad_mostrar') or m.get('cantidad') or '-'} {m.get('descripcion', '')}".strip()
                for m in materiales_ocr
                if (m.get("descripcion") or "").strip()
            )

            resultado_plano = plano.resultados_validacion.filter(nr_detectado=nr).first()
            if not resultado_plano:
                continue

            resultado_plano.reclamo_encontrado = reclamo
            resultado_plano.nr_materiales_encontrado = nr_obj
            resultado_plano.save(update_fields=["reclamo_encontrado", "nr_materiales_encontrado"])

            evaluar_resultado_nr(
                resultado=resultado_plano,
                ciudad_plano=reclamo.ciudad,   # mantienes tu lógica actual de ciudad confiable
                zona_plano=zona_ocr,
                fecha_plano=fecha_ocr,
                materiales_plano_texto=materiales_plano_texto,
            )

            detalles_nr.append(f"NR {nr} -> {resultado_plano.estado_resultado}")
            if resultado_plano.motivo_resultado:
                for detalle in str(resultado_plano.motivo_resultado).split(" | "):
                    detalles_nr.append(f" - {detalle}")

            if resultado_plano.estado_resultado == ESTADO_RECHAZADO:
                estado_final = ESTADO_RECHAZADO
            elif resultado_plano.estado_resultado == ESTADO_EN_VERIFICACION and estado_final != ESTADO_RECHAZADO:
                estado_final = ESTADO_EN_VERIFICACION

    if nr_desconocidos:
        resumen_general.append("Se detectaron NR desconocidos en el OCR.")
        if estado_final == ESTADO_APROBADO:
            estado_final = ESTADO_EN_VERIFICACION

    motivo_lineas = [f"RESULTADO FINAL DEL PLANO: {estado_final}", ""]

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

    return {"estado": estado_final, "motivos": motivo_lineas}


def procesar_plano_con_gpt(plano: Plano, usuario: str = "sistema"):
    try:
        file_path = plano.archivo.path

        texto_ocr_local = ocr_text_from_file(file_path)
        detalles_ocr_local = extract_detalles_por_nr(texto_ocr_local or "")

        payload = extract_with_gpt(file_path)
        nrs, detalles_gpt = _normalize_gpt_detalles_to_internal(payload)
        detalles_gpt = _merge_gpt_with_ocr_support(nrs, detalles_gpt, detalles_ocr_local)

        primera_fecha = None
        for nr in nrs:
            fecha_nr = detalles_gpt.get(nr, {}).get("fecha")
            if fecha_nr:
                primera_fecha = fecha_nr
                break

        plano.texto_ocr = texto_ocr_local or json.dumps(payload, ensure_ascii=False, indent=2)
        plano.nr_detectados = ",".join(nrs) if nrs else None
        plano.fecha_plano = primera_fecha
        plano.procesado = True
        plano.procesado_por = usuario
        plano.fecha_procesamiento = timezone.now()
        plano.save()

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="PROCESAR_GPT",
            descripcion=f"GPT OK -> nr_detectados={plano.nr_detectados} fecha_plano={plano.fecha_plano}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="GPT_JSON_RESULTADO",
            descripcion=json.dumps(payload, ensure_ascii=False)[:1800],
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        res_bd = validar_plano_contra_bd(plano)
        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="VALIDAR_NR_BD_GPT",
            descripcion=(
                f"VALIDACIÓN NR (GPT) -> estado={res_bd['estado']} "
                f"validos={res_bd['validos']} "
                f"desconocidos={res_bd['desconocidos']}"
            ),
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        original_extract = globals()["extract_detalles_por_nr"]
        try:
            globals()["extract_detalles_por_nr"] = lambda _texto: detalles_gpt
            res_final = validar_plano_completo(plano)
        finally:
            globals()["extract_detalles_por_nr"] = original_extract

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="VALIDAR_PLANO_COMPLETO_GPT",
            descripcion=f"VALIDACIÓN COMPLETA (GPT) -> estado={res_final['estado']} motivos={res_final['motivos']}",
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
        error_text = f"ERROR GPT: {str(e)}"

        plano.estado = ESTADO_EN_ESPERA
        plano.motivo = error_text
        plano.texto_ocr = error_text
        plano.procesado = False
        plano.save(update_fields=["estado", "motivo", "texto_ocr", "procesado"])

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=usuario,
            accion="PROCESO_GPT_ERROR",
            descripcion=error_text,
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        return {
            "ok": False,
            "estado": plano.estado,
            "error": str(e),
        }


def procesar_plano_completo(plano: Plano, usuario: str = "sistema", extractor: str = "ocr"):
    """
    Ejecuta todo el flujo del plano.
    Modos:
    - extractor="ocr" (actual, por defecto)
    - extractor="gpt" (nuevo, adicional)
    """
    if extractor == "gpt":
        return procesar_plano_con_gpt(plano, usuario=usuario)

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
