from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("assignments", "0002_alter_assignmentrule_id"),
    ]

    operations = [
        migrations.RenameModel(
            old_name="AssignmentRule",
            new_name="AssignmentJob",
        ),
        migrations.AlterModelTable(
            name="assignmentjob",
            table="assignment_jobs",
        ),
    ]
