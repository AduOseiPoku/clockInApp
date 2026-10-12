from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('attendance', '0007_feepayment_days_covered_feepayment_is_advance'),
    ]

    operations = [
        migrations.AlterField(
            model_name='clockinrecord',
            name='date',
            field=models.DateField(db_index=True, default=django.utils.timezone.localdate),
        ),
        migrations.AddIndex(
            model_name='clockinrecord',
            index=models.Index(fields=['date', 'status'], name='clockin_date_status_idx'),
        ),
        migrations.AlterField(
            model_name='feepayment',
            name='fee_type',
            field=models.CharField(choices=[('BUS', 'Bus Fee'), ('CANTEEN', 'Canteen / Lunch Fee')], db_index=True, max_length=20),
        ),
        migrations.AlterField(
            model_name='feepayment',
            name='payment_date',
            field=models.DateField(db_index=True, default=django.utils.timezone.localdate),
        ),
        migrations.AddIndex(
            model_name='feepayment',
            index=models.Index(fields=['fee_type', 'payment_date'], name='feepay_type_date_idx'),
        ),
        migrations.AddIndex(
            model_name='feepayment',
            index=models.Index(fields=['recorded_by', 'payment_date'], name='feepay_rec_date_idx'),
        ),
        migrations.AddIndex(
            model_name='feepayment',
            index=models.Index(fields=['student', 'fee_type', 'payment_date'], name='feepay_std_type_date_idx'),
        ),
    ]
