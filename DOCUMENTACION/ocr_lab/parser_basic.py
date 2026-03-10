import re
from pathlib import Path

TEXT_FILE = Path("samples/ocr_output.txt")

RE_RECLAMO = re.compile(
    r"(reclamo|eclamo)\s*(n[°o*]|nro|n)\s*[:\-]?\s*(\d{5,8}[-/]\d{2,4})",
    re.IGNORECASE
)

RE_ITEM = re.compile(
    r"^\s*[A-Za-z]?\s*(\d+)\s*[-:\.]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE
)

def parse(text: str) -> dict:
    # Reclamo
    m = RE_RECLAMO.search(text)
    reclamo = m.group(3) if m else None

    # Ítems (líneas tipo "1- Cambio de ...")
    items = []
    for mm in RE_ITEM.finditer(text):
        cantidad = int(mm.group(1))
        descripcion = mm.group(2).strip()
        items.append({"cantidad": cantidad, "descripcion": descripcion})

    return {"reclamo": reclamo, "items": items}

def main():
    if not TEXT_FILE.exists():
        raise FileNotFoundError(
            f"Falta {TEXT_FILE}. Primero guardá el texto OCR en ese archivo."
        )

    text = TEXT_FILE.read_text(encoding="utf-8", errors="ignore")
    data = parse(text)
    print(data)

if __name__ == "__main__":
    main()
