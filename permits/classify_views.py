"""Classify views (P22b) — heuristic permit-type classifier + label collection.

POST /api/ftth/permits/classify/          → {features|flat body} → {permit_type, rule_id, confidence, reasons, candidates}
POST /api/ftth/permits/classify/feedback/ → {features, predicted, true_rule_id|true_permit_type} → stored label
GET  /api/ftth/permits/classify/stats/    → {total, by_true_rule, by_predicted_rule, milestone_500}

Heuristic is deterministic (permits/classify.py). Feedback is stored as
JSONL next to the module (classify_labels.jsonl) so the training set grows
without a migration. No LLM. JWT-required like the rest of permits.

Cross-project: classifier works on trench attributes alone; optional
project_id / permit_id are stored with the label for later stratified training.
"""

from __future__ import annotations

from django.http import JsonResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from .classify import classify, label_stats, store_feedback


def _coerce_features(data: dict) -> dict:
    """Accept either {features: {...}} or a flat feature dict."""
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("features"), dict):
        # Merge top-level hints like project_id/permit_id into features if present,
        # but keep the features dict as the truth.
        feats = dict(data["features"])
        for k in ("project_id", "permit_id", "municipality", "street_name"):
            if k in data and k not in feats:
                feats[k] = data[k]
        return feats
    # Flat body — strip API envelope keys
    skip = {"project_id", "permit_id", "true_rule_id", "true_permit_type", "note", "predicted"}
    return {k: v for k, v in data.items() if k not in skip}


class PermitClassifyView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        if isinstance(data, dict) and "features" in data:
            features = _coerce_features(data)
        else:
            # Also accept plain feature dict in the body
            features = _coerce_features(data) if isinstance(data, dict) else {}
        # Also handle nested 'trench' / 'row' envelope
        if not features and isinstance(data, dict):
            for key in ("trench", "row", "attributes"):
                if isinstance(data.get(key), dict):
                    features = data[key]
                    break
        if not isinstance(features, dict):
            return JsonResponse({"detail": "features must be an object."}, status=400)

        result = classify(features)
        # Echo a few envelope fields for convenience
        result["project_id"] = (data.get("project_id") if isinstance(data, dict) else None) or features.get("project_id")
        result["permit_id"] = (data.get("permit_id") if isinstance(data, dict) else None) or features.get("permit_id")
        return JsonResponse(result)

    def get(self, request):
        # Convenience: GET with ?fclass=&SURFACE=&... also works (copilot quick check)
        q = request.GET
        if not q:
            return JsonResponse({"detail": "Use POST with {features: {...}} — or pass ?fclass=&SURFACE= as a quick check."}, status=400)
        features: dict = {}
        for k in ("fclass", "highway", "SURFACE", "surface", "trench_type", "TRENCH_TYPE", "CONSTRUCT", "REINSTATE", "VERIFY_STATUS", "REUSE_SOURCE", "INFRA_STATUS", "street_name", "municipality", "zone_type", "rail_crossing", "river_crossing"):
            v = q.get(k)
            if v not in (None, ""):
                features[k] = v
        # env_flags as comma list or repeated ?env=railway,waterway
        env_raw = q.get("env_flags") or q.get("env") or ""
        if env_raw:
            env: dict[str, bool] = {}
            for part in str(env_raw).split(","):
                p = part.strip().lower()
                if p:
                    env[p] = True
            if env:
                features["env_flags"] = env
        for flag in ("railway", "waterway", "protected_area", "tree"):
            v = q.get(flag)
            if v not in (None, ""):
                features.setdefault("env_flags", {})[flag] = str(v).lower() in ("1", "true", "yes", "on")
        result = classify(features)
        return JsonResponse(result)


class PermitClassifyFeedbackView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        if not isinstance(data, dict):
            return JsonResponse({"detail": "JSON body is required."}, status=400)

        features = data.get("features")
        if features is None:
            # Accept flat body as features
            features = _coerce_features(data)
        if not isinstance(features, dict):
            return JsonResponse({"detail": "features must be an object."}, status=400)

        predicted = data.get("predicted")
        if predicted is None:
            # Re-run classify to get predicted if caller didn't send it
            predicted = classify(features)
        if not isinstance(predicted, dict):
            predicted = {}

        true_rule_id = (data.get("true_rule_id") or data.get("true_rule") or "").strip() or None
        true_permit_type = (data.get("true_permit_type") or data.get("true_type") or data.get("permit_type") or "").strip() or None
        if not true_rule_id and not true_permit_type:
            return JsonResponse({"detail": "true_rule_id or true_permit_type is required — the correction you are providing."}, status=400)

        project_id = (data.get("project_id") or features.get("project_id") or "").strip() or None
        permit_id = (data.get("permit_id") or features.get("permit_id") or "").strip() or None
        note = data.get("note")
        user_email = None
        try:
            user_email = getattr(request.user, "email", None) or str(request.user)
        except Exception:
            pass

        record = store_feedback(
            features,
            predicted,
            true_rule_id,
            true_permit_type,
            project_id=project_id,
            permit_id=permit_id,
            user_email=user_email,
            note=note,
        )
        stats = label_stats()
        return JsonResponse({"stored": record, "stats": stats}, status=201)


class PermitClassifyStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return JsonResponse(label_stats())
