from django.contrib import admin
from .models import Bus, Student, FeePayment, ClockInRecord, SchoolClass

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
