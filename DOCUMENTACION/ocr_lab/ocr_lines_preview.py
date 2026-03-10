from pathlib import Path
import cv2
import pytesseract
import os
import re

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
os.environ["TESSDATA_PREFIX"] = r"C:\Program Files\Tesseract-OCR\tessdata"

INPUT_IMAGE = Path("samples/test_image.jpg")

def ocr_data(thr):
    config = "--oem 3 --psm 11"
    return pytesseract.image_to_data(
        thr,
        lang="spa",
        config=config,
        output_type=pytesseract.Output.DICT
    )

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

def group_into_lines(data, y_tol=10, conf_min=20):
    """
    Agrupa tokens en líneas usando la coordenada 'top' (y).
    y_tol controla qué tan cerca en Y deben estar para ser misma línea.
    conf_min filtra tokens de baja confianza (si filtra demasiado, bájalo a 0).
    """
    tokens = []
    n = len(data["text"])

    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue

        # conf viene como string (a veces "-1")
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

    # ordenar por Y y luego X
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

    # ordenar tokens dentro de cada línea por X y formar texto
    for line in lines:
        line["tokens"].sort(key=lambda t: t["left"])
        line["text"] = " ".join(t["text"] for t in line["tokens"])
        line["x_min"] = min(t["left"] for t in line["tokens"])
        line["x_max"] = max(t["left"] + t["w"] for t in line["tokens"])

    # ordenar líneas de arriba a abajo
    lines.sort(key=lambda l: l["y"])
    return lines

def main():
    print(f"[INFO] Usando imagen: {INPUT_IMAGE.resolve()}")

    img = cv2.imread(str(INPUT_IMAGE))
    if img is None:
        raise ValueError("No se pudo leer la imagen. ¿Existe samples/test_image.jpg?")

    thr = preprocess(img)
    data = ocr_data(thr)

    lines = group_into_lines(data, y_tol=10, conf_min=20)

    out = Path("samples/ocr_lines.txt")
    dump = []
    for i, line in enumerate(lines[:300]):  # primeras 300 líneas
        dump.append(f"{i:03d}  y={line['y']:4d}  x=[{line['x_min']:3d},{line['x_max']:3d}]  {line['text']}")
    out.write_text("\n".join(dump), encoding="utf-8")

    # Detectores robustos
    RE_ANCLA = re.compile(r"(?i)\b(r\.\s*n|r\s*n|recl|reci|rect|nrecl|eclamo)\b")
    RE_ID = re.compile(r"(?<!\d)(\d{5,8})\s*[-/\.]?\s*(\d{2,4})(?!\d)")

    print(f"[OK] Generado: {out}")
    print("Mostrando líneas candidatas a ANCLA (reclamo) y/o con ID:")

    shown = 0
    for line in lines:
        t = line["text"]

        is_anchor = bool(RE_ANCLA.search(t))
        has_id = bool(RE_ID.search(t))

        if is_anchor or has_id:
            tag = []
            if is_anchor:
                tag.append("ANCLA")
            if has_id:
                tag.append("ID")
            tag = ",".join(tag)
            print(f"[{tag}] y={line['y']:4d} x=[{line['x_min']:3d},{line['x_max']:3d}]  {t}")
            shown += 1
            if shown >= 50:
                break

if __name__ == "__main__":
    main()
