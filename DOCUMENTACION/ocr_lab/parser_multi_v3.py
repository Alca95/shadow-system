import re
from pathlib import Path

TEXT_FILE = Path("samples/ocr_output.txt")

RE_FECHA = re.compile(r"(?i)\bfecha\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")

# Detecta IDs tipo 834077-23 o 83407723 o 834077.23 o 834077/23
RE_ID_RECLAMO = re.compile(r"(?<!\d)(\d{5,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)")

# Ítems tipo "1- ...." (tolera basura)
RE_ITEM = re.compile(r"(?im)^\s*[A-Za-z]?\s*(\d+)\s*[-:\.]\s*(.+?)\s*$")

# Filtros de basura
RE_BASURA_LINEA = re.compile(r"(?i)^\s*item\s*\d+\s*$")

def fix_year_if_suspicious(reclamo: str) -> str:
    # reclamo formato: 834079-73
    try:
        base, yy = reclamo.split("-")
    except ValueError:
        return reclamo

    # Si el año no está en rango razonable, lo marcamos como sospechoso.
    # (ajustable según tus datos reales)
    if not yy.isdigit():
        return reclamo

    y = int(yy)
    if y < 15 or y > 35:  # ejemplo: 2015..2035 (en formato 2 dígitos)
        return base + "-??"  # dejamos marcado para revisión/admin
    return reclamo

def normalize_reclamo(a: str, b: str) -> str:
    # a = parte larga, b = año (2 o 4)
    a = re.sub(r"\s+", "", a)
    b = re.sub(r"\s+", "", b)
    if len(b) == 4:
        b = b[-2:]  # nos quedamos con los últimos 2
    return f"{a}-{b}"

def find_reclamos_positions(lines):
    """
    Devuelve lista de (line_index, reclamo_id)
    """
    found = []
    for i, line in enumerate(lines):
        # ignorar basura
        if RE_BASURA_LINEA.match(line.strip()):
            continue

        for m in RE_ID_RECLAMO.finditer(line):
            a, b = m.group(1), m.group(2)

            # evitar capturar la FECHA (02/10/23) por tamaño: a=02 no entra porque pide 5-8 dígitos
            reclamo = normalize_reclamo(a, b)
            found.append((i, reclamo))

    # quitar duplicados preservando orden
    seen = set()
    uniq = []
    for pos, rec in found:
        if rec not in seen:
            seen.add(rec)
            uniq.append((pos, rec))
    return uniq

def extract_items_from_block(block_lines):
    items = []
    for line in block_lines:
        if RE_BASURA_LINEA.match(line.strip()):
            continue
        m = RE_ITEM.match(line)
        if not m:
            continue
        cant = int(m.group(1))
        desc = m.group(2).strip()

        # desc incompleta típica del OCR (no aporta)
        desc_low = desc.lower().strip()
        if desc_low in {"cambio de", "camblo de", "combio de", "cambilo", "camvio de"}:
                continue
        if desc_low.endswith(" de") or desc_low.endswith(" de:"):
                continue


        # limpieza mínima
        desc = desc.replace("  ", " ")
        if len(desc) < 4:
            continue
        items.append({"cantidad": cant, "descripcion": desc})
    return items

def parse(text: str):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    fecha = None
    m = RE_FECHA.search(text)
    if m:
        fecha = m.group(1)

    reclamos_pos = find_reclamos_positions(lines)
    reclamos = []

    if not reclamos_pos:
        return {"fecha_plano": fecha, "reclamos": []}

    # Bloques por líneas: desde un reclamo hasta el próximo reclamo
    for idx, (line_i, rec) in enumerate(reclamos_pos):
        start = line_i + 1
        end = reclamos_pos[idx + 1][0] if idx + 1 < len(reclamos_pos) else len(lines)
        block_lines = lines[start:end]
        items = extract_items_from_block(block_lines)

        rec_fixed = fix_year_if_suspicious(rec)
        reclamos.append({"reclamo": rec_fixed, "items": items})


    return {"fecha_plano": fecha, "reclamos": reclamos}

def main():
    text = TEXT_FILE.read_text(encoding="utf-8", errors="ignore")
    data = parse(text)

    print("FECHA PLANO:", data["fecha_plano"])
    print("RECLAMOS DETECTADOS:", len(data["reclamos"]))
    for r in data["reclamos"][:12]:
        print("-", r["reclamo"], "items:", len(r["items"]))
        for it in r["items"][:3]:
            print("   ", it)

if __name__ == "__main__":
    main()
