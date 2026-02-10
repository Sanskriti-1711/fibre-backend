# projects/services/metrics_service.py
from projects.models import Feature


def calculate_project_completion(project):
    total = Feature.objects.filter(project=project).count()
    completed = Feature.objects.filter(
        project=project,
        status=Feature.STATUS_APPROVED,
    ).count()

    return 0 if total == 0 else round((completed / total) * 100, 2)
