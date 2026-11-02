from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from attendance.models import Bus, UserProfile, Student, SchoolClass

class Command(BaseCommand):
    help = 'Populates 8 sample buses with fares and 8 sample teachers with multi-bus assignments.'

    def handle(self, *args, **options):
        User = get_user_model()
        self.stdout.write(self.style.NOTICE("Populating 8 buses..."))

        new_buses_data = [
            {
                'name': 'Dansoman Express',
                'fee': 45.00,
                'route_description': 'Dansoman Exhibition, Control, Roundabout, Keep Fit Club',
                'driver_name': 'Kwame Asante',
                'driver_phone': '+233 24 555 1201',
                'plate_number': 'GW-4821-21',
            },
            {
                'name': 'Kasoa Route',
                'fee': 85.00,
                'route_description': 'Kasoa Toll Booth, Galilea, Weija Junction, Mallam Interchange',
                'driver_name': 'Emmanuel Mensah',
                'driver_phone': '+233 20 888 3412',
                'plate_number': 'GS-3104-22',
            },
            {
                'name': 'Achimota - Dome Route',
                'fee': 55.00,
                'route_description': 'Achimota Old Station, Neoplan, Dome Pillar 2, Christian Village',
                'driver_name': 'Samuel Coffie',
                'driver_phone': '+233 50 123 9087',
                'plate_number': 'GE-7789-20',
            },
            {
                'name': 'Lapaz - Abeka Route',
                'fee': 40.00,
                'route_description': 'Lapaz Station, Abrantie, Abeka Post Office, Total Filling Station',
                'driver_name': 'Isaac Appiah',
                'driver_phone': '+233 27 654 3210',
                'plate_number': 'GR-1124-21',
            },
            {
                'name': 'East Legon - Adjiringanor Route',
                'fee': 75.00,
                'route_description': 'American House, Adjiringanor Shell, School Junction, Trasacco Valley gate',
                'driver_name': 'Joshua Tetteh',
                'driver_phone': '+233 24 999 4321',
                'plate_number': 'GT-9912-23',
            },
            {
                'name': 'Cantonments - Osu Route',
                'fee': 65.00,
                'route_description': 'Danquah Circle, Oxford Street, Cantonments Post Office, Labone Junction',
                'driver_name': 'Benjamin Ofori',
                'driver_phone': '+233 54 222 7654',
                'plate_number': 'GN-5501-22',
            },
            {
                'name': 'Kwashieman - Awoshie Route',
                'fee': 48.00,
                'route_description': 'Awoshie Last Stop, Baah Yard, Kwashieman Market, Odorkor Official Town',
                'driver_name': 'Frank Boakye',
                'driver_phone': '+233 20 444 6789',
                'plate_number': 'GW-8812-20',
            },
            {
                'name': 'Haatso - Agbogba Route',
                'fee': 52.00,
                'route_description': 'Agbogba Junction, Wisconsin University, Haatso Supermarket',
                'driver_name': 'Peter Addo',
                'driver_phone': '+233 55 333 8908',
                'plate_number': 'GT-6643-23',
            },
        ]

        buses_created = {}
        for b_data in new_buses_data:
            bus, created = Bus.objects.update_or_create(
                name=b_data['name'],
                defaults=b_data
            )
            buses_created[bus.name] = bus
            status = "Created" if created else "Updated"
            self.stdout.write(f"  [+] {status} Bus: {bus.name} -> Fare: GHS {bus.fee}")

        tema_bus = Bus.objects.filter(name='Tema Bus').first()
        spintex_bus = Bus.objects.filter(name='Spintex Bus').first()
        madina_bus = Bus.objects.filter(name='Madina Bus').first()
        adenta_bus = Bus.objects.filter(name='Adenta Bus').first()

        self.stdout.write(self.style.NOTICE("\nPopulating 8 teachers with multi-bus assignments..."))

        teachers_data = [
            {
                'username': 'akosua_m',
                'first_name': 'Akosua',
                'last_name': 'Mensah',
                'email': 'akosua.mensah@school.edu',
                'phone_number': '+233 24 100 0001',
                'assigned_class': 'KG 1',
                'buses': [buses_created['Dansoman Express'], buses_created['Kwashieman - Awoshie Route']],
                'can_collect_fees': True,
            },
            {
                'username': 'kwabena_o',
                'first_name': 'Kwabena',
                'last_name': 'Osei',
                'email': 'kwabena.osei@school.edu',
                'phone_number': '+233 24 100 0002',
                'assigned_class': 'KG 2',
                'buses': [buses_created['Lapaz - Abeka Route'], buses_created['Achimota - Dome Route']],
                'can_collect_fees': True,
            },
            {
                'username': 'efua_a',
                'first_name': 'Efua',
                'last_name': 'Appiah',
                'email': 'efua.appiah@school.edu',
                'phone_number': '+233 24 100 0003',
                'assigned_class': 'Class 1B',
                'buses': [buses_created['East Legon - Adjiringanor Route'], buses_created['Haatso - Agbogba Route']],
                'can_collect_fees': True,
            },
            {
                'username': 'samuel_d',
                'first_name': 'Samuel',
                'last_name': 'Danquah',
                'email': 'samuel.danquah@school.edu',
                'phone_number': '+233 24 100 0004',
                'assigned_class': 'Class 2A',
                'buses': [buses_created['Kasoa Route'], buses_created['Cantonments - Osu Route']],
                'can_collect_fees': True,
            },
            {
                'username': 'grace_b',
                'first_name': 'Grace',
                'last_name': 'Badu',
                'email': 'grace.badu@school.edu',
                'phone_number': '+233 24 100 0005',
                'assigned_class': 'Class 3A',
                'buses': [b for b in [tema_bus, spintex_bus] if b],
                'can_collect_fees': True,
            },
            {
                'username': 'daniel_k',
                'first_name': 'Daniel',
                'last_name': 'Kusi',
                'email': 'daniel.kusi@school.edu',
                'phone_number': '+233 24 100 0006',
                'assigned_class': 'Class 3B',
                'buses': [b for b in [madina_bus, adenta_bus] if b],
                'can_collect_fees': True,
            },
            {
                'username': 'abena_a',
                'first_name': 'Abena',
                'last_name': 'Antwi',
                'email': 'abena.antwi@school.edu',
                'phone_number': '+233 24 100 0007',
                'assigned_class': 'Nursery 1',
                'buses': [],
                'can_collect_fees': True,
            },
            {
                'username': 'isaac_d',
                'first_name': 'Isaac',
                'last_name': 'Darko',
                'email': 'isaac.darko@school.edu',
                'phone_number': '+233 24 100 0008',
                'assigned_class': 'Class 2B',
                'buses': [],
                'can_collect_fees': True,
            },
        ]

        for t_data in teachers_data:
            user, created = User.objects.get_or_create(
                username=t_data['username'],
                defaults={
                    'first_name': t_data['first_name'],
                    'last_name': t_data['last_name'],
                    'email': t_data['email'],
                    'is_staff': True,
                    'is_active': True,
                }
            )
            user.set_password('teacher123')
            user.is_staff = True
            user.is_active = True
            user.save()

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = UserProfile.ROLE_TEACHER
            profile.phone_number = t_data['phone_number']
            profile.assigned_class = t_data['assigned_class']
            profile.can_collect_fees = t_data['can_collect_fees']
            if t_data['buses']:
                profile.assigned_buses.set(t_data['buses'])
                profile.assigned_bus = t_data['buses'][0]
            else:
                profile.assigned_buses.clear()
                profile.assigned_bus = None
            profile.save()

            bus_names = ", ".join(b.name for b in t_data['buses']) if t_data['buses'] else "Classroom only"
            status = "Created" if created else "Updated"
            self.stdout.write(f"  [+] {status} Teacher: {user.get_full_name()} (@{user.username}) -> Password: teacher123 | Buses: {bus_names}")

        self.stdout.write(self.style.NOTICE("\nAdding riders/students across the new buses..."))

        students_to_add = [
            ('Kofi', 'Acheampong', 'Class 1A', buses_created['Dansoman Express']),
            ('Maame', 'Yaa', 'Class 2B', buses_created['Dansoman Express']),
            ('Prince', 'Nyarko', 'Class 3A', buses_created['Kasoa Route']),
            ('Esi', 'Armah', 'Class 1A', buses_created['Kasoa Route']),
            ('Nana', 'Boadu', 'Class 2A', buses_created['Achimota - Dome Route']),
            ('Adwoa', 'Donkor', 'Class 2B', buses_created['Achimota - Dome Route']),
            ('Yaw', 'Anane', 'Class 3B', buses_created['Lapaz - Abeka Route']),
            ('Abigail', 'Boadi', 'Class 1B', buses_created['Lapaz - Abeka Route']),
            ('Selorm', 'Dela', 'Class 3A', buses_created['East Legon - Adjiringanor Route']),
            ('Kekeli', 'Gbeho', 'Class 2A', buses_created['East Legon - Adjiringanor Route']),
            ('David', 'Lamptey', 'Class 1A', buses_created['Cantonments - Osu Route']),
            ('Naa', 'Ashorkor', 'Class 2B', buses_created['Cantonments - Osu Route']),
            ('Jojo', 'Mills', 'Class 3A', buses_created['Kwashieman - Awoshie Route']),
            ('Beryl', 'Quaye', 'Class 1B', buses_created['Kwashieman - Awoshie Route']),
            ('Elikem', 'Kotoka', 'Class 2A', buses_created['Haatso - Agbogba Route']),
            ('Peace', 'Ametefe', 'Class 3B', buses_created['Haatso - Agbogba Route']),
        ]

        for first, last, cls, bus in students_to_add:
            s, created = Student.objects.update_or_create(
                first_name=first,
                last_name=last,
                defaults={
                    'student_class': cls,
                    'bus': bus,
                    'canteen_enrolled': True,
                    'is_active': True,
                }
            )
            status = "Enrolled" if created else "Updated"
            self.stdout.write(f"  [+] {status} Student: {s.full_name} ({s.student_class}) -> Bus: {bus.name} (Fare: GHS {bus.fee})")

        self.stdout.write(self.style.SUCCESS("\nAll 8 buses, 8 teachers, and 16 student riders successfully populated!"))
