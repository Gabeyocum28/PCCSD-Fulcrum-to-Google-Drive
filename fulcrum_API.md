# Fulcrum API Complete Reference

Base URL: `https://api.fulcrumapp.com/api/v2`

**Last Verified**: January 2026

---

## Authentication

```
X-ApiToken: <your_api_token>
Accept: application/json
Content-Type: application/json  (for POST/PUT)
```

Get token from: https://web.fulcrumapp.com/settings/api

---

## Rate Limits

- **REST API**: 5,000 calls per hour per user
- **Query API**: 5,000 calls per hour per user + 10 second max query time

---

## Pagination

All list endpoints return:
```json
{
  "current_page": 1,
  "total_pages": 10,
  "total_count": 1000,
  "per_page": 100,
  "<resource>": [...]
}
```

Parameters:
- `page` - Page number (default: 1)
- `per_page` - Items per page (default/max: 20,000)

---

## Account Stats (Your Account)

| Resource | Count |
|----------|-------|
| Forms | 341 |
| Records | 521,736 |
| Photos | 1,582,157 |
| Videos | 89 |
| Audio | 1 |
| Signatures | 58 |
| Projects | 33 |
| Layers | 63 |
| Memberships | 10 |
| Choice Lists | 8 |
| Classification Sets | 8 |
| Report Templates | 2,447 |
| Changesets | 140,489 |

---

# REST API Endpoints

## Users API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/users.json` | ✓ 200 | Get current user info |

**Response**: `{"user": {...}}`

---

## Forms API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/forms.json` | ✓ 200 | List all forms |
| GET | `/forms/{id}.json` | ✓ 200 | Get single form |
| GET | `/forms/{id}/history.json` | ✓ 200 | Get form revision history |
| POST | `/forms.json` | - | Create form |
| PUT | `/forms/{id}.json` | - | Update form (requires full object) |
| DELETE | `/forms/{id}.json` | - | Delete form |

**List Parameters**:
- `page`, `per_page`

**Required for Create/Update**: `name`, `elements[]`

**Form Object Keys**:
```
name, description, record_count, record_changed_at, status, version,
bounding_box, record_title_key, title_field_keys, status_field, auto_assign,
hidden_on_dashboard, geometry_types, geometry_required, script, system_type,
projects_enabled, assignment_enabled, attachment_ids, field_effects,
fastfill_audio_enabled, id, created_at, updated_at, image, image_thumbnail,
image_small, image_large, created_by, created_by_id, updated_by, updated_by_id,
report_templates, elements
```

**Field Types in elements[]**:
- TextField, DateTimeField, TimeField, DateField
- ChoiceField, ClassificationField, YesNoField
- PhotoField, VideoField, AudioField, SignatureField
- AddressField, BarcodeField, CalculatedField
- RecordLinkField, HyperlinkField
- Section (container), Repeatable (nested records)

---

## Records API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/records.json` | ✓ 200 | List all records |
| GET | `/records/{id}.json` | ✓ 200 | Get single record |
| GET | `/records/{id}/history.json` | ✓ 200 | Get record revision history |
| POST | `/records.json` | - | Create record |
| PUT | `/records/{id}.json` | - | Update record (requires full object) |
| PATCH | `/records/{id}.json` | - | Partial update |
| DELETE | `/records/{id}.json` | - | Delete record |

**List Parameters**:
- `page`, `per_page`
- `form_id` - Filter by form
- `project_id` - Filter by project
- `updated_since` - Epoch seconds
- `created_since` - Epoch seconds
- `client_created_since`, `client_created_before` - Epoch seconds

**Optional Headers**:
- `x-skipworkflows: true` - Skip workflow triggers
- `x-skipwebhooks: true` - Skip webhook triggers

**Required for Create**: `form_id`, `latitude`, `longitude`, `form_values`

**Record Object Keys**:
```
id, status, version, created_at, updated_at, client_created_at, client_updated_at,
created_by, created_by_id, updated_by, updated_by_id, created_location,
updated_location, created_duration, updated_duration, edited_duration,
form_id, project_id, record_series_id, assigned_to, assigned_to_id,
form_values, latitude, longitude, altitude, geometry, speed, course,
horizontal_accuracy, vertical_accuracy, system_status
```

**form_values**: Object with field keys (4-char hex) mapping to field values

**geometry**: GeoJSON (Point, LineString, Polygon, MultiLineString, MultiPolygon)
- Coordinates use [longitude, latitude] order

---

## Photos API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/photos.json` | ✓ 200 | List all photos |
| GET | `/photos/{id}.json` | ✓ 200 | Get photo metadata |
| GET | `/photos/{id}.jpg` | ✓ 200 | Get original photo file |
| GET | `/photos/{id}/thumbnail.jpg` | ✓ 200 | Get thumbnail (small) |
| GET | `/photos/{id}/large.jpg` | ✓ 200 | Get large version |
| POST | `/photos.json` | - | Upload photo |

**List Parameters**:
- `page`, `per_page`
- `form_id` - Filter by form
- `record_id` - Filter by record

**Upload**: `multipart/form-data` with `photo[access_key]`, `photo[file]`

**Photo Object Keys**:
```
access_key, created_at, updated_at, created_by, created_by_id,
updated_by, updated_by_id, uploaded, stored, processed, deleted_at,
record_id, form_id, file_size, content_type, latitude, longitude, url
```

**Note**: No DELETE method. Unlink from record to remove.

---

## Videos API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/videos.json` | ✓ 200 | List all videos |
| GET | `/videos/{id}.json` | ✓ 200 | Get video metadata |
| GET | `/videos/{id}.mp4` | ✓ 200 | Get original video |
| GET | `/videos/{id}/small.mp4` | ✓ 200 | Get small version |
| GET | `/videos/{id}/medium.mp4` | - | Get medium version |
| GET | `/videos/{id}/thumbnail_small.jpg` | ✓ 200 | Small thumbnail |
| GET | `/videos/{id}/thumbnail_small_square.jpg` | - | Small square thumbnail |
| GET | `/videos/{id}/thumbnail_medium.jpg` | - | Medium thumbnail |
| GET | `/videos/{id}/thumbnail_large.jpg` | - | Large thumbnail |
| GET | `/videos/{id}/thumbnail_huge.jpg` | - | Huge thumbnail |
| GET | `/videos/{id}/track.json` | ✓/404 | GPS track (JSON) |
| GET | `/videos/{id}/track.geojson` | ✓/404 | GPS track (GeoJSON) |
| GET | `/videos/{id}/track.gpx` | ✓/404 | GPS track (GPX) |
| GET | `/videos/{id}/track.kml` | ✓/404 | GPS track (KML) |
| POST | `/videos.json` | - | Upload video |

**Note**: Track endpoints return 404 if video has no GPS track. Thumbnail/file endpoints return 404 if `stored=false` or `processed=false` or `deleted_at` is set.

**Video Object Keys**:
```
access_key, created_at, updated_at, created_by, created_by_id,
updated_by, updated_by_id, uploaded, stored, processed, deleted_at,
record_id, form_id, file_size, content_type, url, track, status
```

---

## Audio API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/audio.json` | ✓ 200 | List all audio |
| GET | `/audio/{id}.json` | ✓ 200 | Get audio metadata |
| GET | `/audio/{id}.m4a` | - | Get audio file |
| GET | `/audio/{id}/track.json` | ✓/404 | GPS track (JSON) |
| GET | `/audio/{id}/track.geojson` | ✓/404 | GPS track (GeoJSON) |
| GET | `/audio/{id}/track.gpx` | ✓/404 | GPS track (GPX) |
| GET | `/audio/{id}/track.kml` | ✓/404 | GPS track (KML) |
| POST | `/audio.json` | - | Upload audio |

**Note**: Track endpoints return 404 if audio has no GPS track.

---

## Signatures API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/signatures.json` | ✓ 200 | List all signatures |
| GET | `/signatures/{id}.json` | ✓ 200 | Get signature metadata |
| GET | `/signatures/{id}.png` | - | Get signature file |
| GET | `/signatures/{id}/thumbnail.png` | - | Get thumbnail |
| POST | `/signatures.json` | - | Upload signature |

---

## Sketches API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/sketches.json` | ✓ 200 | List all sketches |
| GET | `/sketches/{id}.json` | ✓ 200 | Get sketch metadata |
| GET | `/sketches/{id}.png` | - | Get sketch file |

---

## Projects API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/projects.json` | ✓ 200 | List all projects |
| GET | `/projects/{id}.json` | ✓ 200 | Get single project |
| POST | `/projects.json` | - | Create project |
| PUT | `/projects/{id}.json` | - | Update project |
| DELETE | `/projects/{id}.json` | - | Delete project |

**Required for Create**: `name`

**Optional**: `description`, `status`, `customer`, `external_job_id`, `start_date`, `end_date`, `project_manager_id`

---

## Layers API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/layers.json` | ✓ 200 | List all layers |
| GET | `/layers/{id}.json` | ✓ 200 | Get single layer |
| POST | `/layers.json` | - | Create layer |
| PUT | `/layers/{id}.json` | - | Update layer |
| DELETE | `/layers/{id}.json` | - | Delete layer |

**Required for Create**: `name`, `type`, `source`

**Layer Types**: `fulcrum`, `xyz`, `tilejson`, `geojson`, `mbtiles`, `wms`, `feature-service`

---

## Choice Lists API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/choice_lists.json` | ✓ 200 | List all choice lists |
| GET | `/choice_lists/{id}.json` | ✓ 200 | Get single choice list |
| POST | `/choice_lists.json` | - | Create choice list |
| PUT | `/choice_lists/{id}.json` | - | Update choice list |
| DELETE | `/choice_lists/{id}.json` | - | Delete choice list |

**Required for Create**: `name`, `choices[]`

**Choice Structure**: `{"label": "Display Text", "value": "stored_value"}`

---

## Classification Sets API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/classification_sets.json` | ✓ 200 | List all classification sets |
| GET | `/classification_sets/{id}.json` | ✓ 200 | Get single classification set |
| POST | `/classification_sets.json` | - | Create classification set |
| PUT | `/classification_sets/{id}.json` | - | Update classification set |
| DELETE | `/classification_sets/{id}.json` | - | Delete classification set |

**Required for Create**: `name`, `items[]`

**Item Structure**: `{"label": "...", "value": "...", "child_classifications": [...]}`

---

## Roles API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/roles.json` | ✓ 200 | List all roles |

---

## Memberships API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/memberships.json` | ✓ 200 | List all memberships |
| GET | `/memberships/{id}.json` | ✓ 200 | Get single membership |
| POST | `/memberships.json` | - | Create membership |
| PUT | `/memberships/{id}.json` | - | Update membership |
| DELETE | `/memberships/{id}.json` | - | Delete membership |
| POST | `/memberships/{id}/permission_changes.json` | - | Modify permissions |

**Required for Create**: `email`

**Optional**: `first_name`, `last_name`, `role_id`

---

## Groups API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/groups.json` | ✓ 200 | List all groups |
| GET | `/groups/{id}.json` | ✓ 200 | Get single group |
| POST | `/groups.json` | - | Create group |
| PUT | `/groups/{id}.json` | - | Update group |
| DELETE | `/groups/{id}.json` | - | Delete group |
| POST | `/groups/{id}/permission_changes.json` | - | Modify group permissions |

---

## Authorizations API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/authorizations.json` | ✓ 200 | List all authorizations |
| GET | `/authorizations/{id}.json` | ✓ 200 | Get single authorization |
| POST | `/authorizations.json` | - | Create authorization |
| PUT | `/authorizations/{id}.json` | - | Update authorization |
| DELETE | `/authorizations/{id}.json` | - | Delete authorization |

---

## Webhooks API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/webhooks.json` | ✓ 200 | List all webhooks |
| GET | `/webhooks/{id}.json` | - | Get single webhook |
| POST | `/webhooks.json` | - | Create webhook |
| PUT | `/webhooks/{id}.json` | - | Update webhook |
| DELETE | `/webhooks/{id}.json` | - | Delete webhook |

**Required for Create**: `name`, `url`

**Optional**: `active`, `run_for_bulk_actions`

**Webhook Events**:
- `form.create`, `form.update`, `form.delete`
- `record.create`, `record.update`, `record.delete`
- `choice_list.create`, `choice_list.update`, `choice_list.delete`
- `classification_set.create`, `classification_set.update`, `classification_set.delete`

**Rules**:
- Max 10 active webhooks per plan
- Must respond HTTP 200-207 within 20 seconds
- Retries: exponential backoff, max 25 attempts over ~20 days

---

## Workflows API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/workflows.json` | ✓ 200 | List all workflows |
| GET | `/workflows/{id}.json` | ✓ 200 | Get single workflow |
| POST | `/workflows.json` | - | Create workflow |
| PUT | `/workflows/{id}.json` | - | Update workflow |
| DELETE | `/workflows/{id}.json` | - | Delete workflow |

---

## Changesets API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/changesets.json` | ✓ 200 | List all changesets |
| GET | `/changesets/{id}.json` | ✓ 200 | Get single changeset |
| POST | `/changesets.json` | - | Create changeset |
| PUT | `/changesets/{id}.json` | - | Update changeset |

**Required for Create**: `form_id`

---

## Batch API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/batch.json` | ✓ 200 | List all batches |
| GET | `/batch/{id}.json` | ✓ 200 | Get single batch |
| POST | `/batch.json` | - | Create batch operation |
| POST | `/batch/{id}/operations.json` | - | Add operations to batch |
| POST | `/batch/{id}/start.json` | - | Start batch |

**Supported Operations**:
- Delete records
- Update project/assignee/status

**Limits**: Max 10,000 records per batch

**Selection Methods** (use one, not both):
- `query`: SQL query returning `id` column
- `ids`: Array of record IDs

**Warning**: Once started, batches CANNOT be terminated

---

## Reports API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| POST | `/reports.json` | ✓ 201 | Generate report |
| GET | `/reports/{id}.json` | ✓ 200 | Get report status |

**Required for Create**: `record_id`

**Optional**: `template_id`

**Report States**: `pending`, `running`, `completed`, `failed`

---

## Report Templates API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/report_templates.json` | ✓ 200 | List all templates |
| GET | `/report_templates/{id}.json` | ✓ 200 | Get single template |
| POST | `/report_templates.json` | - | Create template |
| PUT | `/report_templates/{id}.json` | - | Update template |
| DELETE | `/report_templates/{id}.json` | - | Delete template |

---

## Audit Logs API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/audit_logs.json` | ✓ 200 | List all audit logs |
| GET | `/audit_logs/{id}.json` | ✓ 200 | Get single audit log |

---

## Attachments API

| Method | Endpoint | Status | Description |
|--------|----------|--------|-------------|
| GET | `/attachments.json` | ✗ 400 | Requires parameters |
| GET | `/attachments/{id}.json` | - | Get single attachment |
| POST | `/attachments.json` | - | Create attachment |
| POST | `/attachments/{id}/track.json` | - | Track attachment ownership |

**Note**: GET /attachments.json returns 400 without proper parameters

---

# Query API

Endpoint: `GET` or `POST` `/query`

Executes SQL queries against PostgreSQL database with PostGIS support.

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `q` or `sql` | Yes | SQL query string |
| `format` | No | Output format: `json` (default), `csv`, `geojson` |
| `headers` | No | Include headers in CSV |
| `metadata` | No | Include metadata |
| `page` | No | Page number |
| `per_page` | No | Results per page |

## Output Formats

- **json**: Default, returns `{"fields": [...], "rows": [...]}`
- **csv**: Comma-separated values
- **geojson**: Requires `_geometry` column in query

## System Tables (19 total)

| Table | Verified | Key Columns |
|-------|----------|-------------|
| `audio` | ✓ | audio_id, record_id, form_id, metadata, file_size |
| `changesets` | ✓ | changeset_id, form_id, metadata, closed_at, created_by_id |
| `choice_lists` | ✓ | choice_list_id, name, description, version, items |
| `classification_sets` | ✓ | classification_set_id, name, description, version, items |
| `devices` | ✓ | device_id, identifier, platform, platform_version, manufacturer |
| `forms` | ✓ | form_id, name, description, version, elements |
| `memberships` | ✓ | membership_id, user_id, first_name, last_name, name |
| `memberships_devices` | - | Join table |
| `memberships_forms` | - | Join table |
| `memberships_layers` | - | Join table |
| `memberships_projects` | - | Join table |
| `photos` | ✓ | photo_id, record_id, form_id, exif, file_size |
| `projects` | ✓ | project_id, name, description, created_by_id, created_at |
| `record_links` | - | Join table |
| `record_series` | - | Series tracking |
| `roles` | ✓ | role_id, name, description, created_by_id, updated_by_id |
| `signatures` | ✓ | signature_id, record_id, form_id, file_size, created_by_id |
| `sketches` | - | Sketch metadata |
| `videos` | ✓ | video_id, record_id, form_id, metadata, file_size |

## Form Tables

Query forms by name or ID:

```sql
-- By name (use double quotes)
SELECT * FROM "My Form Name" LIMIT 10

-- By form ID
SELECT * FROM "abc123-def456-..." LIMIT 10
```

**Related Tables** (auto-generated):
- `"Form Name/repeatable_field"` - Repeatable section records
- `"Form Name/photo_field"` - Photo metadata
- `"Form Name/record_link_field"` - Record link join table

## System Columns (All Records)

```
_record_id, _project_id, _assigned_to_id, _status, _version, _title,
_created_at, _updated_at, _server_created_at, _server_updated_at,
_created_by_id, _updated_by_id, _changeset_id,
_latitude, _longitude, _geometry, _altitude, _speed, _course,
_horizontal_accuracy, _vertical_accuracy
```

## Example Queries

```sql
-- List all tables
SELECT * FROM tables ORDER BY name

-- Count records per form
SELECT name, record_count FROM forms ORDER BY record_count DESC

-- Get records with location
SELECT _record_id, _title, _latitude, _longitude
FROM "Form Name"
WHERE _latitude IS NOT NULL

-- Spatial query (PostGIS)
SELECT _record_id, _title
FROM "Form Name"
WHERE ST_DWithin(_geometry, ST_MakePoint(-117.1, 32.9)::geography, 1000)
```

## Constraints

- **Read-only**: Cannot modify data
- **10 second max**: Query timeout
- **63 char limit**: Table names (use form ID for longer names)

---

# Important API Rules

1. **PUT requests require full objects** - Omitting fields causes data loss
2. **No DELETE for media** - Photos, videos, audio, signatures must be unlinked from record
3. **Upload media first** - Upload file, then reference in record's form_values
4. **Timestamps in epoch seconds** - For date filter parameters
5. **Coordinates in WGS 84** - Decimal degrees
6. **GeoJSON uses [lon, lat]** - Longitude first, then latitude
7. **Validation errors** - Return HTTP 422 with error details
8. **Media availability** - Check `stored` and `processed` flags before downloading files

---

# Response Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 201 | Created |
| 400 | Bad Request (invalid parameters) |
| 401 | Unauthorized (invalid/missing token) |
| 404 | Not Found |
| 422 | Validation Error |
| 429 | Rate Limited |

---

# Servers (Regional)

| Region | Base URL |
|--------|----------|
| US (default) | `https://api.fulcrumapp.com/api/v2` |
| Australia | `https://api.au.fulcrumapp.com/api/v2` |
| Canada | `https://api.ca.fulcrumapp.com/api/v2` |
| EU | `https://api.eu.fulcrumapp.com/api/v2` |
