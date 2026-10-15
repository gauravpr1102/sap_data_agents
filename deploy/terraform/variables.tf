variable "project_id" {
  description = "Google Cloud project for this environment."
  type        = string
}

variable "environment" {
  description = "stage or prod."
  type        = string
  validation {
    condition     = contains(["stage", "prod"], var.environment)
    error_message = "environment must be stage or prod."
  }
}

variable "client_id" {
  description = "Client id from config/<client>.yaml (names the data file)."
  type        = string
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "vertex_region" {
  description = "Region for Claude on Vertex AI ('global' or a supported region)."
  type        = string
  default     = "global"
}

variable "bq_location" {
  type    = string
  default = "US"
}

variable "dataset_prefix" {
  description = "Prefix for the raw / mm / ops BigQuery datasets."
  type        = string
  default     = "sapkit"
}

variable "image" {
  description = "Container image, e.g. us-central1-docker.pkg.dev/<project>/sapkit-stage/sapkit:<tag>."
  type        = string
}

variable "iap_members" {
  description = "Principals allowed through IAP, e.g. [\"group:sap-planners@client.com\"]."
  type        = list(string)
  default     = []
}
