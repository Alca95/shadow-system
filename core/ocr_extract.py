from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Optional, List, Dict, Any

import cv2
import pytesseract
from dateutil import parser as dateparser
from rapidfuzz import fuzz


RE_FECHA_NUMERICA = re.compile(
    r"(?<!\d)(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})(?!\d)"
)

RE_FECHA_TEXTUAL = re.compile(
    r"(?i)\b(\d{1,2}\s*(?:de\s+)?"
    r"(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
    r"\s*(?:de\s+)?\d{2,4})\b"
)

RE_FECHA_LABEL_FLEX = re.compile(r"(?i)\bf[eé]ch[a4]\b")

RE_NR_NUMERO = re.compile(r"(?<!\d)(\d{6,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)")

RE_ETIQUETA_NR = re.compile(
    r"(?i)\b(?:r\s*\.?\s*n|rn|r\s+n|nr|n[°ºo*]?\s*r|n[°ºo*])\b"
)

RE_ETIQUETA_NR_FLEX = re.compile(
    r"(?i)(?:\b(?:nr|rn)\b|n\s*[°ºo*]?\s*r|r\s*\.?\s*n|n[°ºo*])"
)

RE_LINEA_RUIDO = re.compile(
    r"(?i)\b(x|y)\s*[:=]|\bplano\b|\boem\b|\blpn\b|\bpartida\b|\bart[ií]culo\b"
)

RE_ZONA_FLEX = re.compile(
    r"(?i)\bz[o0]n[a4]\b\s*[:\-]?\s*(.+)"
)

RE_MATERIAL_LINEA = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s+([A-ZÁÉÍÓÚÑ0-9][A-ZÁÉÍÓÚÑ0-9\s./,\-]+)$",
    re.IGNORECASE,
)

RE_MATERIAL_SEGMENTO = re.compile(
    r"(?i)(\d+(?:[.,]\d+)?)\s+([A-ZÁÉÍÓÚÑ][A-ZÁÉÍÓÚÑ0-9\s./,\-]{2,}?)(?=\s+\d+(?:[.,]\d+)?\s+[A-ZÁÉÍÓÚÑ]|\Z)"
)

RE_LINEA_RUIDO_DETALLE = re.compile(
    r"(?i)\b(x|y)\s*[:=]|\boem\b|\blpn\b|\bitem\b|\bmantenimientos\b|"
    r"\brep\.?\s*t[eé]cnico\b|\bfiscal\b|\binfluencia\b|\bdist\.\b|"
    r"\bsecc\.\b|\bpy\d+\b|\bbca\b|\bbcc\b|\bblc\b"
)

RE_SEPARADORES_RAROS = re.compile(r"[–—_=]+")
RE_MULTI_SPACE = re.compile(r"\s+")

MATERIALES_CATALOGO = [
    ("IFE", "unidad"),
    ("IGNITOR", "unidad"),
    ("CAPACITOR", "unidad"),
    ("LIMPIEZA DE TULIPA", None),
    ("MANTENIMIENTO SOLO", None),
    ("PORTA IFE", "unidad"),
    ("PORTALAMPARA", "unidad"),
    ("ZOCALO P/ IFE", "unidad"),
    ("ZOCALO PARA IFE", "unidad"),
    ("EQUIPO COMPLETO LED", "unidad"),
    ("LAMPARA DE 100W-NA", "unidad"),
    ("LAMPARA DE 150W-NA", "unidad"),
    ("LAMPARA DE 250W-NA", "unidad"),
    ("LAMPARA DE 400W-NA", "unidad"),
    ("REACT. INT. DE 100W-NA", "unidad"),
    ("REACT. INT. DE 150W-NA", "unidad"),
    ("REACT. INT. DE 250W-NA", "unidad"),
    ("REACT. INT. DE 400W", "unidad"),
    ("REACT. EXT. DE 150W-NA", "unidad"),
    ("REACT. EXT. DE 250W-NA", "unidad"),
    ("FUSIBLE MT DE 5A", "unidad"),
    ("FUSIBLE MT DE 25A", "unidad"),
    ("FUSIBLE NH 125A", "unidad"),
    ("CABLE 2X2,5MM2", "metro"),
]
MATERIALES_CATALOGO_NORM = []


def strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_text_soft(text: str) -> str:
    if not text:
        return ""
    text = str(text).strip().lower()
    text = strip_accents(text)
    text = text.replace("²", "2")
    text = re.sub(r"[^a-z0-9\s./:\-,]", " ", text)
    text = RE_MULTI_SPACE.sub(" ", text).strip()
    return text


def normalize_material_text(text: str) -> str:
    if not text:
        return ""

    text = normalize_text_soft(text)

    replacements = [
        ("lamparade", "lampara de "),
        ("lamparadz", "lampara de "),
        ("lamparadez", "lampara de "),
        ("lampara de250w", "lampara de 250w"),
        ("lampara de150w", "lampara de 150w"),
        ("lampara de100w", "lampara de 100w"),
        ("lampara de400w", "lampara de 400w"),
        ("react int de", "react. int. de "),
        ("react ext de", "react. ext. de "),
        ("porta lampara", "portalampara"),
        ("zocalo p ife", "zocalo p/ ife"),
        ("zocalo p/ife", "zocalo p/ ife"),
        ("zocalo para ife", "zocalo para ife"),
        ("umpeiza de tulipa", "limpieza de tulipa"),
        ("umpieza de tulipa", "limpieza de tulipa"),
        ("impeza de tulipa", "limpieza de tulipa"),
        ("mts", ""),
        (" mt", ""),
        ("mm²", "mm2"),
        ("mm2", "mm2"),
    ]

    for old, new in replacements:
        text = text.replace(old, new)

    text = re.sub(r"\s*-\s*", "-", text)
    return RE_MULTI_SPACE.sub(" ", text).strip(" -.:,")


for nombre, unidad in MATERIALES_CATALOGO:
    MATERIALES_CATALOGO_NORM.append(
        {
            "descripcion": nombre,
            "descripcion_norm": normalize_material_text(nombre),
            "unidad_medida": unidad,
        }
    )


def normalize_nr(a: str, b: str) -> str:
    a = re.sub(r"\s+", "", a)
    b = re.sub(r"\s+", "", b)
    if len(b) == 4:
        b = b[-2:]
    return f"{a}-{b}"


def is_valid_nr_candidate(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if not a.isdigit() or not b.isdigit():
        return False
    if len(a) < 6 or len(a) > 8:
        return False
    if len(b) not in (2, 4):
        return False
    if set(a) == {"0"} or set(b) == {"0"}:
        return False
    return True


def _to_gray(img_bgr):
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)


def preprocess_image(img_bgr):
    """
    Preprocesado más suave para no romper texto fino.
    Se mantiene como imagen principal del flujo.
    """
    gray = _to_gray(img_bgr)

    # Contraste suave, sin blur agresivo
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    return gray


def build_ocr_variants(img_bgr):
    """
    Devuelve variantes de la imagen desde una toma más natural
    hasta umbrales más fuertes. La idea es no depender de una sola
    transformación visual.
    """
    gray = _to_gray(img_bgr)

    variants = []

    # 1) Natural / gris
    variants.append(("natural_gray", gray))

    # 2) Contraste suave
    clahe_soft = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(gray)
    variants.append(("clahe_soft", clahe_soft))

    # 3) Otsu suave
    _, otsu = cv2.threshold(clahe_soft, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(("otsu", otsu))

    # 4) Adaptativo, pero sin blur previo
    adaptive = cv2.adaptiveThreshold(
        clahe_soft,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        8,
    )
    variants.append(("adaptive_soft", adaptive))

    return variants


def choose_best_ocr_text(texts: List[str]) -> str:
    """
    Elige la salida OCR más útil priorizando:
    - más NR detectados
    - más materiales/keywords
    - más fechas
    """
    best_text = ""
    best_score = -1

    for text in texts:
        if not text:
            continue

        score = 0
        text_norm = normalize_text_soft(text)

        # NRs
        score += len(extract_nrs(text)) * 50

        # Fechas
        fechas = RE_FECHA_NUMERICA.findall(text)
        score += len(fechas) * 10

        # Keywords útiles
        for token in [
            "zona",
            "fecha",
            "ife",
            "ignitor",
            "capacitor",
            "lampara",
            "react",
            "limpieza",
            "zocalo",
            "porta",
            "equipo",
            "fusible",
            "cable",
        ]:
            if token in text_norm:
                score += 4

        if score > best_score:
            best_score = score
            best_text = text

    return best_text


def ocr_text_from_file(file_path: str) -> str:
    img = cv2.imread(file_path)
    if img is None:
        raise ValueError(f"No se pudo leer imagen: {file_path}")

    variants = build_ocr_variants(img)
    outputs = []

    for _, variant in variants:
        for psm in ("11", "6"):
            try:
                text = pytesseract.image_to_string(
                    variant,
                    lang="spa",
                    config=f"--oem 3 --psm {psm}",
                )
                if text and text.strip():
                    outputs.append(text)
            except Exception:
                continue

    if not outputs:
        # fallback
        thr = preprocess_image(img)
        return pytesseract.image_to_string(thr, lang="spa", config="--oem 3 --psm 11")

    return choose_best_ocr_text(outputs)


def deduplicate_keep_order(values: List[str]) -> List[str]:

    uniq = []
    seen = set()
    for value in values:
        if value not in seen:
            uniq.append(value)
            seen.add(value)
    return uniq


def clean_ocr_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\r", "\n")
    text = RE_SEPARADORES_RAROS.sub("-", text)
    text = text.replace("|", " ")
    text = text.replace("\\", "/")

    text = re.sub(r"(?i)\bN\s*[°ºo*]?\s*R\b", "NR", text)
    text = re.sub(r"(?i)\bR\s*\.?\s*N\b", "NR", text)
    text = re.sub(r"(?i)\bR\s+N\b", "NR", text)
    text = re.sub(r"(?i)\bR\.?\s*N\.?\b", "NR", text)

    text = re.sub(r"(?i)\bz0na\b", "Zona", text)
    text = re.sub(r"(?i)\bfech4\b", "Fecha", text)
    text = re.sub(r"(?i)\bfecna\b", "Fecha", text)
    text = re.sub(r"(?i)\bfecba\b", "Fecha", text)

    cleaned_lines = []
    for line in text.splitlines():
        line = RE_MULTI_SPACE.sub(" ", line).strip()
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def normalize_line_for_search(line: str) -> str:
    if not line:
        return ""
    line = RE_SEPARADORES_RAROS.sub("-", line)
    line = RE_MULTI_SPACE.sub(" ", line).strip()
    return line


def extract_candidates_from_line(line: str) -> List[str]:
    encontrados = []
    line = normalize_line_for_search(line)
    for match in RE_NR_NUMERO.finditer(line):
        a = match.group(1)
        b = match.group(2)
        if is_valid_nr_candidate(a, b):
            encontrados.append(normalize_nr(a, b))
    return encontrados


def line_has_nr_label(line: str) -> bool:
    if not line:
        return False
    line = normalize_line_for_search(line)
    return bool(RE_ETIQUETA_NR.search(line) or RE_ETIQUETA_NR_FLEX.search(line))


def should_skip_line(line: str) -> bool:
    if not line:
        return True
    return bool(RE_LINEA_RUIDO.search(line))


def extract_nrs(text: str) -> List[str]:
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
    encontrados = []

    for i, line in enumerate(lines):
        if should_skip_line(line):
            continue
        if line_has_nr_label(line):
            encontrados.extend(extract_candidates_from_line(line))
            if i + 1 < len(lines) and not should_skip_line(lines[i + 1]):
                encontrados.extend(extract_candidates_from_line(lines[i + 1]))
            if i + 2 < len(lines) and not should_skip_line(lines[i + 2]):
                encontrados.extend(extract_candidates_from_line(lines[i + 2]))

    for line in lines:
        if not should_skip_line(line):
            encontrados.extend(extract_candidates_from_line(line))

    return deduplicate_keep_order(encontrados)


def parse_date_value(raw: str) -> Optional[date]:
    if not raw:
        return None
    raw = str(raw).strip().replace(".", "/").replace("-", "/")
    try:
        dt = dateparser.parse(raw, dayfirst=True)
        return dt.date() if dt else None
    except Exception:
        return None


def find_date_in_line(line: str) -> Optional[date]:
    if not line:
        return None
    line_norm = normalize_line_for_search(line)

    for regex in (RE_FECHA_NUMERICA, RE_FECHA_TEXTUAL):
        m = regex.search(line_norm)
        if m:
            fecha = parse_date_value(m.group(1))
            if fecha:
                return fecha

    if RE_FECHA_LABEL_FLEX.search(line_norm):
        after = RE_FECHA_LABEL_FLEX.split(line_norm, maxsplit=1)
        if len(after) > 1:
            candidate = after[1].strip(" :-")
            for regex in (RE_FECHA_NUMERICA, RE_FECHA_TEXTUAL):
                m = regex.search(candidate)
                if m:
                    fecha = parse_date_value(m.group(1))
                    if fecha:
                        return fecha

    return None


def extract_fecha_plano(text: str) -> Optional[date]:
    if not text:
        return None
    cleaned_text = clean_ocr_text(text)
    for line in cleaned_text.splitlines():
        fecha = find_date_in_line(line)
        if fecha:
            return fecha
    return None


def is_probable_noise_detail_line(line: str) -> bool:
    if not line:
        return True
    line_norm = normalize_line_for_search(line)
    if not line_norm:
        return True
    if RE_LINEA_RUIDO_DETALLE.search(line_norm):
        return True
    if line_norm.upper().startswith("X=") or line_norm.upper().startswith("Y="):
        return True
    return False


def find_nr_line_indexes(lines: List[str]) -> List[Dict[str, Any]]:
    encontrados = []
    for idx, line in enumerate(lines):
        candidatos = extract_candidates_from_line(line)
        if not candidatos:
            continue
        if line_has_nr_label(line) or len(candidatos) == 1:
            for nr in candidatos:
                encontrados.append({"index": idx, "nr": nr})

    vistos = set()
    salida = []
    for item in encontrados:
        key = (item["index"], item["nr"])
        if key not in vistos:
            salida.append(item)
            vistos.add(key)
    return salida


def sanitize_zone_text(value: str) -> str:
    if not value:
        return ""
    value = normalize_line_for_search(value)
    value = re.sub(r"(?i)\bfecha\b.*$", "", value).strip()
    value = re.sub(r"(?i)^zona\s*", "", value).strip()
    return value.strip(" -.:")


def smart_normalize_location(value: str) -> str:
    if not value:
        return ""
    value = normalize_text_soft(value)
    value = re.sub(r"^zona", "", value).strip()

    correcciones = {
        "conto": "centro",
        "contro": "centro",
        "contio": "centro",
        "contto": "centro",
        "contiro": "centro",
        "zonacontro": "centro",
        "zonacentro": "centro",
        "centio": "centro",
        "ceniro": "centro",
        "sari isidro": "san isidro",
        "san isiio": "san isidro",
        "san isiro": "san isidro",
        "san isido": "san isidro",
        "tuyu puco": "tuyu pucu",
    }
    return correcciones.get(value, value)


def is_short_location_candidate(line: str) -> bool:
    if not line:
        return False
    line_norm = normalize_text_soft(line)
    if not line_norm:
        return False
    if any(ch.isdigit() for ch in line_norm):
        return False
    if len(line_norm) > 25:
        return False
    if is_probable_noise_detail_line(line_norm):
        return False
    return True


def extract_zona_from_lines(lines: List[str]) -> Optional[str]:
    for i, line in enumerate(lines):
        if not line:
            continue
        line_norm = normalize_line_for_search(line)
        m = RE_ZONA_FLEX.search(line_norm)
        if m:
            zona = sanitize_zone_text(m.group(1))
            zona = smart_normalize_location(zona)
            if zona:
                return zona.title()

        if re.search(r"(?i)\bz[o0]n[a4]\b", line_norm):
            if i + 1 < len(lines):
                candidate = sanitize_zone_text(lines[i + 1])
                candidate = smart_normalize_location(candidate)
                if candidate and not is_probable_noise_detail_line(candidate):
                    return candidate.title()
    return None


def normalize_material_description(text: str) -> str:
    text = RE_MULTI_SPACE.sub(" ", text).strip(" -.:")
    text = re.sub(r"\s*-\s*", "-", text)
    text = text.replace("²", "2")
    text = re.sub(r"(?i)\blamparade\b", "LAMPARA DE ", text)
    text = re.sub(r"(?i)\blamparade", "LAMPARA DE ", text)
    text = re.sub(r"(?i)\blamparadz\b", "LAMPARA DE ", text)
    text = re.sub(r"(?i)\blamparadez\b", "LAMPARA DE ", text)
    text = re.sub(r"(?i)\blamparadez", "LAMPARA DE ", text)
    text = re.sub(r"(?i)\bumpieza de tulipa\b", "LIMPIEZA DE TULIPA", text)
    text = re.sub(r"(?i)\bumpieza de tulipa\b", "LIMPIEZA DE TULIPA", text)
    text = re.sub(r"(?i)\bimpeza de tulipa\b", "LIMPIEZA DE TULIPA", text)
    text = re.sub(r"(?i)\bporta ife\b", "PORTA IFE", text)
    text = re.sub(r"(?i)\bportalampara\b", "PORTALAMPARA", text)
    text = re.sub(r"(?i)\breact int de\b", "REACT. INT. DE ", text)
    text = re.sub(r"(?i)\breact ext de\b", "REACT. EXT. DE ", text)
    text = re.sub(r"(?i)\bzocalo p/?\s*ife\b", "ZOCALO P/ IFE", text)
    text = re.sub(r"(?i)\bzocalo para ife\b", "ZOCALO PARA IFE", text)
    text = re.sub(r"(?i)\bequipo completo led\b", "EQUIPO COMPLETO LED", text)
    return text.strip()


def normalizar_material_catalogo(texto: str) -> Dict[str, Any]:
    texto_norm = normalize_material_text(texto)
    mejor_nombre = None
    mejor_unidad = None
    mejor_score = -1

    for item in MATERIALES_CATALOGO_NORM:
        score = fuzz.token_sort_ratio(texto_norm, item["descripcion_norm"])
        if score > mejor_score:
            mejor_score = score
            mejor_nombre = item["descripcion"]
            mejor_unidad = item["unidad_medida"]

    if mejor_nombre and mejor_score >= 70:
        return {
            "descripcion": mejor_nombre,
            "unidad_medida": mejor_unidad,
            "catalogo_match": True,
            "score_catalogo": mejor_score,
        }

    unidad_fallback = "metro" if "cable" in texto_norm else "unidad"
    if "mantenimiento solo" in texto_norm or "limpieza de tulipa" in texto_norm:
        unidad_fallback = None

    return {
        "descripcion": texto.upper(),
        "unidad_medida": unidad_fallback,
        "catalogo_match": False,
        "score_catalogo": mejor_score if mejor_score >= 0 else 0,
    }


def format_quantity_for_display(cantidad: Optional[float]) -> str:
    if cantidad is None:
        return "-"
    if float(cantidad).is_integer():
        return str(int(cantidad))
    return str(cantidad).replace(".", ",")


def is_material_description_candidate(text: str) -> bool:
    if not text:
        return False

    text_norm = normalize_material_text(text)
    invalid_tokens = [
        "fecha",
        "zona",
        "carayao",
        "yegros",
        "compasa",
        "cancha",
        "graldiaz",
        "gral diaz",
        "san isidro",
        "centro",
    ]
    if any(tok in text_norm for tok in invalid_tokens):
        return False

    material_tokens = [
        "ife",
        "ignitor",
        "capacitor",
        "mantenimiento solo",
        "limpieza de tulipa",
        "lampara",
        "react",
        "porta ife",
        "portalampara",
        "zocalo",
        "equipo completo led",
        "fusible",
        "cable",
    ]
    return any(tok in text_norm for tok in material_tokens)


def build_material_item(cantidad_raw: str, descripcion: str, texto_original: str) -> Optional[Dict[str, Any]]:
    try:
        cantidad = float(cantidad_raw.replace(",", "."))
    except Exception:
        cantidad = None

    descripcion = normalize_material_description(descripcion)
    if not descripcion:
        return None

    material_final = normalizar_material_catalogo(descripcion)
    return {
        "cantidad": cantidad,
        "cantidad_mostrar": format_quantity_for_display(cantidad),
        "descripcion": material_final["descripcion"],
        "unidad_medida": material_final["unidad_medida"],
        "catalogo_match": material_final["catalogo_match"],
        "score_catalogo": material_final["score_catalogo"],
        "texto_original": texto_original.strip(),
    }


def extract_materiales_from_lines(lines: List[str]) -> List[Dict[str, Any]]:
    materiales = []
    i = 0

    while i < len(lines):
        line = lines[i]

        if not line or is_probable_noise_detail_line(line):
            i += 1
            continue

        line_norm = normalize_line_for_search(line)

        if line_has_nr_label(line_norm) or RE_ZONA_FLEX.search(line_norm) or find_date_in_line(line_norm):
            i += 1
            continue

        # Caso 1: múltiples materiales en una sola línea OCR
        segmentos = list(RE_MATERIAL_SEGMENTO.finditer(line_norm))
        if segmentos:
            encontrados_linea = 0
            for seg in segmentos:
                item = build_material_item(seg.group(1), seg.group(2), seg.group(0))
                if item and is_material_description_candidate(item["descripcion"]):
                    materiales.append(item)
                    encontrados_linea += 1
            if encontrados_linea > 0:
                i += 1
                continue

        # Caso 2: material normal en una sola línea
        m = RE_MATERIAL_LINEA.match(line_norm)
        if m:
            item = build_material_item(m.group(1), m.group(2), line)
            if item and is_material_description_candidate(item["descripcion"]):
                materiales.append(item)
                i += 1
                continue

        # Caso 3: OCR parte el material en dos líneas (cantidad / descripción)
        if re.fullmatch(r"\d+(?:[.,]\d+)?", line_norm) and i + 1 < len(lines):
            next_line = lines[i + 1]
            next_norm = normalize_line_for_search(next_line)

            if (
                next_line
                and not is_probable_noise_detail_line(next_line)
                and not line_has_nr_label(next_norm)
                and not RE_ZONA_FLEX.search(next_norm)
                and not find_date_in_line(next_norm)
            ):
                # intento directo con la siguiente línea
                item = build_material_item(line_norm, next_norm, f"{line} {next_line}")
                if item and is_material_description_candidate(item["descripcion"]):
                    materiales.append(item)
                    i += 2
                    continue

                # si la siguiente línea es una letra o fragmento corto, intento unir hasta 2 líneas más
                combined = next_norm
                consumed = 2
                j = i + 2
                while j < len(lines) and consumed <= 3:
                    extra = normalize_line_for_search(lines[j])
                    if (
                        not extra
                        or is_probable_noise_detail_line(extra)
                        or line_has_nr_label(extra)
                        or RE_ZONA_FLEX.search(extra)
                        or find_date_in_line(extra)
                    ):
                        break
                    combined = f"{combined} {extra}".strip()
                    item = build_material_item(line_norm, combined, f"{line} {combined}")
                    if item and is_material_description_candidate(item["descripcion"]):
                        materiales.append(item)
                        i = j + 1
                        break
                    consumed += 1
                    j += 1
                else:
                    i += 1
                    continue

                # si salió por break exitoso del while, ya actualizamos i
                if i > j - 1:
                    continue

        i += 1

    salida = []
    vistos = set()
    for item in materiales:
        key = (
            item["cantidad_mostrar"],
            normalize_material_text(item["descripcion"]),
        )
        if key not in vistos:
            salida.append(item)
            vistos.add(key)

    return salida


def extract_fecha_from_lines(lines: List[str]) -> Optional[date]:
    for i, line in enumerate(lines):
        if not line:
            continue
        fecha = find_date_in_line(line)
        if fecha:
            return fecha
        line_norm = normalize_line_for_search(line)
        if RE_FECHA_LABEL_FLEX.search(line_norm) and i + 1 < len(lines):
            fecha = find_date_in_line(lines[i + 1])
            if fecha:
                return fecha
    return None


def extract_ordered_details_from_block(block_lines: List[str]) -> Dict[str, Any]:
    zona = None
    fecha = None
    materiales = []

    detail_lines = block_lines[1:] if len(block_lines) > 1 else []
    zona_idx = None
    fecha_idx = None

    for idx, line in enumerate(detail_lines[:4]):
        if not line:
            continue
        if RE_ZONA_FLEX.search(line):
            zona = extract_zona_from_lines([line])
            if zona:
                zona_idx = idx
                break

    if not zona:
        for idx, line in enumerate(detail_lines[:4]):
            if is_short_location_candidate(line):
                zona = smart_normalize_location(line).title()
                zona_idx = idx
                break

    for idx, line in enumerate(detail_lines[:5]):
        if not line:
            continue
        fecha = find_date_in_line(line)
        if fecha:
            fecha_idx = idx
            break

    start_material_idx = 0
    if fecha_idx is not None:
        start_material_idx = fecha_idx + 1
    elif zona_idx is not None:
        start_material_idx = zona_idx + 1

    no_material_consecutivos = 0
    material_chunk = detail_lines[start_material_idx:]

    if material_chunk:
        materiales = extract_materiales_from_lines(material_chunk)

    if materiales:
        no_material_consecutivos = 0
    else:
        for line in material_chunk:
            if not line:
                if materiales:
                    break
                continue
            encontrados_linea = extract_materiales_from_lines([line])
            if encontrados_linea:
                materiales.extend(encontrados_linea)
                no_material_consecutivos = 0
            elif materiales:
                no_material_consecutivos += 1
                if no_material_consecutivos >= 2:
                    break

    if not zona:
        zona = extract_zona_from_lines(block_lines)
    if not fecha:
        fecha = extract_fecha_from_lines(block_lines)
    if not materiales:
        materiales = extract_materiales_from_lines(block_lines)

    return {"zona": zona, "fecha": fecha, "materiales": materiales}


def extract_nr_sections(text: str, max_lines_per_section: int = 18) -> List[Dict[str, Any]]:
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
    nr_positions = find_nr_line_indexes(lines)

    if not nr_positions:
        return []

    secciones = []
    for i, item in enumerate(nr_positions):
        start_idx = item["index"]
        nr = item["nr"]

        if i + 1 < len(nr_positions):
            next_idx = nr_positions[i + 1]["index"]
            end_idx = min(next_idx, start_idx + max_lines_per_section + 1)
        else:
            end_idx = min(len(lines), start_idx + max_lines_per_section + 1)

        block_lines = lines[start_idx:end_idx]
        ordered = extract_ordered_details_from_block(block_lines)

        secciones.append(
            {
                "nr": nr,
                "linea_inicio": start_idx,
                "lineas": block_lines,
                "zona": ordered["zona"],
                "fecha": ordered["fecha"],
                "materiales": ordered["materiales"],
            }
        )

    resultado_final = []
    vistos = set()
    for item in secciones:
        nr = item["nr"]
        if nr in vistos:
            continue
        resultado_final.append(item)
        vistos.add(nr)

    return resultado_final


def extract_detalles_por_nr(text: str) -> Dict[str, Dict[str, Any]]:
    detalles = {}
    for item in extract_nr_sections(text):
        detalles[item["nr"]] = {
            "zona": item.get("zona"),
            "fecha": item.get("fecha"),
            "materiales": item.get("materiales", []),
            "lineas": item.get("lineas", []),
        }
    return detalles


# =========================================================
# REASIGNACIÓN INTELIGENTE DE MATERIALES EN OCR DESORDENADO
# =========================================================

_OLD_EXTRACT_NR_SECTIONS = extract_nr_sections


def _merge_material_lists(base: List[Dict[str, Any]], extra: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged = list(base or [])
    seen = {
        (
            item.get("cantidad_mostrar"),
            normalize_material_text(item.get("descripcion", "")),
        )
        for item in merged
    }

    for item in extra or []:
        key = (
            item.get("cantidad_mostrar"),
            normalize_material_text(item.get("descripcion", "")),
        )
        if key not in seen:
            merged.append(item)
            seen.add(key)

    return merged


def _extract_nr_sections_inteligente(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]

    detalles: Dict[str, Dict[str, Any]] = {}
    orden_nrs: List[str] = []

    current_nr: Optional[str] = None
    prev_nr: Optional[str] = None
    i = 0

    while i < len(lines):
        line = lines[i]
        line_norm = normalize_line_for_search(line)

        candidatos = extract_candidates_from_line(line)
        if candidatos and (line_has_nr_label(line) or len(candidatos) == 1):
            nr = candidatos[0]

            if nr not in detalles:
                detalles[nr] = {
                    "nr": nr,
                    "linea_inicio": i,
                    "lineas": [],
                    "zona": None,
                    "fecha": None,
                    "materiales": [],
                }
                orden_nrs.append(nr)

            prev_nr = current_nr
            current_nr = nr
            detalles[current_nr]["lineas"].append(line)
            i += 1
            continue

        if current_nr:
            detalles[current_nr]["lineas"].append(line)

            if not detalles[current_nr]["zona"]:
                zona = extract_zona_from_lines([line])
                if zona:
                    detalles[current_nr]["zona"] = zona
                    i += 1
                    continue

            if not detalles[current_nr]["fecha"]:
                fecha = find_date_in_line(line)
                if fecha:
                    detalles[current_nr]["fecha"] = fecha
                    i += 1
                    continue

            # Ventana chica para detectar materiales en OCR desordenado
            window = lines[i:i+4]
            mats = extract_materiales_from_lines(window)

            if mats:
                target_nr = current_nr

                # Si acaba de aparecer un nuevo NR pero todavía no tiene zona/fecha,
                # los primeros materiales suelen pertenecer al NR anterior.
                current_has_context = bool(
                    detalles[current_nr].get("zona") or detalles[current_nr].get("fecha")
                )

                if not current_has_context and prev_nr:
                    target_nr = prev_nr

                detalles[target_nr]["materiales"] = _merge_material_lists(
                    detalles[target_nr]["materiales"],
                    mats,
                )

                # Avance heurístico
                if re.fullmatch(r"\d+(?:[.,]\d+)?", line_norm) and i + 1 < len(lines):
                    i += 2
                else:
                    i += 1
                continue

        i += 1

    return [detalles[nr] for nr in orden_nrs]


def extract_nr_sections(text: str, max_lines_per_section: int = 18) -> List[Dict[str, Any]]:
    base_sections = _OLD_EXTRACT_NR_SECTIONS(text, max_lines_per_section=max_lines_per_section)
    smart_sections = _extract_nr_sections_inteligente(text)

    merged_map: Dict[str, Dict[str, Any]] = {
        item["nr"]: {
            "nr": item["nr"],
            "linea_inicio": item.get("linea_inicio"),
            "lineas": list(item.get("lineas", [])),
            "zona": item.get("zona"),
            "fecha": item.get("fecha"),
            "materiales": list(item.get("materiales", [])),
        }
        for item in base_sections
    }

    ordered_nrs = [item["nr"] for item in base_sections]

    for item in smart_sections:
        nr = item["nr"]

        if nr not in merged_map:
            merged_map[nr] = {
                "nr": nr,
                "linea_inicio": item.get("linea_inicio"),
                "lineas": list(item.get("lineas", [])),
                "zona": item.get("zona"),
                "fecha": item.get("fecha"),
                "materiales": list(item.get("materiales", [])),
            }
            ordered_nrs.append(nr)
            continue

        if not merged_map[nr].get("zona") and item.get("zona"):
            merged_map[nr]["zona"] = item.get("zona")

        if not merged_map[nr].get("fecha") and item.get("fecha"):
            merged_map[nr]["fecha"] = item.get("fecha")

        merged_map[nr]["materiales"] = _merge_material_lists(
            merged_map[nr].get("materiales", []),
            item.get("materiales", []),
        )

        extra_lines = item.get("lineas", [])
        if extra_lines:
            merged_map[nr]["lineas"].extend(extra_lines)

    return [merged_map[nr] for nr in ordered_nrs if nr in merged_map]


def extract_detalles_por_nr(text: str) -> Dict[str, Dict[str, Any]]:
    detalles = {}
    for item in extract_nr_sections(text):
        detalles[item["nr"]] = {
            "zona": item.get("zona"),
            "fecha": item.get("fecha"),
            "materiales": item.get("materiales", []),
            "lineas": item.get("lineas", []),
        }
    return detalles


# =========================================================
# AJUSTE FINAL: CAMBIO DE NR SOLO CUANDO EL NUEVO YA TIENE CONTEXTO
# =========================================================

def _extract_nr_sections_inteligente(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]

    detalles: Dict[str, Dict[str, Any]] = {}
    orden_nrs: List[str] = []

    current_nr: Optional[str] = None
    prev_nr: Optional[str] = None
    i = 0

    while i < len(lines):
        line = lines[i]
        line_norm = normalize_line_for_search(line)

        candidatos = extract_candidates_from_line(line)
        if candidatos and (line_has_nr_label(line) or len(candidatos) == 1):
            nr = candidatos[0]

            if nr not in detalles:
                detalles[nr] = {
                    "nr": nr,
                    "linea_inicio": i,
                    "lineas": [],
                    "zona": None,
                    "fecha": None,
                    "materiales": [],
                }
                orden_nrs.append(nr)

            prev_nr = current_nr
            current_nr = nr
            detalles[current_nr]["lineas"].append(line)
            i += 1
            continue

        if current_nr:
            detalles[current_nr]["lineas"].append(line)

            if not detalles[current_nr]["zona"]:
                zona = extract_zona_from_lines([line])
                if zona:
                    detalles[current_nr]["zona"] = zona
                    i += 1
                    continue

            if not detalles[current_nr]["fecha"]:
                fecha = find_date_in_line(line)
                if fecha:
                    detalles[current_nr]["fecha"] = fecha
                    i += 1
                    continue

            # Detectar materiales usando una ventana corta.
            window = lines[i:i+4]
            mats = extract_materiales_from_lines(window)

            if mats:
                current_has_full_context = bool(
                    detalles[current_nr].get("zona") and detalles[current_nr].get("fecha")
                )

                # Regla clave:
                # Mientras el NR nuevo todavía NO tenga zona y fecha,
                # los materiales cercanos siguen perteneciendo al NR anterior.
                target_nr = current_nr
                if not current_has_full_context and prev_nr:
                    target_nr = prev_nr

                detalles[target_nr]["materiales"] = _merge_material_lists(
                    detalles[target_nr]["materiales"],
                    mats,
                )

                # Avance heurístico.
                if re.fullmatch(r"\d+(?:[.,]\d+)?", line_norm) and i + 1 < len(lines):
                    i += 2
                else:
                    i += 1
                continue

        i += 1

    return [detalles[nr] for nr in orden_nrs]


def extract_nr_sections(text: str, max_lines_per_section: int = 18) -> List[Dict[str, Any]]:
    base_sections = _OLD_EXTRACT_NR_SECTIONS(text, max_lines_per_section=max_lines_per_section)
    smart_sections = _extract_nr_sections_inteligente(text)

    merged_map: Dict[str, Dict[str, Any]] = {
        item["nr"]: {
            "nr": item["nr"],
            "linea_inicio": item.get("linea_inicio"),
            "lineas": list(item.get("lineas", [])),
            "zona": item.get("zona"),
            "fecha": item.get("fecha"),
            "materiales": list(item.get("materiales", [])),
        }
        for item in base_sections
    }

    ordered_nrs = [item["nr"] for item in base_sections]

    for item in smart_sections:
        nr = item["nr"]

        if nr not in merged_map:
            merged_map[nr] = {
                "nr": nr,
                "linea_inicio": item.get("linea_inicio"),
                "lineas": list(item.get("lineas", [])),
                "zona": item.get("zona"),
                "fecha": item.get("fecha"),
                "materiales": list(item.get("materiales", [])),
            }
            ordered_nrs.append(nr)
            continue

        if not merged_map[nr].get("zona") and item.get("zona"):
            merged_map[nr]["zona"] = item.get("zona")

        if not merged_map[nr].get("fecha") and item.get("fecha"):
            merged_map[nr]["fecha"] = item.get("fecha")

        merged_map[nr]["materiales"] = _merge_material_lists(
            merged_map[nr].get("materiales", []),
            item.get("materiales", []),
        )

        extra_lines = item.get("lineas", [])
        if extra_lines:
            merged_map[nr]["lineas"].extend(extra_lines)

    return [merged_map[nr] for nr in ordered_nrs if nr in merged_map]


def extract_detalles_por_nr(text: str) -> Dict[str, Dict[str, Any]]:
    detalles = {}
    for item in extract_nr_sections(text):
        detalles[item["nr"]] = {
            "zona": item.get("zona"),
            "fecha": item.get("fecha"),
            "materiales": item.get("materiales", []),
            "lineas": item.get("lineas", []),
        }
    return detalles
