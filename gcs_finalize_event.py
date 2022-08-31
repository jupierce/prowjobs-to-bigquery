#!/usr/bin/env python3

import pathlib
import json
import multiprocessing

from typing import NamedTuple, List
from model import Model, Missing

from google.cloud import bigquery, storage

# Destination for parsed prowjob info
JOBS_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.jobs'

# Using globals is ugly, but when running in cold load mode, these will be set for each separate process.
# https://stackoverflow.com/questions/10117073/how-to-use-initializer-to-set-up-my-multiprocess-pool
global_storage_client = None
global_origin_ci_test_bucket_client = None


def process_connection_setup():
    global global_storage_client
    global global_origin_ci_test_bucket_client
    global_storage_client = storage.Client()
    global_origin_ci_test_bucket_client = global_storage_client.bucket('origin-ci-test')


class JobsRecord(NamedTuple):
    created: str
    prowjob_build_id: str
    prowjob_type: str
    ci_op_cloud: str
    ci_op_cluster_profile: str
    context: str
    name: str
    org: str
    repo: str
    pr_number: str
    base_ref: str
    prowjob_cluster: str
    prowjob_job_name: str
    pr_author: str
    base_sha: str
    pr_sha: str
    prowjob_start: str
    prowjob_completion: str
    prowjob_state: str
    prowjob_labels: List[str]
    prowjob_annotations: List[str]
    features: List[str]
    chatbot_user: str
    chatbot_mode: str
    is_release_verify: bool
    release_verify_tag: str
    prpq: str
    manager: str
    schema_level: int
    retest: str


def or_none(v):
    if v is Missing:
        return None
    return v


def to_ts(s):
    if s is Missing:
        return None
    return s.rstrip('Z')


def to_kv_list(kv_map):
    if kv_map is Missing:
        return []
    kv_list = []
    for key, value in kv_map.primitive().items():
        kv_list.append(f'{key}={value}')
    return kv_list


def parse_prowjob_json(prowjob_json_text):
    try:
        prowjob_dict = json.loads(prowjob_json_text)
    except:
        print(f'Found invalid json' )
        return None

    prowjob = Model(prowjob_dict)

    if not prowjob.status.completionTime:
        # CI will upload prowjob.json as soon as it is pending.
        # We only want the record when the prowjob is completed.
        return None

    labels = prowjob.metadata.labels
    annotations = prowjob.metadata.annotations
    refs = prowjob.spec.refs
    pull = Model()

    if refs is Missing:
        if prowjob.spec.extra_refs:
            refs = prowjob.spec.extra_refs[0]

    if refs.pulls:
        pull = refs.pulls[0]

    manager = "unknown"
    # If the prowjob injects managedFields, find the manager
    # that owns the .spec.
    for managedField in prowjob.metadata.managedFields:
        if managedField.fieldsV1['f:spec']['.'] is not Missing:
            manager = managedField.manager
            break

    cluster_profile = None
    for v in prowjob.spec.pod_spec.volumes:
        if v.name == 'cluster-profile':
            cluster_profile = v.secret.secretName

            if v.projected.sources:
                for source in v.projected.sources:
                    if source.secret:
                        cluster_profile = source.secret.name
                        break

            if cluster_profile is not Missing:
                cluster_profile = cluster_profile.removeprefix('cluster-secrets-')
                break

    if not cluster_profile:
        cluster_profile = labels['ci-operator.openshift.io/cloud-cluster-profile']

    prowjob_name = or_none(prowjob.spec.job) or ""

    features_list = []

    record = JobsRecord(
        created=to_ts(prowjob.metadata.creationTimestamp),
        prowjob_build_id=or_none(prowjob.status.build_id),
        prowjob_type=or_none(prowjob.spec.type),
        ci_op_cloud=or_none(labels['ci-operator.openshift.io/cloud']),
        ci_op_cluster_profile=or_none(cluster_profile),
        context=or_none(prowjob.spec.context),
        name=or_none(prowjob.metadata.name),
        org=or_none(refs.org),
        repo=or_none(refs.repo),
        pr_number=or_none(pull.number),
        base_ref=or_none(refs.base_ref),
        prowjob_cluster=or_none(prowjob.spec.cluster),
        prowjob_job_name=prowjob_name,
        pr_author=or_none(pull.author),
        base_sha=or_none(refs.base_ref),
        pr_sha=or_none(pull.sha),
        prowjob_start=to_ts(prowjob.status.startTime),
        prowjob_completion=to_ts(prowjob.status.completionTime),
        prowjob_state=or_none(prowjob.status.state),
        prowjob_labels=to_kv_list(labels),
        prowjob_annotations=to_kv_list(annotations),
        chatbot_mode=or_none(annotations['ci-chat-bot.openshift.io/mode']),
        chatbot_user=or_none(annotations['ci-chat-bot.openshift.io/user']),
        is_release_verify=or_none(labels['release.openshift.io/verify'] == "true"),
        release_verify_tag=or_none(annotations['release.openshift.io/tag']),
        features=features_list,
        prpq=or_none(labels['pullrequestpayloadqualificationruns.ci.openshift.io']),
        manager=manager,
        schema_level=10,
        retest=or_none(labels['prow.k8s.io/retest']),
    )

    record_dict = dict(record._asdict())
    return record_dict


def parse_prowjob_from_gcs(file_path: str):
    b = global_origin_ci_test_bucket_client.get_blob(file_path)
    if not b:
        return None

    prowjob_json_text = b.download_as_text()
    return parse_prowjob_json(prowjob_json_text)


def process_prowjob_from_gcs(file_path: str):
    record_dict = parse_prowjob_from_gcs(file_path)
    if not record_dict:
        return
    bq = bigquery.Client()
    errors = bq.insert_rows_json(JOBS_TABLE_ID, [record_dict])
    if errors == []:
        print("New rows have been added.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def gcs_finalize(event, context):
    """Triggered by a change to a Cloud Storage bucket.
    Args:
         event (dict): Event payload.
         context (google.cloud.functions.Context): Metadata for the event.
    """
    file = event
    file_path: str = file['name']
    base_file_name = pathlib.Path(file_path).name

    process_connection_setup()

    if base_file_name == "prowjob.json":
        process_prowjob_from_gcs(file_path)

    print(f"Processing file: {file['name']}.")


def cold_load_all():
    origin_ci_test_usage_table_id = 'openshift-gce-devel.ci_analysis.origin-ci-test-usage'
    query_all_prowjob_jsons = f"""
    SELECT DISTINCT cs_object FROM `{origin_ci_test_usage_table_id}`
    WHERE time_micros > 1630468800000 AND cs_object LIKE "%/prowjob.json"
    """
    bq = bigquery.Client()
    prowjob_paths = bq.query(query_all_prowjob_jsons)

    # Pool cannot serialize a row for the other processes, so just extract the filename
    def cs_object_generator(row_generator):
        for r in row_generator:
            yield r['cs_object']

    inserts = []
    total_inserts = 0

    def send_inserts():
        if not inserts:  # No items to insert?
            return 0
        count = len(inserts)
        errors = bq.insert_rows_json(JOBS_TABLE_ID, inserts)
        if errors == []:
            print("New rows have been added.")
        else:
            raise IOError("Encountered errors while inserting rows: {}".format(errors))
        inserts.clear()
        return count

    # Have multiple processes per processor since read from GCS will take a few I/O beats
    pool = multiprocessing.Pool(multiprocessing.cpu_count()*12, process_connection_setup)
    vals = pool.imap_unordered(parse_prowjob_from_gcs, cs_object_generator(prowjob_paths), chunksize=1000)
    for val in vals:
        if not val:
            continue
        inserts.append(val)
        if len(inserts) > 1000:
            total_inserts += send_inserts()
            print(f'Rows inserted so far: {total_inserts}')

    total_inserts += send_inserts()
    print(f'Total number of rows inserted: {total_inserts}')


if __name__ == '__main__':
    # parse_prowjob_json(pathlib.Path("prowjobs/payload-pr.json").read_text())
    cold_load_all()
