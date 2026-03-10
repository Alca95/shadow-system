from .models import NRMateriales, Plano, Auditoria
from .validator import parse_csv, compare_text_fuzzy, compare_dates, validar_plano_contra_bd
from .ocr_extract import ocr_text_from_file, extract_nrs, extract_fecha_plano


ESTADO_APROBADO = "APROBADO"
ESTADO_RECHAZADO = "RECHAZADO"
ESTADO_EN_VERIFICACION = "EN_VERIFICACION"


def validar_nr_contra_reclamo(nr_obj):
    """
    Valida un NRMateriales contra su Reclamo asociado.

    Retorna:
    {
        "nr": str,
        "estado": "APROBADO" | "RECHAZADO" | "EN_VERIFICACION",
        "detalles": [str, ...]
    }
    """
    detalles = []

    reclamo = nr_obj.reclamo
    if not reclamo:
        return {
            "nr": nr_obj.numero_nr,
            "estado": ESTADO_EN_VERIFICACION,
            "detalles": ["NR sin reclamo asociado."],
        }

    resultado_ciudad = compare_text_fuzzy(nr_obj.ciudad, reclamo.ciudad)
    if resultado_ciudad["comparable"]:
        if resultado_ciudad["matched"]:
            detalles.append(
                f"Ciudad OK: '{nr_obj.ciudad}' ~ '{reclamo.ciudad}' (score={resultado_ciudad['score']})"
            )
        else:
            detalles.append(
                f"Ciudad NO coincide: '{nr_obj.ciudad}' vs '{reclamo.ciudad}' (score={resultado_ciudad['score']})"
            )
    else:
        detalles.append("Ciudad pendiente: falta dato en NR o Reclamo.")

    resultado_zona = compare_text_fuzzy(nr_obj.zona, reclamo.zona)
    if resultado_zona["comparable"]:
        if resultado_zona["matched"]:
            detalles.append(
                f"Zona OK: '{nr_obj.zona}' ~ '{reclamo.zona}' (score={resultado_zona['score']})"
            )
        else:
            detalles.append(
                f"Zona NO coincide: '{nr_obj.zona}' vs '{reclamo.zona}' (score={resultado_zona['score']})"
            )
    else:
        detalles.append("Zona pendiente: falta dato en NR o Reclamo.")

    resultado_fecha = compare_dates(nr_obj.fecha_trabajo, reclamo.fecha_reclamo)
    if resultado_fecha["comparable"]:
        if resultado_fecha["matched"]:
            detalles.append(
                f"Fecha OK: {nr_obj.fecha_trabajo} >= {reclamo.fecha_reclamo}"
            )
        else:
            detalles.append(
                f"Fecha NO válida: {nr_obj.fecha_trabajo} < {reclamo.fecha_reclamo}"
            )
    else:
        detalles.append("Fecha pendiente: falta fecha_trabajo o fecha_reclamo.")

    hay_contradiccion = (
        (resultado_ciudad["comparable"] and resultado_ciudad["matched"] is False) or
        (resultado_zona["comparable"] and resultado_zona["matched"] is False) or
        (resultado_fecha["comparable"] and resultado_fecha["matched"] is False)
    )

    hay_pendientes = (
        not resultado_ciudad["comparable"] or
        not resultado_zona["comparable"] or
        not resultado_fecha["comparable"]
    )

    if hay_contradiccion:
        estado = ESTADO_RECHAZADO
    elif hay_pendientes:
        estado = ESTADO_EN_VERIFICACION
    else:
        estado = ESTADO_APROBADO

    return {
        "nr": nr_obj.numero_nr,
        "estado": estado,
        "detalles": detalles,
    }


def validar_plano_completo(plano: Plano):
    """
    Valida todos los NR válidos del plano contra sus reclamos asociados.

    Reglas:
    - Si no hay NR válidos -> EN_VERIFICACION
    - Si hay NR desconocidos -> como mínimo EN_VERIFICACION
    - Si algún NR da RECHAZADO -> plano RECHAZADO
    - Si todos los NR válidos pasan y no hay desconocidos -> APROBADO
    - Si faltan datos -> EN_VERIFICACION
    """
    nr_validos = parse_csv(plano.nr_validos)
    nr_desconocidos = parse_csv(plano.nr_desconocidos)

    detalles_nr = []
    resumen_general = []
    estado_final = ESTADO_APROBADO

    if not nr_validos:
        estado_final = ESTADO_EN_VERIFICACION
        resumen_general.append("No hay NR válidos para validar contra Reclamo.")
    else:
        nr_objs = NRMateriales.objects.select_related("reclamo").filter(
            numero_nr__in=nr_validos
        )

        for nr_obj in nr_objs:
            resultado = validar_nr_contra_reclamo(nr_obj)

            detalles_nr.append(f"NR {resultado['nr']} -> {resultado['estado']}")
            for detalle in resultado["detalles"]:
                detalles_nr.append(f" - {detalle}")

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

    motivo_lineas = []
    motivo_lineas.append(f"RESULTADO FINAL DEL PLANO: {estado_final}")
    motivo_lineas.append("")

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
    plano.motivo = "\n".join(motivo_lineas)
    plano.save()

    return {
        "estado": estado_final,
        "motivos": motivo_lineas,
    }


def procesar_plano_completo(plano: Plano, usuario: str = "sistema"):
    """
    Ejecuta todo el flujo del plano:
    1. OCR
    2. extracción de NR y fecha
    3. validación contra BD
    4. validación completa contra Reclamo
    """
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
            usuario=usuario,
            accion=f"OCR OK -> nr_detectados={plano.nr_detectados} fecha_plano={plano.fecha_plano}"
        )

        res_bd = validar_plano_contra_bd(plano)
        Auditoria.objects.create(
            plano=plano,
            usuario=usuario,
            accion=f"VALIDACIÓN NR -> estado={res_bd['estado']} validos={res_bd['validos']} desconocidos={res_bd['desconocidos']}"
        )

        res_final = validar_plano_completo(plano)
        Auditoria.objects.create(
            plano=plano,
            usuario=usuario,
            accion=f"VALIDACIÓN COMPLETA -> estado={res_final['estado']} motivos={res_final['motivos']}"
        )

        return {
            "ok": True,
            "estado": plano.estado,
            "plano_id": plano.id,
            "motivo": plano.motivo,
        }

    except Exception as e:
        plano.estado = "EN_ESPERA"
        plano.save()

        Auditoria.objects.create(
            plano=plano,
            usuario=usuario,
            accion=f"PROCESO COMPLETO ERROR: {e}"
        )

        return {
            "ok": False,
            "estado": plano.estado,
            "error": str(e),
        }