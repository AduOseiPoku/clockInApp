from django.core.management.base import BaseCommand
from django.utils import timezone
from decimal import Decimal
from datetime import time
import random
from attendance.models import Bus, SchoolClass, Student, FeePayment, ClockInRecord
from django.contrib.auth import get_user_model
from django.conf import settings

class Command(BaseCommand):
    help = 'Populates each class with 25 realistic students (bringing totals to 28-30 per class) with bus assignments and payments.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Populating 25 students per class with realistic Ghanaian names and bus assignments..."))

        User = get_user_model()
        admin_user = User.objects.filter(is_superuser=True).first() or User.objects.first()
        period = getattr(settings, 'CURRENT_ACADEMIC_PERIOD', 'Term 1 - 2026')
        today = timezone.localdate()

        buses = list(Bus.objects.filter(is_active=True))
        classes = list(SchoolClass.objects.all())

        if not classes:
            self.stdout.write(self.style.ERROR("No classes found. Creating standard classes..."))
            for c_name in ['Class 1A', 'Class 1B', 'Class 2A', 'Class 2B', 'Nursery 1']:
                sc, _ = SchoolClass.objects.get_or_create(name=c_name)
                classes.append(sc)

        # Diverse Ghanaian student names per class
        names_pool = {
            'Class 1A': [
                ('Kofi', 'Adjei'), ('Ama', 'Kwarteng'), ('Kwaku', 'Ofori'), ('Abena', 'Boakye'),
                ('Samuel', 'Appiah'), ('Esther', 'Mensah'), ('Kwame', 'Sarpong'), ('Grace', 'Darko'),
                ('Yaw', 'Frimpong'), ('Akosua', 'Agyemang'), ('Daniel', 'Baah'), ('Rebecca', 'Donkor'),
                ('Emmanuel', 'Oppong'), ('Sarah', 'Annan'), ('Kwesi', 'Boadu'), ('Priscilla', 'Adu'),
                ('Joseph', 'Tetteh'), ('Mercy', 'Lamptey'), ('Michael', 'Nyarko'), ('Bernice', 'Tagoe'),
                ('David', 'Fosu'), ('Faith', 'Quaye'), ('Bright', 'Ackon'), ('Peace', 'Ghartey'),
                ('Kelvin', 'Forson'),
            ],
            'Class 1B': [
                ('Kwabena', 'Atta'), ('Efua', 'Baffoe'), ('Fiifi', 'Wiredu'), ('Yaa', 'Kuffour'),
                ('Isaac', 'Kyeremateng'), ('Joy', 'Opoku'), ('Joshua', 'Twumasi'), ('Victoria', 'Gyasi'),
                ('Caleb', 'Poky'), ('Gloria', 'Ababio'), ('Dennis', 'Armah'), ('Emmanuella', 'Ashitey'),
                ('Godwin', 'Nortey'), ('Doris', 'Lartey'), ('Prosper', 'Aryee'), ('Eunice', 'Cudjoe'),
                ('Justice', 'Turkson'), ('Beatrice', 'Dadzie'), ('Stephen', 'Dadson'), ('Comfort', 'Essien'),
                ('Frank', 'Acheampong'), ('Blessing', 'Badu'), ('Prince', 'Sutherland'), ('Araba', 'Arthur'),
                ('Richmond', 'Danquah'),
            ],
            'Class 2A': [
                ('Yoofi', 'Mensah'), ('Baaba', 'Osei'), ('Jojo', 'Boateng'), ('Serwaa', 'Appiah'),
                ('Collins', 'Owusu'), ('Mansah', 'Antwi'), ('Gideon', 'Agyemang'), ('Oforiwaa', 'Adu'),
                ('Kelvin', 'Asante'), ('Boatemaa', 'Frimpong'), ('Bright', 'Amponsah'), ('Agyeiwaa', 'Baah'),
                ('Dennis', 'Donkor'), ('Asantewaa', 'Adjei'), ('Godwin', 'Darko'), ('Patience', 'Tetteh'),
                ('Stephen', 'Quaye'), ('Nana Ama', 'Annan'), ('Frank', 'Ofori'), ('Adwoa', 'Addo'),
                ('Samuel', 'Lamptey'), ('Afua', 'Tagoe'), ('Daniel', 'Kwarteng'), ('Akua', 'Boakye'),
                ('Emmanuel', 'Sarpong'),
            ],
            'Class 2B': [
                ('Kwame', 'Oppong'), ('Ama', 'Acheampong'), ('Kofi', 'Agyei'), ('Akosua', 'Nyarko'),
                ('Kwesi', 'Boadu'), ('Abena', 'Danquah'), ('Yaw', 'Fosu'), ('Yaa', 'Arthur'),
                ('Kwaku', 'Badu'), ('Efua', 'Sutherland'), ('Kojo', 'Ackon'), ('Esi', 'Dadzie'),
                ('Kwabena', 'Ghartey'), ('Afua', 'Essien'), ('Nana', 'Forson'), ('Adwoa', 'Turkson'),
                ('Paa Kwesi', 'Cudjoe'), ('Araba', 'Aryee'), ('Ekow', 'Quaye'), ('Baaba', 'Lartey'),
                ('David', 'Nortey'), ('Sarah', 'Ashitey'), ('Michael', 'Armah'), ('Esther', 'Ababio'),
                ('Joseph', 'Twumasi'),
            ],
            'Nursery 1': [
                ('Prince', 'Gyasi'), ('Grace', 'Poky'), ('Joshua', 'Wiredu'), ('Rebecca', 'Kuffour'),
                ('Caleb', 'Kyeremateng'), ('Priscilla', 'Opoku'), ('Kelvin', 'Atta'), ('Mercy', 'Baffoe'),
                ('Bright', 'Adjei'), ('Bernice', 'Kwarteng'), ('Dennis', 'Ofori'), ('Faith', 'Boakye'),
                ('Godwin', 'Appiah'), ('Peace', 'Mensah'), ('Prosper', 'Sarpong'), ('Joy', 'Darko'),
                ('Justice', 'Frimpong'), ('Victoria', 'Agyemang'), ('Stephen', 'Baah'), ('Gloria', 'Donkor'),
                ('Frank', 'Oppong'), ('Emmanuella', 'Annan'), ('Richmond', 'Boadu'), ('Doris', 'Adu'),
                ('Samuel', 'Tetteh'),
            ],
        }

        created_count = 0

        for sc in classes:
            self.stdout.write(f"\nProcessing {sc.name} (currently {sc.students.count()} students)...")
            students_list = names_pool.get(sc.name, [])
            
            # If a class is not in the predefined map, generate 25 unique names
            if not students_list:
                first_names = ['Kofi', 'Kwame', 'Ama', 'Akosua', 'Yaw', 'Abena', 'Kwesi', 'Kwaku', 'Yaa', 'Esi', 'Samuel', 'Emmanuel', 'Grace', 'Esther']
                last_names = ['Mensah', 'Osei', 'Boateng', 'Appiah', 'Owusu', 'Antwi', 'Agyemang', 'Adu', 'Frimpong', 'Darko', 'Tetteh', 'Quaye']
                students_list = [(random.choice(first_names), f"{random.choice(last_names)}_{i}") for i in range(1, 26)]

            for first, last in students_list:
                # Check if already exists
                existing = Student.objects.filter(first_name=first, last_name=last, student_class=sc.name).first()
                if existing:
                    continue

                # Bus assignment: ~80% assigned to a bus, ~20% walkers
                assigned_bus = None
                if buses and random.random() < 0.80:
                    assigned_bus = random.choice(buses)

                student = Student.objects.create(
                    first_name=first,
                    last_name=last,
                    student_class=sc.name,
                    school_class=sc,
                    bus=assigned_bus,
                    canteen_enrolled=random.random() < 0.90,
                    is_active=True,
                )
                created_count += 1

                # Randomize today's clock-in: ~75% present
                if random.random() < 0.75:
                    ClockInRecord.objects.get_or_create(
                        student=student,
                        date=today,
                        defaults={
                            'clock_in_time': time(random.randint(7, 8), random.randint(10, 50)),
                            'status': 'PRESENT',
                            'recorded_by': admin_user,
                        }
                    )


                # Randomize payment for bus riders: ~65% paid, ~35% unpaid
                if assigned_bus:
                    if random.random() < 0.65:
                        FeePayment.objects.create(
                            student=student,
                            fee_type='BUS',
                            amount=assigned_bus.fee,
                            payment_date=today,
                            period=period,
                            payment_method='CASH',
                            recorded_by=admin_user,
                        )

                # Randomize payment for canteen: ~70% paid
                if student.canteen_enrolled:
                    if random.random() < 0.70:
                        canteen_fee = getattr(settings, 'DEFAULT_CANTEEN_FEE', Decimal('10.00'))
                        FeePayment.objects.create(
                            student=student,
                            fee_type='CANTEEN',
                            amount=canteen_fee,
                            payment_date=today,
                            period=period,
                            payment_method='CASH',
                            recorded_by=admin_user,
                        )

        self.stdout.write(self.style.SUCCESS(f"\n[SUCCESS] Added {created_count} new students across classes!"))
        for sc in SchoolClass.objects.all():
            self.stdout.write(f"  {sc.name}: {sc.students.count()} students total")
