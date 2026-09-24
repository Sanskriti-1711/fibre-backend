# Generated for permits AI advisory drafts (deterministic-guarded).
import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("permits", "0003_permitsubmission_permitmatrix_submission_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("ftth_hld", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="PermitAiDraft",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("permit_group", models.CharField(blank=True, default="", max_length=128)),
                ("permit_type", models.CharField(blank=True, default="", max_length=64)),
                ("draft_type", models.CharField(choices=[("cover", "Cover text"), ("narrative", "Narrative"), ("requirements_extract", "Requirements extract"), ("completeness", "Completeness explainer"), ("risk", "Risk note"), ("timeline", "Timeline note")], max_length=32)),
                ("content", models.TextField(blank=True, default="")),
                ("deterministic_fallback", models.TextField(blank=True, default="")),
                ("is_ai_generated", models.BooleanField(default=False)),
                ("disclaimer", models.TextField(blank=True, default="")),
                ("meta", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("reviewed", models.BooleanField(default=False)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="permit_ai_drafts", to="ftth_hld.ftthproject")),
                ("permit", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ai_drafts", to="permits.permitmatrix")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="reviewed_permit_drafts", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "ftth_permit_ai_drafts", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="permitaidraft",
            index=models.Index(fields=["project", "draft_type"], name="ftth_permit_ai_proj_typ_idx"),
        ),
        migrations.AddIndex(
            model_name="permitaidraft",
            index=models.Index(fields=["permit", "draft_type"], name="ftth_permit_ai_perm_typ_idx"),
        ),
    ]
