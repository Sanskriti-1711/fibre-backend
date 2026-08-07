"""API views for the survey app — with pagination and filtering."""

from datetime import datetime
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db.models import Q

from .models import (
    GPSTrace,
    GPSPoint,
    TrenchSurvey,
    ExistingAsset,
    RiskAssessment,
    Hazard,
    FieldEvidence,
    SurveyChange,
    SurveyStatus,
    SyncQueueItem,
    SurveyFeature,
)
from .serializers import (
    GPSTraceSerializer,
    GPSPointSerializer,
    TrenchSurveySerializer,
    ExistingAssetSerializer,
    RiskAssessmentSerializer,
    HazardSerializer,
    FieldEvidenceSerializer,
    SurveyChangeSerializer,
    SurveyStatusSerializer,
    SyncQueueItemSerializer,
    SurveyFeatureSerializer,
)

# ── Pagination Defaults ───────────────────────────────────────────────────
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


# ── Helpers ────────────────────────────────────────────────────────────────
def _get_engineer(request):
    """Get the authenticated engineer from request."""
    return request.user


def _survey_scope(request, qs):
    """Scope survey data by role.

    Engineers see only their own survey rows. Platform (SUBADMIN) users see
    all engineers' data — needed for planner review / LLD — and may narrow
    to a single engineer with ``?engineer=<user_id>``.
    """
    user = request.user
    if getattr(user, 'role', None) == 'SUBADMIN':
        engineer_id = request.GET.get('engineer')
        if engineer_id:
            qs = qs.filter(engineer_id=engineer_id)
        return qs
    return qs.filter(engineer=user)


def _paginate(request, queryset, serializer_class, context=None):
    """Paginate a queryset and return a standardised paginated response.

    Query params:
        page      — page number (default 1)
        page_size — items per page (default 20, max 100)

    Response shape:
        { count, page, page_size, total_pages, results: [...] }
    """
    try:
        page = int(request.GET.get('page', 1))
    except (ValueError, TypeError):
        page = 1
    try:
        page_size = int(request.GET.get('page_size', DEFAULT_PAGE_SIZE))
    except (ValueError, TypeError):
        page_size = DEFAULT_PAGE_SIZE

    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    total = queryset.count()
    offset = (page - 1) * page_size

    serializer = serializer_class(
        queryset[offset:offset + page_size],
        many=True,
        context=context or {'request': request},
    )

    return Response({
        'count': total,
        'page': page,
        'page_size': page_size,
        'total_pages': max(1, (total + page_size - 1) // page_size),
        'results': serializer.data,
    })


def _apply_date_filter(qs, request, date_field='created_at'):
    """Apply date_from / date_to query param filters to a queryset."""
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    if date_from:
        try:
            qs = qs.filter(**{f'{date_field}__gte': datetime.fromisoformat(date_from)})
        except (ValueError, TypeError):
            pass
    if date_to:
        try:
            qs = qs.filter(**{f'{date_field}__lte': datetime.fromisoformat(date_to)})
        except (ValueError, TypeError):
            pass
    return qs


def _apply_search(qs, request, search_fields):
    """Apply ?search= param to search across multiple fields."""
    search = request.GET.get('search', '').strip()
    if search:
        query = Q()
        for field in search_fields:
            query |= Q(**{f'{field}__icontains': search})
        qs = qs.filter(query)
    return qs


# ── GPS Traces ─────────────────────────────────────────────────────────────

class GPSTraceListCreateAPIView(APIView):
    """GET /api/survey/gps-traces/ — list engineer's traces
       POST /api/survey/gps-traces/ — create new trace"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = GPSTrace.objects.filter(engineer=engineer)
        qs = _apply_date_filter(qs, request, 'started_at')
        qs = _apply_search(qs, request, ['id'])
        return _paginate(request, qs, GPSTraceSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = GPSTraceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GPSTraceDetailAPIView(APIView):
    """GET /api/survey/gps-traces/<id>/ — get trace with points
       PATCH /api/survey/gps-traces/<id>/ — update trace"""

    def get(self, request, trace_id):
        engineer = _get_engineer(request)
        trace = get_object_or_404(GPSTrace, id=trace_id, engineer=engineer)
        serializer = GPSTraceSerializer(trace)
        return Response(serializer.data)

    def patch(self, request, trace_id):
        engineer = _get_engineer(request)
        trace = get_object_or_404(GPSTrace, id=trace_id, engineer=engineer)
        serializer = GPSTraceSerializer(trace, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GPSPointBatchAPIView(APIView):
    """POST /api/survey/gps-traces/<trace_id>/points/ — batch-create points"""

    def post(self, request, trace_id):
        engineer = _get_engineer(request)
        trace = get_object_or_404(GPSTrace, id=trace_id, engineer=engineer)
        points_data = request.data if isinstance(request.data, list) else [request.data]
        created = []
        for i, pt in enumerate(points_data):
            pt['order'] = pt.get('order', i)
            serializer = GPSPointSerializer(data=pt)
            if serializer.is_valid():
                serializer.save(trace=trace)
                created.append(serializer.data)

        trace.point_count = trace.points.count()
        trace.save(update_fields=['point_count'])
        return Response(created, status=status.HTTP_201_CREATED)


# ── Trench Surveys ─────────────────────────────────────────────────────────

class TrenchSurveyListCreateAPIView(APIView):
    """GET /api/survey/trenches/ — list engineer's trench surveys
       POST /api/survey/trenches/ — create trench survey"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = TrenchSurvey.objects.filter(engineer=engineer)
        feature_id = request.GET.get('feature')
        trench_type = request.GET.get('trench_type')
        surface_type = request.GET.get('surface_type')
        if feature_id:
            qs = qs.filter(feature_id=feature_id)
        if trench_type:
            qs = qs.filter(trench_type=trench_type)
        if surface_type:
            qs = qs.filter(surface_type=surface_type)
        qs = _apply_date_filter(qs, request)
        return _paginate(request, qs, TrenchSurveySerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = TrenchSurveySerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TrenchSurveyDetailAPIView(APIView):
    """GET /api/survey/trenches/<id>/"""

    def get(self, request, trench_id):
        engineer = _get_engineer(request)
        trench = get_object_or_404(TrenchSurvey, id=trench_id, engineer=engineer)
        serializer = TrenchSurveySerializer(trench)
        return Response(serializer.data)

    def patch(self, request, trench_id):
        engineer = _get_engineer(request)
        trench = get_object_or_404(TrenchSurvey, id=trench_id, engineer=engineer)
        serializer = TrenchSurveySerializer(trench, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Existing Assets ────────────────────────────────────────────────────────

class ExistingAssetListCreateAPIView(APIView):
    """GET /api/survey/assets/ — list assets
       POST /api/survey/assets/ — create asset"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = ExistingAsset.objects.filter(engineer=engineer)
        asset_type = request.GET.get('asset_type')
        condition = request.GET.get('condition')
        if asset_type:
            qs = qs.filter(asset_type=asset_type)
        if condition:
            qs = qs.filter(condition=condition)
        qs = _apply_date_filter(qs, request)
        return _paginate(request, qs, ExistingAssetSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = ExistingAssetSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Risk Assessments ────────────────────────────────────────────────────

class RiskAssessmentListCreateAPIView(APIView):
    """GET /api/survey/risks/ — list risk assessments
       POST /api/survey/risks/ — create risk assessment"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = RiskAssessment.objects.filter(engineer=engineer)
        feature_id = request.GET.get('feature')
        category = request.GET.get('category')
        severity = request.GET.get('severity')
        risk_status = request.GET.get('status')
        if feature_id:
            qs = qs.filter(feature_id=feature_id)
        if category:
            qs = qs.filter(category=category)
        if severity:
            qs = qs.filter(severity=severity)
        if risk_status:
            qs = qs.filter(status=risk_status)
        qs = _apply_date_filter(qs, request)
        return _paginate(request, qs, RiskAssessmentSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = RiskAssessmentSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class RiskAssessmentDetailAPIView(APIView):
    """GET /api/survey/risks/<id>/"""

    def get(self, request, risk_id):
        engineer = _get_engineer(request)
        risk = get_object_or_404(RiskAssessment, id=risk_id, engineer=engineer)
        serializer = RiskAssessmentSerializer(risk)
        return Response(serializer.data)

    def patch(self, request, risk_id):
        engineer = _get_engineer(request)
        risk = get_object_or_404(RiskAssessment, id=risk_id, engineer=engineer)
        serializer = RiskAssessmentSerializer(risk, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Hazards ────────────────────────────────────────────────────────────────

class HazardListCreateAPIView(APIView):
    """GET /api/survey/hazards/ — list hazards
       POST /api/survey/hazards/ — create hazard"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = Hazard.objects.filter(engineer=engineer)
        hazard_type = request.GET.get('hazard_type')
        is_active = request.GET.get('is_active')
        if hazard_type:
            qs = qs.filter(hazard_type=hazard_type)
        if is_active is not None:
            qs = qs.filter(is_active=is_active.lower() == 'true')
        qs = _apply_date_filter(qs, request)
        return _paginate(request, qs, HazardSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = HazardSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Field Evidence ─────────────────────────────────────────────────────────

class FieldEvidenceListCreateAPIView(APIView):
    """GET /api/survey/evidence/ — list evidence
       POST /api/survey/evidence/ — upload evidence"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = FieldEvidence.objects.filter(engineer=engineer)
        evidence_type = request.GET.get('evidence_type')
        feature_id = request.GET.get('feature')
        if evidence_type:
            qs = qs.filter(evidence_type=evidence_type)
        if feature_id:
            qs = qs.filter(feature_id=feature_id)
        qs = _apply_date_filter(qs, request, 'captured_at')
        return _paginate(request, qs, FieldEvidenceSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = FieldEvidenceSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ── Survey Changes ─────────────────────────────────────────────────────────

class SurveyChangeListAPIView(APIView):
    """GET /api/survey/changes/ — list changes for a feature"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = _survey_scope(request, SurveyChange.objects.all())
        feature_id = request.GET.get('feature')
        field_name = request.GET.get('field_name')
        if feature_id:
            qs = qs.filter(feature_id=feature_id)
        if field_name:
            qs = qs.filter(field_name__icontains=field_name)
        qs = _apply_date_filter(qs, request)
        return _paginate(request, qs, SurveyChangeSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = SurveyChangeSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SurveyStatusAPIView(APIView):
    """GET /api/survey/status/ — get feature status
       POST /api/survey/status/ — create/update status"""

    def get(self, request):
        engineer = _get_engineer(request)
        feature_id = request.GET.get('feature')
        status_filter = request.GET.get('status')
        if feature_id:
            qs = SurveyStatus.objects.filter(feature_id=feature_id)
        else:
            qs = _survey_scope(request, SurveyStatus.objects.all())
        if status_filter:
            qs = qs.filter(status=status_filter)
        qs = _apply_date_filter(qs, request, 'updated_at')
        return _paginate(request, qs, SurveyStatusSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        feature_id = request.data.get('feature')
        if feature_id:
            # Create or update while preserving original engineer on update
            try:
                obj = SurveyStatus.objects.get(feature_id=feature_id)
                if request.data.get('status'):
                    obj.status = request.data['status']
                if request.data.get('notes') is not None:
                    obj.notes = request.data['notes']
                obj.save()
            except SurveyStatus.DoesNotExist:
                obj = SurveyStatus.objects.create(
                    feature_id=feature_id,
                    engineer=engineer,
                    status=request.data.get('status', 'visited'),
                    notes=request.data.get('notes', ''),
                )
            serializer = SurveyStatusSerializer(obj)
            return Response(serializer.data)
        return Response({'error': 'feature is required'}, status=status.HTTP_400_BAD_REQUEST)


# ── Sync Queue ─────────────────────────────────────────────────────────────

class SyncQueueListCreateAPIView(APIView):
    """POST /api/survey/sync/ — push items to sync queue
       GET /api/survey/sync/ — list pending sync items"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = SyncQueueItem.objects.filter(engineer=engineer)
        sync_status = request.GET.get('status', 'pending')
        item_type = request.GET.get('item_type')
        if sync_status and sync_status != 'all':
            qs = qs.filter(status=sync_status)
        if item_type:
            qs = qs.filter(item_type=item_type)
        qs = _apply_date_filter(qs, request)
        return _paginate(request, qs, SyncQueueItemSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        items = request.data if isinstance(request.data, list) else [request.data]
        created = []
        for item in items:
            serializer = SyncQueueItemSerializer(data=item)
            if serializer.is_valid():
                serializer.save(engineer=engineer)
                created.append(serializer.data)
        return Response(created, status=status.HTTP_201_CREATED)


class SyncQueueProcessAPIView(APIView):
    """POST /api/survey/sync/process/ — process pending sync items"""

    def post(self, request):
        engineer = _get_engineer(request)
        qs = SyncQueueItem.objects.filter(engineer=engineer, status='pending')
        processed = []
        for item in qs:
            try:
                # Mark as in progress
                item.status = SyncQueueItem.Status.IN_PROGRESS
                item.save(update_fields=['status'])

                # Process based on type (would integrate with actual handlers)
                _process_sync_item(item)

                item.status = SyncQueueItem.Status.SYNCED
                item.save(update_fields=['status'])
                processed.append(SyncQueueItemSerializer(item).data)
            except Exception as e:
                item.status = SyncQueueItem.Status.FAILED
                item.error_message = str(e)
                item.save(update_fields=['status', 'error_message'])
        return Response({'processed': len(processed)})


def _process_sync_item(item):
    """Process a sync item based on its type.

    Deserializes the item's payload and creates or updates the corresponding
    survey model (TrenchSurvey, RiskAssessment, Hazard, FieldEvidence).
    GPS trace sync is not yet implemented.
    """
    handler_map = {
        'trench_classification': (TrenchSurveySerializer, TrenchSurvey),
        'risk_assessment': (RiskAssessmentSerializer, RiskAssessment),
        'hazard': (HazardSerializer, Hazard),
        'feature_update': (FieldEvidenceSerializer, FieldEvidence),
        'photo_upload': (FieldEvidenceSerializer, FieldEvidence),
    }

    if item.item_type not in handler_map:
        raise ValueError(f"Unknown sync item type: {item.item_type}")

    serializer_cls, model_cls = handler_map[item.item_type]
    serializer = serializer_cls(data=item.payload)
    if serializer.is_valid():
        serializer.save(engineer=item.engineer)


# ── Survey Features (HLD/Survey Separation) ───────────────────────────────

class SurveyFeatureListCreateAPIView(APIView):
    """GET /api/survey/survey-features/ — list survey features for a project
       POST /api/survey/survey-features/ — create or update a survey feature"""

    def get(self, request):
        engineer = _get_engineer(request)
        qs = _survey_scope(request, SurveyFeature.objects.all())
        project_id = request.GET.get('project')
        layer_id = request.GET.get('layer_id')
        survey_status = request.GET.get('survey_status')
        sync_status = request.GET.get('sync_status')
        hld_feature = request.GET.get('hld_feature')
        if project_id:
            qs = qs.filter(project_id=project_id)
        if layer_id:
            qs = qs.filter(layer_id=layer_id)
        if survey_status:
            qs = qs.filter(survey_status=survey_status)
        if sync_status:
            qs = qs.filter(sync_status=sync_status)
        if hld_feature:
            qs = qs.filter(original_hld_feature_id=hld_feature)
        qs = _apply_date_filter(qs, request, 'updated_at')
        return _paginate(request, qs, SurveyFeatureSerializer)

    def post(self, request):
        engineer = _get_engineer(request)
        serializer = SurveyFeatureSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(engineer=engineer)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SurveyFeatureDetailAPIView(APIView):
    """GET   /api/survey/survey-features/<id>/ — retrieve a survey feature
       PATCH  /api/survey/survey-features/<id>/ — update geometry/attributes/status
       DELETE /api/survey/survey-features/<id>/ — remove a survey feature"""

    def get(self, request, feature_id):
        qs = _survey_scope(request, SurveyFeature.objects.all())
        sf = get_object_or_404(qs, id=feature_id)
        serializer = SurveyFeatureSerializer(sf)
        return Response(serializer.data)

    def patch(self, request, feature_id):
        engineer = _get_engineer(request)
        sf = get_object_or_404(SurveyFeature, id=feature_id, engineer=engineer)
        serializer = SurveyFeatureSerializer(sf, data=request.data, partial=True)
        if serializer.is_valid():
            # Bump version when geometry or attributes change
            data = request.data
            if 'survey_geometry' in data or 'survey_attributes' in data:
                sf.version_number = (sf.version_number or 1) + 1
                if sf.survey_status == SurveyFeature.SurveyStatus.NEW:
                    sf.survey_status = SurveyFeature.SurveyStatus.MODIFIED
                elif sf.survey_status in (
                    SurveyFeature.SurveyStatus.APPROVED,
                    SurveyFeature.SurveyStatus.REJECTED,
                    SurveyFeature.SurveyStatus.COMPLETED,
                ):
                    # Re-edit after a decision -> re-enter the approval queue
                    sf.survey_status = SurveyFeature.SurveyStatus.MODIFIED
                sf.sync_status = SurveyFeature.SyncState.PENDING
            serializer.save(version_number=sf.version_number,
                            survey_status=sf.survey_status,
                            sync_status=sf.sync_status)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, feature_id):
        engineer = _get_engineer(request)
        sf = get_object_or_404(SurveyFeature, id=feature_id, engineer=engineer)
        sf.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SurveyFeatureUpsertAPIView(APIView):
    """POST /api/survey/survey-features/upsert/ — create-or-update by HLD feature

    Body: { original_hld_feature, project, layer_id, ... }
    If a SurveyFeature already exists for the given HLD feature, it is updated;
    otherwise a new one is created.  This is the primary endpoint used by the
    mobile app when the engineer starts editing an HLD feature."""

    def post(self, request):
        engineer = _get_engineer(request)
        hld_feature_id = request.data.get('original_hld_feature')
        project_id = request.data.get('project')
        if not hld_feature_id or not project_id:
            return Response(
                {'error': 'original_hld_feature and project are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sf = SurveyFeature.objects.filter(
            original_hld_feature_id=hld_feature_id,
            project_id=project_id,
            engineer=engineer,
        ).first()

        if sf:
            serializer = SurveyFeatureSerializer(sf, data=request.data, partial=True)
        else:
            serializer = SurveyFeatureSerializer(data=request.data)

        if serializer.is_valid():
            # Conditionally bump version + transition status (matches PATCH logic)
            extra = {}
            if sf:
                data = request.data
                if 'survey_geometry' in data or 'survey_attributes' in data:
                    extra['version_number'] = (sf.version_number or 1) + 1
                    if sf.survey_status == SurveyFeature.SurveyStatus.NEW:
                        extra['survey_status'] = SurveyFeature.SurveyStatus.MODIFIED
                    elif sf.survey_status in (
                        SurveyFeature.SurveyStatus.APPROVED,
                        SurveyFeature.SurveyStatus.REJECTED,
                        SurveyFeature.SurveyStatus.COMPLETED,
                    ):
                        # Re-edit after a decision -> re-enter the approval queue
                        extra['survey_status'] = SurveyFeature.SurveyStatus.MODIFIED
                    extra['sync_status'] = SurveyFeature.SyncState.PENDING
            serializer.save(engineer=engineer, **extra)
            return Response(serializer.data, status=status.HTTP_200_OK if sf else status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class SurveyFeaturePhotoUploadView(APIView):
    """POST /api/survey/survey-features/<id>/upload-photo/

    Attach an evidence photo to a survey feature. Mirrors the HLD
    FeaturePhotoUploadView (projects.api.layers) so engineer-created points
    (which have no HLD Feature row) can carry field photos too.
    """

    def post(self, request, feature_id):
        engineer = _get_engineer(request)
        sf = get_object_or_404(SurveyFeature, id=feature_id, engineer=engineer)

        if "photo" not in request.FILES:
            return Response(
                {"detail": "No photo provided"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        photo = request.FILES["photo"]

        # Validate file type
        allowed_types = ["image/jpeg", "image/png", "image/jpg"]
        if photo.content_type not in allowed_types:
            return Response(
                {"detail": "Invalid file type. Only JPEG and PNG are allowed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate file size (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        if photo.size > max_size:
            return Response(
                {"detail": "File too large. Maximum size is 10MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Save the photo
        sf.photo = photo
        sf.save()

        return Response(
            {
                "id": str(sf.id),
                "photo_url": request.build_absolute_uri(sf.photo.url) if sf.photo else None,
                "uploaded_at": sf.updated_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


class SurveyFeatureApprovalAPIView(APIView):
    """POST /api/survey/survey-features/<id>/approval/

    Planner-only (SUBADMIN) decision endpoint.

    Body: { decision: 'approve' | 'redo', notes?: str }
    - approve -> survey_status = approved
    - redo    -> survey_status = rejected (engineer sees the notes)
    """

    def post(self, request, feature_id):
        if getattr(request.user, 'role', None) != 'SUBADMIN':
            return Response(
                {'error': 'Only planners (SUBADMIN) can approve survey changes'},
                status=status.HTTP_403_FORBIDDEN,
            )

        sf = get_object_or_404(SurveyFeature, id=feature_id)
        decision = request.data.get('decision')
        notes = request.data.get('notes', '') or ''

        if decision == 'approve':
            sf.survey_status = SurveyFeature.SurveyStatus.APPROVED
        elif decision == 'redo':
            sf.survey_status = SurveyFeature.SurveyStatus.REJECTED
        else:
            return Response(
                {'error': "decision must be 'approve' or 'redo'"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sf.review_notes = notes
        sf.save(update_fields=['survey_status', 'review_notes', 'updated_at'])
        serializer = SurveyFeatureSerializer(sf, context={'request': request})
        return Response(serializer.data)
