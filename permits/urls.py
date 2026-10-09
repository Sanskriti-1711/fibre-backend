"""URL routing for the FTTH Permit module.

All endpoints are prefixed with ``api/ftth/permits/`` and require JWT auth.
The permit engine stays isolated from the HLD/LLD engines (it only consumes
their outputs), matching the separation rule in ``docs/subprojects/permit-engine/DESIGN.md``.
"""

from django.urls import path

from .ai.views import (
    PermitAiCompletenessView,
    PermitAiDraftReviewView,
    PermitAiDraftView,
    PermitAiRequirementsView,
    PermitAiRiskView,
    PermitAiTimelineView,
)
from .classify_views import (
    PermitClassifyFeedbackView,
    PermitClassifyStatsView,
    PermitClassifyView,
)
from .expiry_views import PermitExpiryView
from .qa_views import PermitQaView
from .sync_views import (
    PermitSubmissionCsvSyncView,
    PermitSubmissionSyncView,
    PermitSyncPollView,
    PermitSyncStatusView,
)
from .views import (
    HldPermitPackageDownloadView,
    HldPermitPackageView,
    PermitAllView,
    PermitAnalyzeView,
    PermitDetailView,
    PermitMatrixView,
    PermitPackageDownloadView,
    PermitPackageView,
    PermitSubmissionDetailView,
    PermitSubmissionListView,
    PermitSubmissionTransitionView,
    PermitSummaryView,
)

urlpatterns = [
    path('ftth/permits/', PermitAllView.as_view(), name='ftth-permits-all'),
    path('ftth/permits/summary/', PermitSummaryView.as_view(), name='ftth-permits-summary'),
    path(
        'ftth/permits/submissions/',
        PermitSubmissionListView.as_view(),
        name='ftth-permits-submissions',
    ),
    path(
        'ftth/permits/submissions/<uuid:submission_id>/',
        PermitSubmissionDetailView.as_view(),
        name='ftth-permits-submission-detail',
    ),
    path(
        'ftth/permits/submissions/<uuid:submission_id>/transition/',
        PermitSubmissionTransitionView.as_view(),
        name='ftth-permits-submission-transition',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/permits/',
        PermitMatrixView.as_view(),
        name='ftth-permits-matrix',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/permits/analyze/',
        PermitAnalyzeView.as_view(),
        name='ftth-permits-analyze',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/hld-package/',
        HldPermitPackageView.as_view(),
        name='ftth-permits-hld-package',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/hld-package/download/',
        HldPermitPackageDownloadView.as_view(),
        name='ftth-permits-hld-package-download',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/package/',
        PermitPackageView.as_view(),
        name='ftth-permits-package',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/package/download/',
        PermitPackageDownloadView.as_view(),
        name='ftth-permits-package-download',
    ),
    path(
        'ftth/permits/permits/<str:permit_id>/',
        PermitDetailView.as_view(),
        name='ftth-permits-detail',
    ),
    # AI advisory (advisory-only; never mutates PermitMatrix readiness/status)
    path(
        'ftth/permits/projects/<str:project_id>/ai/draft/',
        PermitAiDraftView.as_view(),
        name='ftth-permits-ai-draft',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/ai/requirements/',
        PermitAiRequirementsView.as_view(),
        name='ftth-permits-ai-requirements',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/ai/risk/',
        PermitAiRiskView.as_view(),
        name='ftth-permits-ai-risk',
    ),
    path(
        'ftth/permits/projects/<str:project_id>/ai/timeline/',
        PermitAiTimelineView.as_view(),
        name='ftth-permits-ai-timeline',
    ),
    path(
        'ftth/permits/permits/<str:permit_id>/ai/completeness/',
        PermitAiCompletenessView.as_view(),
        name='ftth-permits-ai-completeness',
    ),
    path(
        'ftth/permits/ai/drafts/', PermitAiDraftReviewView.as_view(), name='ftth-permits-ai-drafts'
    ),
    path(
        'ftth/permits/ai/drafts/<uuid:draft_id>/review/',
        PermitAiDraftReviewView.as_view(),
        name='ftth-permits-ai-draft-review',
    ),
    # Expiry renewal reminders (P21b) — read-only, no status mutation
    path('ftth/permits/expiries/', PermitExpiryView.as_view(), name='ftth-permits-expiries'),
    path(
        'ftth/permits/projects/<str:project_id>/expiries/',
        PermitExpiryView.as_view(),
        name='ftth-permits-project-expiries',
    ),
    # Package QA (P19b) — full-package audit, deterministic + optional AI paragraph
    # Collection route supports ?project_id= filter so the workspace can stay cross-project.
    path('ftth/permits/qa/', PermitQaView.as_view(), name='ftth-permits-qa'),
    path(
        'ftth/permits/projects/<str:project_id>/qa/',
        PermitQaView.as_view(),
        name='ftth-permits-project-qa',
    ),
    # Status-sync poller (P20b) — pluggable adapters + webhook/CSV inbox
    path(
        'ftth/permits/sync/status/', PermitSyncStatusView.as_view(), name='ftth-permits-sync-status'
    ),
    path('ftth/permits/sync/poll/', PermitSyncPollView.as_view(), name='ftth-permits-sync-poll'),
    path(
        'ftth/permits/submissions/sync/',
        PermitSubmissionSyncView.as_view(),
        name='ftth-permits-submission-sync',
    ),
    path(
        'ftth/permits/submissions/sync/<uuid:submission_id>/',
        PermitSubmissionSyncView.as_view(),
        name='ftth-permits-submission-sync-detail',
    ),
    path(
        'ftth/permits/submissions/sync/csv/',
        PermitSubmissionCsvSyncView.as_view(),
        name='ftth-permits-submission-csv-sync',
    ),
    # Auto-classify (P22b) — heuristic now + label collection for future ML
    path('ftth/permits/classify/', PermitClassifyView.as_view(), name='ftth-permits-classify'),
    path(
        'ftth/permits/classify/feedback/',
        PermitClassifyFeedbackView.as_view(),
        name='ftth-permits-classify-feedback',
    ),
    path(
        'ftth/permits/classify/stats/',
        PermitClassifyStatsView.as_view(),
        name='ftth-permits-classify-stats',
    ),
]
