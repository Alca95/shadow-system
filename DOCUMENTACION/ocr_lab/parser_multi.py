import re
from pathlib import Path

TEXT_FILE = Path("samples/ocr_output.txt")

# FECHA: 02/10/23
RE_FECHA = re.compile(r"(?i)\bfecha\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})")

# Detecta "Reclamo N° ...." con muchas variantes OCR (reclamo/reciamo/rectamo/teciamo/eclamo)
# y captura el número aunque venga pegado o con separadores raros.
RE_RECLAMO_ANYWHERE = re.compile(
    r"(?i)(?:reclamo|reciamo|rectamo|teciamo|declamo|eclamo)\s*"
    r"(?:n[°o\"*]|nro|n)\s*[:\-]?\s*"
    r"(?P<num>\d{5,10}\s*[-/\.]?\s*\d{2,4}|\d{7,10})"
)

# Ítems tipo "1- Cambio de ..." (tolera basura antes del número)
RE_ITEM = re.compile(r"(?im)^\s*[A-Za-z]?\s*(?P<cant>\d+)\s*[-:\.]\s*(?P<desc>.+?)\s*$")

def normalize_reclamo(raw: str) -> str:
    s = re.sub(r"\s+", "", raw)
    s = s.replace(".", "-").replace("/", "-")

    # Si viene todo pegado y termina en 2 dígitos de año: 83897023 -> 838970-23
    # (solo si tiene 8 dígitos y NO tiene separador)
    if "-" not in s and len(s) == 8:
        s = s[:-2] + "-" + s[-2:]

    return s

def split_blocks_by_reclamos(text: str):
    matches = list(RE_RECLAMO_ANYWHERE.finditer(text))
    blocks = []

    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        num = normalize_reclamo(m.group("num"))
        block_text = text[m.end():end]  # contenido después del número del reclamo
        blocks.append((num, block_text))

    return blocks

def parse(text: str) -> dict:
    fecha = None
    m = RE_FECHA.search(text)
    if m:
        fecha = m.group(1)

    reclamos = []
    blocks = split_blocks_by_reclamos(text)

    for reclamo_num, block in blocks:
        items = []
        for it in RE_ITEM.finditer(block):
            cant = int(it.group("cant"))
            desc = it.group("desc").strip()

            # Filtrar basura típica
            if desc.lower().startswith("item "):
                continue

            # Filtrar cosas vacías o muy cortas
            if len(desc) < 3:
                continue

            items.append({"cantidad": cant, "descripcion": desc})

        reclamos.append({"reclamo": reclamo_num, "items": items})

    return {"fecha_plano": fecha, "reclamos": reclamos}

def main():
    text = TEXT_FILE.read_text(encoding="utf-8", errors="ignore")
    data = parse(text)

    print("FECHA PLANO:", data["fecha_plano"])
    print("RECLAMOS DETECTADOS:", len(data["reclamos"]))

    # Mostrar muestra
    for r in data["reclamos"][:12]:
        print("-", r["reclamo"], "items:", len(r["items"]))
        for it in r["items"][:3]:
            print("   ", it)

if __name__ == "__main__":
    main()
