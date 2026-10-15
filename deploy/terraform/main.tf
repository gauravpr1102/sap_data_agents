# Google Cloud deployment of the SAP planning assistant (docs/DEPLOYMENT.md).
#
# What this creates:
# - BigQuery datasets: raw SAP extract, curated rule views, ops (audit, traces)
# - a least-privilege service account for the app
# - Artifact Registry repository for the container image
# - Cloud Storage bucket for the pilot's data file (mounted into Cloud Run)
# - Cloud Run service, reachable only through the load balancer + IAP
# - IAP access for the users listed in var.iap_members
# - Vertex AI access so the app can call Claude with Google credentials (no Anthropic key)
#
# Apply per environment with its own tfvars file:
#   terraform init && terraform apply -var-file=envs/stage.tfvars

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0, < 8.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  app_name = "sapkit-${var.environment}"
  labels = {
    app         = "sapkit"
    environment = var.environment
    managed_by  = "terraform"
  }
}

# ------------------------------------------------------------------ APIs
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "bigquery.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "iap.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ------------------------------------------------------------------ identity
resource "google_service_account" "app" {
  account_id   = "${local.app_name}-sa"
  display_name = "SAP planning assistant (${var.environment})"
}

resource "google_project_iam_member" "app_roles" {
  for_each = toset([
    "roles/bigquery.jobUser", # run queries
    "roles/aiplatform.user",  # call Claude on Vertex AI
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.app.email}"
}

# ------------------------------------------------------------------ data
resource "google_bigquery_dataset" "raw" {
  dataset_id                 = "${var.dataset_prefix}_raw"
  location                   = var.bq_location
  description                = "SAP extract as delivered (read-only to the app)."
  labels                     = local.labels
  delete_contents_on_destroy = false
}

resource "google_bigquery_dataset" "curated" {
  dataset_id  = "${var.dataset_prefix}_mm"
  location    = var.bq_location
  description = "Business-rule views generated from sql/views.sql (sapkit bigquery)."
  labels      = local.labels
}

resource "google_bigquery_dataset" "ops" {
  dataset_id  = "${var.dataset_prefix}_ops"
  location    = var.bq_location
  description = "Audit log, traces, drafts and replies. Append-only for the app."
  labels      = local.labels
}

resource "google_bigquery_dataset_iam_member" "raw_read" {
  dataset_id = google_bigquery_dataset.raw.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.app.email}"
}

resource "google_bigquery_dataset_iam_member" "curated_read" {
  dataset_id = google_bigquery_dataset.curated.dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.app.email}"
}

resource "google_bigquery_dataset_iam_member" "ops_write" {
  dataset_id = google_bigquery_dataset.ops.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.app.email}"
}

resource "google_storage_bucket" "data" {
  name                        = "${var.project_id}-${local.app_name}-data"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  labels                      = local.labels
  versioning {
    enabled = true
  }
}

resource "google_storage_bucket_iam_member" "data_rw" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.app.email}"
}

# ------------------------------------------------------------------ image
resource "google_artifact_registry_repository" "images" {
  repository_id = local.app_name
  location      = var.region
  format        = "DOCKER"
  labels        = local.labels
}

# ------------------------------------------------------------------ optional secret (content filter endpoint)
resource "google_secret_manager_secret" "content_filter_url" {
  secret_id = "${local.app_name}-content-filter-url"
  labels    = local.labels
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "content_filter_read" {
  secret_id = google_secret_manager_secret.content_filter_url.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}

# ------------------------------------------------------------------ app
resource "google_cloud_run_v2_service" "app" {
  name     = local.app_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" # only via the HTTPS load balancer with IAP
  labels   = local.labels

  template {
    service_account = google_service_account.app.email
    # DuckDB pilot: one writer. Raise only after moving ops writes to BigQuery.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }
    containers {
      image = var.image
      resources {
        limits = { cpu = "2", memory = "2Gi" }
      }
      env {
        name  = "SAPKIT_AUTH"
        value = "iap"
      }
      env {
        name  = "SAPKIT_LLM_PROVIDER"
        value = "vertex"
      }
      env {
        name  = "SAPKIT_VERTEX_PROJECT"
        value = var.project_id
      }
      env {
        name  = "SAPKIT_VERTEX_REGION"
        value = var.vertex_region
      }
      env {
        name  = "SAPKIT_DB"
        value = "/app/data/${var.client_id}.duckdb"
      }
      volume_mounts {
        name       = "data"
        mount_path = "/app/data"
      }
    }
    volumes {
      name = "data"
      gcs {
        bucket    = google_storage_bucket.data.name
        read_only = false
      }
    }
  }
  depends_on = [google_project_service.apis]
}

# IAP: only these people (or groups) can reach the app at all; RBAC then decides what they see.
resource "google_iap_web_iam_member" "users" {
  for_each = toset(var.iap_members)
  project  = var.project_id
  role     = "roles/iap.httpsResourceAccessor"
  member   = each.value
}
