from __future__ import annotations

import re
from datetime import date
from typing import Optional, List

import cv2
import pytesseract
from dateutil import parser as dateparser


RE_FECHA = re.compile(r"(?i)\bfecha\b[^0-9]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")

# Número tipo 5072455-24 / 5072455/24 / 507245524
RE_NR_NUMERO = re.compile(r"(?<!\d)(\d{6,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)")

# Línea que parece contener una etiqueta válida de NR
RE_ETIQUETA_NR = re.compile(
    r"(?i)\b(r\s*\.?\s*n|rn|r\s+n|n[°ºo*]?\s*r|n[°ºo*])\b"
)

# Líneas que suelen traer ruido y no deben aportar NR
RE_LINEA_RUIDO = re.compile(
    r"(?i)\b(x|y)\s*[:=]|\bplano\b|\boem\b|\blpn\b|\bzona\b|\bpartida\b|\bart[ií]culo\b"
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

    # Para este tipo de plano conviene ser más estrictos:
    # la parte principal del NR suele venir con 7 dígitos
    # aceptamos 6 a 8 para no romper otros casos
    if len(a) < 6 or len(a) > 8:
        return False

    if len(b) not in (2, 4):
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


def extract_candidates_from_line(line: str) -> List[str]:
    encontrados = []

    for match in RE_NR_NUMERO.finditer(line):
        a = match.group(1)
        b = match.group(2)

        if is_valid_nr_candidate(a, b):
            encontrados.append(normalize_nr(a, b))

    return encontrados


def extract_nrs(text: str) -> List[str]:
    """
    Extrae NR usando una estrategia por líneas para evitar que
    coordenadas X/Y y otros números del plano se confundan con NR.
    """
    if not text:
        return []

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    encontrados = []

    for i, line in enumerate(lines):
        line_upper = line.upper()

        # Ignorar líneas de ruido típico del plano
        if RE_LINEA_RUIDO.search(line):
            continue

        tiene_etiqueta = bool(RE_ETIQUETA_NR.search(line))

        # Caso 1: línea con etiqueta + número en la misma línea
        if tiene_etiqueta:
            encontrados.extend(extract_candidates_from_line(line))

            # Caso 2: la etiqueta está en una línea y el número en la siguiente
            if i + 1 < len(lines):
                next_line = lines[i + 1]
                if not RE_LINEA_RUIDO.search(next_line):
                    encontrados.extend(extract_candidates_from_line(next_line))

    # Respaldo:
    # si no encontró nada con etiquetas, usar búsqueda general,
    # pero excluyendo líneas de ruido
    if not encontrados:
        for line in lines:
            if RE_LINEA_RUIDO.search(line):
                continue
            encontrados.extend(extract_candidates_from_line(line))

    return deduplicate_keep_order(encontrados)


def extract_fecha_plano(text: str) -> Optional[date]:
    m = RE_FECHA.search(text)
    if not m:
        return None

    raw = m.group(1)

    try:
        dt = dateparser.parse(raw, dayfirst=True)
        return dt.date() if dt else None
    except Exception:
        return None