"""URL routing for the FTTH Permit module.

All endpoints are prefixed with ``api/ftth/permits/`` and require JWT auth.
The permit engine stays isolated from the HLD/LLD engines (it only consumes
their outputs), matching the separation rule in ``docs/subprojects/permit-engine/DESIGN.md``.
"""

from django.urls import path

from .views import (
    PermitAnalyzeView,
    PermitDetailView,
    PermitMatrixView,
    PermitSummaryView,
)

urlpatterns = [
    path("ftth/permits/summary/",
         PermitSummaryView.as_view(), name="ftth-permits-summary"),
    path("ftth/permits/projects/<str:project_id>/permits/",
         PermitMatrixView.as_view(), name="ftth-permits-matrix"),
    path("ftth/permits/projects/<str:project_id>/permits/analyze/",
         PermitAnalyzeView.as_view(), name="ftth-permits-analyze"),
    path("ftth/permits/permits/<str:permit_id>/",
         PermitDetailView.as_view(), name="ftth-permits-detail"),
]
