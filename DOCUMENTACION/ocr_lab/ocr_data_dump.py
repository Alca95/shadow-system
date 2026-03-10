from pathlib import Path
import cv2
import pytesseract
import os

pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
os.environ["TESSDATA_PREFIX"] = r"C:\Program Files\Tesseract-OCR\tessdata"

INPUT_IMAGE = Path("samples/test_image.jpg")

def main():
    img = cv2.imread(str(INPUT_IMAGE))
    if img is None:
        raise ValueError("No se pudo leer la imagen.")

    # Preprocesamiento (el que te está funcionando bien)
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

    # OCR con texto disperso
    config = "--oem 3 --psm 11"

    data = pytesseract.image_to_data(
        thr,
        lang="spa",
        config=config,
        output_type=pytesseract.Output.DICT
    )

    print("OCR tokens:", len(data["text"]))
    print("Primeros textos:", [t for t in data["text"][:30] if t.strip()])


    # Guardar un CSV simple para inspección
    out = Path("samples/ocr_data.tsv")
    lines = ["i\ttext\tconf\tleft\ttop\twidth\theight"]
    n = len(data["text"])

    for i in range(n):
        txt = (data["text"][i] or "").strip()
        conf = data["conf"][i]
        left = data["left"][i]
        top = data["top"][i]
        w = data["width"][i]
        h = data["height"][i]

        # Guardamos TODO (incluye vacíos), así vemos la estructura real
        # Reemplazamos tabs en texto por espacio
        txt = txt.replace("\t", " ")
        lines.append(f"{i}\t{txt}\t{conf}\t{left}\t{top}\t{w}\t{h}")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] Generado: {out}")
    print("Abrilo en VS Code o Excel para ver coordenadas.")

if __name__ == "__main__":
    main()
