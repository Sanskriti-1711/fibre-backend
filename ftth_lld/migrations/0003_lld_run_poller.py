from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('ftth_lld', '0002_lld_run_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='lldrun',
            name='poller_token',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='lldrun',
            name='poller_heartbeat',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
