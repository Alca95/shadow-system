from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date
from typing import Optional, List, Dict, Any, Tuple

import cv2
import pytesseract
from dateutil import parser as dateparser
from rapidfuzz import fuzz


# =========================================================
# CACHE OCR ESTRUCTURADO
# =========================================================

_OCR_STRUCTURED_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}


# =========================================================
# REGEX BASE
# =========================================================

RE_MULTI_SPACE = re.compile(r"\s+")
RE_SEPARADORES_RAROS = re.compile(r"[–—_=]+")

RE_FECHA_NUMERICA = re.compile(
    r"(?<!\d)(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})(?!\d)"
)

RE_FECHA_TEXTUAL = re.compile(
    r"(?i)\b(\d{1,2}\s*(?:de\s+)?"
    r"(?:enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
    r"\s*(?:de\s+)?\d{2,4})\b"
)

RE_FECHA_LABEL = re.compile(r"(?i)\bfecha\b\s*[:\-]?\s*(.+)?")

RE_NR_LINE = re.compile(
    r"(?i)\b(?:r\s*\.?\s*n|rn|nr|r\s+n|r\s*-\s*n|n\s*[°ºo*]?\s*r|n\s*[°ºo*])\b"
)

RE_NR_NUMERO = re.compile(
    r"(?<!\d)(\d{6,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)"
)

RE_ZONA_LINE = re.compile(
    r"(?i)\bz[o0]n[a4]\b\s*[:\-]?\s*(.*)$"
)

RE_COORD_LINE = re.compile(r"(?i)^\s*[xy]\s*[:=]")
RE_RUIDO_GLOBAL = re.compile(
    r"(?i)\b(oem|lpn|item|mantenimientos|rep\.?\s*t[eé]cnico|fiscal|ingenier[ií]a|omega|influencia|dist\.?|secc\.?|py\d+|bcc|blc|bca)\b"
)

RE_CANTIDAD_SOLO = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")
RE_MATERIAL_LINEA = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s+(.+?)\s*$",
    re.IGNORECASE,
)


# =========================================================
# CATÁLOGO DE MATERIALES
# =========================================================

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

MATERIALES_CATALOGO_NORM: List[Dict[str, Any]] = []


# =========================================================
# NORMALIZADORES BASE
# =========================================================

def strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", text)
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


def normalize_line_for_search(line: str) -> str:
    if not line:
        return ""
    line = str(line).replace("\r", " ").replace("\n", " ")
    line = RE_SEPARADORES_RAROS.sub("-", line)
    line = line.replace("|", " ")
    line = line.replace("\\", "/")
    line = RE_MULTI_SPACE.sub(" ", line).strip()
    return line


def deduplicate_keep_order(values: List[str]) -> List[str]:
    uniq: List[str] = []
    seen = set()
    for value in values:
        if value not in seen:
            uniq.append(value)
            seen.add(value)
    return uniq


def _text_cache_key(text: str) -> str:
    cleaned = clean_ocr_text(text)
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()


# =========================================================
# OCR PREPROCESS
# =========================================================

def _to_gray(img_bgr):
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)


def preprocess_image(img_bgr):
    gray = _to_gray(img_bgr)
    clahe = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    return gray


def build_ocr_variants(img_bgr):
    gray = _to_gray(img_bgr)
    variants = []

    variants.append(("natural_gray", gray))

    clahe_soft = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(gray)
    variants.append(("clahe_soft", clahe_soft))

    _, otsu = cv2.threshold(
        clahe_soft, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    variants.append(("otsu", otsu))

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


# =========================================================
# LIMPIEZA OCR
# =========================================================

def clean_ocr_text(text: str) -> str:
    if not text:
        return ""

    text = str(text).replace("\r", "\n")
    text = RE_SEPARADORES_RAROS.sub("-", text)
    text = text.replace("|", " ")
    text = text.replace("\\", "/")

    replacements = [
        (r"(?i)\bR\s*\.?\s*N\b", "NR"),
        (r"(?i)\bR\s+N\b", "NR"),
        (r"(?i)\bR\s*-\s*N\b", "NR"),
        (r"(?i)\bR\.?\s*N\.?\b", "NR"),
        (r"(?i)\bN\s*[°ºo*]?\s*R\b", "NR"),
        (r"(?i)\bR\s*N\s*[:;]?", "NR: "),
        (r"(?i)r\s*-\s*n\s*\*?\s*[:;]?", "NR: "),
        (r"(?i)\bR\s*\-\s*N\s*\*?\s*[:;]?", "NR: "),
        (r"(?i)\bR\.?N\s*[:;]?", "NR: "),
        (r"(?i)\bR\s*\.?\s*N\s*[°ºo*]?\s*[:;]?", "NR: "),
        (r"(?i)\b2ona\b", "Zona"),
        (r"(?i)\b2ono\b", "Zona"),
        (r"(?i)\b2one\b", "Zona"),
        (r"(?i)\bz0na\b", "Zona"),
        (r"(?i)\bFecho\b", "Fecha"),
        (r"(?i)\bFechar\b", "Fecha"),
        (r"(?i)\bFecher\b", "Fecha"),
        (r"(?i)\bFecna\b", "Fecha"),
        (r"(?i)\bFecba\b", "Fecha"),
        (r"(?i)\b1GNITOR\b", "IGNITOR"),
        (r"(?i)\bUMPIEZADE\b", "LIMPIEZA DE"),
        (r"(?i)\bUMPIEZA DE\b", "LIMPIEZA DE"),
        (r"(?i)\bIMPIEZA DE\b", "LIMPIEZA DE"),
        (r"(?i)\bLAMPARADE\b", "LAMPARA DE"),
        (r"(?i)\bLAMPARADEZ\b", "LAMPARA DE"),
    ]

    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)

    cleaned_lines: List[str] = []
    for raw_line in text.splitlines():
        line = normalize_line_for_search(raw_line)
        if line:
            cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


# =========================================================
# NR
# =========================================================

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


def extract_candidates_from_line(line: str) -> List[str]:
    encontrados: List[str] = []
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
    return bool(RE_NR_LINE.search(normalize_line_for_search(line)))


def should_skip_line_for_nr(line: str) -> bool:
    if not line:
        return True
    line_norm = normalize_line_for_search(line)
    if RE_COORD_LINE.search(line_norm):
        return True
    if RE_RUIDO_GLOBAL.search(line_norm):
        return True
    return False


def extract_nrs(text: str) -> List[str]:
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
    encontrados: List[str] = []

    for i, line in enumerate(lines):
        if should_skip_line_for_nr(line):
            continue

        if line_has_nr_label(line):
            encontrados.extend(extract_candidates_from_line(line))
            if i + 1 < len(lines) and not should_skip_line_for_nr(lines[i + 1]):
                encontrados.extend(extract_candidates_from_line(lines[i + 1]))

    if not encontrados:
        for line in lines:
            if not should_skip_line_for_nr(line):
                encontrados.extend(extract_candidates_from_line(line))

    return deduplicate_keep_order(encontrados)


# =========================================================
# FECHAS
# =========================================================

def parse_date_value(raw: str) -> Optional[date]:
    if not raw:
        return None

    raw = str(raw).strip().replace(".", "/").replace("-", "/")
    try:
        dt = dateparser.parse(raw, dayfirst=True)
        return dt.date() if dt else None
    except Exception:
        return None


def fix_ocr_year(fecha: date) -> date:
    if not fecha:
        return fecha

    if fecha.year in {2028, 2078, 2088, 2020, 2038, 2058, 2068}:
        try:
            return date(2026, fecha.month, fecha.day)
        except Exception:
            return fecha

    if fecha.year > 2032:
        try:
            return date(2026, fecha.month, fecha.day)
        except Exception:
            return fecha

    return fecha


def find_date_in_line(line: str) -> Optional[date]:
    if not line:
        return None

    line_norm = normalize_line_for_search(line)

    m_num = RE_FECHA_NUMERICA.search(line_norm)
    if m_num:
        fecha = parse_date_value(m_num.group(1))
        if fecha:
            return fix_ocr_year(fecha)

    m_txt = RE_FECHA_TEXTUAL.search(line_norm)
    if m_txt:
        fecha = parse_date_value(m_txt.group(1))
        if fecha:
            return fix_ocr_year(fecha)

    m_label = RE_FECHA_LABEL.search(line_norm)
    if m_label and m_label.group(1):
        candidate = m_label.group(1).strip(" :-")
        m_num = RE_FECHA_NUMERICA.search(candidate)
        if m_num:
            fecha = parse_date_value(m_num.group(1))
            if fecha:
                return fix_ocr_year(fecha)

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


# =========================================================
# ZONA / UBICACIÓN
# =========================================================

def sanitize_zone_text(value: str) -> str:
    if not value:
        return ""

    value = normalize_line_for_search(value)
    value = re.sub(r"(?i)\bfecha\b.*$", "", value).strip()
    value = re.sub(r"(?i)^zona\s*", "", value).strip()
    value = value.strip(" -.:,")
    return value


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
        "centio": "centro",
        "ceniro": "centro",
        "cento": "centro",
        "cantro": "centro",
        "sani siro": "san isidro",
        "sanisiro": "san isidro",
        "san isiro": "san isidro",
        "san isiio": "san isidro",
        "san isido": "san isidro",
        "sanisidro": "san isidro",
        "tuyu puco": "tuyu pucu",
        "graldiaz": "gral diaz",
    }

    return correcciones.get(value, value)


def is_probable_noise_detail_line(line: str) -> bool:
    if not line:
        return True

    line_norm = normalize_line_for_search(line)
    if not line_norm:
        return True

    if RE_COORD_LINE.search(line_norm):
        return True

    if RE_RUIDO_GLOBAL.search(line_norm):
        return True

    return False


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

    invalid_tokens = {
        "carayao",
        "compasa",
        "cancha",
        "gral diaz",
        "graldiaz",
        "14 de mayo",
        "yegros",
    }
    if line_norm in invalid_tokens:
        return False

    if RE_RUIDO_GLOBAL.search(line_norm):
        return False

    return True


def extract_zona_from_lines(lines: List[str]) -> Optional[str]:
    for i, line in enumerate(lines):
        if not line:
            continue

        line_norm = normalize_line_for_search(line)
        m = RE_ZONA_LINE.search(line_norm)
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


# =========================================================
# MATERIALES
# =========================================================

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
        ("umpieza de tulipa", "limpieza de tulipa"),
        ("impeza de tulipa", "limpieza de tulipa"),
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


def normalize_material_description(text: str) -> str:
    text = RE_MULTI_SPACE.sub(" ", text).strip(" -.:")
    text = re.sub(r"\s*-\s*", "-", text)
    text = text.replace("²", "2")

    replacements = [
        (r"(?i)\blamparade\b", "LAMPARA DE "),
        (r"(?i)\blamparadz\b", "LAMPARA DE "),
        (r"(?i)\blamparadez\b", "LAMPARA DE "),
        (r"(?i)\bumpieza de tulipa\b", "LIMPIEZA DE TULIPA"),
        (r"(?i)\bimpeza de tulipa\b", "LIMPIEZA DE TULIPA"),
        (r"(?i)\bporta ife\b", "PORTA IFE"),
        (r"(?i)\bportalampara\b", "PORTALAMPARA"),
        (r"(?i)\breact int de\b", "REACT. INT. DE "),
        (r"(?i)\breact ext de\b", "REACT. EXT. DE "),
        (r"(?i)\bzocalo p/?\s*ife\b", "ZOCALO P/ IFE"),
        (r"(?i)\bzocalo para ife\b", "ZOCALO PARA IFE"),
        (r"(?i)\bequipo completo led\b", "EQUIPO COMPLETO LED"),
    ]

    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)

    return text.strip()


def _looks_like_ife_candidate(text: str) -> bool:
    """
    OCR típico para IFE:
    IFE, 1FE, FE, 1F, IFE., "FE, I F E
    """
    if not text:
        return False

    t = normalize_text_soft(text)
    t = t.replace('"', "").replace("'", "").replace(".", "").replace(" ", "")

    if t in {"ife", "1fe", "fe", "1f", "if", "lfe"}:
        return True

    if len(t) <= 3 and "f" in t and ("i" in t or "1" in t or t == "fe"):
        return True

    return False


def _is_plausible_material_quantity(cantidad_raw: str, descripcion: str) -> bool:
    """
    Evita cantidades basura como coordenadas:
    213233, 559877, 7213264, etc.
    """
    try:
        cantidad = float(str(cantidad_raw).replace(",", "."))
    except Exception:
        return False

    desc_norm = normalize_material_text(descripcion)

    # cantidades absurdas
    if cantidad > 100:
        return False

    # materiales usuales casi siempre 1-5
    if "cable" not in desc_norm and cantidad > 10:
        return False

    # cable admite algo más, pero nunca miles
    if "cable" in desc_norm and cantidad > 500:
        return False

    return True


def normalizar_material_catalogo(texto: str) -> Dict[str, Any]:
    if _looks_like_ife_candidate(texto):
        return {
            "descripcion": "IFE",
            "unidad_medida": "unidad",
            "catalogo_match": True,
            "score_catalogo": 100,
        }

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

    if mejor_nombre and mejor_score >= 78:
        return {
            "descripcion": mejor_nombre,
            "unidad_medida": mejor_unidad,
            "catalogo_match": True,
            "score_catalogo": mejor_score,
        }

    return {
        "descripcion": "",
        "unidad_medida": None,
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

    if _looks_like_ife_candidate(text):
        return True

    text_norm = normalize_material_text(text)

    invalid_tokens = [
        "fecha",
        "zona",
        "carayao",
        "yegros",
        "compasa",
        "cancha",
        "gral diaz",
        "san isidro",
        "centro",
    ]
    if any(tok == text_norm for tok in invalid_tokens):
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


def build_material_item(
    cantidad_raw: str,
    descripcion: str,
    texto_original: str,
) -> Optional[Dict[str, Any]]:
    descripcion = normalize_material_description(descripcion)
    if not descripcion:
        return None

    material_final = normalizar_material_catalogo(descripcion)
    if not material_final["catalogo_match"]:
        return None

    if not _is_plausible_material_quantity(cantidad_raw, material_final["descripcion"]):
        return None

    try:
        cantidad = float(str(cantidad_raw).replace(",", "."))
    except Exception:
        return None

    return {
        "cantidad": cantidad,
        "cantidad_mostrar": format_quantity_for_display(cantidad),
        "descripcion": material_final["descripcion"],
        "unidad_medida": material_final["unidad_medida"],
        "catalogo_match": material_final["catalogo_match"],
        "score_catalogo": material_final["score_catalogo"],
        "texto_original": texto_original.strip(),
    }


def _join_material_fragment(lines: List[str], start_idx: int) -> Tuple[str, int]:
    base = normalize_line_for_search(lines[start_idx])
    consumed = 1

    current = base
    for j in range(start_idx + 1, min(len(lines), start_idx + 3)):
        extra = normalize_line_for_search(lines[j])
        if not extra:
            break
        if is_probable_noise_detail_line(extra):
            break
        if line_has_nr_label(extra):
            break
        if RE_ZONA_LINE.search(extra):
            break
        if find_date_in_line(extra):
            break
        if RE_CANTIDAD_SOLO.match(extra):
            break

        current = f"{current} {extra}".strip()
        consumed += 1

        if is_material_description_candidate(current):
            return current, consumed

    return base, 1


def extract_materiales_from_lines(lines: List[str]) -> List[Dict[str, Any]]:
    materiales: List[Dict[str, Any]] = []
    i = 0

    while i < len(lines):
        line = normalize_line_for_search(lines[i])

        if not line or is_probable_noise_detail_line(line):
            i += 1
            continue

        if line_has_nr_label(line) or RE_ZONA_LINE.search(line) or find_date_in_line(line):
            i += 1
            continue

        # Caso especial: IFE comprimido por OCR (1FE, FE, 1F, etc.)
        if _looks_like_ife_candidate(line):
            item = build_material_item("1", "IFE", line)
            if item:
                materiales.append(item)
                i += 1
                continue

        # Caso especial: material sin cantidad explícita, asumimos 1
        line_material = line.lstrip('+*-• ').strip()
        if is_material_description_candidate(line_material):
            item = build_material_item("1", line_material, line)
            if item:
                materiales.append(item)
                i += 1
                continue

        m = RE_MATERIAL_LINEA.match(line)
        if m:
            item = build_material_item(m.group(1), m.group(2), line)
            if item:
                materiales.append(item)
                i += 1
                continue

        m_cant = RE_CANTIDAD_SOLO.match(line)
        if m_cant and i + 1 < len(lines):
            descripcion_unida, consumed = _join_material_fragment(lines, i + 1)
            item = build_material_item(
                m_cant.group(1),
                descripcion_unida,
                f"{line} {descripcion_unida}",
            )
            if item:
                materiales.append(item)
                i += 1 + consumed
                continue

        i += 1

    salida: List[Dict[str, Any]] = []
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


# =========================================================
# OCR ESPACIAL
# =========================================================

def _extract_lines_from_image_data(img_variant) -> List[Dict[str, Any]]:
    data = pytesseract.image_to_data(
        img_variant,
        lang="spa",
        config="--oem 3 --psm 11",
        output_type=pytesseract.Output.DICT,
    )

    grouped: Dict[Tuple[int, int, int], Dict[str, Any]] = {}

    n = len(data.get("text", []))
    for i in range(n):
        text = str(data["text"][i]).strip()
        conf_raw = str(data["conf"][i]).strip()

        if not text:
            continue

        try:
            conf = float(conf_raw)
        except Exception:
            conf = -1.0

        if conf < -1:
            continue

        key = (
            int(data["block_num"][i]),
            int(data["par_num"][i]),
            int(data["line_num"][i]),
        )

        left = int(data["left"][i])
        top = int(data["top"][i])
        width = int(data["width"][i])
        height = int(data["height"][i])
        right = left + width
        bottom = top + height

        if key not in grouped:
            grouped[key] = {
                "texts": [],
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "block_num": key[0],
                "par_num": key[1],
                "line_num": key[2],
            }
        else:
            grouped[key]["left"] = min(grouped[key]["left"], left)
            grouped[key]["top"] = min(grouped[key]["top"], top)
            grouped[key]["right"] = max(grouped[key]["right"], right)
            grouped[key]["bottom"] = max(grouped[key]["bottom"], bottom)

        grouped[key]["texts"].append((left, text))

    lines: List[Dict[str, Any]] = []
    for line in grouped.values():
        ordered = sorted(line["texts"], key=lambda x: x[0])
        text = " ".join(t for _, t in ordered)
        line_text = clean_ocr_text(text)
        if not line_text:
            continue

        lines.append(
            {
                "text": line_text,
                "left": line["left"],
                "top": line["top"],
                "right": line["right"],
                "bottom": line["bottom"],
                "block_num": line["block_num"],
                "par_num": line["par_num"],
                "line_num": line["line_num"],
            }
        )

    lines.sort(key=lambda x: (x["top"], x["left"]))
    return lines


def _find_spatial_nr_anchors(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    anchors: List[Dict[str, Any]] = []

    for line in lines:
        text = line["text"]
        cands = extract_candidates_from_line(text)
        if not cands:
            continue

        if line_has_nr_label(text) or len(cands) == 1:
            nr = cands[0]
            anchors.append(
                {
                    "nr": nr,
                    "left": line["left"],
                    "top": line["top"],
                    "right": line["right"],
                    "bottom": line["bottom"],
                    "text": text,
                }
            )

    uniq: List[Dict[str, Any]] = []
    seen = set()
    for a in sorted(anchors, key=lambda x: (x["top"], x["left"])):
        if a["nr"] in seen:
            continue
        uniq.append(a)
        seen.add(a["nr"])

    return uniq





def _collect_spatial_block_lines(
    all_lines: List[Dict[str, Any]],
    anchor: Dict[str, Any],
    image_width: Optional[int] = None,
) -> List[str]:
    """
    Bloque espacial definitivo por distancia real + separación lateral.
    - usa cercanía geométrica al NR
    - evita mezclar cuadrante izquierdo/derecho
    - no depende del orden textual del OCR
    """
    ax1 = anchor["left"]
    ay1 = anchor["top"]
    ax2 = anchor["right"]
    ay2 = anchor["bottom"]

    nr_center_x = (ax1 + ax2) / 2.0

    # Radios calibrados para este tipo de plano
    max_dx_left = 90
    max_dx_right = 260
    max_dy_up = 55
    max_dy_down = 140

    selected: List[Tuple[int, int, str]] = []

    for line in all_lines:
        text = line["text"]
        if not text:
            continue

        lx1 = line["left"]
        ly1 = line["top"]
        lx2 = line["right"]

        # Filtro lateral por mitad del plano.
        # Esto evita que un NR del lado derecho robe materiales del lado izquierdo
        # y viceversa.
        if image_width:
            line_center_x = (lx1 + lx2) / 2.0
            mitad = image_width / 2.0

            if nr_center_x >= mitad:
                if line_center_x < mitad - 20:
                    continue
            else:
                if line_center_x > mitad + 20:
                    continue

        # Filtro de distancia real respecto al ancla NR
        if lx1 < ax1 - max_dx_left:
            continue
        if lx1 > ax2 + max_dx_right:
            continue
        if ly1 < ay1 - max_dy_up:
            continue
        if ly1 > ay2 + max_dy_down:
            continue

        # Excluir otros NR distintos del ancla
        cands = extract_candidates_from_line(text)
        if cands and line_has_nr_label(text) and anchor["nr"] not in cands:
            continue

        if is_probable_noise_detail_line(text):
            continue

        selected.append((line["top"], line["left"], text))

    selected.sort(key=lambda x: (x[0], x[1]))

    salida: List[str] = []
    vistos = set()
    for _, _, line_text in selected:
        if line_text not in vistos:
            salida.append(line_text)
            vistos.add(line_text)

    nr_line = anchor["text"]
    if nr_line in salida:
        salida.remove(nr_line)
    salida.insert(0, nr_line)

    return salida


def _extract_structured_details_from_image(img_variant) -> Dict[str, Dict[str, Any]]:
    lines = _extract_lines_from_image_data(img_variant)
    anchors = _find_spatial_nr_anchors(lines)

    try:
        image_width = int(img_variant.shape[1])
    except Exception:
        image_width = None

    detalles: Dict[str, Dict[str, Any]] = {}
    for anchor in anchors:
        block_lines = _collect_spatial_block_lines(lines, anchor, image_width=image_width)
        ordered = extract_ordered_details_from_block(block_lines)

        detalles[anchor["nr"]] = {
            "zona": ordered.get("zona"),
            "fecha": ordered.get("fecha"),
            "materiales": ordered.get("materiales", []),
            "lineas": block_lines,
        }

    return detalles



# =========================================================
# SEGMENTACIÓN POR TEXTO (FALLBACK)
# =========================================================

def find_nr_line_indexes(lines: List[str]) -> List[Dict[str, Any]]:
    encontrados: List[Dict[str, Any]] = []

    for idx, line in enumerate(lines):
        candidatos = extract_candidates_from_line(line)
        if not candidatos:
            continue

        if line_has_nr_label(line):
            for nr in candidatos:
                encontrados.append({"index": idx, "nr": nr})

    if not encontrados:
        for idx, line in enumerate(lines):
            candidatos = extract_candidates_from_line(line)
            if len(candidatos) == 1 and not should_skip_line_for_nr(line):
                encontrados.append({"index": idx, "nr": candidatos[0]})

    salida: List[Dict[str, Any]] = []
    vistos = set()
    for item in encontrados:
        key = (item["index"], item["nr"])
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

        if RE_FECHA_LABEL.search(line) and i + 1 < len(lines):
            fecha = find_date_in_line(lines[i + 1])
            if fecha:
                return fecha

    return None


def extract_ordered_details_from_block(block_lines: List[str]) -> Dict[str, Any]:
    zona = None
    fecha = None
    materiales: List[Dict[str, Any]] = []

    detail_lines = block_lines[1:] if len(block_lines) > 1 else []

    zona_idx = None
    fecha_idx = None

    for idx, line in enumerate(detail_lines[:5]):
        zona_tmp = extract_zona_from_lines([line])
        if zona_tmp:
            zona = zona_tmp
            zona_idx = idx
            break

    if not zona:
        for idx, line in enumerate(detail_lines[:4]):
            if is_short_location_candidate(line):
                zona = smart_normalize_location(line).title()
                zona_idx = idx
                break

    for idx, line in enumerate(detail_lines[:6]):
        fecha_tmp = find_date_in_line(line)
        if fecha_tmp:
            fecha = fecha_tmp
            fecha_idx = idx
            break

    start_material_idx = 0
    if fecha_idx is not None:
        start_material_idx = fecha_idx + 1
    elif zona_idx is not None:
        start_material_idx = zona_idx + 1

    material_chunk = detail_lines[start_material_idx:]
    materiales = extract_materiales_from_lines(material_chunk)

    if not zona:
        zona = extract_zona_from_lines(block_lines)

    if not fecha:
        fecha = extract_fecha_from_lines(block_lines)

    if not materiales:
        materiales = extract_materiales_from_lines(block_lines)

    return {
        "zona": zona,
        "fecha": fecha,
        "materiales": materiales,
    }


def extract_nr_sections(text: str, max_lines_per_section: int = 16) -> List[Dict[str, Any]]:
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]
    nr_positions = find_nr_line_indexes(lines)

    if not nr_positions:
        return []

    secciones: List[Dict[str, Any]] = []

    for i, item in enumerate(nr_positions):
        start_idx = item["index"]
        nr = item["nr"]

        if i + 1 < len(nr_positions):
            next_idx = nr_positions[i + 1]["index"]
            end_idx = next_idx
        else:
            end_idx = min(len(lines), start_idx + max_lines_per_section + 1)

        block_lines = lines[start_idx:end_idx]
        block_lines = block_lines[: max_lines_per_section + 1]

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

    resultado_final: List[Dict[str, Any]] = []
    vistos = set()
    for item in secciones:
        nr = item["nr"]
        if nr in vistos:
            continue
        resultado_final.append(item)
        vistos.add(nr)

    return resultado_final


def extract_detalles_por_nr(text: str) -> Dict[str, Dict[str, Any]]:
    if not text:
        return {}

    cache_key = _text_cache_key(text)
    if cache_key in _OCR_STRUCTURED_CACHE:
        return _OCR_STRUCTURED_CACHE[cache_key]

    detalles: Dict[str, Dict[str, Any]] = {}
    for item in extract_nr_sections(text):
        detalles[item["nr"]] = {
            "zona": item.get("zona"),
            "fecha": item.get("fecha"),
            "materiales": item.get("materiales", []),
            "lineas": item.get("lineas", []),
        }
    return detalles


# =========================================================
# OCR PRINCIPAL CON CACHE DE DETALLE ESPACIAL
# =========================================================

def _score_ocr_text(text: str) -> int:
    if not text:
        return -1

    score = 0
    text_norm = normalize_text_soft(text)

    score += len(extract_nrs(text)) * 50
    score += len(RE_FECHA_NUMERICA.findall(text)) * 10

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

    return score


def ocr_text_from_file(file_path: str) -> str:
    img = cv2.imread(file_path)
    if img is None:
        raise ValueError(f"No se pudo leer imagen: {file_path}")

    variants = build_ocr_variants(img)
    best_text = ""
    best_variant = None
    best_score = -1

    for _, variant in variants:
        for psm in ("11", "6"):
            try:
                text = pytesseract.image_to_string(
                    variant,
                    lang="spa",
                    config=f"--oem 3 --psm {psm}",
                )
                if not text or not text.strip():
                    continue

                score = _score_ocr_text(text)
                if score > best_score:
                    best_score = score
                    best_text = text
                    best_variant = variant
            except Exception:
                continue

    if not best_text:
        thr = preprocess_image(img)
        best_text = pytesseract.image_to_string(
            thr,
            lang="spa",
            config="--oem 3 --psm 11",
        )
        best_variant = thr

    try:
        detalles = _extract_structured_details_from_image(best_variant)
        if detalles:
            _OCR_STRUCTURED_CACHE[_text_cache_key(best_text)] = detalles
    except Exception:
        pass

    return best_text
