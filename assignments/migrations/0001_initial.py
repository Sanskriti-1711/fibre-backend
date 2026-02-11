# Generated manually because manage.py makemigrations failed in this environment
from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("projects", "0002_feature_table"),
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssignmentRule",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)),
                ("layer_id", models.CharField(blank=True, max_length=255, null=True)),
                ("layer_name", models.CharField(blank=True, max_length=255)),
                ("scope", models.CharField(max_length=20, choices=[("project", "Project"), ("layer", "Layer"), ("feature", "Feature")])),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assignee", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="assignment_rules", to="users.user")),
                ("feature", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.CASCADE, related_name="assignment_rules", to="projects.feature")),
                ("project", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="assignment_rules", to="projects.project")),
            ],
            options={
                "db_table": "assignment_rules",
            },
        ),
        migrations.AddConstraint(
            model_name="assignmentrule",
            constraint=models.UniqueConstraint(
                fields=["project"],
                condition=models.Q(scope="project"),
                name="unique_project_assignment",
            ),
        ),
        migrations.AddConstraint(
            model_name="assignmentrule",
            constraint=models.UniqueConstraint(
                fields=["project", "layer_id"],
                condition=models.Q(scope="layer"),
                name="unique_layer_assignment",
            ),
        ),
        migrations.AddConstraint(
            model_name="assignmentrule",
            constraint=models.UniqueConstraint(
                fields=["feature"],
                condition=models.Q(scope="feature"),
                name="unique_feature_assignment",
            ),
        ),
        migrations.AddConstraint(
            model_name="assignmentrule",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(scope="project", layer_id__isnull=True, feature__isnull=True)
                    | models.Q(scope="layer", layer_id__isnull=False, feature__isnull=True)
                    | models.Q(scope="feature", feature__isnull=False)
                ),
                name="valid_scope_constraints",
            ),
        ),
    ]
