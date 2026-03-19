from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Count
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone

from .forms import UsuarioCrearForm, UsuarioEditarForm
from .models import Carpeta, EmpresaContratista, Plano, Auditoria, PerfilUsuario
from .services import procesar_plano_completo


def get_or_create_perfil(user):
    if user.is_superuser:
        return None
    perfil, _ = PerfilUsuario.objects.get_or_create(user=user)
    return perfil


def user_is_admin(user):
    return user.is_authenticated and user.is_superuser


def user_is_funcionario(user):
    if user.is_superuser:
        return False
    perfil = get_or_create_perfil(user)
    return bool(perfil and perfil.rol == "FUNCIONARIO" and perfil.activo)


def user_is_contratista(user):
    if user.is_superuser:
        return False
    perfil = get_or_create_perfil(user)
    return bool(perfil and perfil.rol == "CONTRATISTA" and perfil.activo)


def require_admin_or_funcionario(request):
    if request.user.is_superuser:
        return
    if user_is_funcionario(request.user):
        return
    raise PermissionDenied("No tienes permisos para realizar esta acción.")


def login_view(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect("dashboard")

        perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

        if perfil.rol == "CONTRATISTA":
            return redirect("dashboard_contratista")
        if perfil.rol == "FUNCIONARIO":
            return redirect("dashboard_funcionario")

        return redirect("dashboard")

    error = None

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if user.is_superuser:
                login(request, user)
                return redirect("dashboard")

            perfil, _ = PerfilUsuario.objects.get_or_create(user=user)

            if not perfil.activo:
                error = "Tu perfil de usuario está inactivo."
            else:
                login(request, user)

                if perfil.rol == "CONTRATISTA":
                    return redirect("dashboard_contratista")
                if perfil.rol == "FUNCIONARIO":
                    return redirect("dashboard_funcionario")

                return redirect("dashboard")
        else:
            error = "Usuario o contraseña incorrectos."

    return render(request, "login.html", {
        "error": error,
    })


@login_required
def dashboard_view(request):
    carpetas_recientes = (
        Carpeta.objects
        .filter(eliminada=False)
        .select_related("empresa")
        .order_by("-fecha_creacion")[:5]
    )

    if request.user.is_superuser:
        return render(request, "dashboard.html", {
            "app_name": "Shadow",
            "titulo_pantalla": "Inicio administrador",
            "carpetas_recientes": carpetas_recientes,
        })

    perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

    if perfil.rol == "FUNCIONARIO":
        return render(request, "dashboard_funcionario.html", {
            "app_name": "Shadow",
            "titulo_pantalla": "Inicio funcionario",
            "carpetas_recientes": carpetas_recientes,
        })

    return render(request, "dashboard.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Inicio",
        "carpetas_recientes": carpetas_recientes,
    })


@login_required
def dashboard_funcionario_view(request):
    if request.user.is_superuser:
        return redirect("dashboard")

    perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

    if perfil.rol != "FUNCIONARIO" or not perfil.activo:
        return redirect("dashboard")

    carpetas_recientes = (
        Carpeta.objects
        .filter(eliminada=False)
        .select_related("empresa")
        .order_by("-fecha_creacion")[:5]
    )

    return render(request, "dashboard_funcionario.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Inicio funcionario",
        "carpetas_recientes": carpetas_recientes,
    })


@login_required
def dashboard_contratista_view(request):
    perfil, _ = PerfilUsuario.objects.get_or_create(user=request.user)

    if perfil.rol != "CONTRATISTA":
        return redirect("dashboard")

    carpetas_recientes = (
        Carpeta.objects
        .filter(
            empresa=perfil.empresa,
            eliminada=False
        )
        .select_related("empresa")
        .order_by("-fecha_creacion")[:5]
    )

    return render(request, "dashboard_contratista.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Inicio contratista",
        "carpetas_recientes": carpetas_recientes,
    })


@login_required
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def carpetas_view(request):
    query = request.GET.get("q", "").strip()
    estado = request.GET.get("estado", "").strip()
    empresa_id = request.GET.get("empresa", "").strip()
    mes = request.GET.get("mes", "").strip()
    anio = request.GET.get("anio", "").strip()

    carpetas = Carpeta.objects.filter(eliminada=False).select_related("empresa")

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        carpetas = carpetas.filter(empresa=perfil.empresa)

    if query:
        carpetas = carpetas.filter(codigo_carpeta__icontains=query)

    if estado:
        carpetas = carpetas.filter(estado=estado)

    if empresa_id and not user_is_contratista(request.user):
        carpetas = carpetas.filter(empresa_id=empresa_id)

    if mes:
        carpetas = carpetas.filter(mes=mes)

    if anio:
        carpetas = carpetas.filter(anio=anio)

    carpetas = carpetas.order_by("-anio", "-mes", "-fecha_creacion")
    empresas = EmpresaContratista.objects.filter(activo=True).order_by("nombre")

    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Carpetas",
        "carpetas": carpetas,
        "empresas": empresas,
        "filtros": {
            "q": query,
            "estado": estado,
            "empresa": "" if user_is_contratista(request.user) else empresa_id,
            "mes": mes,
            "anio": anio,
        },
        "estado_choices": Carpeta.ESTADO_CHOICES,
    }

    if user_is_contratista(request.user):
        return render(request, "carpetas_contratista.html", contexto)

    return render(request, "carpetas.html", contexto)


@login_required
def crear_carpeta_view(request):
    require_admin_or_funcionario(request)

    empresas = EmpresaContratista.objects.filter(activo=True).order_by("nombre")
    error = None

    if request.method == "POST":
        mes = request.POST.get("mes")
        anio = request.POST.get("anio")
        empresa_id = request.POST.get("empresa")
        observacion_general = request.POST.get("observacion_general", "").strip()

        if not mes or not anio or not empresa_id:
            error = "Debes completar mes, año y empresa."
        else:
            try:
                empresa = EmpresaContratista.objects.get(id=empresa_id, activo=True)

                nombre_usuario = (
                    request.user.get_full_name().strip()
                    if request.user.get_full_name()
                    else request.user.username
                )

                carpeta = Carpeta.objects.create(
                    mes=int(mes),
                    anio=int(anio),
                    empresa=empresa,
                    observacion_general=observacion_general or None,
                    creada_por=nombre_usuario,
                )

                Auditoria.objects.create(
                    carpeta=carpeta,
                    usuario=str(request.user),
                    accion="CREAR_CARPETA",
                    descripcion=f"Se creó la carpeta {carpeta.codigo_carpeta}",
                    entidad="Carpeta",
                    entidad_id=str(carpeta.id),
                )

                return redirect("detalle_carpeta", carpeta_id=carpeta.id)

            except EmpresaContratista.DoesNotExist:
                error = "La empresa seleccionada no existe."
            except Exception as e:
                error = f"No se pudo crear la carpeta: {e}"

    return render(request, "crear_carpeta.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Crear carpeta",
        "empresas": empresas,
        "error": error,
    })


@login_required
def detalle_carpeta_view(request, carpeta_id):
    carpeta = get_object_or_404(
        Carpeta.objects.select_related("empresa"),
        id=carpeta_id,
        eliminada=False,
    )

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        if carpeta.empresa_id != perfil.empresa_id:
            raise PermissionDenied("No puedes acceder a esta carpeta.")

    estado_plano = request.GET.get("estado", "").strip()

    planos_base = carpeta.planos.filter(eliminado=False)

    total_planos = planos_base.count()
    planos_aprobados = planos_base.filter(estado="APROBADO").count()
    planos_en_revision = planos_base.filter(estado="EN_REVISION").count()
    planos_en_verificacion = planos_base.filter(estado="EN_VERIFICACION").count()
    planos_rechazados = planos_base.filter(estado="RECHAZADO").count()

    planos = planos_base
    if estado_plano:
        planos = planos.filter(estado=estado_plano)

    planos = planos.order_by("-fecha_carga")

    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Detalle de carpeta",
        "carpeta": carpeta,
        "planos": planos,
        "estado_plano": estado_plano,
        "estado_choices_plano": Plano.ESTADO_CHOICES,
        "total_planos": total_planos,
        "planos_aprobados": planos_aprobados,
        "planos_en_revision": planos_en_revision,
        "planos_en_verificacion": planos_en_verificacion,
        "planos_rechazados": planos_rechazados,
    }

    if user_is_contratista(request.user):
        return render(request, "detalle_carpeta_contratista.html", contexto)

    return render(request, "detalle_carpeta.html", contexto)


@login_required
def eliminar_carpeta_view(request, carpeta_id):
    require_admin_or_funcionario(request)

    carpeta = get_object_or_404(Carpeta, id=carpeta_id, eliminada=False)

    if request.method == "POST":
        carpeta.eliminada = True
        carpeta.fecha_eliminacion = timezone.now()
        carpeta.save()

        Auditoria.objects.create(
            carpeta=carpeta,
            usuario=str(request.user),
            accion="ELIMINAR_CARPETA",
            descripcion=f"Eliminación lógica de carpeta {carpeta.codigo_carpeta}",
            entidad="Carpeta",
            entidad_id=str(carpeta.id),
        )

        return redirect("carpetas")

    return redirect("detalle_carpeta", carpeta_id=carpeta.id)


@login_required
def subir_plano_view(request, carpeta_id=None):
    require_admin_or_funcionario(request)

    error = None
    carpeta = None

    if carpeta_id is not None:
        carpeta = get_object_or_404(
            Carpeta.objects.select_related("empresa"),
            id=carpeta_id,
            eliminada=False,
        )

    carpetas = (
        Carpeta.objects
        .filter(eliminada=False)
        .select_related("empresa")
        .order_by("-anio", "-mes", "empresa__nombre")
    )

    if request.method == "POST":
        carpeta_id_post = request.POST.get("carpeta")
        id_plano_deposito = request.POST.get("id_plano_deposito")
        archivo = request.FILES.get("archivo")

        if not carpeta_id_post or not id_plano_deposito or not archivo:
            error = "Debes completar carpeta, ID del plano y archivo."
        else:
            try:
                carpeta_obj = Carpeta.objects.get(id=carpeta_id_post, eliminada=False)

                plano = Plano.objects.create(
                    carpeta=carpeta_obj,
                    id_plano_deposito=id_plano_deposito,
                    archivo=archivo,
                    estado="EN_ESPERA",
                )

                Auditoria.objects.create(
                    plano=plano,
                    carpeta=carpeta_obj,
                    usuario=str(request.user),
                    accion="SUBIR_PLANO",
                    descripcion=f"Se cargó el plano {plano.id_plano_deposito}",
                    entidad="Plano",
                    entidad_id=str(plano.id),
                )

                return redirect("detalle_plano", plano_id=plano.id)

            except Carpeta.DoesNotExist:
                error = "La carpeta seleccionada no existe."
            except Exception as e:
                error = f"No se pudo guardar el plano: {e}"

    return render(request, "subir_plano.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Subir plano",
        "carpeta_actual": carpeta,
        "carpetas": carpetas,
        "error": error,
    })


@login_required
def detalle_plano_view(request, plano_id):
    plano = get_object_or_404(
        Plano.objects.select_related("carpeta", "carpeta__empresa"),
        id=plano_id,
        eliminado=False,
    )

    if user_is_contratista(request.user):
        perfil = get_or_create_perfil(request.user)
        if plano.carpeta.empresa_id != perfil.empresa_id:
            raise PermissionDenied("No puedes acceder a este plano.")

    resultados = list(
        plano.resultados_validacion
        .select_related("nr_materiales_encontrado", "reclamo_encontrado")
        .all()
    )

    total_nr = len(resultados)
    total_aprobados = sum(1 for r in resultados if r.estado_resultado == "APROBADO")
    total_rechazados = sum(1 for r in resultados if r.estado_resultado == "RECHAZADO")
    total_en_verificacion = sum(1 for r in resultados if r.estado_resultado == "EN_VERIFICACION")

    contexto = {
        "app_name": "Shadow",
        "titulo_pantalla": "Detalle del plano",
        "plano": plano,
        "resultados": resultados,
        "total_nr": total_nr,
        "total_aprobados": total_aprobados,
        "total_rechazados": total_rechazados,
        "total_en_verificacion": total_en_verificacion,
    }

    if user_is_contratista(request.user):
        return render(request, "detalle_plano_contratista.html", contexto)

    return render(request, "detalle_plano.html", contexto)


@login_required
def procesar_plano_view(request, plano_id):
    require_admin_or_funcionario(request)

    plano = get_object_or_404(Plano, id=plano_id, eliminado=False)

    if request.method == "POST":
        es_reproceso = bool(plano.procesado)

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="REPROCESAR_PLANO" if es_reproceso else "INICIAR_PROCESAMIENTO_PLANO",
            descripcion=(
                f"Se solicitó reprocesar el plano {plano.id_plano_deposito}"
                if es_reproceso
                else f"Se solicitó procesar el plano {plano.id_plano_deposito}"
            ),
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        procesar_plano_completo(plano, usuario=str(request.user))

    return redirect("detalle_plano", plano_id=plano.id)


@login_required
def cancelar_plano_view(request, plano_id):
    require_admin_or_funcionario(request)

    plano = get_object_or_404(Plano, id=plano_id, eliminado=False)

    if request.method == "POST":
        plano.eliminado = True
        plano.fecha_eliminacion = timezone.now()
        plano.save()

        Auditoria.objects.create(
            plano=plano,
            carpeta=plano.carpeta,
            usuario=str(request.user),
            accion="ELIMINAR_PLANO",
            descripcion=f"Eliminación lógica del plano {plano.id_plano_deposito}",
            entidad="Plano",
            entidad_id=str(plano.id),
        )

        return redirect("detalle_carpeta", carpeta_id=plano.carpeta.id)

    return redirect("detalle_plano", plano_id=plano.id)


@login_required
@user_passes_test(user_is_admin)
def gestion_usuarios_view(request):
    usuarios = User.objects.select_related("perfil").all().order_by("username")

    return render(request, "gestion_usuarios/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Gestión de usuarios",
        "usuarios": usuarios,
    })


@login_required
@user_passes_test(user_is_admin)
def crear_usuario_view(request):
    if request.method == "POST":
        form = UsuarioCrearForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Usuario creado correctamente.")
            return redirect("gestion_usuarios")
    else:
        form = UsuarioCrearForm()

    return render(request, "gestion_usuarios/form.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Crear usuario",
        "form": form,
        "modo": "crear",
    })


@login_required
@user_passes_test(user_is_admin)
def editar_usuario_view(request, user_id):
    usuario = get_object_or_404(User, pk=user_id)

    if request.method == "POST":
        form = UsuarioEditarForm(request.POST, user_instance=usuario)
        if form.is_valid():
            form.save()
            messages.success(request, "Usuario actualizado correctamente.")
            return redirect("gestion_usuarios")
    else:
        form = UsuarioEditarForm(user_instance=usuario)

    return render(request, "gestion_usuarios/form.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Editar usuario",
        "form": form,
        "modo": "editar",
        "usuario_obj": usuario,
    })


@login_required
@user_passes_test(user_is_admin)
def toggle_usuario_activo_view(request, user_id):
    usuario = get_object_or_404(User, pk=user_id)

    if usuario == request.user:
        messages.warning(request, "No puedes desactivar tu propio usuario.")
        return redirect("gestion_usuarios")

    usuario.is_active = not usuario.is_active
    usuario.save()

    perfil, _ = PerfilUsuario.objects.get_or_create(user=usuario)
    perfil.activo = usuario.is_active
    perfil.save()

    estado = "activado" if usuario.is_active else "desactivado"
    messages.success(request, f"Usuario {estado} correctamente.")

    return redirect("gestion_usuarios")


@login_required
@user_passes_test(user_is_admin)
def empresas_contratistas_view(request):
    q = request.GET.get("q", "").strip()

    empresas = EmpresaContratista.objects.all()

    if q:
        empresas = empresas.filter(
            Q(nombre__icontains=q) |
            Q(ruc__icontains=q)
        )

    empresas = empresas.order_by("-activo", "nombre")

    total_empresas = empresas.count()
    activas = empresas.filter(activo=True).count()

    return render(request, "empresas_contratistas/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Empresas contratistas",
        "empresas": empresas,
        "filtros": {"q": q},
        "total_empresas": total_empresas,
        "empresas_activas": activas,
    })

@login_required
def auditoria_view(request):
    require_admin_or_funcionario(request)

    query = request.GET.get("q", "").strip()
    accion = request.GET.get("accion", "").strip()
    entidad = request.GET.get("entidad", "").strip()
    fecha_desde = request.GET.get("fecha_desde", "").strip()
    fecha_hasta = request.GET.get("fecha_hasta", "").strip()

    auditorias = Auditoria.objects.all().order_by("-fecha")

    if query:
        auditorias = auditorias.filter(
            Q(usuario__icontains=query) |
            Q(accion__icontains=query) |
            Q(descripcion__icontains=query) |
            Q(entidad__icontains=query) |
            Q(entidad_id__icontains=query)
        )

    if accion:
        auditorias = auditorias.filter(accion=accion)

    if entidad:
        auditorias = auditorias.filter(entidad=entidad)

    if fecha_desde:
        auditorias = auditorias.filter(fecha__date__gte=fecha_desde)

    if fecha_hasta:
        auditorias = auditorias.filter(fecha__date__lte=fecha_hasta)

    acciones = (
        Auditoria.objects
        .exclude(accion__isnull=True)
        .exclude(accion__exact="")
        .values_list("accion", flat=True)
        .distinct()
        .order_by("accion")
    )

    entidades = (
        Auditoria.objects
        .exclude(entidad__isnull=True)
        .exclude(entidad__exact="")
        .values_list("entidad", flat=True)
        .distinct()
        .order_by("entidad")
    )

    return render(request, "auditoria/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Auditoría",
        "auditorias": auditorias,
        "acciones": acciones,
        "entidades": entidades,
        "filtros": {
            "q": query,
            "accion": accion,
            "entidad": entidad,
            "fecha_desde": fecha_desde,
            "fecha_hasta": fecha_hasta,
        },
    })

@login_required
def estadisticas_view(request):
    require_admin_or_funcionario(request)

    total_carpetas = Carpeta.objects.filter(eliminada=False).count()
    total_planos = Plano.objects.filter(eliminado=False).count()
    total_planos_procesados = Plano.objects.filter(eliminado=False, procesado=True).count()
    total_planos_pendientes = Plano.objects.filter(eliminado=False, procesado=False).count()
    total_usuarios_activos = User.objects.filter(is_active=True).count()

    carpetas_por_estado_qs = (
        Carpeta.objects
        .filter(eliminada=False)
        .values("estado")
        .order_by("estado")
        .annotate(total=Count("id"))
    )

    planos_por_estado_qs = (
        Plano.objects
        .filter(eliminado=False)
        .values("estado")
        .order_by("estado")
        .annotate(total=Count("id"))
    )

    usuarios_por_rol_qs = (
        PerfilUsuario.objects
        .values("rol")
        .order_by("rol")
        .annotate(total=Count("id"))
    )

    planos_por_empresa_qs = (
        Plano.objects
        .filter(eliminado=False)
        .values("carpeta__empresa__nombre")
        .annotate(total=Count("id"))
        .order_by("-total", "carpeta__empresa__nombre")
    )

    planos_por_empresa = []
    for item in planos_por_empresa_qs:
        nombre_empresa = item.get("carpeta__empresa__nombre") or "Sin empresa"
        planos_por_empresa.append({
            "empresa": nombre_empresa,
            "total": item["total"],
        })

    return render(request, "estadisticas/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Estadísticas",
        "resumen": {
            "total_carpetas": total_carpetas,
            "total_planos": total_planos,
            "total_planos_procesados": total_planos_procesados,
            "total_planos_pendientes": total_planos_pendientes,
            "total_usuarios_activos": total_usuarios_activos,
        },
        "carpetas_por_estado": carpetas_por_estado_qs,
        "planos_por_estado": planos_por_estado_qs,
        "usuarios_por_rol": usuarios_por_rol_qs,
        "planos_por_empresa": planos_por_empresa,
        "estadisticas_v2_pendiente": True,
    })

@login_required
def reportes_view(request):
    require_admin_or_funcionario(request)

    tab = request.GET.get("tab", "carpetas").strip() or "carpetas"

    # Filtros carpetas
    carpeta_q = request.GET.get("carpeta_q", "").strip()
    carpeta_estado = request.GET.get("carpeta_estado", "").strip()
    carpeta_empresa = request.GET.get("carpeta_empresa", "").strip()
    carpeta_mes = request.GET.get("carpeta_mes", "").strip()
    carpeta_anio = request.GET.get("carpeta_anio", "").strip()

    # Filtros planos
    plano_q = request.GET.get("plano_q", "").strip()
    plano_estado = request.GET.get("plano_estado", "").strip()
    plano_empresa = request.GET.get("plano_empresa", "").strip()
    plano_procesado = request.GET.get("plano_procesado", "").strip()

    carpetas = Carpeta.objects.filter(eliminada=False).select_related("empresa").order_by("-fecha_creacion")
    if carpeta_q:
        carpetas = carpetas.filter(codigo_carpeta__icontains=carpeta_q)
    if carpeta_estado:
        carpetas = carpetas.filter(estado=carpeta_estado)
    if carpeta_empresa:
        carpetas = carpetas.filter(empresa_id=carpeta_empresa)
    if carpeta_mes:
        carpetas = carpetas.filter(mes=carpeta_mes)
    if carpeta_anio:
        carpetas = carpetas.filter(anio=carpeta_anio)

    planos = Plano.objects.filter(eliminado=False).select_related("carpeta", "carpeta__empresa").order_by("-fecha_carga")
    if plano_q:
        planos = planos.filter(
            Q(id_plano_deposito__icontains=plano_q) |
            Q(carpeta__codigo_carpeta__icontains=plano_q)
        )
    if plano_estado:
        planos = planos.filter(estado=plano_estado)
    if plano_empresa:
        planos = planos.filter(carpeta__empresa_id=plano_empresa)
    if plano_procesado == "si":
        planos = planos.filter(procesado=True)
    elif plano_procesado == "no":
        planos = planos.filter(procesado=False)

    empresas = EmpresaContratista.objects.all().order_by("nombre")
    carpeta_estado_choices = Carpeta.ESTADO_CHOICES
    plano_estado_choices = Plano.ESTADO_CHOICES

    return render(request, "reportes/listado.html", {
        "app_name": "Shadow",
        "titulo_pantalla": "Reportes",
        "tab": tab,
        "carpetas": carpetas,
        "planos": planos,
        "empresas": empresas,
        "carpeta_estado_choices": carpeta_estado_choices,
        "plano_estado_choices": plano_estado_choices,
        "filtros": {
            "carpeta_q": carpeta_q,
            "carpeta_estado": carpeta_estado,
            "carpeta_empresa": carpeta_empresa,
            "carpeta_mes": carpeta_mes,
            "carpeta_anio": carpeta_anio,
            "plano_q": plano_q,
            "plano_estado": plano_estado,
            "plano_empresa": plano_empresa,
            "plano_procesado": plano_procesado,
        }
    })

