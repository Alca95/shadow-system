from django.db import models


class Carpeta(models.Model):
    mes = models.IntegerField()
    anio = models.IntegerField()
    empresa = models.CharField(max_length=150)
    estado = models.CharField(max_length=30, default="ABIERTA")
    fecha_creacion = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.mes}/{self.anio} - {self.empresa}"


class Reclamo(models.Model):
    numero_reclamo = models.CharField(max_length=20, unique=True)

    nombre_cliente = models.CharField(max_length=200)
    ciudad = models.CharField(max_length=150)
    zona = models.CharField(max_length=150)

    fecha_reclamo = models.DateField()
    descripcion_falla = models.TextField()

    def __str__(self):
        return self.numero_reclamo


class NRMateriales(models.Model):
    numero_nr = models.CharField(max_length=20, unique=True)

    reclamo = models.ForeignKey(Reclamo, on_delete=models.CASCADE)

    ciudad = models.CharField(max_length=150, blank=True, null=True)
    zona = models.CharField(max_length=150, blank=True, null=True)
    fecha_trabajo = models.DateField()

    def __str__(self):
        return self.numero_nr


class ItemNRMateriales(models.Model):
    nr = models.ForeignKey(NRMateriales, on_delete=models.CASCADE, related_name="items")

    descripcion = models.CharField(max_length=200)
    cantidad = models.FloatField()
    unidad_medida = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.descripcion} ({self.cantidad})"


class Plano(models.Model):
    carpeta = models.ForeignKey(Carpeta, on_delete=models.CASCADE)
    id_plano_deposito = models.CharField(max_length=50, unique=True)

    archivo = models.FileField(upload_to="planos/")
    texto_ocr = models.TextField(blank=True, null=True)

    nr_detectados = models.TextField(blank=True, null=True)
    fecha_plano = models.DateField(blank=True, null=True)

    nr_validos = models.TextField(blank=True, null=True)
    nr_desconocidos = models.TextField(blank=True, null=True)
    motivo = models.TextField(blank=True, null=True)

    estado = models.CharField(max_length=30, default="EN_REVISION")
    fecha_carga = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.id_plano_deposito


class Auditoria(models.Model):
    plano = models.ForeignKey(Plano, on_delete=models.CASCADE)
    usuario = models.CharField(max_length=150)
    accion = models.TextField()
    fecha = models.DateTimeField(auto_now_add=True)