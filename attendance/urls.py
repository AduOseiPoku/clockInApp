from django.urls import path
from . import views

urlpatterns = [
    # Authentication
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # Dashboard & Teacher Clock-In
    path('', views.dashboard_view, name='dashboard'),
    
    # AJAX APIs for instant clock-in & quick payment
    path('api/clock-in/toggle/', views.toggle_clock_in_api, name='api_clock_in_toggle'),
    path('api/payment/quick/', views.quick_payment_api, name='api_quick_payment'),

    # Bus Management
    path('buses/', views.bus_list_view, name='bus_list'),
    path('buses/add/', views.bus_create_view, name='bus_create'),
    path('buses/<int:pk>/edit/', views.bus_update_view, name='bus_update'),
    path('buses/<int:pk>/delete/', views.bus_delete_view, name='bus_delete'),

    # Student Roster & Bulk CSV Import
    path('students/', views.student_list_view, name='student_list'),
    path('students/add/', views.student_create_view, name='student_create'),
    path('students/import/', views.student_import_view, name='student_import'),
    path('students/template/', views.student_template_download, name='student_template_download'),
    path('students/<int:pk>/edit/', views.student_update_view, name='student_update'),
    path('students/<int:pk>/delete/', views.student_delete_view, name='student_delete'),

    # Payments Ledger & Printable Receipts
    path('payments/', views.payment_list_view, name='payment_list'),
    path('payments/record/', views.payment_create_view, name='payment_create'),
    path('payments/<int:pk>/receipt/', views.payment_receipt_view, name='payment_receipt'),
    path('payments/<int:pk>/delete/', views.payment_delete_view, name='payment_delete'),

    # Reports & Reconciliation & CSV Exports
    path('reports/', views.reports_view, name='reports'),
    path('reports/export/attendance/', views.export_attendance_csv, name='export_attendance_csv'),
    path('reports/export/debtors/', views.export_debtors_csv, name='export_debtors_csv'),

    # Parent Notifications Audit Trail
    path('notifications/', views.notifications_view, name='notifications'),

    # Teacher & Staff Account Management (Principal Only)
    path('teachers/', views.teacher_list_view, name='teacher_list'),
    path('teachers/add/', views.teacher_create_view, name='teacher_create'),
    path('teachers/<int:pk>/edit/', views.teacher_update_view, name='teacher_update'),
    path('teachers/<int:pk>/reset-password/', views.teacher_password_reset_view, name='teacher_password_reset'),
    path('teachers/<int:pk>/toggle-status/', views.teacher_toggle_status_view, name='teacher_toggle_status'),

    # System & School Billing Settings
    path('settings/', views.school_settings_view, name='school_settings'),
]
