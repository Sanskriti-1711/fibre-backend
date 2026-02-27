# API Documentation

## Base URL
```
{{server}}
```

---

## 1. Authentication API

Most endpoints require:
```
Authorization: Bearer <access_token>
```

### Create User
**POST** `/api/users/`

**Body:**
```json
{
  "email": "eng1@example.com",
  "password": "Pass@12345",
  "role": "ENGINEER"
}
```

**Notes:**
- Role can be `ENGINEER`, `ADMIN`, etc.

---

### Login
**POST** `/api/users/login/`

**Body:**
```json
{
  "email": "admin@admin.com",
  "password": "admin123$"
}
```

**Response:**
```json
{
  "access": "...",
  "refresh": "..."
}
```

**Notes:**
- Frontend should store tokens securely.

---

### Refresh Token
**POST** `/api/users/token/refresh/`

**Body:**
```json
{
  "refresh": "<refresh_token>"
}
```

---

### List Engineers
**GET** `/api/users/engineers/`

**Description:** Returns all engineer users.

---

### Delete Engineer
**DELETE** `/api/users/{engineer_uuid}/`

---

## 2. Projects API

### Create Project
**POST** `/api/projects/`

**Body:**
```json
{
  "name": "Fiber Rollout – Zone A",
  "description": "London North rollout",
  "region": "London"
}
```

---

### List Projects
**GET** `/api/projects/`

**Optional filters:**
- `?status=draft`
- `?region=London`

---

### Latest Projects
**GET** `/api/projects/latest/`

---

### Get Single Project
**GET** `/api/projects/{proj_id}/`

---

### Delete Project
**DELETE** `/api/projects/{proj_id}/`

**Description:** Permanently deletes a project and all associated data including features, assignment jobs, import sessions, and uploaded files. Also cleans up project layers in the microservice.

**Response:** `204 No Content`

**Error Responses:**
- `404 Not Found` - Project does not exist
- `502 Bad Gateway` - Failed to delete project layers in microservice

---

## 3. Import API

Handles GPKG file upload and import pipeline.

### Upload GPKG
**POST** `/api/projects/{proj_id}/import/upload/`

**Form Data:**
- `file`: `<gpkg_file>`

---

### Discover Layers
**POST** `/api/projects/{proj_id}/import/discover/`

**Description:** Returns available layers inside GPKG.

---

### Import Selected Layers
**POST** `/api/projects/{proj_id}/import/import/`

**Body:**
```json
{
  "selected_layers": ["Duct", "JC_HH New"]
}
```

---

### Microservice Direct Import (Optional)
**POST** `https://fiber-import.zeabur.app/import/gpkg`

---

## 4. Geo Data, Layers & Features API

### Fetch All Layers (Django)
**GET** `/api/projects/{proj_id}/layers/`

---

### Fetch Single Layer
**GET** `/api/projects/{proj_id}/layers/{layer_id}/`

---

### Fetch Single Feature
**GET** `/api/projects/{proj_id}/features/{feature_id}/`

---

### Microservice — All Layers GEOJSON
**GET** `/geo/projects/{uuid}/layers`

---

### Map Data (All Layers Combined)
**GET** `/geo/projects/{uuid}/map-data`

**Description:** Returns GeoJSON ready for map rendering.

---

### Health Check
**GET** `/health`

---

## 5. Assignments & Activity API

### Create Assignment
**POST** `/api/assignments/`

**Body:**
```json
{
  "project": "PROJECT_UUID",
  "layer_name": "Duct",
  "feature_id": "FEATURE_UUID",
  "feature_name": "Duct #A-11",
  "engineer": "ENGINEER_UUID"
}
```

---

### Delete Assignment
**DELETE** `/api/assignments/{assignment_id}/`

---

### Activity List
**GET** `/api/activity/`

---

### Create Activity
**POST** `/api/activity/`

**Body:**
```json
{
  "project": "PROJECT_UUID",
  "event_type": "assignment.created",
  "payload": {
    "feature_id": "FEATURE_UUID",
    "layer_name": "Duct",
    "engineer_id": "ENGINEER_UUID"
  }
}
```

---

## 6. Assignment Jobs API

This is the new assignment job system with scope.

**Scopes:**
- `feature`
- `layer`
- `project`

---

### List Jobs
**GET** `/api/assignments/`

**Query Parameters:**
- `project={proj_id}`
- `layer_id={layer_id}` (optional)
- `scope=feature|layer|project` (optional)
- `assignee={engineer_user_id}` (optional)

---

### Create Assignment Job

**POST** `/api/assignments/`

#### Layer Scope Example
```json
{
  "project": "proj_id",
  "scope": "layer",
  "layer_id": "layer_id",
  "assignee": "engineer_id"
}
```

#### Feature Scope Example
```json
{
  "project": "proj_id",
  "scope": "feature",
  "feature": "feature_id",
  "assignee": "engineer_id"
}
```

#### Project Scope Example
```json
{
  "project": "proj_id",
  "scope": "project",
  "assignee": "engineer_id"
}
```

---

### Retrieve Single Job
**GET** `/api/assignments/{job_id}/`

---

### Update Assignee
**PATCH** `/api/assignments/{job_id}/`

**Body:**
```json
{
  "assignee": "new_engineer_user_id"
}
```

---

### Delete Job
**DELETE** `/api/assignments/{job_id}/`

---

### Assignment Summary
**GET** `/api/assignments/summary/`

**Query Parameters:**
- `?project={proj_id}`

**Description:** Returns aggregated stats.

---

## 7. Job Assignments List API

**GET** `/api/assignments/jobs/`

**Description:** Returns job assignments with filtering, pagination, and aggregated statistics. Jobs track assigned features with status aggregation for project/layer scope.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `search` | string | Filter by job ID, project name, or engineer email |
| `project` | uuid | Filter by project ID |
| `layer` | string | Filter by layer ID |
| `status` | string | Filter by status: `pending`, `assigned`, `under_review`, `approved`, `redo` |
| `engineer` | uuid | Filter by assignee ID |
| `scope` | string | Filter by scope: `project`, `layer`, `feature` |
| `page` | integer | Page number (default: 1) |
| `page_size` | integer | Items per page (default: 20) |

**Response:**

```json
{
  "count": 25,
  "page": 1,
  "page_size": 20,
  "results": [
    {
      "id": "46d3b046-53a...",
      "project": {
        "id": "proj_uuid",
        "name": "Vodafone Fiber Rollout - London"
      },
      "scope": "feature",
      "scope_display": "Feature",
      "assignee": {
        "id": "user_uuid",
        "email": "fe4@admin.com",
        "initials": "F"
      },
      "feature": {
        "id": "feature_uuid",
        "status": "under_review",
        "layer_id": "layer_123",
        "layer_name": "Duct"
      },
      "feature_count": 1,
      "status": "under_review",
      "status_display": "Under Review",
      "created_at": "2026-02-26T12:00:00Z"
    }
  ],
  "stats": {
    "total": 100,
    "under_review": 20,
    "approved": 70,
    "redo": 5,
    "pending": 3,
    "assigned": 2
  }
}
```

**Notes:**
- `stats.total` represents the total number of features, not jobs
- For project/layer scope jobs, status is aggregated from all child features
- Status aggregation priority: `redo` > `under_review` > `approved` > `pending` > `assigned`
- Results sorted by `created_at` descending (newest first)

---

## 8. Engineer Activity API

### Get Activity Timeline

**GET** `/api/engineer/activity/`

**Description:** Returns a timeline of engineer activity including recent assignments and feature status changes.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `engineer` | uuid | Yes | Engineer user ID |
| `days` | integer | No | Number of days to look back (default: 30) |

**Response:**

```json
{
  "engineer_id": "uuid",
  "period_days": 30,
  "activities": [
    {
      "type": "assignment",
      "timestamp": "2026-02-27T10:00:00Z",
      "project": {
        "id": "proj_uuid",
        "name": "Fiber Rollout - Zone A"
      },
      "scope": "layer",
      "scope_display": "Layer",
      "target": {
        "type": "layer",
        "layer_id": "layer_123"
      }
    },
    {
      "type": "feature_update",
      "timestamp": "2026-02-26T15:30:00Z",
      "feature": {
        "id": "feature_uuid",
        "layer_name": "Duct",
        "status": "under_review",
        "status_display": "Under Review"
      },
      "project": {
        "id": "proj_uuid",
        "name": "Fiber Rollout - Zone A"
      }
    }
  ],
  "total_count": 2
}
```

---

## 9. Engineer Stats API

### Get Performance Statistics

**GET** `/api/engineer/stats/`

**Description:** Returns performance metrics for an engineer including overall stats, daily breakdown, and project breakdown.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `engineer` | uuid | Yes | Engineer user ID |
| `days` | integer | No | Period for daily breakdown and recent stats (default: 30) |

**Response:**

```json
{
  "engineer_id": "uuid",
  "period_days": 30,
  "overall": {
    "total": 150,
    "approved": 120,
    "under_review": 20,
    "redo": 5,
    "assigned": 3,
    "pending": 2,
    "approval_rate": 80.0
  },
  "recent_period": {
    "total": 45,
    "approved": 30
  },
  "daily_breakdown": [
    {
      "date": "2026-01-28",
      "updated": 2,
      "approved": 1
    }
  ],
  "project_breakdown": [
    {
      "project": {
        "id": "proj_uuid",
        "name": "Fiber Rollout - Zone A"
      },
      "total": 100,
      "approved": 80,
      "under_review": 15,
      "redo": 5
    }
  ]
}
```

---

## 10. Feature Field Measurements API

### Update Field Measurements

**PATCH** `/api/features/{feature_id}/field-measurements/`

**Description:** Updates field measurements and comparison notes for a feature.

**Body:**

```json
{
  "field_measurements": {
    "length": 120.5,
    "width": 50.2,
    "depth": 1.5
  },
  "comparison_notes": "Verified measurements on site. Actual depth differs from plan by 0.3m."
}
```

**Response:**

```json
{
  "id": "feature_uuid",
  "field_measurements": {
    "length": 120.5,
    "width": 50.2,
    "depth": 1.5
  },
  "comparison_notes": "Verified measurements on site. Actual depth differs from plan by 0.3m.",
  "updated_at": "2026-02-27T10:30:00Z"
}
```

**Notes:**
- Either `field_measurements` or `comparison_notes` can be provided independently
- `field_measurements` must be a JSON object

---

## 11. Feature Submit API

### Submit Features for Review

**POST** `/api/features/submit/`

**Description:** Bulk submit assigned features for review. Changes status from `assigned` or `redo` to `under_review`.

**Body:**

```json
{
  "feature_ids": ["feature_uuid_1", "feature_uuid_2"],
  "engineer": "engineer_uuid"
}
```

**Response:**

```json
{
  "submitted_count": 2,
  "feature_ids": ["feature_uuid_1", "feature_uuid_2"],
  "new_status": "under_review",
  "status_display": "Under Review"
}
```

**Error Responses:**

```json
{
  "detail": "Features not found or not assigned: ['feature_uuid_3']"
}
```

```json
{
  "detail": "Some features cannot be submitted",
  "invalid_features": [
    {"id": "feature_uuid_1", "status": "approved"}
  ]
}
```

**Notes:**
- Features must be assigned to the specified engineer
- Only features with status `assigned` or `redo` can be submitted
- Features with status `approved`, `under_review`, or `pending` cannot be submitted
