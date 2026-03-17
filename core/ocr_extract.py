from __future__ import annotations

import re
from datetime import date
from typing import Optional, List

import cv2
import pytesseract
from dateutil import parser as dateparser


RE_FECHA = re.compile(r"(?i)\bfecha\b[^0-9]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")

# Número tipo:
# 5072455-24
# 5072455/24
# 5072455.24
# 507245524
RE_NR_NUMERO = re.compile(r"(?<!\d)(\d{6,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)")

# Línea que parece contener una etiqueta válida de NR
RE_ETIQUETA_NR = re.compile(
    r"(?i)\b(?:r\s*\.?\s*n|rn|r\s+n|nr|n[°ºo*]?\s*r|n[°ºo*])\b"
)

# Etiqueta compacta dentro de texto OCR sucio
RE_ETIQUETA_NR_FLEX = re.compile(
    r"(?i)(?:\b(?:nr|rn)\b|n\s*[°ºo*]?\s*r|r\s*\.?\s*n|n[°ºo*])"
)

# Líneas que suelen traer ruido y no deben aportar NR
RE_LINEA_RUIDO = re.compile(
    r"(?i)\b(x|y)\s*[:=]|\bplano\b|\boem\b|\blpn\b|\bzona\b|\bpartida\b|\bart[ií]culo\b"
)

# Separadores raros que a veces mete OCR
RE_SEPARADORES_RAROS = re.compile(r"[–—_=]+")

# Espacios múltiples
RE_MULTI_SPACE = re.compile(r"\s+")


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

    # Mantengo tu criterio original para no romper casos ya válidos
    if len(a) < 6 or len(a) > 8:
        return False

    if len(b) not in (2, 4):
        return False

    # Filtro suave para evitar basura típica
    if set(a) == {"0"} or set(b) == {"0"}:
        return False

    return True


def preprocess_image(img_bgr):
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    thr = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        35,
        10,
    )
    return thr


def ocr_text_from_file(file_path: str) -> str:
    img = cv2.imread(file_path)
    if img is None:
        raise ValueError(f"No se pudo leer imagen: {file_path}")

    thr = preprocess_image(img)

    config = "--oem 3 --psm 11"
    text = pytesseract.image_to_string(thr, lang="spa", config=config)
    return text


def deduplicate_keep_order(values: List[str]) -> List[str]:
    uniq = []
    seen = set()

    for value in values:
        if value not in seen:
            uniq.append(value)
            seen.add(value)

    return uniq


def clean_ocr_text(text: str) -> str:
    """
    Limpieza conservadora del OCR para mejorar detección sin alterar demasiado
    el contenido original.
    """
    if not text:
        return ""

    text = text.replace("\r", "\n")
    text = RE_SEPARADORES_RAROS.sub("-", text)
    text = text.replace("|", " ")
    text = text.replace("\\", "/")

    # Unifica variantes comunes de NR
    text = re.sub(r"(?i)\bN\s*[°ºo*]?\s*R\b", "NR", text)
    text = re.sub(r"(?i)\bR\s*\.?\s*N\b", "NR", text)
    text = re.sub(r"(?i)\bR\s+N\b", "NR", text)
    text = re.sub(r"(?i)\bR\.?\s*N\.?\b", "NR", text)

    # Limpieza de espacios por línea
    cleaned_lines = []
    for line in text.splitlines():
        line = RE_MULTI_SPACE.sub(" ", line).strip()
        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


def normalize_line_for_search(line: str) -> str:
    """
    Normalización ligera por línea para tolerar OCR ruidoso.
    """
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
    """
    Extrae NR usando estrategia por líneas para evitar que coordenadas X/Y
    y otros números del plano se confundan con NR.

    Mejora:
    - limpia OCR antes de buscar
    - tolera etiquetas NR con más variantes
    - revisa línea actual y siguientes líneas cercanas
    - SIEMPRE hace una pasada general adicional para no perder NR
      cuyo prefijo/etiqueta haya sido deformado por OCR
    """
    if not text:
        return []

    cleaned_text = clean_ocr_text(text)
    lines = [line.strip() for line in cleaned_text.splitlines() if line.strip()]

    encontrados = []

    # Primera pasada: líneas con etiqueta
    for i, line in enumerate(lines):
        if should_skip_line(line):
            continue

        tiene_etiqueta = line_has_nr_label(line)

        if tiene_etiqueta:
            # Caso 1: línea con etiqueta + número en la misma línea
            encontrados.extend(extract_candidates_from_line(line))

            # Caso 2: número en línea siguiente
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if not should_skip_line(next_line):
                    encontrados.extend(extract_candidates_from_line(next_line))

            # Caso 3: por si OCR dejó una línea vacía/intermedia o ruido leve
            if i + 2 < len(lines):
                next_next_line = lines[i + 2]
                if not should_skip_line(next_next_line):
                    encontrados.extend(extract_candidates_from_line(next_next_line))

    # Segunda pasada SIEMPRE:
    # captura NR visibles aunque la etiqueta se haya roto en OCR
    for line in lines:
        if should_skip_line(line):
            continue
        encontrados.extend(extract_candidates_from_line(line))

    return deduplicate_keep_order(encontrados)


def extract_fecha_plano(text: str) -> Optional[date]:
    if not text:
        return None

    cleaned_text = clean_ocr_text(text)
    m = RE_FECHA.search(cleaned_text)
    if not m:
        return None

    raw = m.group(1)

    try:
        dt = dateparser.parse(raw, dayfirst=True)
        return dt.date() if dt else None
    except Exception:
        return None