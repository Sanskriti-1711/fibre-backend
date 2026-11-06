"""Who may read a project's layers and features.

The project layer/feature endpoints and the supervision gallery all need the
same answer, so it lives here rather than being copied into each view module.

Any assignment scope grants read access: supervision work is normally assigned
per layer, per polygon or per feature, and requiring a project-scope job made
every one of those engineers 403 on the project they were sent to inspect. The
project list has always treated every scope as "works on that project", so this
is the same rule applied to the endpoints.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response


def engineer_can_access(request, project) -> bool:
    """Can this caller read ``project``?

    Planners/admins are not restricted. An engineer needs at least one
    assignment job on the project, whatever its scope. Anonymous callers are
    always refused, even if a role attribute somehow says otherwise.
    """
    user = getattr(request, 'user', request)
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'role', None) != 'ENGINEER':
        return True

    from assignments.models import AssignmentJob

    return AssignmentJob.objects.filter(project=project, assignee=user).exists()


def deny() -> Response:
    """The one answer for a project the caller may not read."""
    return Response(
        {'detail': 'You do not have access to this project.'},
        status=status.HTTP_403_FORBIDDEN,
    )
