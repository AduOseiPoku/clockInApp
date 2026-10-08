import datetime
from django.db import models
from django.conf import settings
from django.utils import timezone
from django.db.models import Sum
from decimal import Decimal


def count_school_days_between(start_date, end_date):
    """
    Counts school days (Monday to Friday) strictly in range [start_date, end_date).
    Does not include end_date itself.
    """
    if not start_date or not end_date or start_date >= end_date:
        return 0
    days = 0
    cur = start_date
    while cur < end_date:
        if cur.weekday() < 5:
            days += 1
        cur += datetime.timedelta(days=1)
    return days


class Bus(models.Model):
    """
    Dynamic Bus/Route Model.
    Each bus has its own configurable fee rate set by the administrator.
    Example: 'Tema Bus' (GH₵80.00), 'Madina Bus' (GH₵50.00).
    """
    name = models.CharField(max_length=100, unique=True, help_text="e.g. 'Tema Bus', 'Madina Bus', 'Spintex Bus'")
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'), help_text="Fee charged for this bus route")
    route_description = models.CharField(max_length=255, blank=True, help_text="Major stops or pickup zones")
    driver_name = models.CharField(max_length=100, blank=True)
    driver_phone = models.CharField(max_length=30, blank=True)
    plate_number = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Bus'
        verbose_name_plural = 'Buses'
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')}{self.fee:.2f})"

    @property
    def student_count(self):
        return self.students.filter(is_active=True).count()

    @property
    def total_expected_revenue(self):
        return Decimal(self.student_count) * self.fee


class SchoolClass(models.Model):
    """
    School Class model managed by school administrators.
    e.g. 'Class 1A', 'Class 2B', 'Nursery 1', 'Grade 3'.
    """
    name = models.CharField(max_length=50, unique=True, help_text="e.g. 'Class 1A', 'Class 2B', 'Nursery 1'")
    grade_level = models.CharField(max_length=50, blank=True, help_text="Optional level group (e.g. Primary, JHS, Kindergarten)")
    is_active = models.BooleanField(default=True, help_text="Active classes appear in fee recording and attendance")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Class'
        verbose_name_plural = 'Classes'
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def student_count(self):
        return self.students.filter(is_active=True).count()


class SchoolSetting(models.Model):
    """
    Key-value dynamic system configuration for school settings
    (e.g., DEFAULT_CANTEEN_FEE, CURRENT_ACADEMIC_PERIOD, SCHOOL_NAME, CURRENCY_SYMBOL).
    Enables administrators to adjust billing rates and school information directly from the web UI.
    """
    key = models.CharField(max_length=60, unique=True, help_text="Configuration key name")
    value = models.CharField(max_length=255, help_text="Configuration value")
    description = models.CharField(max_length=255, blank=True, help_text="Short explanation of what this controls")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'School Setting'
        verbose_name_plural = 'School Settings'
        ordering = ['key']

    def __str__(self):
        return f"{self.key} = {self.value}"

    @classmethod
    def get_setting(cls, key, default=None):
        try:
            item = cls.objects.filter(key=key).first()
            if item and item.value is not None:
                return item.value
        except Exception:
            pass
        return default

    @classmethod
    def set_setting(cls, key, value, description=""):
        cls.objects.update_or_create(
            key=key,
            defaults={'value': str(value), 'description': description}
        )


def get_default_canteen_fee():
    """Returns active default daily canteen fee (Decimal)."""
    val = SchoolSetting.get_setting('DEFAULT_CANTEEN_FEE')
    if val is not None:
        try:
            return Decimal(str(val))
        except Exception:
            pass
    return getattr(settings, 'DEFAULT_CANTEEN_FEE', Decimal('10.00'))


class Student(models.Model):
    """
    Student model.
    Enrolled with first name, last name, and class (no student ID required).
    Assigns each student to their designated bus or leaves unassigned (for walkers).
    """
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    student_class = models.CharField(max_length=50, help_text="e.g. 'Class 1A', 'Class 2B', 'Nursery 1'")
    school_class = models.ForeignKey(SchoolClass, on_delete=models.SET_NULL, null=True, blank=True, related_name='students')
    bus = models.ForeignKey(Bus, on_delete=models.SET_NULL, null=True, blank=True, related_name='students')
    canteen_enrolled = models.BooleanField(default=True, help_text="Whether student takes school lunch/canteen")
    custom_canteen_fee = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Override standard canteen fee")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.school_class:
            self.student_class = self.school_class.name
        elif self.student_class:
            cls_obj, _ = SchoolClass.objects.get_or_create(name=self.student_class)
            self.school_class = cls_obj
        super().save(*args, **kwargs)

    class Meta:
        verbose_name = 'Student'
        verbose_name_plural = 'Students'
        ordering = ['student_class', 'first_name', 'last_name']

    def __str__(self):
        return f"{self.full_name} ({self.student_class})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def initials(self):
        f = self.first_name[0].upper() if self.first_name else ''
        l = self.last_name[0].upper() if self.last_name else ''
        return f"{f}{l}" or "ST"

    @property
    def bus_fee_required(self):
        if self.bus:
            return self.bus.fee
        return Decimal('0.00')

    @property
    def canteen_fee_required(self):
        if not self.canteen_enrolled:
            return Decimal('0.00')
        if self.custom_canteen_fee is not None:
            return self.custom_canteen_fee
        return get_default_canteen_fee()

    def get_daily_bus_paid(self, target_date=None):
        """Total bus payments recorded on target_date."""
        if target_date is None:
            target_date = timezone.localdate()
        result = self.payments.filter(fee_type='BUS', payment_date=target_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_daily_canteen_paid(self, target_date=None):
        """Total canteen payments recorded on target_date."""
        if target_date is None:
            target_date = timezone.localdate()
        result = self.payments.filter(fee_type='CANTEEN', payment_date=target_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_bus_credit_balance(self, target_date=None):
        """
        Available prepaid credit on target_date before covering target_date.
        Calculates cumulative bus payments made up to target_date minus cost of past days attended/elapsed.
        """
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        payments_qs = self.payments.filter(fee_type='BUS', payment_date__lte=target_date)
        total_paid = payments_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        if total_paid <= Decimal('0.00'):
            return Decimal('0.00')

        earliest_p = payments_qs.order_by('payment_date').first()
        start_date = earliest_p.payment_date if earliest_p else target_date
        if self.clock_in_records.filter(date__gte=start_date).exists():
            past_attended_days = self.clock_in_records.filter(date__gte=start_date, date__lt=target_date).count()
        else:
            past_attended_days = count_school_days_between(start_date, target_date)
        past_cost = Decimal(past_attended_days) * self.bus_fee_required
        return max(Decimal('0.00'), total_paid - past_cost)

    def get_canteen_credit_balance(self, target_date=None):
        """
        Available prepaid canteen credit on target_date before covering target_date.
        Calculates cumulative canteen payments made up to target_date minus cost of past days attended/elapsed.
        """
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        payments_qs = self.payments.filter(fee_type='CANTEEN', payment_date__lte=target_date)
        total_paid = payments_qs.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        if total_paid <= Decimal('0.00'):
            return Decimal('0.00')

        earliest_p = payments_qs.order_by('payment_date').first()
        start_date = earliest_p.payment_date if earliest_p else target_date
        if self.clock_in_records.filter(date__gte=start_date).exists():
            past_attended_days = self.clock_in_records.filter(date__gte=start_date, date__lt=target_date).count()
        else:
            past_attended_days = count_school_days_between(start_date, target_date)
        past_cost = Decimal(past_attended_days) * self.canteen_fee_required
        return max(Decimal('0.00'), total_paid - past_cost)

    def is_bus_paid_for_date(self, target_date=None):
        """Check if bus fee is covered for target_date via direct payment or prepaid credit."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return True
        if target_date is None:
            target_date = timezone.localdate()
        if self.get_daily_bus_paid(target_date) >= self.bus_fee_required:
            return True
        return self.get_bus_credit_balance(target_date) >= self.bus_fee_required

    def is_canteen_paid_for_date(self, target_date=None):
        """Check if canteen fee is covered for target_date via direct payment or prepaid credit."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return True
        if target_date is None:
            target_date = timezone.localdate()
        if self.get_daily_canteen_paid(target_date) >= self.canteen_fee_required:
            return True
        return self.get_canteen_credit_balance(target_date) >= self.canteen_fee_required

    def get_daily_bus_balance(self, target_date=None):
        """Remaining bus fee owed for target_date."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        direct = self.get_daily_bus_paid(target_date)
        credit = self.get_bus_credit_balance(target_date)
        covered = max(direct, credit)
        return max(Decimal('0.00'), self.bus_fee_required - covered)

    def get_daily_canteen_balance(self, target_date=None):
        """Remaining canteen fee owed for target_date."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return Decimal('0.00')
        if target_date is None:
            target_date = timezone.localdate()
        direct = self.get_daily_canteen_paid(target_date)
        credit = self.get_canteen_credit_balance(target_date)
        covered = max(direct, credit)
        return max(Decimal('0.00'), self.canteen_fee_required - covered)

    def get_bus_credit_days(self, target_date=None):
        """Days of prepaid bus credit remaining after covering target_date."""
        if not self.bus or self.bus_fee_required <= Decimal('0.00'):
            return 0
        if target_date is None:
            target_date = timezone.localdate()
        credit = self.get_bus_credit_balance(target_date)
        if credit >= self.bus_fee_required:
            remaining = credit - self.bus_fee_required
            return int(remaining // self.bus_fee_required)
        return 0

    def get_canteen_credit_days(self, target_date=None):
        """Days of prepaid canteen credit remaining after covering target_date."""
        if not self.canteen_enrolled or self.canteen_fee_required <= Decimal('0.00'):
            return 0
        if target_date is None:
            target_date = timezone.localdate()
        credit = self.get_canteen_credit_balance(target_date)
        if credit >= self.canteen_fee_required:
            remaining = credit - self.canteen_fee_required
            return int(remaining // self.canteen_fee_required)
        return 0

    def get_prepaid_status(self, target_date=None):
        """
        Summary of prepaid credit and remaining days for bus and canteen on target_date.
        Includes partial credit detection and remainder top-up calculations.
        """
        if target_date is None:
            target_date = timezone.localdate()
        bus_credit = self.get_bus_credit_balance(target_date)
        bus_days = self.get_bus_credit_days(target_date)
        can_credit = self.get_canteen_credit_balance(target_date)
        can_days = self.get_canteen_credit_days(target_date)
        bus_active = bool(self.bus and bus_credit >= self.bus_fee_required)
        can_active = bool(self.canteen_enrolled and can_credit >= self.canteen_fee_required)

        bus_paid_today = self.get_daily_bus_paid(target_date)
        canteen_paid_today = self.get_daily_canteen_paid(target_date)

        bus_has_partial = bool(
            self.bus and
            not self.is_bus_paid_for_date(target_date) and
            Decimal('0.00') < bus_credit < self.bus_fee_required
        )
        bus_remainder_due = max(Decimal('0.00'), self.bus_fee_required - bus_credit - bus_paid_today) if bus_has_partial else Decimal('0.00')

        can_has_partial = bool(
            self.canteen_enrolled and
            not self.is_canteen_paid_for_date(target_date) and
            Decimal('0.00') < can_credit < self.canteen_fee_required
        )
        can_remainder_due = max(Decimal('0.00'), self.canteen_fee_required - can_credit - canteen_paid_today) if can_has_partial else Decimal('0.00')

        has_credit = bus_active or can_active or bus_has_partial or can_has_partial
        is_expiring = (bus_active and bus_days <= 1) or (can_active and can_days <= 1) or bus_has_partial or can_has_partial

        return {
            'has_credit': has_credit,
            'bus_has_credit': bus_active,
            'bus_credit_balance': bus_credit,
            'bus_credit_days': bus_days,
            'bus_has_partial': bus_has_partial,
            'bus_remainder_due': bus_remainder_due,
            'canteen_has_credit': can_active,
            'canteen_credit_balance': can_credit,
            'canteen_credit_days': can_days,
            'canteen_has_partial': can_has_partial,
            'canteen_remainder_due': can_remainder_due,
            'is_expiring_soon': is_expiring,
        }

    def get_bus_paid_amount(self, period_or_date=None):
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_bus_paid(period_or_date)
        if period_or_date is None:
            return self.get_daily_bus_paid()
        result = self.payments.filter(fee_type='BUS', period=period_or_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def get_canteen_paid_amount(self, period_or_date=None):
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_canteen_paid(period_or_date)
        if period_or_date is None:
            return self.get_daily_canteen_paid()
        result = self.payments.filter(fee_type='CANTEEN', period=period_or_date).aggregate(total=Sum('amount'))['total']
        return result or Decimal('0.00')

    def is_bus_paid(self, period_or_date=None):
        if not self.bus:
            return True  # Walkers have no bus fee
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.is_bus_paid_for_date(period_or_date)
        if period_or_date is None:
            return self.is_bus_paid_for_date()
        return self.is_bus_paid_for_date() or (self.get_bus_paid_amount(period_or_date) >= self.bus_fee_required)

    def is_canteen_paid(self, period_or_date=None):
        if not self.canteen_enrolled:
            return True  # Opted out of canteen
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.is_canteen_paid_for_date(period_or_date)
        if period_or_date is None:
            return self.is_canteen_paid_for_date()
        return self.is_canteen_paid_for_date() or (self.get_canteen_paid_amount(period_or_date) >= self.canteen_fee_required)

    def get_bus_balance(self, period_or_date=None):
        if not self.bus:
            return Decimal('0.00')
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_bus_balance(period_or_date)
        if period_or_date is None:
            return self.get_daily_bus_balance()
        if self.is_bus_paid(period_or_date):
            return Decimal('0.00')
        balance = self.bus_fee_required - self.get_bus_paid_amount(period_or_date)
        return max(Decimal('0.00'), balance)

    def get_canteen_balance(self, period_or_date=None):
        if not self.canteen_enrolled:
            return Decimal('0.00')
        if isinstance(period_or_date, (datetime.date, datetime.datetime)):
            return self.get_daily_canteen_balance(period_or_date)
        if period_or_date is None:
            return self.get_daily_canteen_balance()
        if self.is_canteen_paid(period_or_date):
            return Decimal('0.00')
        balance = self.canteen_fee_required - self.get_canteen_paid_amount(period_or_date)
        return max(Decimal('0.00'), balance)

    def get_today_clock_in(self, target_date=None):
        if target_date is None:
            target_date = timezone.localdate()
        return self.clock_in_records.filter(date=target_date).first()


class FeePayment(models.Model):
    """
    Record of bus or canteen fee payment made for a student.
    """
    FEE_TYPES = [
        ('BUS', 'Bus Fee'),
        ('CANTEEN', 'Canteen Fee'),
    ]

    PAYMENT_METHODS = [
        ('CASH', 'Cash'),
        ('MOBILE_MONEY', 'Mobile Money'),
        ('BANK_TRANSFER', 'Bank Transfer'),
        ('CHEQUE', 'Cheque'),
        ('CARD', 'Card'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='payments')
    bus = models.ForeignKey('Bus', on_delete=models.SET_NULL, null=True, blank=True, related_name='payments', help_text="Specific bus route this fee payment was collected on")
    is_temporary_bus = models.BooleanField(default=False, help_text="True if recorded for a one-off/guest bus ride different from student regular route")
    is_advance = models.BooleanField(default=False, help_text="True if recorded as advance payment covering future days")
    days_covered = models.PositiveIntegerField(default=1, help_text="Number of school days this payment is intended to cover")
    fee_type = models.CharField(max_length=20, choices=FEE_TYPES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateField(default=timezone.localdate)
    period = models.CharField(max_length=50, default='Term 1 - 2026', help_text="e.g. 'Term 1 - 2026'")
    payment_method = models.CharField(max_length=30, choices=PAYMENT_METHODS, default='CASH')
    receipt_number = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Fee Payment'
        verbose_name_plural = 'Fee Payments'
        ordering = ['-payment_date', '-created_at']

    def __str__(self):
        return f"{self.student.full_name} - {self.get_fee_type_display()} - {getattr(settings, 'CURRENCY_SYMBOL', 'GH₵')}{self.amount:.2f}"


class ClockInRecord(models.Model):
    """
    Daily Student Clock-In Record.
    Stamps student arrival time for morning attendance.
    Enforces uniqueness per student per day.
    """
    STATUS_CHOICES = [
        ('PRESENT', 'Present'),
        ('LATE', 'Late'),
        ('EXCUSED', 'Excused'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='clock_in_records')
    date = models.DateField(default=timezone.localdate)
    clock_in_time = models.TimeField(default=timezone.now)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PRESENT')
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Clock-In Record'
        verbose_name_plural = 'Clock-In Records'
        unique_together = ('student', 'date')
        ordering = ['-date', '-clock_in_time']

    def __str__(self):
        return f"{self.student.full_name} - {self.date} at {self.clock_in_time.strftime('%I:%M %p')}"


class UserProfile(models.Model):
    """
    User Profile extending Django User with specific school roles:
    ADMIN (Full Control), TEACHER (Clock-in & Roster View), ACCOUNTANT (Payments & Billing)
    """
    ROLE_ADMIN = 'ADMIN'
    ROLE_TEACHER = 'TEACHER'
    ROLE_ACCOUNTANT = 'ACCOUNTANT'

    ROLE_CHOICES = [
        (ROLE_ADMIN, 'School Administrator'),
        (ROLE_TEACHER, 'Teacher'),
        (ROLE_ACCOUNTANT, 'Accountant / Bursar'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_TEACHER)
    phone_number = models.CharField(max_length=30, blank=True)
    assigned_class = models.CharField(max_length=50, blank=True, help_text="Specific class for class teacher (optional)")
    assigned_bus = models.ForeignKey('Bus', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_teachers', help_text="Specific bus route for bus teacher/conductor (optional)")
    can_collect_fees = models.BooleanField(
        default=False,
        help_text="Designates whether this teacher is authorized to record and collect fee payments."
    )

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_school_admin(self):
        return self.role == self.ROLE_ADMIN or self.user.is_superuser

    @property
    def is_principal(self):
        return self.is_school_admin

    @property
    def is_accountant(self):
        return self.role == self.ROLE_ACCOUNTANT or self.is_school_admin

    @property
    def is_teacher(self):
        return self.role == self.ROLE_TEACHER or self.is_school_admin

    @property
    def is_fee_teacher(self):
        return (self.role == self.ROLE_TEACHER and self.can_collect_fees) or self.is_school_admin

    @property
    def is_bus_fee_teacher(self):
        return self.is_fee_teacher and bool(self.assigned_bus)

    @property
    def fee_teacher_title(self):
        if self.assigned_bus:
            return f"Fee Teacher ({self.assigned_bus.name})"
        elif self.assigned_class:
            return f"Class Teacher ({self.assigned_class})"
        elif self.can_collect_fees:
            return "Fee Collector (Floating)"
        return "Teacher"

    @property
    def can_view_revenue(self):
        return self.role in [self.ROLE_ADMIN, self.ROLE_ACCOUNTANT] or self.user.is_superuser

    @property
    def can_record_payments(self):
        if self.is_school_admin:
            return True
        if self.role == self.ROLE_TEACHER:
            return self.can_collect_fees
        return False

    @property
    def can_manage_settings(self):
        return self.is_school_admin

    @property
    def can_manage_buses(self):
        return self.is_school_admin

    @property
    def can_delete_payments(self):
        return self.is_school_admin


class NotificationLog(models.Model):
    """
    Log of parent notifications (arrival SMS/WhatsApp alerts and fee reminders).
    """
    TYPE_CLOCK_IN = 'CLOCK_IN'
    TYPE_FEE_REMINDER = 'FEE_REMINDER'

    TYPE_CHOICES = [
        (TYPE_CLOCK_IN, 'Arrival / Clock-In SMS'),
        (TYPE_FEE_REMINDER, 'Fee Arrears Reminder'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='notifications')
    notification_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    recipient = models.CharField(max_length=100, default='Parent / Guardian')
    phone_number = models.CharField(max_length=30, blank=True)
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='SENT')

    class Meta:
        verbose_name = 'Notification Log'
        verbose_name_plural = 'Notification Logs'
        ordering = ['-sent_at']

    def __str__(self):
        return f"{self.get_notification_type_display()} for {self.student.full_name} ({self.sent_at.strftime('%Y-%m-%d %H:%M')})"


from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model

@receiver(post_save, sender=get_user_model())
def create_or_save_user_profile(sender, instance, created, **kwargs):
    if created:
        role = UserProfile.ROLE_ADMIN if instance.is_superuser else UserProfile.ROLE_TEACHER
        UserProfile.objects.create(user=instance, role=role)
    else:
        if hasattr(instance, 'profile'):
            if instance.is_superuser and instance.profile.role != UserProfile.ROLE_ADMIN:
                instance.profile.role = UserProfile.ROLE_ADMIN
            instance.profile.save()


