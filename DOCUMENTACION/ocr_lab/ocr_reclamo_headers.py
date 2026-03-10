from pathlib import Path
import cv2
import pytesseract
import os
import re

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
os.environ["TESSDATA_PREFIX"] = r"C:\Program Files\Tesseract-OCR\tessdata"

INPUT_IMAGE = Path("samples/test_image.jpg")

# ID reclamo estilo 834250-23 o 83425023
RE_ID = re.compile(r"(?<!\d)(\d{5,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)")

# filtros para NO confundir con ciudad/zona
RE_ITEM = re.compile(r"(?i)^\s*\d+\s*[-:\.]")  # 1- ...
RE_ITEM2 = re.compile(r"(?i)^\s*\d+\s+\w+")    # 1 IFE, 1 IGNITOR, etc.
RE_COORD = re.compile(r"(?i)\b(x\s*=|y\s*=)\b")
RE_BASURA = re.compile(r"(?i)^\s*item\s*\d*\s*$")

RE_TRABAJO = re.compile(
    r"(?i)\b(cambio|camblo|combio|camvio|reparaci[oó]n|limpieza|impieza|falso|contacto|acometida)\b"
)
RE_MATERIAL = re.compile(
    r"(?i)\b(l[aá]mpara|lampara|ignitor|capacitor|ife|fotoc[eé]lula|tulipa|ap)\b"
)

def normalize_reclamo(a: str, b: str) -> str:
    a = re.sub(r"\s+", "", a)
    b = re.sub(r"\s+", "", b)
    if len(b) == 4:
        b = b[-2:]
    return f"{a}-{b}"

def preprocess(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    thr = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        35, 10
    )
    return thr

def ocr_data(thr):
    config = "--oem 3 --psm 11"
    return pytesseract.image_to_data(
        thr,
        lang="spa",
        config=config,
        output_type=pytesseract.Output.DICT
    )

def group_into_lines(data, y_tol=10, conf_min=20):
    tokens = []
    n = len(data["text"])

    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue

        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1.0

        if conf >= 0 and conf < conf_min:
            continue

        tokens.append({
            "text": txt,
            "left": int(data["left"][i]),
            "top": int(data["top"][i]),
            "w": int(data["width"][i]),
            "h": int(data["height"][i]),
            "conf": conf,
        })

    tokens.sort(key=lambda t: (t["top"], t["left"]))

    lines = []
    for t in tokens:
        placed = False
        for line in lines:
            if abs(t["top"] - line["y"]) <= y_tol:
                line["tokens"].append(t)
                line["y"] = int((line["y"] + t["top"]) / 2)
                placed = True
                break
        if not placed:
            lines.append({"y": t["top"], "tokens": [t]})

    for line in lines:
        line["tokens"].sort(key=lambda t: t["left"])
        line["text"] = " ".join(t["text"] for t in line["tokens"])
        line["x_min"] = min(t["left"] for t in line["tokens"])
        line["x_max"] = max(t["left"] + t["w"] for t in line["tokens"])

    lines.sort(key=lambda l: l["y"])
    return lines

def is_header_candidate(text: str) -> bool:
    t = text.strip()
    if len(t) < 3:
        return False
    if RE_BASURA.match(t):
        return False
    if RE_COORD.search(t):
        return False
    if RE_ITEM.match(t) or RE_ITEM2.match(t):
        return False
    if RE_TRABAJO.search(t) or RE_MATERIAL.search(t):
        return False

    # evitar líneas “sucias” típicas del OCR
    if any(ch in t for ch in ["$", "|", "—", "_"]):
        return False

    # no debe tener muchos números
    digits = sum(ch.isdigit() for ch in t)
    if digits >= 2:
        return False

    # ciudad/zona suele ser corto
    words = [w for w in t.split() if w]
    if len(words) > 5:
        return False

    return True

def find_id_bbox(line):
    """
    Devuelve bbox (x_min, x_max) usando tokens numéricos.
    """
    m = RE_ID.search(line["text"])
    if not m:
        return (line["x_min"], line["x_max"], None)

    a, b = m.group(1), m.group(2)
    rec_norm = normalize_reclamo(a, b)

    digit_tokens = [t for t in line["tokens"] if any(ch.isdigit() for ch in t["text"])]
    if digit_tokens:
        x_min = min(t["left"] for t in digit_tokens)
        x_max = max(t["left"] + t["w"] for t in digit_tokens)
        return (x_min, x_max, rec_norm)

    return (line["x_min"], line["x_max"], rec_norm)

def main():
    print(f"[INFO] Usando imagen: {INPUT_IMAGE.resolve()}")

    img = cv2.imread(str(INPUT_IMAGE))
    if img is None:
        raise ValueError("No se pudo leer la imagen.")

    thr = preprocess(img)
    data = ocr_data(thr)
    lines = group_into_lines(data)

    # 1) detectar reclamos
    reclamos = []
    for line in lines:
        if not RE_ID.search(line["text"]):
            continue
        x_id_min, x_id_max, rec = find_id_bbox(line)
        reclamos.append({
            "reclamo": rec,
            "y": line["y"],
            "x_id_min": x_id_min,
            "x_id_max": x_id_max,
            "line_text": line["text"],
        })

    print(f"[OK] Reclamos detectados: {len(reclamos)}")

    # 2) para cada reclamo: buscar ciudad/zona por cercanía espacial (dx/dy)
    for r in reclamos:
        y_r = r["y"]
        x_min_r = r["x_id_min"]
        x_max_r = r["x_id_max"]

        # centro X del ID del reclamo
        x_center = (x_min_r + x_max_r) // 2

        candidates = []
        raw_candidates = []

        for line in lines:
            if line["y"] >= y_r:
                continue

            dy = y_r - line["y"]
            if dy > 900:
                continue

            # distancia horizontal al centro
            line_center = (line["x_min"] + line["x_max"]) // 2
            dx = abs(line_center - x_center)

            if dx > 250:
                continue

            raw_score = (900 - dy) + max(0, 250 - dx)
            raw_candidates.append((raw_score, line, dy, dx))

            if not is_header_candidate(line["text"]):
                continue

            score = (900 - dy) + max(0, 250 - dx)

            letters = [ch for ch in line["text"] if ch.isalpha()]
            if letters:
                upper_ratio = sum(ch.isupper() for ch in letters) / len(letters)
                score += int(60 * upper_ratio)

            candidates.append((score, line, dy, dx))

        raw_candidates.sort(key=lambda x: x[0], reverse=True)
        candidates.sort(key=lambda x: x[0], reverse=True)

        ciudad = candidates[0][1]["text"] if len(candidates) >= 1 else None
        zona = candidates[1][1]["text"] if len(candidates) >= 2 else None

        print("\n==============================")
        print("RECLAMO:", r["reclamo"])
        print("Linea ID:", r["line_text"])
        print("BBox ID X:", (x_min_r, x_max_r), "Y:", y_r)

        print("Top 12 candidatos CRUDOS (sin filtro):")
        for sc, ln, dy, dx in raw_candidates[:12]:
            print(f"  raw={sc:4d} dy={dy:4d} dx={dx:3d} y={ln['y']:4d} x=[{ln['x_min']},{ln['x_max']}]  {ln['text']}")

        print("Top 8 candidatos FILTRADOS (posibles ciudad/zona):")
        for sc, ln, dy, dx in candidates[:8]:
            print(f"  sc ={sc:4d} dy={dy:4d} dx={dx:3d} y={ln['y']:4d} x=[{ln['x_min']},{ln['x_max']}]  {ln['text']}")

        print("CIUDAD (candidata):", ciudad)
        print("ZONA (candidata):", zona)

if __name__ == "__main__":
    main()