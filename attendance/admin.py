from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth import get_user_model
from .models import Bus, Student, FeePayment, ClockInRecord, SchoolClass, UserProfile

User = get_user_model()


class UserProfileInline(admin.StackedInline):
    model = UserProfile
    can_delete = False
    verbose_name_plural = 'Profile & Permissions'
    fk_name = 'user'
    fields = ('role', 'assigned_class', 'assigned_bus', 'can_collect_fees', 'phone_number')


# Unregister default User admin and re-register with UserProfile inline
try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class CustomUserAdmin(BaseUserAdmin):
    inlines = (UserProfileInline,)
    list_display = ('username', 'first_name', 'last_name', 'email', 'get_role', 'is_staff', 'is_superuser', 'is_active')
    list_filter = ('is_active', 'is_staff', 'is_superuser', 'profile__role')

    def get_role(self, instance):
        if hasattr(instance, 'profile') and instance.profile:
            return instance.profile.get_role_display()
        return '-'
    get_role.short_description = 'School Role'


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'assigned_class', 'assigned_bus', 'can_collect_fees', 'phone_number')
    list_filter = ('role', 'can_collect_fees', 'assigned_class')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'phone_number')


@admin.register(SchoolClass)
class SchoolClassAdmin(admin.ModelAdmin):
    list_display = ('name', 'grade_level', 'student_count', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'grade_level')


@admin.register(Bus)
class BusAdmin(admin.ModelAdmin):
    list_display = ('name', 'fee', 'route_description', 'driver_name', 'driver_phone', 'plate_number', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'route_description', 'driver_name', 'plate_number')


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('first_name', 'last_name', 'student_class', 'bus', 'canteen_enrolled', 'is_active')
    list_filter = ('student_class', 'bus', 'canteen_enrolled', 'is_active')
    search_fields = ('first_name', 'last_name', 'student_class')


@admin.register(FeePayment)
class FeePaymentAdmin(admin.ModelAdmin):
    list_display = ('student', 'fee_type', 'amount', 'period', 'payment_date', 'payment_method', 'receipt_number')
    list_filter = ('fee_type', 'period', 'payment_method', 'payment_date')
    search_fields = ('student__first_name', 'student__last_name', 'receipt_number')


@admin.register(ClockInRecord)
class ClockInRecordAdmin(admin.ModelAdmin):
    list_display = ('student', 'date', 'clock_in_time', 'status', 'recorded_by')
    list_filter = ('date', 'status')
    search_fields = ('student__first_name', 'student__last_name')

