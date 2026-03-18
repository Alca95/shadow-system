from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import PerfilUsuario, EmpresaContratista


class UsuarioBaseForm(forms.Form):
    username = forms.CharField(
        label="Nombre de usuario",
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ingrese el nombre de usuario"
        })
    )
    first_name = forms.CharField(
        label="Nombre",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ingrese el nombre"
        })
    )
    last_name = forms.CharField(
        label="Apellido",
        max_length=150,
        required=False,
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Ingrese el apellido"
        })
    )
    email = forms.EmailField(
        label="Correo electrónico",
        required=False,
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "placeholder": "Ingrese el correo electrónico"
        })
    )
    rol = forms.ChoiceField(
        label="Rol",
        choices=PerfilUsuario.ROLES,
        widget=forms.Select(attrs={
            "class": "form-select"
        })
    )
    empresa = forms.ModelChoiceField(
        label="Empresa contratista",
        queryset=EmpresaContratista.objects.filter(activo=True).order_by("nombre"),
        required=False,
        empty_label="Seleccione una empresa",
        widget=forms.Select(attrs={
            "class": "form-select"
        })
    )
    activo = forms.BooleanField(
        label="Usuario activo",
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={
            "class": "form-check-input"
        })
    )

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if not username:
            raise forms.ValidationError("El nombre de usuario es obligatorio.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip()
        return email

    def clean(self):
        cleaned_data = super().clean()
        rol = cleaned_data.get("rol")
        empresa = cleaned_data.get("empresa")

        # Si no es contratista, limpiamos empresa para evitar inconsistencias.
        if rol != "CONTRATISTA":
            cleaned_data["empresa"] = None

        # Si es contratista, la empresa puede quedar opcional por ahora,
        # respetando tu enfoque actual y evitando bloquear carga innecesariamente.
        return cleaned_data


class UsuarioCrearForm(UsuarioBaseForm):
    password1 = forms.CharField(
        label="Contraseña",
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Ingrese la contraseña"
        })
    )
    password2 = forms.CharField(
        label="Confirmar contraseña",
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Repita la contraseña"
        })
    )

    def clean_username(self):
        username = super().clean_username()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("Ya existe un usuario con ese nombre de usuario.")
        return username

    def clean_email(self):
        email = super().clean_email()
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Ya existe un usuario con ese correo electrónico.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "Las contraseñas no coinciden.")

        if password1:
            try:
                validate_password(password1)
            except ValidationError as e:
                self.add_error("password1", e)

        return cleaned_data

    def save(self):
        user = User.objects.create_user(
            username=self.cleaned_data["username"],
            password=self.cleaned_data["password1"],
            first_name=self.cleaned_data.get("first_name", ""),
            last_name=self.cleaned_data.get("last_name", ""),
            email=self.cleaned_data.get("email", ""),
            is_active=self.cleaned_data.get("activo", True),
        )

        perfil, _ = PerfilUsuario.objects.get_or_create(user=user)
        perfil.rol = self.cleaned_data["rol"]
        perfil.empresa = self.cleaned_data.get("empresa")
        perfil.activo = self.cleaned_data.get("activo", True)
        perfil.save()

        return user


class UsuarioEditarForm(UsuarioBaseForm):
    def __init__(self, *args, **kwargs):
        self.user_instance = kwargs.pop("user_instance", None)
        super().__init__(*args, **kwargs)

        if self.user_instance:
            perfil = getattr(self.user_instance, "perfil", None)

            self.fields["username"].initial = self.user_instance.username
            self.fields["first_name"].initial = self.user_instance.first_name
            self.fields["last_name"].initial = self.user_instance.last_name
            self.fields["email"].initial = self.user_instance.email
            self.fields["activo"].initial = self.user_instance.is_active

            if perfil:
                self.fields["rol"].initial = perfil.rol
                self.fields["empresa"].initial = perfil.empresa
                self.fields["activo"].initial = perfil.activo

    def clean_username(self):
        username = super().clean_username()
        qs = User.objects.filter(username__iexact=username)
        if self.user_instance:
            qs = qs.exclude(pk=self.user_instance.pk)
        if qs.exists():
            raise forms.ValidationError("Ya existe un usuario con ese nombre de usuario.")
        return username

    def clean_email(self):
        email = super().clean_email()
        if email:
            qs = User.objects.filter(email__iexact=email)
            if self.user_instance:
                qs = qs.exclude(pk=self.user_instance.pk)
            if qs.exists():
                raise forms.ValidationError("Ya existe un usuario con ese correo electrónico.")
        return email

    def save(self):
        user = self.user_instance
        user.username = self.cleaned_data["username"]
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        user.email = self.cleaned_data.get("email", "")
        user.is_active = self.cleaned_data.get("activo", True)
        user.save()

        perfil, _ = PerfilUsuario.objects.get_or_create(user=user)
        perfil.rol = self.cleaned_data["rol"]
        perfil.empresa = self.cleaned_data.get("empresa")
        perfil.activo = self.cleaned_data.get("activo", True)
        perfil.save()

        return user