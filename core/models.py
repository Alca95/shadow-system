from django.db import models
from django.contrib.auth.models import User


class EmpresaContratista(models.Model):
    nombre = models.CharField(max_length=150, unique=True)
    ruc = models.CharField(max_length=30, blank=True, null=True)
    contacto = models.CharField(max_length=150, blank=True, null=True)
    telefono = models.CharField(max_length=50, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    activo = models.BooleanField(default=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Empresa contratista"
        verbose_name_plural = "Empresas contratistas"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class PerfilUsuario(models.Model):
    ROLES = [
        ("ADMIN", "Administrador"),
        ("FUNCIONARIO", "Funcionario"),
        ("CONTRATISTA", "Contratista"),
    ]

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="perfil"
    )

    rol = models.CharField(
        max_length=20,
        choices=ROLES,
        default="FUNCIONARIO"
    )

    empresa = models.ForeignKey(
        EmpresaContratista,
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    activo = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.user.username} - {self.rol}"


class Carpeta(models.Model):
    ESTADO_CHOICES = [
        ("ABIERTA", "Abierta"),
        ("EN_REVISION", "En revisión"),
        ("CERRADA", "Cerrada"),
        ("OBSERVADA", "Observada"),
    ]

    codigo_carpeta = models.CharField(max_length=20, unique=True, blank=True)
    mes = models.IntegerField()
    anio = models.IntegerField()
    empresa = models.ForeignKey(
        EmpresaContratista,
        on_delete=models.PROTECT,
        related_name="carpetas",
        blank=True,
        null=True,
    )
    estado = models.CharField(max_length=30, choices=ESTADO_CHOICES, default="ABIERTA")
    observacion_general = models.TextField(blank=True, null=True)
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    eliminada = models.BooleanField(default=False)
    fecha_eliminacion = models.DateTimeField(blank=True, null=True)

    creada_por = models.CharField(max_length=150, blank=True, null=True)

    class Meta:
        ordering = ["-anio", "-mes", "-fecha_creacion"]

    def __str__(self):
        empresa_nombre = self.empresa.nombre if self.empresa else "Sin empresa"
        return f"{self.codigo_carpeta or 'SIN-CODIGO'} - {self.mes}/{self.anio} - {empresa_nombre}"

    def save(self, *args, **kwargs):
        if not self.codigo_carpeta:
            ultimo_id = Carpeta.objects.all().order_by("id").last()
            siguiente_numero = 1 if not ultimo_id else ultimo_id.id + 1
            self.codigo_carpeta = f"CARP{siguiente_numero:03d}"
        super().save(*args, **kwargs)


class Reclamo(models.Model):
    numero_reclamo = models.CharField(max_length=20, unique=True)
    nombre_cliente = models.CharField(max_length=200)
    ciudad = models.CharField(max_length=150)
    zona = models.CharField(max_length=150)
    fecha_reclamo = models.DateField()
    descripcion_falla = models.TextField()

    empresa = models.ForeignKey(
        EmpresaContratista,
        on_delete=models.SET_NULL,
        related_name="reclamos",
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["-fecha_reclamo", "numero_reclamo"]

    def __str__(self):
        return self.numero_reclamo


class NRMateriales(models.Model):
    numero_nr = models.CharField(max_length=20, unique=True)

    reclamo = models.ForeignKey(
        Reclamo,
        on_delete=models.CASCADE,
        related_name="nr_materiales",
    )

    ciudad = models.CharField(max_length=150, blank=True, null=True)
    zona = models.CharField(max_length=150, blank=True, null=True)
    fecha_trabajo = models.DateField()
    observacion = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "NR Materiales"
        verbose_name_plural = "NR Materiales"
        ordering = ["-fecha_trabajo", "numero_nr"]

    def __str__(self):
        return self.numero_nr


class ItemNRMateriales(models.Model):
    nr = models.ForeignKey(
        NRMateriales,
        on_delete=models.CASCADE,
        related_name="items",
    )

    descripcion = models.CharField(max_length=200)
    cantidad = models.FloatField()
    unidad_medida = models.CharField(max_length=50)
    observacion = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Ítem de NR Materiales"
        verbose_name_plural = "Ítems de NR Materiales"
        ordering = ["descripcion"]

    def __str__(self):
        return f"{self.descripcion} ({self.cantidad})"


class Plano(models.Model):
    ESTADO_CHOICES = [
        ("EN_ESPERA", "En espera"),
        ("EN_REVISION", "En revisión"),
        ("EN_VERIFICACION", "En verificación"),
        ("APROBADO", "Aprobado"),
        ("RECHAZADO", "Rechazado"),
    ]

    carpeta = models.ForeignKey(
        Carpeta,
        on_delete=models.CASCADE,
        related_name="planos",
    )
    id_plano_deposito = models.CharField(max_length=50, unique=True)

    archivo = models.FileField(upload_to="planos/")
    texto_ocr = models.TextField(blank=True, null=True)

    nr_detectados = models.TextField(blank=True, null=True)
    fecha_plano = models.DateField(blank=True, null=True)

    nr_validos = models.TextField(blank=True, null=True)
    nr_desconocidos = models.TextField(blank=True, null=True)
    motivo = models.TextField(blank=True, null=True)
    observacion = models.TextField(blank=True, null=True)

    estado = models.CharField(max_length=30, choices=ESTADO_CHOICES, default="EN_REVISION")
    fecha_carga = models.DateTimeField(auto_now_add=True)

    procesado = models.BooleanField(default=False)
    procesado_por = models.CharField(max_length=150, blank=True, null=True)
    fecha_procesamiento = models.DateTimeField(blank=True, null=True)

    nr_detectados_total = models.PositiveIntegerField(default=0)
    nr_validos_total = models.PositiveIntegerField(default=0)
    nr_desconocidos_total = models.PositiveIntegerField(default=0)

    eliminado = models.BooleanField(default=False)
    fecha_eliminacion = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-fecha_carga"]

    def __str__(self):
        return self.id_plano_deposito


class ResultadoValidacionPlano(models.Model):
    ESTADO_RESULTADO_CHOICES = [
        ("APROBADO", "Aprobado"),
        ("RECHAZADO", "Rechazado"),
        ("EN_VERIFICACION", "En verificación"),
    ]

    plano = models.ForeignKey(
        Plano,
        on_delete=models.CASCADE,
        related_name="resultados_validacion",
    )

    nr_detectado = models.CharField(max_length=30)
    nr_normalizado = models.CharField(max_length=30, blank=True, null=True)

    nr_materiales_encontrado = models.ForeignKey(
        NRMateriales,
        on_delete=models.SET_NULL,
        related_name="resultados_validacion",
        blank=True,
        null=True,
    )

    reclamo_encontrado = models.ForeignKey(
        Reclamo,
        on_delete=models.SET_NULL,
        related_name="resultados_validacion",
        blank=True,
        null=True,
    )

    estado_resultado = models.CharField(
        max_length=30,
        choices=ESTADO_RESULTADO_CHOICES,
        default="EN_VERIFICACION",
    )

    ciudad_ok = models.BooleanField(default=False)
    zona_ok = models.BooleanField(default=False)
    fecha_ok = models.BooleanField(default=False)

    motivo_resultado = models.TextField(blank=True, null=True)
    observacion = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Resultado de validación de plano"
        verbose_name_plural = "Resultados de validación de planos"
        ordering = ["id"]

    def __str__(self):
        return f"{self.nr_detectado} - {self.estado_resultado}"


class Auditoria(models.Model):
    plano = models.ForeignKey(
        Plano,
        on_delete=models.CASCADE,
        related_name="auditorias",
        blank=True,
        null=True,
    )
    carpeta = models.ForeignKey(
        Carpeta,
        on_delete=models.CASCADE,
        related_name="auditorias",
        blank=True,
        null=True,
    )

    usuario = models.CharField(max_length=150)
    accion = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True, null=True)
    entidad = models.CharField(max_length=100, blank=True, null=True)
    entidad_id = models.CharField(max_length=50, blank=True, null=True)

    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Auditoría"
        verbose_name_plural = "Auditorías"
        ordering = ["-fecha"]

    def __str__(self):
        return f"{self.usuario} - {self.accion} - {self.fecha}"


class StockMaterial(models.Model):
    nombre_material = models.CharField(max_length=200)
    codigo_material = models.CharField(max_length=50, unique=True, blank=True, null=True)
    cantidad_disponible = models.FloatField(default=0)
    unidad = models.CharField(max_length=50, default="unidad")
    stock_minimo = models.FloatField(default=0)
    activo = models.BooleanField(default=True)
    fecha_actualizacion = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Stock de material"
        verbose_name_plural = "Stock de materiales"
        ordering = ["nombre_material"]

    def __str__(self):
        return f"{self.nombre_material} - {self.cantidad_disponible}"