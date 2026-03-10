

# 📘 RESUMEN MAESTRO v1 — Tesis ANDE OCR (Django + PostgreSQL)

## 0) Objetivo del sistema

Sistema web (Python + Django + PostgreSQL) para **verificar planos/planillas** presentados por contratistas y **decidir Aprobado/Rechazado/En verificación**, usando OCR y cruces contra base de datos.

En el proceso real:

* Se genera un **Reclamo** (con datos: ciudad, zona, fecha, etc.)
* Contratistas ejecutan trabajos y presentan **carpetas mensuales** con **planos** y **NR (N°R / R. N°)** asociados a materiales utilizados.
* El sistema debe:

  * Extraer desde imagen/foto del plano el **NR** (y fecha, cuando esté)
  * Buscar esos NR en base de datos
  * Validar reglas de negocio
  * Registrar auditoría y motivos de rechazo/verificación
  * Permitir a contratistas (solo lectura) ver estado/diagnóstico

**Decisión clave (para simplificar OCR y aumentar robustez):**
El OCR se enfoca principalmente en detectar el **NR**.
La **ciudad/zona/fecha** se “estiran” desde `NRMateriales` (cargado por depósito), y se comparan contra `Reclamo`.
Si faltan ciudad/zona en NR o Reclamo, el plano **no se rechaza**, pasa a **EN_VERIFICACION** para corrección manual.

---

## 1) Entorno y herramientas

* OS: Windows (usuario Alexander Cabrera)
* Editor: VS Code
* Terminal: Git Bash / MINGW64
* DB: PostgreSQL vía pgAdmin (se resolvió error de collation con `ALTER DATABASE template1 REFRESH COLLATION VERSION;`)
* OCR: Tesseract 5.5.0 instalado y funcionando
* `pytesseract` configurado con:

  * `pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"`
  * `os.environ["TESSDATA_PREFIX"] = r"C:\Program Files\Tesseract-OCR\tessdata"`
* Lenguaje OCR: español (`lang="spa"`) — se detectó que en tessdata no estaba `spa.traineddata`, pero se resolvió y ya funciona.
* Librerías instaladas (mínimo actual):

  * `opencv-python`
  * `pytesseract`
  * `rapidfuzz` (para comparación aproximada ciudad/zona)
  * `python-dateutil` (para parse de fechas)

---

## 2) Datos que hay que comparar para aprobación

En una versión inicial el plano tenía “zona, id plano/reclamo, materiales, fecha”, pero se ajustó:

**Nueva estrategia (más robusta):**

* El plano/foto contiene un **NR** (R. N° / N°R) que identifica el paquete de materiales usados.
* Ese NR, en el “depósito”, debe contener:

  * ciudad (a veces falta)
  * zona (a veces falta)
  * fecha trabajo (a veces falta)
  * lista de materiales (items)
* El sistema valida contra el Reclamo:

  1. `fecha_trabajo >= fecha_reclamo` (si ambos existen)
  2. ciudad coincide (comparación aproximada) (si ambos existen)
  3. zona coincide (comparación aproximada) (si ambos existen)
  4. antifraude (planificado): no repetir trabajo mismo reclamo/zona en menos de 90 días y/o no duplicar mismos materiales (si hay datos)
* Si falta dato (ciudad/zona/fecha): **EN_VERIFICACION**, no rechazo directo.

---

## 3) Modelos / BD (Django app: `core`)

Se creó el proyecto Django y la app `core`. Ya hay migraciones aplicadas y admin operativo.

### Entidades principales (conceptuales)

* **Carpeta**: agrupa planos por mes/empresa (ej: “2/2026 - Empresa1”)
* **Plano**: archivo/foto subido + OCR + NR detectados + estado
* **Reclamo**: registro inicial del cliente
* **NRMateriales**: número de materiales (NR) emitido por depósito, con vínculo a reclamo + ciudad/zona/fecha trabajo + items
* **ItemNRMateriales**: materiales dentro de un NR (cantidad + descripción + unidad)
* **Auditoria**: registro de acciones (OCR, validaciones, errores, correcciones)

### Ajustes importantes hechos

* Se agregó a `Plano`:

  * `nr_detectados` (texto con CSV)
  * `fecha_plano` (fecha detectada por OCR)
  * `nr_validos` (CSV de NR existentes en BD)
  * `nr_desconocidos` (CSV de NR detectados que NO existen en BD)
  * `motivo` (texto con explicación de validación)
* Se modificó `NRMateriales` para permitir guardar sin ciudad/zona:

  * `ciudad` → `blank=True, null=True`
  * `zona` → `blank=True, null=True`
* Base de datos PostgreSQL sincronizada con migraciones.

---

## 4) Admin Django (panel /admin)

Se registraron modelos y se configuró un `PlanoAdmin` con acciones.

### Funcionalidades confirmadas en Admin:

✅ Crear manualmente:

* 1 Reclamo
* 1 NRMateriales (asociado a Reclamo)
* 1 ItemNRMateriales (dentro del NR)
* 1 Carpeta
* 1 Plano (subir imagen/foto)

✅ Acciones en Planos:

1. **Procesar OCR del plano (extraer NR y fecha)**

   * Guarda:

     * `texto_ocr`
     * `nr_detectados`
     * `fecha_plano`
2. **Validar NR detectados contra la BD (marca válidos/desconocidos)**

   * Guarda:

     * `nr_validos`
     * `nr_desconocidos`
     * `motivo`
     * cambia `estado` a `EN_VERIFICACION` si hay desconocidos o no hay válidos

✅ Resultado real observado (captura):

* Plano `plano-prueba-02`:

  * `nr_detectados`: 6 valores (incluye ruido OCR)
  * `nr_validos`: 1 valor (el único existente en BD)
  * `nr_desconocidos`: el resto
  * Estado: `EN_VERIFICACION`

---

## 5) OCR: pipeline implementado y archivos clave

Se empezó con scripts en carpeta `ocr_lab` para pruebas OCR y parsing.
Luego se integró OCR al sistema Django mediante módulos dentro de `core`.

### Preprocesamiento que funcionó mejor en fotos reales

* Gris
* CLAHE
* Gaussian blur (leve)
* Adaptive threshold (Gaussian)
* Config OCR: `--oem 3 --psm 11` (para texto disperso)

Se guardó debug visual:

* `samples/debug_thr.png`

### Extracción actual

* Se extraen NR mediante regex robusto:

  * detecta formatos con guión, sin guión, etc.
  * normaliza a `XXXXXX-YY`
* Se extrae fecha de plano cuando está presente (p.ej. “FECHA: 02/10/23”)

### Problemas OCR detectados (esperables)

* Detecta NR falsos (ruido)
* Algunos reclamos aparecen como `834079-73` (error del OCR)
* Por eso se creó la validación contra BD.

### Módulos creados (en Django)

* `core/ocr_extract.py`

  * `ocr_text_from_file(file_path)`
  * `extract_nrs(text)` → lista normalizada
  * `extract_fecha_plano(text)` → `date` o None
* Acciones Admin llaman a estas funciones.

---

## 6) Validación contra BD (ya implementada)

Se creó lógica para:

* Tomar `plano.nr_detectados`
* Revisar cuáles existen en tabla `NRMateriales`
* Guardar:

  * `nr_validos`
  * `nr_desconocidos`
* Estado:

  * Si no hay válidos o hay desconocidos → `EN_VERIFICACION`
  * Si hay conflicto grave (planificado: duplicados, etc.) → `RECHAZADO`

**Importante:**
No se aprueba todavía por ciudad/zona/fecha/materiales. Eso es el siguiente paso.

---

## 7) Decisiones de diseño importantes (para tesis y para robustez)

1. **Aprobación por Plano**

   * Un plano puede contener múltiples reclamos/NR.
   * Resultado final simple: Aprobado/Rechazado/En verificación.
2. **Diagnóstico interno por NR/reclamo/material** (auditoría)

   * Guardar motivos: qué NR faltó, qué dato no coincide, etc.
3. **Evitar fraude**

   * Reglas antifraude propuestas:

     * fecha trabajo no puede ser anterior al reclamo
     * evitar repetición de trabajos similares en misma zona en menos de 90 días
     * evitar duplicados de plano / id_plano_deposito
4. **Tolerancia a datos faltantes**

   * Si depósito no carga ciudad/zona/fecha, no se rechaza: va a `EN_VERIFICACION`.
5. **Comparación aproximada**

   * Ciudad y zona se comparan con `rapidfuzz` (ej: “Cnel. Oviedo” ≈ “Coronel Oviedo”).

---

## 8) Estado actual del proyecto (hasta lo último)

✅ Django + PostgreSQL funcionando
✅ Admin operativo con modelos visibles
✅ Subida de imagen de plano funcionando
✅ OCR integrado al sistema y guarda resultados
✅ Detecta fecha del plano correctamente
✅ Detecta varios NR (con ruido)
✅ Valida NR contra BD y separa válidos/desconocidos
✅ Estado del plano cambia a EN_VERIFICACION si corresponde
✅ `NRMateriales` ahora puede guardarse sin ciudad o zona
✅ `rapidfuzz` instalado para validación aproximada

---

## 9) Lo próximo a implementar (siguiente sprint)

### A) Validación completa NR vs Reclamo (reglas de negocio)

Para cada NR válido:

* Traer `NRMateriales` (con `reclamo` relacionado)
* Validar:

  1. `fecha_trabajo >= fecha_reclamo` (si existen)
  2. ciudad coincide (fuzzy, si existen)
  3. zona coincide (fuzzy, si existen)
  4. antifraude 90 días (si hay datos)
  5. (opcional) materiales: comparar items del NR vs lo esperado del reclamo o contra catálogo
* Resultado final:

  * APROBADO si todo lo validable pasa y no hay pendientes
  * RECHAZADO si hay contradicción comprobable
  * EN_VERIFICACION si faltan datos o hay NR desconocidos

### B) Auditoría detallada

Guardar en `Auditoria`:

* quién validó
* qué regla falló
* qué NR causó el rechazo
* qué campo faltó

### C) Roles de usuarios (planificado)

* Funcionario/Admin: puede corregir/confirmar, dejar comentarios, aprobar/rechazar
* Contratista: solo lectura (estado + diagnóstico)

---