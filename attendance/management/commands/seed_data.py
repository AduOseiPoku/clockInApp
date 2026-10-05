from django.core.management.base import BaseCommand
from django.utils import timezone
from decimal import Decimal
from datetime import time
from attendance.models import Bus, Student, FeePayment, ClockInRecord
from django.conf import settings

class Command(BaseCommand):
    help = 'Seeds realistic sample buses, students, fee payments, and clock-in records.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Seeding sample data for School Clock-In & Fee Tracking System..."))

        period = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        today = timezone.localdate()

        # 0. Create Demo Users for Roles
        from django.contrib.auth import get_user_model
        from attendance.models import UserProfile
        User = get_user_model()

        users_info = [
            ('admin', 'admin@example.com', 'admin123', UserProfile.ROLE_ADMIN, True),
            ('teacher', 'teacher@example.com', 'teacher123', UserProfile.ROLE_TEACHER, False),
            ('bursar', 'bursar@example.com', 'bursar123', UserProfile.ROLE_ACCOUNTANT, False),
        ]
        for uname, email, pwd, role, is_super in users_info:
            u, created = User.objects.get_or_create(username=uname, defaults={'email': email})
            u.set_password(pwd)
            u.is_superuser = is_super
            u.is_staff = True
            u.save()
            profile, _ = UserProfile.objects.get_or_create(user=u)
            profile.role = role
            profile.save()
            self.stdout.write(f"  [OK] User: {uname} ({role}) - Pass: {pwd}")

        # 1. Create Buses with daily route fees
        bus_configs = [
            {
                'name': 'Tema Bus',
                'fee': Decimal('8.00'),
                'route_description': 'Comm 1, Comm 7, Comm 25 via Motorway',
                'driver_name': 'Kofi Mensah',
                'driver_phone': '+233 24 111 2233',
                'plate_number': 'GN-4021-24',
            },
            {
                'name': 'Madina Bus',
                'fee': Decimal('5.00'),
                'route_description': 'Madina Zongo Junction, Atomic, Legon bypass',
                'driver_name': 'Kwame Antwi',
                'driver_phone': '+233 20 445 6677',
                'plate_number': 'GE-8902-23',
            },
            {
                'name': 'Spintex Bus',
                'fee': Decimal('7.00'),
                'route_description': 'Texpo, Kotobabi, Baatsona, Manet Junction',
                'driver_name': 'Emmanuel Osei',
                'driver_phone': '+233 27 889 0011',
                'plate_number': 'GW-1123-22',
            },
            {
                'name': 'Adenta Bus',
                'fee': Decimal('6.00'),
                'route_description': 'Adenta Barrier, Frafraha, Amrahia',
                'driver_name': 'Samuel Appiah',
                'driver_phone': '+233 55 332 1199',
                'plate_number': 'GS-7764-24',
            },
        ]

        buses = {}
        for b_data in bus_configs:
            bus, created = Bus.objects.update_or_create(
                name=b_data['name'],
                defaults=b_data
            )
            buses[bus.name] = bus
            self.stdout.write(f"  [OK] Bus: {bus.name} (GHS {bus.fee:.2f}/day)")

        # 2. Sample Students across classes (No Student ID!)
        # Format: (first, last, class, bus, canteen_enrolled, custom_canteen, bus_days_paid, canteen_days_paid, clock_in_today)
        students_data = [
            # Class 1A
            ('Kwesi', 'Arthur', 'Class 1A', buses['Tema Bus'], True, None, 5, 5, True), # 5 days prepaid advance!
            ('Akosua', 'Boateng', 'Class 1A', buses['Tema Bus'], True, None, 0, 1, True), # Bus unpaid, canteen paid 1 day
            ('Yaw', 'Danquah', 'Class 1A', buses['Madina Bus'], True, None, 1, 0, False), # 1 day bus, canteen unpaid
            ('Ama', 'Serwaa', 'Class 1A', None, True, None, 0, 1, True), # Walker, 1 day canteen paid
            ('Nana', 'Opoku', 'Class 1A', buses['Spintex Bus'], False, None, 1, 0, False), # Opted out of canteen, bus paid 1 day

            # Class 1B
            ('Kofi', 'Adu', 'Class 1B', buses['Madina Bus'], True, None, 5, 10, True), # 5-day bus, 10-day canteen prepaid!
            ('Efua', 'Sutherland', 'Class 1B', buses['Tema Bus'], True, None, 0, 0, True), # Both unpaid, clocked in
            ('Esi', 'Badu', 'Class 1B', None, True, None, 0, 5, True), # Walker, 5-day prepaid canteen
            ('Kwabena', 'Owusu', 'Class 1B', buses['Adenta Bus'], True, None, 1, 1, True), # Both paid for today

            # Class 2A
            ('Abena', 'Mansah', 'Class 2A', buses['Spintex Bus'], True, None, 5, 5, True), # Week prepaid
            ('Kweku', 'Baah', 'Class 2A', buses['Adenta Bus'], True, None, 0, 1, False), # Bus unpaid
            ('Yaa', 'Asantewaa', 'Class 2A', buses['Tema Bus'], True, None, 1, 1, True), # Paid today
            ('Francis', 'Lamptey', 'Class 2A', None, True, None, 0, 0, False), # Walker, unpaid

            # Class 2B
            ('Gifty', 'Ofori', 'Class 2B', buses['Madina Bus'], True, None, 1, 1, True), # Paid today
            ('David', 'Quaye', 'Class 2B', buses['Spintex Bus'], True, None, 0, 0, False), # Unpaid
            ('Grace', 'Addison', 'Class 2B', None, False, None, 0, 0, True), # Walker, opted out canteen

            # Nursery 1
            ('Junior', 'Kusi', 'Nursery 1', buses['Adenta Bus'], True, None, 5, 5, True), # 5-day prepaid
            ('Blessing', 'Frimpong', 'Nursery 1', buses['Tema Bus'], True, None, 0, 1, False),
            ('Prince', 'Atta', 'Nursery 1', None, True, None, 0, 1, True), # Walker, canteen paid
        ]

        default_canteen_fee = getattr(settings, 'DEFAULT_CANTEEN_FEE', Decimal('10.00'))

        for first, last, s_class, bus, canteen_enrolled, custom_canteen, bus_days_paid, canteen_days_paid, clock_in_today in students_data:
            student, _ = Student.objects.update_or_create(
                first_name=first,
                last_name=last,
                student_class=s_class,
                defaults={
                    'bus': bus,
                    'canteen_enrolled': canteen_enrolled,
                    'custom_canteen_fee': custom_canteen,
                    'is_active': True,
                }
            )

            # Bus Fee Payment (with multi-day advance option)
            if bus and bus_days_paid > 0:
                bus_paid_amount = bus.fee * Decimal(bus_days_paid)
                FeePayment.objects.get_or_create(
                    student=student,
                    fee_type='BUS',
                    period=period,
                    payment_date=today,
                    defaults={
                        'amount': bus_paid_amount,
                        'payment_method': 'MOBILE_MONEY',
                        'receipt_number': f"REC-BUS-{student.id:03d}",
                        'notes': f"{bus_days_paid}-day bus payment for {bus.name}",
                    }
                )

            # Canteen Fee Payment (with multi-day advance option)
            if canteen_enrolled and canteen_days_paid > 0:
                canteen_rate = custom_canteen if custom_canteen is not None else default_canteen_fee
                canteen_paid_amount = canteen_rate * Decimal(canteen_days_paid)
                FeePayment.objects.get_or_create(
                    student=student,
                    fee_type='CANTEEN',
                    period=period,
                    payment_date=today,
                    defaults={
                        'amount': canteen_paid_amount,
                        'payment_method': 'CASH',
                        'receipt_number': f"REC-CAN-{student.id:03d}",
                        'notes': f"{canteen_days_paid}-day canteen payment",
                    }
                )

            # Clock-In Record
            if clock_in_today:
                ClockInRecord.objects.get_or_create(
                    student=student,
                    date=today,
                    defaults={
                        'clock_in_time': time(8, (student.id * 3) % 60),
                        'status': 'PRESENT',
                        'notes': 'Morning arrival',
                    }
                )

        self.stdout.write(self.style.SUCCESS(f"Successfully seeded {len(students_data)} students, 4 buses, daily payments, and attendance!"))
