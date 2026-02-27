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
