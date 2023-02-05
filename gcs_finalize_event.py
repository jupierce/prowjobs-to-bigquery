#!/usr/bin/env python3
import datetime
import pathlib
import json
import multiprocessing
import re

from typing import NamedTuple, List, Dict
from model import Model, Missing, ListModel

from google.cloud import bigquery, storage

# Destination for parsed prowjob info
JOBS_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.jobs'

CI_OPERATOR_LOGS_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.ci_operator_logs'
CI_OPERATOR_LOGS_SCHEMA_LEVEL = 7

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
    prowjob_url: str
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
    return s.rstrip('Z').split('.')[0]  # '.' split removes 2022-12-19T19:18:44.558622693 subseconds


def to_ms(ns):
    # Duration is in ns. Bring this down to millis.
    if ns is Missing:
        return None
    return int(ns / 1000000)


def to_kv_list(kv_map):
    if kv_map is Missing:
        return []
    kv_list = []
    for key, value in kv_map.primitive().items():
        kv_list.append(f'{key}={value}')
    return kv_list


class CiOperatorLogRecord(NamedTuple):
    schema_level: int
    file_path: str
    prowjob_build_id: str

    test_name: str  # multi-stage-test name when multi-stage
    step_name: str  # if a step was being run
    build_name: str  # if a build is being run
    lease_name: str  # if a lease is being acquired
    started_at: str
    finished_at: str
    duration_ms: int
    success: bool

# Regex for parsing ci-operator.log insights.
# Parsing json and then looking for specific messages is CPU intensive relative to
# a regex. For performance reasons, union the regex into one big one. Setup sub expressions that should consume
# up to, but not including, the closing quotation mark of msg.
sub_expressions = [
    # Example Acquiring leases for test launch: [aws-2-quota-slice]
    r"(Acquiring leases for test (?P<acquiring_test_name>[^:]+): \[(?P<acquiring_slice_names>[^\]]+)\])",
    # Example: Acquired 1 lease(s) for aws-quota-slice: [us-east-1--aws-quota-slice-28]
    r"(Acquired (?P<lease_count>\d+) lease.s. for (?P<acquired_slice_name>[^:]+): (?P<lease_names>[^\s\"]+))",
    # Example: Building src
    r"(Build (?P<build_name_start>[^\"]+))",
    # Examples:
    # - Build aws-ebs-csi-driver succeeded after 2m5s
    # - Build %s already succeeded in %s
    # - Build %s failed, printing logs:
    r"(Build (?P<build_name_outcome>[^\s]+)[^\"]+)",
    # Example: Running multi-stage test e2e-aws-serial
    r"(Running multi-stage test (?P<multi_stage_name>[^\s\"]+))",
    # Example: Running step e2e-aws-serial-ipi-install-hosted-loki.
    r"(Running step (?P<step_name_start>[^\s]+)\.)",
    # Examples:
    # - Step launch-ipi-conf succeeded after 30s
    # - Step launch-ipi-conf failed after 30s
    r"(Step (?P<step_name_outcome>[^\s]+) (?P<step_outcome>[^\s]+) after [^\"]+)",
]

combined_pattern = r"{\"level\":\"\w+\",\"msg\":\"(?:" + \
                    '|'.join(sub_expressions) + \
                   r")\",\"time\":\"(?P<dt>[^\"]+)\"}"

log_entry_pattern = re.compile(combined_pattern)


def parse_ci_operator_log_resources_text(ci_operator_log_file: str, prowjob_build_id: str, ci_operator_log_str: str) -> List[Dict]:

    def ms_diff_from_time_strs(start, stop) -> int:
        ts_start = datetime.datetime.strptime(start + ' UTC', '%Y-%m-%dT%H:%M:%S %Z')
        ts_stop = datetime.datetime.strptime(stop + ' UTC', '%Y-%m-%dT%H:%M:%S %Z')
        return int((ts_stop - ts_start) / datetime.timedelta(milliseconds=1))

    log_records: List[Dict] = list()
    multi_stage_test_name = None
    build_start: Dict[str, str] = dict()
    step_start: Dict[str, str] = dict()
    lease_start: Dict[str, str] = dict()
    for log_line in ci_operator_log_str.splitlines():
        m = log_entry_pattern.match(log_line)
        if not m:
            # Not a log entry we are interested in
            continue

        dt = m.group('dt').rstrip('Z')  # '2023-02-03T13:33:30Z' -> '2023-02-03T13:33:30'

        if m.group('multi_stage_name'):
            multi_stage_test_name = m.group('multi_stage_name')

        # Leases
        if m.group('acquiring_test_name'):
            multi_stage_test_name = m.group('acquiring_test_name')
            for lease_name in m.group('acquiring_slice_names').split():
                lease_start[lease_name] = dt

        if m.group('acquired_slice_name'):
            slice_name = m.group('acquired_slice_name')
            if slice_name in lease_start:
                start_time = lease_start.pop(slice_name)
            else:
                print(f'Unable to find start lease time in {ci_operator_log_file}')
                start_time = dt

            record = CiOperatorLogRecord(
                schema_level=CI_OPERATOR_LOGS_SCHEMA_LEVEL,
                file_path=ci_operator_log_file,
                step_name=None,
                build_name=None,
                lease_name=slice_name,
                started_at=start_time,
                finished_at=dt,
                duration_ms=ms_diff_from_time_strs(start_time, dt),
                prowjob_build_id=prowjob_build_id,
                test_name=multi_stage_test_name,
                success=True,
            )
            log_records.append(record._asdict())

        # Builds
        if m.group('build_name_start'):
            build_start[m.group('build_name_start')] = dt

        if m.group('build_name_outcome'):
            start_time = build_start.pop(m.group('build_name_outcome'))
            record = CiOperatorLogRecord(
                schema_level=CI_OPERATOR_LOGS_SCHEMA_LEVEL,
                file_path=ci_operator_log_file,
                step_name=None,
                build_name=m.group('build_name_outcome'),
                lease_name=None,
                started_at=start_time,
                finished_at=dt,
                duration_ms=ms_diff_from_time_strs(start_time, dt),
                prowjob_build_id=prowjob_build_id,
                test_name=multi_stage_test_name,
                success='succeeded' in log_line,
            )
            log_records.append(record._asdict())

        # Steps
        if m.group('step_name_start'):
            step_start[m.group('step_name_start')] = dt

        if m.group('step_name_outcome'):
            start_time = step_start.pop(m.group('step_name_outcome'))
            record = CiOperatorLogRecord(
                schema_level=CI_OPERATOR_LOGS_SCHEMA_LEVEL,
                file_path=ci_operator_log_file,
                step_name=m.group('step_name_outcome')[len(multi_stage_test_name)+1:],  # Strip off multi-stage test name prefix to just leave registry step name
                build_name=None,
                lease_name=None,
                started_at=start_time,
                finished_at=dt,
                duration_ms=ms_diff_from_time_strs(start_time, dt),
                prowjob_build_id=prowjob_build_id,
                test_name=multi_stage_test_name,
                success='succeeded' in m.group('step_outcome'),
            )
            log_records.append(record._asdict())

    return log_records


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
        prowjob_url=or_none(prowjob.status.url),
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


def parse_ci_operator_log_resources_from_gcs(build_id: str, file_path: str) -> List[Dict]:
    try:
        b = global_origin_ci_test_bucket_client.get_blob(file_path)
        if not b:
            return None

        ci_operator_log_text = b.download_as_text()
        return parse_ci_operator_log_resources_text(file_path, build_id, ci_operator_log_text)
    except Exception as e:
        print(f'Error retrieving ci-operator log file: {file_path}: {e}')
        return []


def parse_prowjob_from_gcs(file_path: str):
    b = global_origin_ci_test_bucket_client.get_blob(file_path)
    if not b:
        return None

    prowjob_json_text = b.download_as_text()
    return parse_prowjob_json(prowjob_json_text)


def process_ci_operator_log_from_gcs(build_id: str, file_path: str):
    ci_operator_log_records = parse_ci_operator_log_resources_from_gcs(build_id, file_path)
    if not ci_operator_log_records:
        return
    bq = bigquery.Client()
    errors = bq.insert_rows_json(CI_OPERATOR_LOGS_TABLE_ID, ci_operator_log_records)
    if errors == []:
        print("New rows have been added.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def parse_ci_operator_log_from_gcs_file_path(file_path: str) -> List[Dict]:
    prowjob_build_id = pathlib.Path(file_path).parent.parent.name
    return parse_ci_operator_log_resources_from_gcs(prowjob_build_id, file_path)


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
    gcs_file_name: str = file['name']
    process_connection_setup()

    if gcs_file_name.endswith("/prowjob.json"):
        process_prowjob_from_gcs(gcs_file_name)
    elif gcs_file_name.endswith('/finished.json'):
        # We need to find the prowjob build-id which should be 1 level down
        # branch-ci-openshift-jenkins-release-4.10-images/1604867803796475904/finished.json
        file_path = pathlib.Path(gcs_file_name)
        prowjob_build_id = file_path.parent.name
        ci_operator_log_path = file_path.parent.joinpath('artifacts/ci-operator.log')
        process_ci_operator_log_from_gcs(prowjob_build_id, str(ci_operator_log_path))

    print(f"Processing file: {file['name']}.")


# Pool cannot serialize a row for the other processes, so just extract the filename
def cs_object_generator(row_generator):
    for r in row_generator:
        yield r['cs_object']


def cold_load_all_prowjobs():
    origin_ci_test_usage_table_id = 'openshift-gce-devel.ci_analysis_us.origin-ci-test_usage_analysis'
    query_all_prowjob_jsons = f"""
    SELECT DISTINCT cs_object FROM `{origin_ci_test_usage_table_id}`
    WHERE time_micros > 1630468800000 AND cs_object LIKE "%/prowjob.json"
    """
    bq = bigquery.Client()
    prowjob_paths = bq.query(query_all_prowjob_jsons)

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

    all_paths = [lp['cs_object'] for lp in prowjob_paths]
    total_file_count = len(all_paths)
    print(f'{total_file_count} files need to be processed')

    # Have multiple processes per processor since read from GCS will take a few I/O beats
    pool = multiprocessing.Pool(multiprocessing.cpu_count()*12, process_connection_setup)
    vals = pool.imap_unordered(parse_prowjob_from_gcs, all_paths, chunksize=1000)
    for val in vals:
        if not val:
            continue
        inserts.append(val)
        if len(inserts) > 1000:
            total_inserts += send_inserts()
            print(f'Rows inserted so far: {total_inserts}')

    total_inserts += send_inserts()
    print(f'Total number of rows inserted: {total_inserts}')


def cold_load_all_ci_operator_logs():
    origin_ci_test_usage_table_id = 'openshift-gce-devel.ci_analysis_us.origin-ci-test_usage_analysis'
    query_all_ci_operator_paths = f"""
    SELECT DISTINCT cs_object FROM `{origin_ci_test_usage_table_id}`
    WHERE time_micros > 1630468800000 AND cs_object LIKE "%/ci-operator.log"
    """
    bq = bigquery.Client()
    ci_operator_log_paths = bq.query(query_all_ci_operator_paths)

    inserts = []
    total_inserts = 0
    total_files = 0

    def send_inserts():
        if not inserts:  # No items to insert?
            return 0
        count = len(inserts)
        try:
            errors = bq.insert_rows_json(CI_OPERATOR_LOGS_TABLE_ID, inserts)
            if errors == []:
                print("New rows have been added.")
            else:
                raise IOError("Encountered errors while inserting rows: {}".format(errors))
        except Exception as e:
            print(f'Error inserting rows: {e}')
        inserts.clear()
        return count

    all_paths = [lp['cs_object'] for lp in ci_operator_log_paths]
    total_file_count = len(all_paths)
    print(f'{total_file_count} files need to be processed')

    # Have multiple processes per processor since read from GCS will take a few I/O beats
    pool = multiprocessing.Pool(multiprocessing.cpu_count()*3, process_connection_setup)
    vals = pool.imap_unordered(parse_ci_operator_log_from_gcs_file_path, all_paths, chunksize=1000)
    for val in vals:
        if not val:
            continue
        inserts.extend(val)
        if len(inserts) > 1000:
            total_inserts += send_inserts()
            print(f'Rows inserted so far: {total_inserts} ({total_files} of {total_file_count})')

    total_inserts += send_inserts()
    print(f'Total number of rows inserted: {total_inserts}')


if __name__ == '__main__':
    # outcome = parse_ci_operator_graph_resources_json('abcdefg', pathlib.Path("ci-operator-graphs/ci-operator-step-graph-1.json").read_text())
    # import yaml
    # print(yaml.dump(outcome))
    # parse_prowjob_json(pathlib.Path("prowjobs/payload-pr.json").read_text())
    # cold_load_all()

    cold_load_all_ci_operator_logs()


