# projects/services/metrics_service.py
from projects.models import FeatureStatus

def calculate_project_completion(project):
    total = FeatureStatus.objects.filter(project=project).count()
    completed = FeatureStatus.objects.filter(
        project=project,
        status="completed"
    ).count()

    return 0 if total == 0 else round((completed / total) * 100, 2)
