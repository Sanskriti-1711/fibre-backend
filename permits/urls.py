"""URL routing for the FTTH Permit module.

All endpoints are prefixed with ``api/ftth/permits/`` and require JWT auth.
The permit engine stays isolated from the HLD/LLD engines (it only consumes
their outputs), matching the separation rule in ``docs/subprojects/permit-engine/DESIGN.md``.
"""

from django.urls import path

from .views import (
    PermitAllView,
    PermitAnalyzeView,
    PermitDetailView,
    PermitMatrixView,
    PermitPackageDownloadView,
    PermitPackageView,
    HldPermitPackageView,
    HldPermitPackageDownloadView,
    PermitSubmissionDetailView,
    PermitSubmissionListView,
    PermitSubmissionTransitionView,
    PermitSummaryView,
)

urlpatterns = [
    path("ftth/permits/",
         PermitAllView.as_view(), name="ftth-permits-all"),
    path("ftth/permits/summary/",
         PermitSummaryView.as_view(), name="ftth-permits-summary"),
    path("ftth/permits/submissions/",
         PermitSubmissionListView.as_view(), name="ftth-permits-submissions"),
    path("ftth/permits/submissions/<uuid:submission_id>/",
         PermitSubmissionDetailView.as_view(), name="ftth-permits-submission-detail"),
    path("ftth/permits/submissions/<uuid:submission_id>/transition/",
         PermitSubmissionTransitionView.as_view(), name="ftth-permits-submission-transition"),
    path("ftth/permits/projects/<str:project_id>/permits/",
         PermitMatrixView.as_view(), name="ftth-permits-matrix"),
    path("ftth/permits/projects/<str:project_id>/permits/analyze/",
         PermitAnalyzeView.as_view(), name="ftth-permits-analyze"),
    path("ftth/permits/projects/<str:project_id>/hld-package/",
         HldPermitPackageView.as_view(), name="ftth-permits-hld-package"),
    path("ftth/permits/projects/<str:project_id>/hld-package/download/",
         HldPermitPackageDownloadView.as_view(), name="ftth-permits-hld-package-download"),
    path("ftth/permits/projects/<str:project_id>/package/",
         PermitPackageView.as_view(), name="ftth-permits-package"),
    path("ftth/permits/projects/<str:project_id>/package/download/",
         PermitPackageDownloadView.as_view(), name="ftth-permits-package-download"),
    path("ftth/permits/permits/<str:permit_id>/",
         PermitDetailView.as_view(), name="ftth-permits-detail"),
]
