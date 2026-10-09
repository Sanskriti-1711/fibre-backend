import uuid

from django.db import models

from users.models import User

from .project import Project


class ProjectMember(models.Model):
    """Who is involved in a project, with a role.

    Captures the people side of a project (planner, field engineers, LLD
    reviewer, contractor, observer) so the project overview can show the full
    team alongside the business/technical and design data.
    """

    class Role(models.TextChoices):
        PLANNER = 'planner', 'Planner'
        ENGINEER = 'engineer', 'Field Engineer'
        REVIEWER = 'reviewer', 'LLD Reviewer'
        CONTRACTOR = 'contractor', 'Contractor'
        OBSERVER = 'observer', 'Observer'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='project_memberships')
    role = models.CharField(max_length=20, choices=Role.choices)
    added_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'project_members'
        unique_together = [['project', 'user', 'role']]

    def __str__(self):
        return f'{self.project.name} — {self.user.email} ({self.role})'
