# **Completion Rate System Documentation**
## **Project: Planning & Field Execution Platform**
---
# **1\. Overview**
The system tracks project progress using **two types of completion metrics**:
1. **Standard Completion Rate**  
2. **Dynamic Completion Rate**
Both metrics are required because they measure **different aspects of progress**:
| Metric | Measures |
| :---- | :---- |
| Standard Completion | How many items are completed |
| Dynamic Completion | How important the completed work is |
These metrics are calculated only using **approved features** (not submitted or draft).
---
# **2\. Standard Completion Rate**
## **Definition**
Standard Completion Rate represents the percentage of completed features compared to the total number of features in a project.
It is a **simple count-based progress metric**.
---
## **Formula**
Standard Completion % \=  
(Approved Features Count / Total Features Count) × 100  
---
## **Example**
| Item | Value |
| ----- | ----- |
| Total Features | 1000 |
| Approved Features | 600 |
Standard Completion \= (600 / 1000\) × 100 \= 60%  
---
## **Purpose**
Standard completion is used for:
* High-level project reporting
* Client visibility
* Simple progress tracking
* KPI dashboards
---
## **Characteristics**
| Property | Value |
| ----- | ----- |
| Based on importance | ❌ No |
| Based on count | ✅ Yes |
| Easy to understand | ✅ Yes |
| Accurate effort reflection | ❌ Limited |
---
# **3\. Dynamic Completion Rate**
## **Definition**
Dynamic Completion Rate represents progress based on the **importance or weight** of completed work.
Not all features contribute equally to project completion. Some layers or features may be more critical and should contribute more to the completion percentage.
Dynamic completion accounts for this using **weights**.
---
## **Weight Sources**
Weights can be defined at:
1. **Layer Level** (recommended for Phase 1\)
2. Feature Level (optional future enhancement)
Example:
| Layer | Weight |
| ----- | ----- |
| Backbone Fiber | 40% |
| Distribution | 30% |
| Poles | 20% |
| Chambers | 10% |
Total \= 100%
---
## **Formula**
Step 1 — Calculate layer progress:
Layer Progress % \=  
(Approved Features in Layer / Total Features in Layer) × 100
Step 2 — Apply weight:
Weighted Contribution \=  
Layer Progress × Layer Weight
Step 3 — Sum all layers:
Dynamic Completion % \=  
Sum of all Weighted Contributions  
---
## **Example**
| Layer | Done | Total | Progress | Weight | Contribution |
| ----- | ----- | ----- | ----- | ----- | ----- |
| Backbone | 80 | 100 | 80% | 40% | 32% |
| Distribution | 150 | 300 | 50% | 30% | 15% |
| Poles | 200 | 200 | 100% | 20% | 20% |
| Chambers | 20 | 100 | 20% | 10% | 2% |
Dynamic Completion \= 32 \+ 15 \+ 20 \+ 2 \= 69%  
---
## **Purpose**
Dynamic completion is used for:
* Real project health assessment
* Planning accuracy
* Resource allocation decisions
* Management insights
---
## **Characteristics**
| Property | Value |
| ----- | ----- |
| Based on importance | ✅ Yes |
| Based on count | ❌ No |
| Reflects effort | ✅ Better |
| More complex | ✅ Yes |
---
# **4\. Key Difference**
| Aspect | Standard | Dynamic |
| ----- | ----- | ----- |
| Measures | Quantity | Importance |
| Uses weights | No | Yes |
| Accuracy | Medium | High |
| Complexity | Low | Medium |
| Business insight | Low | High |
---
# **5\. Approval Dependency (Critical Rule)**
Completion must only consider **approved features**.

Workflow:
Engineer completes feature  
→ Status = `assigned` or `redo`  
→ Engineer submits → Status = `under_review`  
→ Admin reviews  
→ Status = `approved`  
→ Included in completion calculations

Statuses:

| Status | Included in Completion? |
| ----- | ----- |
| `pending` | ❌ No |
| `assigned` | ❌ No |
| `under_review` | ❌ No |
| `approved` | ✅ Yes |
| `redo` | ❌ No |
---
# **6\. Data Requirements**

## **Feature Table Fields**
```
Feature
- id: UUID (primary key)
- project_id: ForeignKey to Project
- layer_id: CharField (string identifier)
- layer_name: CharField
- status: CharField (pending, assigned, under_review, approved, redo)
- field_measurements: JSONField
- comparison_notes: TextField
- submitted_at: DateTimeField (null until submitted)
- approved_at: DateTimeField (null until approved)
- created_at, updated_at: DateTimeField
```

**Note:** Assignment tracking is handled via separate `AssignmentJob` model, not `assigned_engineer_id` field.

---

## **LayerWeight Table Fields**
```
LayerWeight
- id: UUID (primary key)
- project_id: ForeignKey to Project
- layer_id: CharField (string identifier)
- weight_percentage: DecimalField (max 100.00)
- created_at, updated_at: DateTimeField
```

**Note:** Weights do NOT need to sum to 100%. Partial weighting is supported - undefined layers receive auto-weighted equal share of remaining percentage.
# **7\. API Requirements**

## **Project Completion API**

**GET** `/api/projects/{id}/completion/`

Response:
```json
{
  "project_id": "uuid-string",
  "standard_completion": 60.0,
  "dynamic_completion": 69.0,
  "total_features": 1000,
  "approved_features": 600,
  "weights_defined": true,
  "layers": [
    {
      "layer_id": "backbone_fiber",
      "layer_name": "Backbone Fiber",
      "weight": 40.0,
      "total_features": 100,
      "approved_features": 80,
      "progress_percentage": 80.0,
      "contribution": 32.0
    }
  ]
}
```

---
# **8\. Validation Rules**
1. Layer weights cannot exceed 100% total (but can be partial).
2. Only approved features count toward completion.
3. Completion cannot exceed 100%.
4. Unweighted layers receive equal share of remaining percentage.
5. Features must be `under_review` to be approved or rejected.
6. Engineers can only submit features assigned to them.
---
# **9\. Phase Scope**
## **Phase 1 (Implemented)**
Included:
* Standard completion calculation
* Dynamic completion with layer-level weights
* Partial weighting support (auto-equal distribution)
* Approval workflow (submit → review → approve/reject)
* Completion API endpoints
* Layer weight management endpoints

Excluded:
* Completion dashboard UI
* Feature-level weighting
* AI-based completion prediction
* Time-based performance scoring
---
# **10\. Simple Summary (For Non-Technical Stakeholders)**
Standard Completion shows how much work is finished.  
 Dynamic Completion shows how valuable the finished work is.
Both are required to understand real project progress.

---

# **11\. Implementation Summary**

## **Built in Phase 1:**
1. ✅ Feature approval workflow (submit → under_review → approve/reject)
2. ✅ Layer weights storage (`LayerWeight` model)
3. ✅ Completion calculation service (on-demand, partial weighting)
4. ✅ Completion API (`GET /api/projects/{id}/completion/`)
5. ✅ Layer Weights API (GET/PUT endpoints)

## **Future Work:**
- Dashboard UI for visualizing completion metrics
- Feature-level weighting (vs layer-level)
- AI-based completion prediction
- Time-based performance scoring

---

# **12\. Implementation Details (Built)**

This section documents the actual implementation as built in the Fiber Backend.

## **12.1 Data Models**

### **LayerWeight Model** (`projects/models/layer_weight.py`)
```python
LayerWeight
- id: UUID (primary key)
- project: ForeignKey to Project
- layer_id: CharField(max_length=255)
- weight_percentage: DecimalField(max_digits=5, decimal_places=2)
- created_at: DateTimeField
- updated_at: DateTimeField

# Unique constraint: (project, layer_id)
```

### **Feature Model Extensions** (`projects/models/feature.py`)
Added fields:
```python
Feature
- submitted_at: DateTimeField(null=True, blank=True)
- approved_at: DateTimeField(null=True, blank=True)
```

Feature statuses:
- `pending` - Initial status after import
- `assigned` - Assigned to an engineer
- `under_review` - Submitted by engineer, awaiting review
- `approved` - Approved by admin, counts toward completion
- `redo` - Rejected, needs rework

### **Project Model** (`projects/models/project.py`)
Renamed field:
```python
Project
- standard_completion: DecimalField(max_digits=5, decimal_places=2, default=0)
  # Previously: completion_percentage
```

## **12.2 Completion Calculation Service** (`projects/services/completion_service.py`)

### **Key Features:**
- **On-demand calculation** - Computed at request time (no caching)
- **Partial weighting support** - Layers without explicit weights receive equal share of remaining percentage
- **Decimal precision** - Uses Decimal for accurate calculations with 2 decimal places

### **Partial Weighting Algorithm:**
```
1. Calculate total defined weight from LayerWeight records
2. Identify undefined layers (layers without explicit weights)
3. remaining_weight = 100 - total_defined_weight
4. If undefined layers exist and remaining_weight > 0:
   auto_weight = remaining_weight / count(undefined_layers)
5. Each undefined layer gets auto_weight
```

Example:
- Defined: Backbone=40%, Distribution=30% (total 70%)
- Undefined layers: Poles, Chambers (2 layers)
- Auto-weight for each: (100-70)/2 = 15%
- Final: Poles=15%, Chambers=15%

## **12.3 API Endpoints**

### **Completion API**
```
GET /api/projects/{id}/completion/
```

### **Layer Weights API**
```
GET /api/projects/{id}/layers/weights/     # Get current weights
PUT /api/projects/{id}/layers/weights/     # Update weights
```

### **Feature Workflow APIs**
```
POST /api/features/submit/    # Engineer submits for review
POST /api/features/approve/   # Admin approves features
POST /api/features/reject/    # Admin rejects features (to redo)
```

## **12.4 Implementation Differences from Original Spec**

| Aspect | Original Spec | Actual Implementation |
|--------|--------------|----------------------|
| **Layer Model** | Dedicated Layer table | LayerWeight model stores weights per (project, layer_id) |
| **Weight Sum** | Must equal 100% | Can be partial (undefined layers auto-weighted) |
| **Assignment** | assigned_engineer_id on Feature | Separate AssignmentJob model |
| **Status Names** | Draft, Submitted, Approved | pending, assigned, under_review, approved, redo |
| **Approval Tracking** | approved_at timestamp | ✅ Implemented as specified |
| **Completion Storage** | Not specified | On-demand calculation (no caching) |

## **12.5 Validation Rules Implemented**

1. **Layer weight validation** - Total weight cannot exceed 100%
2. **Approval dependency** - Only `approved` status features count toward completion
3. **Status workflow** - Features must be `under_review` to be approved/rejected
4. **Assignment check** - Engineers can only submit features assigned to them

## **12.6 File Locations**

```
projects/
  models/
    layer_weight.py          # LayerWeight model
    feature.py                # Feature with submitted_at, approved_at
    project.py                # Project with standard_completion
  services/
    completion_service.py    # Completion calculation logic
  api/
    completion.py            # ProjectCompletionAPIView
    layer_weights.py         # ProjectLayerWeightsAPIView
    urls.py                  # Endpoint routing

assignments/
  api/
    views.py                 # FeatureApproveAPIView, FeatureRejectAPIView
    urls.py                  # Endpoint routing
```

---

# **13\. API Usage Examples**

## **Get Project Completion**
```bash
curl /api/projects/123e4567/completion/
```

## **Set Layer Weights**
```bash
curl -X PUT /api/projects/123e4567/layers/weights/ \
  -H "Content-Type: application/json" \
  -d '{"weights": {"backbone": 40, "distribution": 30}}'
```

## **Approve Features**
```bash
curl -X POST /api/features/approve/ \
  -H "Content-Type: application/json" \
  -d '{
    "feature_ids": ["feat-1", "feat-2"],
    "reviewer": "admin-id",
    "notes": "Approved after verification"
  }'
```

---

*Documentation updated: 2026-02-28*
*Implementation Phase: 1 (Complete)*