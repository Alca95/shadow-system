from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render, get_object_or_404

from .models import Carpeta, Plano
from .services import procesar_plano_completo


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    error = None

    if request.method == "POST":
        username = request.POST.get("username")
        password = request.POST.get("password")

        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect("dashboard")
        else:
            error = "Usuario o contraseña incorrectos."

    return render(request, "login.html", {
        "error": error
    })


@login_required
def dashboard_view(request):
    return render(request, "dashboard.html", {
        "app_name": "Shadow",
    })


def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def subir_plano_view(request):
    carpetas = Carpeta.objects.all().order_by("-anio", "-mes", "empresa")
    error = None

    if request.method == "POST":
        carpeta_id = request.POST.get("carpeta")
        id_plano_deposito = request.POST.get("id_plano_deposito")
        archivo = request.FILES.get("archivo")

        if not carpeta_id or not id_plano_deposito or not archivo:
            error = "Debes completar carpeta, ID del plano y archivo."
        else:
            try:
                carpeta = Carpeta.objects.get(id=carpeta_id)

                plano = Plano.objects.create(
                    carpeta=carpeta,
                    id_plano_deposito=id_plano_deposito,
                    archivo=archivo,
                    estado="EN_REVISION",
                )

                return redirect("detalle_plano", plano_id=plano.id)

            except Carpeta.DoesNotExist:
                error = "La carpeta seleccionada no existe."
            except Exception as e:
                error = f"No se pudo guardar el plano: {e}"

    return render(request, "subir_plano.html", {
        "app_name": "Shadow",
        "carpetas": carpetas,
        "error": error,
    })


@login_required
def detalle_plano_view(request, plano_id):
    plano = get_object_or_404(Plano, id=plano_id)

    return render(request, "detalle_plano.html", {
        "app_name": "Shadow",
        "plano": plano,
    })


@login_required
def procesar_plano_view(request, plano_id):
    plano = get_object_or_404(Plano, id=plano_id)

    if request.method == "POST":
        procesar_plano_completo(plano, usuario=str(request.user))

    return redirect("detalle_plano", plano_id=plano.id)


@login_required
def cancelar_plano_view(request, plano_id):
    plano = get_object_or_404(Plano, id=plano_id)

    if request.method == "POST":
        plano.delete()
        return redirect("subir_plano")

    return redirect("detalle_plano", plano_id=plano.id)