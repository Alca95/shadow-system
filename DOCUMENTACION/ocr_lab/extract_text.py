from pathlib import Path
import cv2
import pytesseract
import os

# Ruta de Tesseract en Windows
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
os.environ["TESSDATA_PREFIX"] = r"C:\Program Files\Tesseract-OCR\tessdata"

INPUT_IMAGE = Path("samples/test_image.jpg")

def main():
    if not INPUT_IMAGE.exists():
        raise FileNotFoundError(f"No existe el archivo: {INPUT_IMAGE.resolve()}")

    img = cv2.imread(str(INPUT_IMAGE))
    if img is None:
        raise ValueError("No se pudo leer la imagen. ¿Es un JPG/PNG válido?")

    # 1) Gris
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # 2) CLAHE (mejor que equalizeHist para fotos reales)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    # 3) Blur leve
    gray = cv2.GaussianBlur(gray, (3, 3), 0)

    # 4) Adaptive threshold (mejor para iluminación desigual)
    thr = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        35,
        10
    )

    # Guardar debug (ver cómo quedó el preprocesamiento)
    debug_path = Path("samples/debug_thr.png")
    cv2.imwrite(str(debug_path), thr)

    # OCR
    config = "--oem 3 --psm 11"

    text = pytesseract.image_to_string(thr, lang="spa", config=config)

    # Guardar OCR en archivo
    out_path = Path("samples/ocr_output.txt")
    out_path.write_text(text, encoding="utf-8")

    print("===== TEXTO OCR (INICIO) =====")
    print(text[:2000])
    print("===== TEXTO OCR (FIN) =====")
    print(f"[OK] OCR guardado en: {out_path}")
    print(f"[OK] Imagen preprocesada guardada en: {debug_path}")

if __name__ == "__main__":
    main()
