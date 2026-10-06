from decimal import Decimal
from django import forms
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from .models import Bus, Student, FeePayment, ClockInRecord, SchoolClass, UserProfile

class BusForm(forms.ModelForm):
    class Meta:
        model = Bus
        fields = ['name', 'fee', 'route_description', 'driver_name', 'driver_phone', 'plate_number', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': "e.g. 'Tema Bus', 'Madina Bus'"}),
            'fee': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.50', 'min': '0'}),
            'route_description': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. Comm 1 to Comm 25, via Highway'}),
            'driver_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Driver full name'}),
            'driver_phone': forms.TextInput(attrs={'class': 'form-input', 'placeholder': '+233 24 000 0000'}),
            'plate_number': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. GT-4920-24'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }


class StudentForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = ['first_name', 'last_name', 'student_class', 'bus', 'canteen_enrolled', 'custom_canteen_fee', 'is_active']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'First Name'}),
            'last_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Last Name'}),
            'student_class': forms.TextInput(attrs={'class': 'form-input', 'placeholder': "e.g. 'Class 1A', 'Class 2B', 'Nursery 1'"}),
            'bus': forms.Select(attrs={'class': 'form-select'}),
            'canteen_enrolled': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
            'custom_canteen_fee': forms.NumberInput(attrs={'class': 'form-input', 'step': '0.50', 'placeholder': 'Leave blank for standard fee'}),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['bus'].empty_label = "-- No Bus (Walker / Private Drop-off) --"
        self.fields['bus'].queryset = Bus.objects.filter(is_active=True)


class FeePaymentForm(forms.Form):
    payment_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'class': 'form-input', 'type': 'date', 'id': 'id_payment_date'})
    )
    student_class = forms.ModelChoiceField(
        queryset=SchoolClass.objects.filter(is_active=True),
        required=False,
        empty_label="-- 1. Select Class First --",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_student_class'})
    )
    student = forms.ModelChoiceField(
        queryset=Student.objects.filter(is_active=True).order_by('student_class', 'first_name'),
        required=True,
        empty_label="-- 2. Select Student --",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_student'})
    )
    bus = forms.ModelChoiceField(
        queryset=Bus.objects.filter(is_active=True).order_by('name'),
        required=False,
        empty_label="-- No Bus (Walker) --",
        widget=forms.Select(attrs={'class': 'form-select', 'id': 'id_bus'})
    )
    bus_amount = forms.DecimalField(
        required=False,
        min_value=Decimal('0.00'),
        decimal_places=2,
        initial=Decimal('0.00'),
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.50', 'id': 'id_bus_amount', 'placeholder': '0.00'})
    )
    canteen_amount = forms.DecimalField(
        required=False,
        min_value=Decimal('0.00'),
        decimal_places=2,
        initial=Decimal('0.00'),
        widget=forms.NumberInput(attrs={'class': 'form-input', 'step': '0.50', 'id': 'id_canteen_amount', 'placeholder': '0.00'})
    )

    # Optional / hidden fields for automated defaults and backward compatibility
    fee_type = forms.CharField(required=False, widget=forms.HiddenInput())
    amount = forms.DecimalField(required=False, min_value=Decimal('0.00'), decimal_places=2, widget=forms.HiddenInput())
    period = forms.CharField(max_length=50, required=False, widget=forms.HiddenInput())
    payment_method = forms.CharField(max_length=30, required=False, widget=forms.HiddenInput())
    receipt_number = forms.CharField(max_length=50, required=False, widget=forms.HiddenInput())
    notes = forms.CharField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['student'].queryset = Student.objects.filter(is_active=True).order_by('student_class', 'first_name')
        if not self.initial.get('payment_date'):
            self.initial['payment_date'] = timezone.localdate()
        if not self.initial.get('period'):
            self.initial['period'] = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        if not self.initial.get('payment_method'):
            self.initial['payment_method'] = 'CASH'

    def clean(self):
        cleaned_data = super().clean()
        bus_amount = cleaned_data.get('bus_amount') or Decimal('0.00')
        canteen_amount = cleaned_data.get('canteen_amount') or Decimal('0.00')

        # Backward compatibility for legacy single fee submission
        legacy_fee_type = cleaned_data.get('fee_type')
        legacy_amount = cleaned_data.get('amount')
        if legacy_fee_type and legacy_amount and legacy_amount > Decimal('0.00'):
            if legacy_fee_type == 'BUS' and bus_amount == Decimal('0.00'):
                bus_amount = legacy_amount
                cleaned_data['bus_amount'] = bus_amount
            elif legacy_fee_type == 'CANTEEN' and canteen_amount == Decimal('0.00'):
                canteen_amount = legacy_amount
                cleaned_data['canteen_amount'] = canteen_amount

        if bus_amount <= Decimal('0.00') and canteen_amount <= Decimal('0.00'):
            raise forms.ValidationError("Please enter an amount greater than 0 for Bus Fee, Canteen Fee, or both.")

        # Set automatic defaults for removed fields
        if not cleaned_data.get('payment_date'):
            cleaned_data['payment_date'] = timezone.localdate()
        if not cleaned_data.get('period'):
            cleaned_data['period'] = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        if not cleaned_data.get('payment_method'):
            cleaned_data['payment_method'] = 'CASH'
        if not cleaned_data.get('notes'):
            cleaned_data['notes'] = ''

        return cleaned_data


class SchoolSettingsForm(forms.Form):
    default_canteen_fee = forms.DecimalField(
        label="Default Daily Canteen Fee",
        min_value=Decimal('0.00'),
        decimal_places=2,
        required=True,
        widget=forms.NumberInput(attrs={
            'class': 'form-input',
            'step': '0.50',
            'id': 'id_default_canteen_fee',
            'placeholder': '10.00'
        }),
        help_text="Standard daily meal plan fee charged to all enrolled students without a custom fee override."
    )
    school_name = forms.CharField(
        label="School Name",
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'id': 'id_school_name',
            'placeholder': 'e.g. Geosaka Model School'
        }),
        help_text="Name displayed on receipts, page headers, and parent notifications."
    )
    current_period = forms.CharField(
        label="Current Academic Period",
        max_length=50,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'id': 'id_current_period',
            'placeholder': 'e.g. Term 1 - 2026'
        }),
        help_text="Active academic term for ledger records and reconciliation."
    )
    currency_symbol = forms.CharField(
        label="Currency Symbol",
        max_length=10,
        required=True,
        widget=forms.TextInput(attrs={
            'class': 'form-input',
            'id': 'id_currency_symbol',
            'placeholder': 'e.g. GH₵'
        }),
        help_text="Currency symbol displayed throughout the application."
    )


class TeacherCreationForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g. kofi_mensah', 'autocomplete': 'off'})
    )
    first_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'First Name'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Last Name'})
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'teacher@school.edu'})
    )
    phone_number = forms.CharField(
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': '+233 24 000 0000'})
    )
    assigned_class = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        help_text="Class this teacher is responsible for (clock-in roster will default to this class)"
    )
    password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'Initial password', 'id': 'id_password'}),
        help_text="Initial temporary password for the teacher"
    )
    confirm_password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'Confirm password', 'id': 'id_confirm_password'})
    )
    is_active = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-checkbox'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        class_choices = [('', '-- No Specific Class (Floating Teacher) --')]
        class_names = set(SchoolClass.objects.filter(is_active=True).values_list('name', flat=True))
        class_names.update(Student.objects.filter(is_active=True).values_list('student_class', flat=True))
        for sc in sorted(class_names):
            if sc:
                class_choices.append((sc, sc))
        self.fields['assigned_class'].widget.choices = class_choices

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        User = get_user_model()
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("A user account with this username already exists.")
        return username

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        confirm_password = cleaned_data.get('confirm_password')
        if password and confirm_password and password != confirm_password:
            self.add_error('confirm_password', "Passwords do not match.")
        return cleaned_data

    def save(self):
        User = get_user_model()
        username = self.cleaned_data['username']
        password = self.cleaned_data['password']
        email = self.cleaned_data.get('email', '')
        first_name = self.cleaned_data.get('first_name', '')
        last_name = self.cleaned_data.get('last_name', '')
        is_active = self.cleaned_data.get('is_active', True)
        phone_number = self.cleaned_data.get('phone_number', '')
        assigned_class = self.cleaned_data.get('assigned_class', '')

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
            is_active=is_active,
            is_staff=True
        )
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.role = UserProfile.ROLE_TEACHER
        profile.phone_number = phone_number
        profile.assigned_class = assigned_class
        profile.save()
        return user


class TeacherUpdateForm(forms.Form):
    first_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'First Name'})
    )
    last_name = forms.CharField(
        max_length=150,
        required=True,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Last Name'})
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={'class': 'form-input', 'placeholder': 'teacher@school.edu'})
    )
    phone_number = forms.CharField(
        max_length=30,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-input', 'placeholder': '+233 24 000 0000'})
    )
    assigned_class = forms.CharField(
        max_length=50,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        help_text="Class this teacher is responsible for"
    )
    is_active = forms.BooleanField(
        required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-checkbox'})
    )

    def __init__(self, *args, user_obj=None, **kwargs):
        self.user_obj = user_obj
        super().__init__(*args, **kwargs)
        class_choices = [('', '-- No Specific Class (Floating Teacher) --')]
        class_names = set(SchoolClass.objects.filter(is_active=True).values_list('name', flat=True))
        class_names.update(Student.objects.filter(is_active=True).values_list('student_class', flat=True))
        if user_obj and hasattr(user_obj, 'profile') and user_obj.profile.assigned_class:
            class_names.add(user_obj.profile.assigned_class)
        for sc in sorted(class_names):
            if sc:
                class_choices.append((sc, sc))
        self.fields['assigned_class'].widget.choices = class_choices

    def save(self):
        user = self.user_obj
        user.first_name = self.cleaned_data['first_name']
        user.last_name = self.cleaned_data['last_name']
        user.email = self.cleaned_data.get('email', '')
        user.is_active = self.cleaned_data.get('is_active', True)
        user.save()

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.phone_number = self.cleaned_data.get('phone_number', '')
        profile.assigned_class = self.cleaned_data.get('assigned_class', '')
        profile.save()
        return user


class TeacherPasswordResetForm(forms.Form):
    new_password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'New password', 'id': 'id_new_password'}),
        help_text="Enter a new password for this teacher"
    )
    confirm_password = forms.CharField(
        required=True,
        widget=forms.PasswordInput(attrs={'class': 'form-input', 'placeholder': 'Confirm new password', 'id': 'id_confirm_password'})
    )

    def __init__(self, *args, user_obj=None, **kwargs):
        self.user_obj = user_obj
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        p1 = cleaned_data.get('new_password')
        p2 = cleaned_data.get('confirm_password')
        if p1 and p2 and p1 != p2:
            self.add_error('confirm_password', "Passwords do not match.")
        return cleaned_data

    def save(self):
        user = self.user_obj
        user.set_password(self.cleaned_data['new_password'])
        user.save()
        return user

