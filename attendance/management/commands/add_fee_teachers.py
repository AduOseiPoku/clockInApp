from decimal import Decimal
import datetime
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from attendance.models import UserProfile, SchoolClass, Bus, Student, FeePayment, SchoolSetting

class Command(BaseCommand):
    help = 'Adds 15 realistic teachers authorized to collect fees with simulated intake history for testing.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Creating 15 authorized fee-collecting teachers..."))
        User = get_user_model()
        today = timezone.localdate()

        teachers_data = [
            {
                'username': 'akosua_mensah',
                'first_name': 'Akosua',
                'last_name': 'Mensah',
                'email': 'akosua.mensah@school.com',
                'assigned_class': 'Class 1B',
                'assigned_bus': None,
                'phone': '0241112233',
            },
            {
                'username': 'kwabena_appiah',
                'first_name': 'Kwabena',
                'last_name': 'Appiah',
                'email': 'kwabena.appiah@school.com',
                'assigned_class': '',
                'assigned_bus': 'Madina Bus',
                'phone': '0242223344',
            },
            {
                'username': 'abena_osei',
                'first_name': 'Abena',
                'last_name': 'Osei',
                'email': 'abena.osei@school.com',
                'assigned_class': 'Class 2B',
                'assigned_bus': None,
                'phone': '0243334455',
            },
            {
                'username': 'yaw_boakye',
                'first_name': 'Yaw',
                'last_name': 'Boakye',
                'email': 'yaw.boakye@school.com',
                'assigned_class': '',
                'assigned_bus': 'Tema Bus',
                'phone': '0244445566',
            },
            {
                'username': 'ama_serwaa',
                'first_name': 'Ama',
                'last_name': 'Serwaa',
                'email': 'ama.serwaa@school.com',
                'assigned_class': 'Nursery 1',
                'assigned_bus': None,
                'phone': '0245556677',
            },
            {
                'username': 'kofi_frimpong',
                'first_name': 'Kofi',
                'last_name': 'Frimpong',
                'email': 'kofi.frimpong@school.com',
                'assigned_class': '',
                'assigned_bus': 'Spintex Bus',
                'phone': '0246667788',
            },
            {
                'username': 'efua_antwi',
                'first_name': 'Efua',
                'last_name': 'Antwi',
                'email': 'efua.antwi@school.com',
                'assigned_class': 'Class 3A',
                'assigned_bus': None,
                'phone': '0247778899',
            },
            {
                'username': 'kwame_asante',
                'first_name': 'Kwame',
                'last_name': 'Asante',
                'email': 'kwame.asante@school.com',
                'assigned_class': 'KG 1',
                'assigned_bus': None,
                'phone': '0248889900',
            },
            {
                'username': 'yaa_agyemang',
                'first_name': 'Yaa',
                'last_name': 'Agyemang',
                'email': 'yaa.agyemang@school.com',
                'assigned_class': 'Class 1A',
                'assigned_bus': None,
                'phone': '0249990011',
            },
            {
                'username': 'kwaku_boateng',
                'first_name': 'Kwaku',
                'last_name': 'Boateng',
                'email': 'kwaku.boateng@school.com',
                'assigned_class': '',
                'assigned_bus': 'Adenta Bus',
                'phone': '0201112233',
            },
            {
                'username': 'akua_darko',
                'first_name': 'Akua',
                'last_name': 'Darko',
                'email': 'akua.darko@school.com',
                'assigned_class': 'Class 2A',
                'assigned_bus': None,
                'phone': '0202223344',
            },
            {
                'username': 'kojo_owusu',
                'first_name': 'Kojo',
                'last_name': 'Owusu',
                'email': 'kojo.owusu@school.com',
                'assigned_class': 'KG 2',
                'assigned_bus': None,
                'phone': '0203334455',
            },
            {
                'username': 'nana_badu',
                'first_name': 'Nana Ama',
                'last_name': 'Badu',
                'email': 'nana.badu@school.com',
                'assigned_class': '',
                'assigned_bus': None,
                'phone': '0204445566',
            },
            {
                'username': 'emmanuel_quaye',
                'first_name': 'Emmanuel',
                'last_name': 'Quaye',
                'email': 'emmanuel.quaye@school.com',
                'assigned_class': '',
                'assigned_bus': 'Madina Bus',
                'phone': '0205556677',
            },
            {
                'username': 'grace_addo',
                'first_name': 'Grace',
                'last_name': 'Addo',
                'email': 'grace.addo@school.com',
                'assigned_class': 'Class 1B',
                'assigned_bus': None,
                'phone': '0206667788',
            },
        ]

        created_teachers = []
        for data in teachers_data:
            user, created = User.objects.get_or_create(
                username=data['username'],
                defaults={
                    'first_name': data['first_name'],
                    'last_name': data['last_name'],
                    'email': data['email'],
                    'is_staff': True,
                    'is_active': True,
                }
            )
            user.set_password('teacher123')
            user.first_name = data['first_name']
            user.last_name = data['last_name']
            user.email = data['email']
            user.is_staff = True
            user.is_active = True
            user.save()

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = UserProfile.ROLE_TEACHER
            profile.can_collect_fees = True
            profile.phone_number = data['phone']
            profile.assigned_class = data['assigned_class']

            if data['assigned_bus']:
                bus_obj = Bus.objects.filter(name=data['assigned_bus']).first()
                profile.assigned_bus = bus_obj
            else:
                profile.assigned_bus = None

            profile.save()
            created_teachers.append(user)
            self.stdout.write(self.style.SUCCESS(
                f"  + Teacher: {user.get_full_name()} (@{user.username}) | Class: '{profile.assigned_class or 'None'}' | Bus: '{profile.assigned_bus or 'None'}'"
            ))

        # Seed sample payments for some teachers to create realistic metrics
        period = SchoolSetting.get_setting('CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        
        # 1. Akosua Mensah (Class 1B) - collected today
        st_1b = Student.objects.filter(student_class='Class 1B', is_active=True)[:2]
        t_akosua = User.objects.get(username='akosua_mensah')
        for i, s in enumerate(st_1b):
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=today,
                fee_type='CANTEEN',
                defaults={
                    'amount': Decimal('10.00'),
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-AKO-{s.id:03d}',
                    'recorded_by': t_akosua,
                    'notes': 'Daily canteen fee intake',
                }
            )

        # 2. Kwabena Appiah (Madina Bus) - collected today
        st_madina = Student.objects.filter(bus__name='Madina Bus', is_active=True)[:3]
        t_kwabena = User.objects.get(username='kwabena_appiah')
        for i, s in enumerate(st_madina):
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=today,
                fee_type='BUS',
                defaults={
                    'amount': s.bus.fee if s.bus else Decimal('50.00'),
                    'bus': s.bus,
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-KWA-{s.id:03d}',
                    'recorded_by': t_kwabena,
                    'notes': 'Morning bus fee collection',
                }
            )

        # 3. Yaw Boakye (Tema Bus) - collected today
        st_tema = Student.objects.filter(bus__name='Tema Bus', is_active=True)[:2]
        t_yaw = User.objects.get(username='yaw_boakye')
        for i, s in enumerate(st_tema):
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=today,
                fee_type='BUS',
                defaults={
                    'amount': s.bus.fee if s.bus else Decimal('80.00'),
                    'bus': s.bus,
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-YAW-{s.id:03d}',
                    'recorded_by': t_yaw,
                    'notes': 'Route bus fare intake',
                }
            )

        # 4. Abena Osei (Class 2B) - 1 today, plus earlier in month
        st_2b = Student.objects.filter(student_class='Class 2B', is_active=True)[:3]
        t_abena = User.objects.get(username='abena_osei')
        if len(st_2b) >= 1:
            FeePayment.objects.get_or_create(
                student=st_2b[0],
                payment_date=today,
                fee_type='CANTEEN',
                defaults={
                    'amount': Decimal('10.00'),
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-ABE-{st_2b[0].id:03d}-TODAY',
                    'recorded_by': t_abena,
                }
            )
        past_date = today - datetime.timedelta(days=3)
        for s in st_2b[1:3]:
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=past_date,
                fee_type='CANTEEN',
                defaults={
                    'amount': Decimal('10.00'),
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-ABE-{s.id:03d}-OCT',
                    'recorded_by': t_abena,
                }
            )

        # 5. Ama Serwaa (Nursery 1) - 0 today, but earlier in October
        st_nur = Student.objects.filter(student_class='Nursery 1', is_active=True)[:3]
        t_ama = User.objects.get(username='ama_serwaa')
        past_date_2 = today - datetime.timedelta(days=4)
        for s in st_nur:
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=past_date_2,
                fee_type='CANTEEN',
                defaults={
                    'amount': Decimal('10.00'),
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-AMA-{s.id:03d}',
                    'recorded_by': t_ama,
                }
            )

        # 6. Kofi Frimpong (Spintex Bus) - collected today
        st_spintex = Student.objects.filter(bus__name='Spintex Bus', is_active=True)[:1]
        t_kofi_f = User.objects.get(username='kofi_frimpong')
        for s in st_spintex:
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=today,
                fee_type='BUS',
                defaults={
                    'amount': s.bus.fee if s.bus else Decimal('60.00'),
                    'bus': s.bus,
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-KFR-{s.id:03d}',
                    'recorded_by': t_kofi_f,
                }
            )

        # 7. Nana Ama Badu (Floating) - collected today
        st_float = Student.objects.filter(student_class='Class 1A', is_active=True)[:1]
        t_nana = User.objects.get(username='nana_badu')
        for s in st_float:
            FeePayment.objects.get_or_create(
                student=s,
                payment_date=today,
                fee_type='CANTEEN',
                defaults={
                    'amount': Decimal('10.00'),
                    'period': period,
                    'payment_method': 'CASH',
                    'receipt_number': f'REC-NAN-{s.id:03d}',
                    'recorded_by': t_nana,
                }
            )

        self.stdout.write(self.style.SUCCESS(f"Successfully configured 15 authorized fee-collecting teachers and realistic sample payments!"))
