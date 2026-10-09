from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from attendance.models import UserProfile, SchoolClass

class Command(BaseCommand):
    help = 'Ensures default demo accounts and initial classes exist with functional passwords.'

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Setting up demo accounts and base classroom data..."))
        User = get_user_model()

        # 1. Ensure basic classes exist for rosters and assignments
        default_classes = [
            ('Class 1A', 'Primary'),
            ('Class 1B', 'Primary'),
            ('Class 2A', 'Primary'),
            ('Class 2B', 'Primary'),
            ('KG 1', 'Kindergarten'),
            ('KG 2', 'Kindergarten'),
        ]
        for class_name, grade_level in default_classes:
            SchoolClass.objects.get_or_create(
                name=class_name,
                defaults={'grade_level': grade_level, 'is_active': True}
            )

        # 2. Configure demo accounts matching login.html buttons
        demo_accounts = [
            {
                'username': 'admin',
                'email': 'admin@school.com',
                'password': 'admin123',
                'first_name': 'School',
                'last_name': 'Administrator',
                'role': UserProfile.ROLE_ADMIN,
                'is_staff': True,
                'is_superuser': True,
                'assigned_class': '',
                'can_collect_fees': True,
            },
            {
                'username': 'teacher',
                'email': 'teacher@school.com',
                'password': 'teacher123',
                'first_name': 'Sarah',
                'last_name': 'Addae',
                'role': UserProfile.ROLE_TEACHER,
                'is_staff': True,
                'is_superuser': False,
                'assigned_class': 'Class 1A',
                'can_collect_fees': True,
            },
            {
                'username': 'bursar',
                'email': 'bursar@school.com',
                'password': 'bursar123',
                'first_name': 'Kwame',
                'last_name': 'Boateng',
                'role': UserProfile.ROLE_ACCOUNTANT,
                'is_staff': True,
                'is_superuser': False,
                'assigned_class': '',
                'can_collect_fees': True,
            },
        ]

        for acc in demo_accounts:
            user, created = User.objects.get_or_create(
                username=acc['username'],
                defaults={
                    'email': acc['email'],
                    'first_name': acc['first_name'],
                    'last_name': acc['last_name'],
                }
            )
            # Guarantee password and status
            user.set_password(acc['password'])
            user.is_staff = acc['is_staff']
            user.is_superuser = acc['is_superuser']
            user.is_active = True
            user.save()

            profile, _ = UserProfile.objects.get_or_create(user=user)
            profile.role = acc['role']
            profile.assigned_class = acc['assigned_class']
            profile.can_collect_fees = acc['can_collect_fees']
            profile.save()

            self.stdout.write(self.style.SUCCESS(
                f"  [OK] '{acc['username']}' ready (Password: {acc['password']} | Role: {acc['role']})"
            ))

        # Also ensure 'Principal' user (if present) has password 'admin123'
        principal_user = User.objects.filter(username__iexact='principal').first()
        if principal_user:
            principal_user.set_password('admin123')
            principal_user.is_staff = True
            principal_user.is_superuser = True
            principal_user.is_active = True
            principal_user.save()
            profile, _ = UserProfile.objects.get_or_create(user=principal_user)
            profile.role = UserProfile.ROLE_ADMIN
            profile.save()
            self.stdout.write(self.style.SUCCESS("  [OK] 'Principal' password synchronized to admin123"))

        self.stdout.write(self.style.SUCCESS("All demo credentials are now active and functional!"))
