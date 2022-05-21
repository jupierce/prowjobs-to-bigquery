# prowjobs-to-bigquery
Tooling to extract details from historical CI GCS files / logs and populate them into bigquery for CI Utilization Cost analysis

# Bulk load
- Change schema_version in gcs_finalize_event.py
- Setup massive system in GCE running debian - configure to runs as aos-kettle.
- sudo apt install python3 python3-pip
- sudo pip install google-cloud-storage google-cloud-bigquery future
- Upload model.py and gcs_finalize_event.py to /loader
- chmod +x gcs_finalize_event.py
- ulimit -n 3048
- ./gcs_finalize_event.py