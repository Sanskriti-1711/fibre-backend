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
