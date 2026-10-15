output "service_account" {
  value = google_service_account.app.email
}

output "cloud_run_service" {
  value = google_cloud_run_v2_service.app.name
}

output "datasets" {
  value = {
    raw     = google_bigquery_dataset.raw.dataset_id
    curated = google_bigquery_dataset.curated.dataset_id
    ops     = google_bigquery_dataset.ops.dataset_id
  }
}

output "image_repository" {
  value = google_artifact_registry_repository.images.id
}
