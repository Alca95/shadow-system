from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render, get_object_or_404
from django.utils import timezone

from .models import Carpeta, EmpresaContratista, Plano, Auditoria, PerfilUsuario
from .services import procesar_plano_completo


def get_or_create_perfil(user):
    if user.is_superuser:
        return None
    perfil, _ = PerfilUsuario.objects.get_or_create(user=user)
    return perfil


def user_is_admin(user):
    return user.is_superuser


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