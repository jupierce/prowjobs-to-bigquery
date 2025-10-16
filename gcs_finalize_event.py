#!/usr/bin/env python3

import datetime
import pathlib
import json
import os
import multiprocessing
import re
import hashlib
import traceback
import time
import html
from xml import sax

from io import StringIO
from typing import NamedTuple, List, Dict, Optional

from model import Model, Missing
from collections import defaultdict

from google.cloud import bigquery, storage

RELEASEINFO_SCHEMA_LEVEL = 2
CI_OPERATOR_LOGS_JSON_SCHEMA_LEVEL = 20
JUNIT_TABLE_SCHEMA_LEVEL = 14
JOB_INTERVALS_SCHEMA_LEVEL = 3
TEMP_CI_OPERATOR_SCHEMA_LEVEL = 3


class BucketInfo(NamedTuple):
    """
    Depending on which GCS bucket was written to, we will be updating different datasets (potentially in different projects).
    """
    # The project in which the bucket resides. This can technically be accessed through
    # en environment variable, but that would require importing 'os' / function call which
    # could impact execution size/time & thus cost.
    bucket_project: str

    # The project in which the dataset resides
    bigquery_project: str

    bucket_url_prefix: str

    # As data is written to a bucket, this cloud function will append data to a set of tables in a destination dataset.
    # Writing to one bucket will trigger updates to
    dest_bigquery_dataset: str

    table_name_jobs: str
    table_name_junit: str
    table_name_junit_pr: str
    table_name_job_intervals: str
    table_name_job_releases: str
    table_name_ci_operator_logs_json: str

    @property
    def table_id_jobs(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_jobs}'

    @property
    def table_id_junit(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_junit}'

    @property
    def table_id_junit_pr(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_junit_pr}'

    @property
    def table_id_job_intervals(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_job_intervals}'

    @property
    def table_id_job_releases(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_job_releases}'

    @property
    def table_id_ci_operator_logs_json(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_ci_operator_logs_json}'


DEFAULT_TABLE_NAMES = {
    'table_name_jobs': 'jobs',
    'table_name_junit': 'junit',
    'table_name_junit_pr': 'junit_pr',
    'table_name_job_intervals': 'job_intervals',
    'table_name_job_releases': 'job_releases',
    'table_name_ci_operator_logs_json': 'ci_operator_logs_json'
}


# The list of buckets to react to and the datasets to update when writes are made to that bucket.
BUCKET_INFO_MAPPING = {
    'test-platform-results': BucketInfo(
        bucket_project='openshift-gce-devel',
        bucket_url_prefix='https://prow.ci.openshift.org/view/gs/test-platform-results/',
        bigquery_project='openshift-gce-devel',
        dest_bigquery_dataset='openshift-gce-devel.ci_analysis_us',
        **DEFAULT_TABLE_NAMES
    ),
    'qe-private-deck': BucketInfo(
        bucket_project='openshift-ci-private',
        bucket_url_prefix='https://qe-private-deck-ci.apps.ci.l2s4.p1.openshiftapps.com/view/gs/qe-private-deck/',
        bigquery_project='openshift-gce-devel',
        dest_bigquery_dataset='openshift-gce-devel.ci_analysis_qe',
        **DEFAULT_TABLE_NAMES
    ),
    'origin-ci-private': BucketInfo(
        bucket_project='openshift-ci-private',
        bucket_url_prefix='https://deck-internal-ci.apps.ci.l2s4.p1.openshiftapps.com/view/gs/origin-ci-private/',
        bigquery_project='openshift-gce-devel',
        dest_bigquery_dataset='openshift-gce-devel.ci_analysis_private',
        **DEFAULT_TABLE_NAMES
    ),
}


# Using globals is ugly, but when running in cold load mode, these will be set for each separate process.
# https://stackoverflow.com/questions/10117073/how-to-use-initializer-to-set-up-my-multiprocess-pool
global_storage_client = None
global_result_storage_bucket_client = None

global_bq_client = None
global_bucket_info: Optional[BucketInfo] = None


use_ET = False
try:
    import xml.etree.ElementTree as ET
    use_ET = True
except:
    print('Unable to use ElementTree')


def process_connection_setup(bucket: str):
    global global_storage_client
    global global_bucket_info
    global global_result_storage_bucket_client
    global global_bq_client

    if not global_storage_client:
        global_bucket_info = BUCKET_INFO_MAPPING.get(bucket, None)

        if not global_bucket_info:
            raise IOError(f'No bucket information has been configured for bucket: {bucket}')

        bucket_project = global_bucket_info.bucket_project
        global_storage_client = storage.Client(project=bucket_project)
        global_result_storage_bucket_client = global_storage_client.bucket(bucket)
        global_bq_client = bigquery.Client(project=global_bucket_info.bigquery_project)


class JunitTestcase:

    def __init__(self, name: str, duration: str = "0"):
        self.name: str = name
        self.time: str = duration
        self.system_out: Optional[str] = None
        self.failure_msg: Optional[str] = None
        self.skipped_msg: Optional[str] = None

    def set_failure_msg(self, msg: Optional[str]):
        self.failure_msg = msg

    def set_skipped_msg(self, msg: Optional[str]):
        self.skipped_msg = msg

    def set_system_out(self, output: Optional[str]):
        self.system_out = output

    def set_time(self, time):
        self.time = str(time)


class JunitFile(NamedTuple):
    gcs_path: str
    suite: str
    properties: Optional[Dict]
    testcases: Optional[List[JunitTestcase]]


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
    prowjob_pending: str
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
    ci_executor: str
    ci_user: str
    schema_level: int
    retest: str
    gcs_bucket: str


class JobReleaseRecord(NamedTuple):
    prowjob_name: str
    prowjob_build_id: str
    release_name: str
    release_digest: str
    release_created: str
    machine_os: str
    tag_name: str
    tag_source_location: str
    tag_commit_id: str
    tag_image: str
    is_pr: bool
    schema_level: int


class JobIntervalsRecord(NamedTuple):
    schema_level: int
    prowjob_name: str
    prowjob_build_id: str
    level: str
    source: str
    from_time: str
    to_time: str
    locator: str
    message: str
    payload: str


def or_none(v):
    if v is Missing:
        return None
    return v


def to_ts(s):
    if s is Missing or s is None:
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


class JUnitTestRecord(NamedTuple):
    schema_level: int
    file_path: str
    prowjob_build_id: str

    test_id: str
    test_name: str
    testsuite: str
    duration_ms: int
    success: bool
    success_val: int
    skipped: bool
    modified_time: str
    branch: str
    prowjob_name: str

    network: str
    platform: str
    arch: str
    upgrade: str
    variants: List[str]
    flat_variants: str

    flake_count: int

    failure_message: str
    failure_content: str
    system_out_content: str


# Group 1 extracts path up to prowjob id (e.g. 	branch-ci-openshift-jenkins-release-4.10-images/1604867803796475904 ).
# Group 2 extracts prowjob name
# Group 3 extracts the prowjob numeric id  (requires id be at least 12 digits to avoid finding PR number)
# Group 4 allows you to match different junit filenames you are interested in including
# Example qe paths:
# cucushift: logs/periodic-ci-openshift-openshift-tests-private-release-4.15-amd64-nightly-aws-ipi-ovn-ipsec-f14/1710439317068845056/artifacts/aws-ipi-ovn-ipsec-f14/cucushift-e2e/artifacts/serial/junit-report/TEST-features-logging-logging_acceptance.xml
# cypress junit: logs/periodic-ci-openshift-openshift-tests-private-release-4.15-amd64-nightly-aws-ipi-ovn-ipsec-f14/1710439317068845056/artifacts/aws-ipi-ovn-ipsec-f14/openshift-extended-web-tests/artifacts/gui_test_screenshots/junit_cypress-f7b54dc2e29e315821abf0acc44c7917.xml
# ginkgo: https://gcsweb-qe-private-deck-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/qe-private-deck/logs/periodic-ci-openshift-openshift-tests-private-release-4.15-amd64-nightly-aws-ipi-ovn-ipsec-f14/1710439317068845056/artifacts/aws-ipi-ovn-ipsec-f14/openshift-extended-test/artifacts/junit/import-Cluster_Observability.xml
#
junit_path_pattern = re.compile(r"^(.*?\/([^\/]+)\/(\d{12,}))\/.*\/?(junit|e2e-monitor-tests|junit\/|junit-report\/TEST|junit_cypress)[^\/]+xml$")
test_id_pattern = re.compile(r"^.*{([a-f0-9]+)}.*$")

# Group 1 extracts path up to prowjob id (e.g. 	branch-ci-openshift-jenkins-release-4.10-images/1604867803796475904 ).
# Group 2 extracts prowjob name
# Group 3 extracts the prowjob numeric id (requires id be at least 12 digits to avoid finding PR number)
prowjob_path_pattern = re.compile(r"^(.*?\/([^\/]+)\/(\d{12,}))\/.*$")

# prowjob build ids should be at least 12 digits
prowjob_build_id_pattern = re.compile(r"^\d{12,}$")

# extracts the first ocp version like '4.11' (in group 1) or 'master' or 'main' (in group 2)
branch_pattern = re.compile(r".*?\D+[-\/](\d\.\d+)\D?.*|.*\-(master|main)\-.*")

go_time_units = {
    'd': 86400,  # 24 * 60 * 60
    'h': 3600,  # 60 * 60
    'm': 60,
    's': 1,
}


def parse_go_duration(duration_str):
    """
    Parse a Go-style duration string into total seconds.
    Supported units: d, h, m, s
    Example: "1d2h30m15s" -> 93615 seconds
    """
    pattern = re.compile(r'(?P<value>\d+)(?P<unit>[dhms])')
    matches = pattern.findall(duration_str)

    if not matches:
        return 0

    total_seconds = 0
    for value, unit in matches:
        if unit not in go_time_units:
            return 0
        total_seconds += int(value) * go_time_units[unit]

    return total_seconds


def parse_job_intervals_json(prowjob_name: str, prowjob_build_id: str, job_intervals_json_text: str, is_pr: bool) -> Optional[List[Dict]]:
    try:
        job_intervals_items = json.loads(job_intervals_json_text)['items']
    except:
        print(f'Found invalid intervals file: {prowjob_build_id}')
        return None

    records: List[Dict] = list()
    for item in job_intervals_items:
        jr = JobIntervalsRecord(
            schema_level=JOB_INTERVALS_SCHEMA_LEVEL,
            prowjob_name=prowjob_name,
            prowjob_build_id=prowjob_build_id,
            level=item.get('level', None),
            source=item.get('source', None),
            from_time=to_ts(item['from']),
            to_time=to_ts(item['to']),
            # .message is a JSON string which insert_rows_json tries to put into a "RECORD".
            # However .message is a string, so it throws an err. Disabling message for now since
            # it is included in payload.
            # message=item.get('message', None),
            message=None,
            locator=item.get('locator', None),
            payload=json.dumps(item),
        )
        record = dict(jr._asdict())
        records.append(record)

    return records


def escape_xml(text: str) -> str:
    return html.escape(text, quote=True)


def write_junit_files(junit_files: Optional[List[JunitFile]]):
    if not junit_files:
        return

    for junit_file in junit_files:
        testcases = junit_file.testcases
        total_tests = len(testcases)
        total_failures = sum(1 for tc in testcases if tc.failure_msg)
        total_skipped = sum(1 for tc in testcases if tc.skipped_msg)
        total_time = sum(float(tc.time or "0") for tc in testcases)

        lines = list()
        lines.append('<?xml version="1.0" encoding="UTF-8"?>')
        lines.append(
            f'<testsuite name="{escape_xml(junit_file.suite)}" '
            f'tests="{total_tests}" skipped="{total_skipped}" '
            f'failures="{total_failures}" time="{int(total_time)}">'
        )
        # Add properties
        if junit_file.properties:
            lines.append('  <properties>')
            for key, value in junit_file.properties.items():
                lines.append(f'    <property name="{escape_xml(str(key))}" value="{escape_xml(str(value))}"/>')
            lines.append('  </properties>')

        # Add test cases
        if testcases:
            for tc in junit_file.testcases:

                lines.append(f'  <testcase name="{escape_xml(tc.name)}" time="{escape_xml(tc.time)}">')

                if tc.failure_msg:
                    lines.append(f'    <failure>{escape_xml(tc.failure_msg)}</failure>')

                if tc.skipped_msg:
                    lines.append(f'    <skipped>{escape_xml(tc.skipped_msg)}</skipped>')

                if tc.system_out:
                    lines.append(f'    <system-out>{escape_xml(tc.system_out)}</system-out>')

                lines.append('  </testcase>')

        lines.append('</testsuite>')

        # Convert to bytes
        xml_content = "\n".join(lines).encode("utf-8")

        try:
            blob = global_result_storage_bucket_client.blob(junit_file.gcs_path)
            if not blob.exists():
                blob.upload_from_string(xml_content, content_type="application/xml")
                print(f"Uploaded JUnit XML to {junit_file.gcs_path}")
            else:
                print(f'File already existed; skipping upload {junit_file.gcs_path}')
        except:
            print(f"Failed uploading JUnit XML to {junit_file.gcs_path}")
            raise


def parse_releaseinfo_json(prowjob_name: str, prowjob_build_id: str, releaseinfo_json_text: str, is_pr: bool) -> Optional[List[Dict]]:
    try:
        releaseinfo_dict = json.loads(releaseinfo_json_text)
    except:
        print(f'Found invalid releaseinfo.json: {prowjob_build_id}')
        return None

    releaseinfo = Model(releaseinfo_dict)
    release_digest = releaseinfo.digest
    release_created = releaseinfo.config.created
    release_name = releaseinfo.metadata.version
    machine_os = releaseinfo.displayVersions['machine-os'].Version

    records: List[Dict] = list()

    for tag in releaseinfo.references.spec.tags:
        jr = JobReleaseRecord(
            prowjob_name=prowjob_name,
            prowjob_build_id=prowjob_build_id,
            release_digest=release_digest,
            release_created=to_ts(release_created),
            release_name=release_name,
            tag_image=tag['from'].name,
            tag_name=tag.name,
            tag_source_location=tag.annotations['io.openshift.build.source-location'],
            tag_commit_id=tag.annotations['io.openshift.build.commit.id'],
            machine_os=machine_os,
            is_pr=is_pr,
            schema_level=RELEASEINFO_SCHEMA_LEVEL,
        )
        record = dict(jr._asdict())
        records.append(record)

    return records


class SimpleErrorHandler(sax.handler.ErrorHandler):
    def __init__(self):
        pass

    def fatalError(self, e):
        pass


class VariantMatcher:

    def __init__(self, name: str, regex_pattern: str):
        self.name = name
        self.pattern = re.compile(regex_pattern)

    def matches(self, lowercase_prowjob_name: str):
        return True if self.pattern.search(lowercase_prowjob_name) else False


# Lazy load to keep non-junit cloud function load time minimal
PLATFORM_VARIANTS: Optional[List[VariantMatcher]] = None


def get_platform_variants() -> List[VariantMatcher]:
    global PLATFORM_VARIANTS
    if PLATFORM_VARIANTS:
        return PLATFORM_VARIANTS
    PLATFORM_VARIANTS = [
        VariantMatcher('metal-assisted', '-metal-assisted'),
        VariantMatcher('metal-assisted', '-metal.*-single-node'),
    ]
    for platform_name in ['alibaba', 'aws', 'azure', 'gcp', 'libvirt', 'openstack', 'ovirt', 'vsphere-upi', 'vsphere', 'metal-ipi', 'metal-upi', 'ibmcloud']:
        PLATFORM_VARIANTS.append(VariantMatcher(platform_name, f'-{platform_name}'))
    return PLATFORM_VARIANTS


def determine_prowjob_platform(lowercase_prowjob_name: str) -> str:
    for pv in get_platform_variants():
        if pv.matches(lowercase_prowjob_name):
            return pv.name
    return 'unknown'


# Lazy load to keep non-junit cloud function load time minimal
ARCH_VARIANTS: Optional[List[VariantMatcher]] = None


def get_arch_variants() -> List[VariantMatcher]:
    global ARCH_VARIANTS
    if ARCH_VARIANTS:
        return ARCH_VARIANTS
    ARCH_VARIANTS = []
    for arch_name in ['heterogeneous', 'ppc64le', 'arm64', 's390x']:
        ARCH_VARIANTS.append(VariantMatcher(arch_name, f'-{arch_name}'))
    ARCH_VARIANTS.append(VariantMatcher('arm64', f'-arm(?:-|$)'))  # periodic-ci-openshift-cluster-control-plane-machine-set-operator-release-4.14-periodics-e2e-aws-arm
    return ARCH_VARIANTS


def determine_prowjob_architecture(lowercase_prowjob_name: str) -> str:
    for pv in get_arch_variants():
        if pv.matches(lowercase_prowjob_name):
            return pv.name
    return 'amd64'


# Lazy load to keep non-junit cloud function load time minimal
NETWORK_VARIANTS: Optional[List[VariantMatcher]] = None


def get_network_variants() -> List[VariantMatcher]:
    global NETWORK_VARIANTS
    if NETWORK_VARIANTS:
        return NETWORK_VARIANTS
    NETWORK_VARIANTS = []
    for network_name in ['sdn', 'ovn']:
        NETWORK_VARIANTS.append(VariantMatcher(network_name, f'-{network_name}'))
    return NETWORK_VARIANTS


def determine_prowjob_network(lowercase_prowjob_name: str, branch: str) -> str:
    for pv in get_network_variants():
        if pv.matches(lowercase_prowjob_name):
            return pv.name

    if branch in {'3.11', '4.6', '4.7', '4.8', '4.9', '4.10', '4.11'}:
        return 'sdn'

    return 'ovn'


UPGRADE_VARIANTS: Optional[List[VariantMatcher]] = None


def get_upgrade_variants() -> List[VariantMatcher]:
    global UPGRADE_VARIANTS
    if UPGRADE_VARIANTS:
        return UPGRADE_VARIANTS
    UPGRADE_VARIANTS = [
        VariantMatcher('upgrade-multi', '-upgrade.*-to-.*-to-'),
        VariantMatcher('upgrade-minor', '-upgrade.*-minor|-upgrade-from'),
        VariantMatcher('upgrade-micro', '-upgrade'),
    ]
    return UPGRADE_VARIANTS


def determine_prowjob_upgrade(lowercase_prowjob_name: str) -> Optional[str]:
    for pv in get_upgrade_variants():
        if pv.matches(lowercase_prowjob_name):
            return pv.name
    return 'no-upgrade'


OTHER_VARIANTS: Optional[List[VariantMatcher]] = None


def get_other_variants() -> List[VariantMatcher]:
    global OTHER_VARIANTS
    if OTHER_VARIANTS:
        return OTHER_VARIANTS
    OTHER_VARIANTS = list()
    for name in ['microshift', 'hypershift', 'serial', 'assisted', 'compact', 'osd', 'fips', 'techpreview', 'realtime', 'proxy', 'single-node', 'rt']:
        OTHER_VARIANTS.append(VariantMatcher(name, f'-{name}'))
    return OTHER_VARIANTS


def determine_other_variants(lowercase_prowjob_name: str) -> List[str]:
    other: List[str] = []
    for pv in get_other_variants():
        if pv.matches(lowercase_prowjob_name):
            other.append(pv.name)

    if not other:
        other.append('standard')

    return sorted(other)


class FlakeInfo:
    def __init__(self):
        self.flake_count = 0
        self.has_succeeded = False


def truncate_middle(content: str):
    """
    Important information tends to be at the beginning and end of errors.
    If a string exceeds a maximum size, return only the first and last part of the
    string with visible "truncated" message in the middle.
    """
    if len(content) < 5*1024 + 50:
        return content
    return content[:2*1024] + '..truncated..' + content[-2*1024:]


class JUnitHandler(sax.handler.ContentHandler):

    def __init__(self, modified_time: str, prowjob_name: str, prowjob_build_id: str, file_path: str):
        self.modified_time = modified_time
        self.prowjob_name = prowjob_name
        self.testsuite = None
        self.test_duration_ms = None
        self.test_id = None
        self.test_name = None
        self.test_success = None
        self.test_skipped = None
        self.failure_message = None
        self.failure_content = None
        self.system_out_content = None
        self.character_content = StringIO()
        self.prowjob_build_id = prowjob_build_id
        self.file_path = file_path
        self.record_dicts: List[Dict] = list()
        self.flake_info: defaultdict[str, FlakeInfo] = defaultdict(FlakeInfo)

        branch_match = branch_pattern.match(self.prowjob_name)
        if branch_match:
            self.branch = branch_match.group(1)
            if not self.branch:
                self.branch = branch_match.group(2)
        else:
            self.branch = 'unknown'

    def startElement(self, name, attrs):
        if name == 'testsuite':
            self.testsuite = attrs.get('name', '')

            if self.testsuite.startswith('OOMCheck'):
                # OOMCheck creates randomized suite names like OOMCheck-collector-<nonce> , OOMCheck-scanner-db-<nonce>.
                # Lop off the nonce so we can effectively GROUP BY on this column.
                self.testsuite = 'OOMCheck'

        elif name == 'failure':
            self.failure_message = truncate_middle(attrs.get('message', ''))

        elif name == 'testcase':
            self.test_name: str = attrs.get('name')
            id_match = test_id_pattern.match(self.test_name)  # Does the test has a test_id override?
            if id_match:
                self.test_id = id_match.group(1)
            else:
                test_hash = hashlib.md5(self.test_name.encode('utf-8')).hexdigest()
                self.test_id = f'{self.testsuite}:{test_hash}'

            try:
                self.test_duration_ms = int(float(attrs.get('time', '0.0')) * 1000)
            except ValueError:
                self.test_duration_ms = 0
            self.test_success = True  # Assume true until we hit a failure element
            self.test_skipped = False  # Assume true until we hit a failure element

    def startElementNS(self, name, qname, attributes):
        self.startElement(name, attributes)

    def endElement(self, name):
        self.character_content.truncate(5*1024)  # Limit the number of characters we put into the database
        collected_content = self.character_content.getvalue().strip()
        self.character_content = StringIO()  # Reset buffer content

        if name == 'failure':
            self.test_success = False
            if not self.failure_content:  # Will be set when using ET, but not when using SAX
                self.failure_content = truncate_middle(collected_content)
        elif name == 'system-out':
            if not self.system_out_content:  # Will be set when using ET, but not when using SAX
                self.system_out_content = truncate_middle(collected_content)
        elif name == 'error':
            self.test_success = False
        elif name == 'skipped':
            self.test_skipped = True
        elif name == 'testcase':

            flake_count_to_record = 0
            test_flake_info = self.flake_info[self.test_id]
            if self.test_success:
                # Set a flag telling any subsequent failure that it should count itself as a flake.
                test_flake_info.has_succeeded = True
                # We know that any preceding failure is now considered a flake. Record the count.
                flake_count_to_record = test_flake_info.flake_count
                # Restart the count in case there are other failures which happen after this success.
                test_flake_info.flake_count = 0
            else:
                test_flake_info.flake_count += 1
                if test_flake_info.has_succeeded:  # There was a success which occurred before this failure
                    flake_count_to_record = test_flake_info.flake_count   # Record this failure as a flake and any failures that preceded it.
                    test_flake_info.flake_count = 0
                else:
                    # The test has not succeeded in this file yet, so we don't know if this particular failure
                    # is a flake or not. Just keep the count rolling upward in flake_count in case we hit a success.
                    pass

            lc = self.prowjob_name.lower()
            other_variants = determine_other_variants(lc)

            if lc == 'periodic-ci-openshift-release-master-nightly-4.15-e2e-vsphere-static-ovn':
                # https://issues.redhat.com/browse/TRT-1508
                other_variants.append('techpreview')

            record = JUnitTestRecord(
                prowjob_build_id=self.prowjob_build_id,
                file_path=self.file_path,
                schema_level=JUNIT_TABLE_SCHEMA_LEVEL,
                test_id=self.test_id,
                success=self.test_success,
                success_val=1 if self.test_success else 0,
                skipped=self.test_skipped,
                test_name=self.test_name,
                duration_ms=self.test_duration_ms,
                modified_time=self.modified_time,
                branch=self.branch,
                prowjob_name=self.prowjob_name,
                network=determine_prowjob_network(lc, self.branch),
                platform=determine_prowjob_platform(lc),
                arch=determine_prowjob_architecture(lc),
                upgrade=determine_prowjob_upgrade(lc),
                variants=other_variants,
                flat_variants=','.join(other_variants),
                flake_count=flake_count_to_record,
                testsuite=self.testsuite,
                failure_content=self.failure_content,
                failure_message='',  # Set to self.failure_message if TRT wants <failure message='...'>. Conserve DB size otherwise.
                system_out_content='',  # Set to self.system_out_content if TRT wants this data. Conserve DB size otherwise.
            )

            self.record_dicts.append(record._asdict())

    def endElementNS(self, name, qname):
        self.endElement(name)

    def characters(self, content):
        # This method needs to be enhanced if there are nested elements with CDATA.
        # It would need to aggregate content based on the current tag.
        self.character_content.write(content)


def parse_junit_xml(junit_xml_text, modified_time: str, prowjob_name: str, prowjob_build_id: str, file_path: str) -> List[Dict]:
    handler = JUnitHandler(modified_time=modified_time, prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id,
                           file_path=file_path)
    if use_ET:
        context = ET.iterparse(StringIO(junit_xml_text), events=("start", "end"))
        for index, (event, elem) in enumerate(context):
            # Get the root element.
            if index == 0:
                root = elem

            if event == 'start':
                if elem.tag == 'testcase':
                    handler.failure_content = ''
                    handler.system_out_content = ''

                if elem.tag == 'system-out':
                    handler.system_out_content = truncate_middle(elem.text.strip()) if elem.text else ""
                    elem.clear()

                if elem.tag == 'failure':
                    handler.failure_content = truncate_middle(elem.text.strip()) if elem.text else ""
                    elem.clear()

                handler.startElement(elem.tag, elem.attrib)

            if event == "end":
                handler.endElement(elem.tag)
                root.clear()

    else:
        sax.parseString(junit_xml_text, handler, SimpleErrorHandler())

    handler.setDocumentLocator(None)
    return handler.record_dicts


def parse_prowjob_json(prowjob_json_text, bkt):
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
        prowjob_pending=to_ts(prowjob.status.pendingTime),
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
        ci_executor=or_none(annotations['ci.openshift.io/executor']),
        ci_user=or_none(annotations['ci.openshift.io/user']),
        schema_level=10,
        retest=or_none(labels['prow.k8s.io/retest']),
        gcs_bucket=bkt,
    )

    record_dict = dict(record._asdict())
    return record_dict


def parse_prowjob_from_gcs(file_path: str, bkt: str):
    b = global_result_storage_bucket_client.get_blob(file_path)
    if not b:
        return None

    prowjob_json_text = b.download_as_text()
    return parse_prowjob_json(prowjob_json_text, bkt)


junk_bytes_pattern = re.compile(r'[^\x20-\x7E]+')


def parse_job_intervals_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str) -> Optional[List[Dict]]:
    b = global_result_storage_bucket_client.get_blob(file_path)
    if not b:
        return None
    is_pr = '/pull/' in file_path
    job_intervals_json_text = b.download_as_text()
    return parse_job_intervals_json(prowjob_name, prowjob_build_id, job_intervals_json_text, is_pr=is_pr)


def parse_releaseinfo_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str) -> Optional[List[Dict]]:
    b = global_result_storage_bucket_client.get_blob(file_path)
    if not b:
        return None
    is_pr = '/pull/' in file_path
    releaseinfo_json_text = b.download_as_text()
    return parse_releaseinfo_json(prowjob_name, prowjob_build_id, releaseinfo_json_text, is_pr=is_pr)


def parse_junit_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str) -> Optional[List[Dict]]:

    if file_path.endswith('junit_operator.xml'):
        # This is ci-operator's junit. Not important for TRT atm.
        return None

    b = global_result_storage_bucket_client.get_blob(file_path)
    if not b:
        return None

    if b.size is not None and b.size == 0:
        # Lots of empty junit xml files it turns out.
        return None

    updated_dt = b.updated
    if not updated_dt:
        updated_dt = datetime.datetime.now()

    junit_xml_text = b.download_as_text()
    junit_xml_text = re.sub(junk_bytes_pattern, ' ', junit_xml_text)
    updated_time_str = updated_dt.strftime('%Y-%m-%d %H:%M:%S')  # goal is something like '2023-03-10 22:46:35'
    return parse_junit_xml(junit_xml_text, modified_time=updated_time_str, prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)


def process_junit_file_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str):
    junit_records = parse_junit_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    if not junit_records:
        return
    bq = global_bq_client
    errors = bq.insert_rows_json(global_bucket_info.table_id_junit, junit_records)
    if errors == []:
        print(f"New rows have been added: {len(junit_records)}.")
    else:
        print(f"Encountered errors while inserting junit rows ({file_path}): {errors[0]}")
        raise IOError("Encountered errors while inserting junit rows")


def process_pr_junit_file_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str):
    junit_records = parse_junit_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    if not junit_records:
        return
    bq = global_bq_client
    errors = []
    chunk_size = 500  # bigquery will return 413 if incoming request is too large (10MB). Chunk the results if they are long
    remaining_records = junit_records
    while remaining_records:
        chunk, remaining_records = remaining_records[:chunk_size], remaining_records[chunk_size:]
        errors.extend(bq.insert_rows_json(global_bucket_info.table_id_junit_pr, chunk))
    if errors == []:
        print(f"New rows have been added: {len(junit_records)}.")
    else:
        print(f"Encountered errors while inserting junit rows ({file_path}): {errors[0]}")
        raise IOError(f"Encountered errors while inserting junit rows")


def parse_junit_from_gcs_file_path(file_path: str) -> List[Dict]:
    try:
        junit_match = junit_path_pattern.match(file_path)
        if junit_match:
            prowjob_name = junit_match.group(2)
            prowjob_build_id = junit_match.group(3)
            junit_records = parse_junit_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id,
                                                 file_path=file_path)

            # There are a huge number of junit records to send. When all records are passed by the main
            # thread, the number of records grows so large that memory is exhausted. To help with that,
            # each thread, if it has a significant number of records, will make its own bigquery insert.

            if junit_records and len(junit_records) > 50:  # Pass small count updates back to the main thread to be grouped
                errors = global_bq_client.insert_rows_json(global_bucket_info.table_id_junit, junit_records)
                if errors != []:
                    print(f'ERROR: thread could not insert records: {errors}')
                junit_records.clear()
            return junit_records
    except Exception as e:
        print(f'\n\nError while processing: {file_path}')
        traceback.print_exc()
    return []


def parse_junit_pr_from_gcs_file_path(file_path: str) -> List[Dict]:
    try:
        junit_match = junit_path_pattern.match(file_path)
        if junit_match:
            prowjob_name = junit_match.group(2)
            prowjob_build_id = junit_match.group(3)
            junit_records = parse_junit_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id,
                                                 file_path=file_path)

            # There are a huge number of junit records to send. When all records are passed by the main
            # thread, the number of records grows so large that memory is exhausted. To help with that,
            # each thread, if it has a significant number of records, will make its own bigquery insert.

            if junit_records and len(junit_records) > 50 and len(junit_records) < 1000:  # Pass small count updates back to the main thread to be grouped
                errors = global_bq_client.insert_rows_json(global_bucket_info.table_id_junit_pr, junit_records)
                if errors != []:
                    print(f'ERROR: thread could not insert records: {errors}')
                junit_records.clear()
            return junit_records
    except Exception as e:
        print(f'\n\nError while processing: {file_path}')
        traceback.print_exc()
    return []


def parse_job_intervals_from_gcs_file_path(file_path: str) -> List[Dict]:
    try:
        prowjob_path_matcher = prowjob_path_pattern.match(file_path)
        if prowjob_path_matcher:
            prowjob_name = prowjob_path_matcher.group(2)
            prowjob_build_id = prowjob_path_matcher.group(3)
            return parse_job_intervals_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    except Exception as e:
        print(f'\n\nError while processing job intervals: {file_path}')
        traceback.print_exc()
    return []


def parse_releaseinfo_from_gcs_file_path(file_path: str) -> List[Dict]:
    try:
        prowjob_path_matcher = prowjob_path_pattern.match(file_path)
        if prowjob_path_matcher:
            prowjob_name = prowjob_path_matcher.group(2)
            prowjob_build_id = prowjob_path_matcher.group(3)
            return parse_releaseinfo_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    except Exception as e:
        print(f'\n\nError while processing releaseinfo: {file_path}')
        traceback.print_exc()
    return []


def process_job_intervals_from_gcs_file_path(file_path: str):
    record_dicts = parse_job_intervals_from_gcs_file_path(file_path)
    if not record_dicts:
        return
    bq = global_bq_client
    errors = []
    chunk_size = 100  # bigquery will return 413 if incoming request is too large (10MB). Chunk the results if they are long
    remaining_records = record_dicts
    while remaining_records:
        chunk, remaining_records = remaining_records[:chunk_size], remaining_records[chunk_size:]
        op_errors = bq.insert_rows_json(global_bucket_info.table_id_job_intervals, chunk)
        errors.extend(op_errors)
        if op_errors != []:
            print(f"Encountered errors while inserting intervals rows ({file_path}): First record: {chunk[0]} First error: {errors[0]}")

    if errors == []:
        print(f"New rows have been added: {len(record_dicts)}.")
    else:
        raise IOError("Encountered errors while inserting intervals rows")


def process_releaseinfo_from_gcs_file_path(file_path: str):
    record_dicts = parse_releaseinfo_from_gcs_file_path(file_path)
    if not record_dicts:
        return
    bq = global_bq_client
    errors = bq.insert_rows_json(global_bucket_info.table_id_job_releases, record_dicts)
    if errors == []:
        print("New rows have been added.")
    else:
        print(f"Encountered errors while inserting release info rows ({file_path}): {errors[0]}")
        raise IOError("Encountered errors while inserting release info rows")


def process_prowjob_from_gcs(file_path: str, bkt: str):
    record_dict = parse_prowjob_from_gcs(file_path, bkt)
    if not record_dict:
        return
    bq = global_bq_client
    errors = bq.insert_rows_json(global_bucket_info.table_id_jobs, [record_dict])
    if errors == []:
        print("New rows have been added.")
    else:
        print(f"Encountered errors while inserting prowjob rows ({file_path}): {errors[0]}")
        raise IOError("Encountered errors while inserting prowjob rows")


def gcs_finalize(event, context):
    """Triggered by a change to a Cloud Storage bucket.
    Args:
         event (dict): Event payload.
         context (google.cloud.functions.Context): Metadata for the event.
    """
    file = event
    bucket: str = file["bucket"]
    gcs_file_name: str = file['name']

    if gcs_file_name.endswith("/prowjob.json"):
        process_connection_setup(bucket=bucket)
        process_prowjob_from_gcs(gcs_file_name, bucket)
    elif gcs_file_name.endswith('/build-log.txt'):
        # there does not seem to be a guarantee on which file is written last. This
        # may lead to duplicates, but that is better than missing data.
        process_connection_setup(bucket=bucket)
        file_path = pathlib.Path(gcs_file_name)
        ci_operator_log_path = file_path.parent.joinpath('build-log.txt')
        new_row, _, junit_files = process_build_log_txt_path(bucket, str(ci_operator_log_path))

        if new_row is not None:
            table_ref = global_bq_client.dataset('ci_analysis_us').table('ci_operator_logs_json')
            ci_operator_logs_json_table = global_bq_client.get_table(table_ref)
            errors = global_bq_client.insert_rows(ci_operator_logs_json_table, [new_row])
            if errors != []:
                print(f'Errors encountered while inserting ci-operator.log for {ci_operator_log_path}: {errors[0]}')
                raise IOError("Encountered errors while inserting ci-operator.log rows")

        write_junit_files(junit_files)

    elif gcs_file_name.endswith('.xml'):
        process_connection_setup(bucket=bucket)
        junit_match = junit_path_pattern.match(gcs_file_name)
        if junit_match:
            prowjob_name = junit_match.group(2)
            prowjob_build_id = junit_match.group(3)
            if '/pull/' in gcs_file_name:
                process_pr_junit_file_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id,
                                               file_path=gcs_file_name)
            else:
                process_junit_file_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id,
                                            file_path=gcs_file_name)
    elif gcs_file_name.endswith('/releaseinfo.json'):
        process_connection_setup(bucket=bucket)
        process_releaseinfo_from_gcs_file_path(gcs_file_name)
    #elif 'e2e-timelines_everything_' in gcs_file_name and gcs_file_name.endswith('.json'):
    #    process_connection_setup(bucket=bucket)
    #    process_job_intervals_from_gcs_file_path(gcs_file_name)


def qe_process_queue(input_queue):
    qe_bucket = 'qe-private-deck'
    process_connection_setup(bucket=qe_bucket)

    for event in iter(input_queue.get, 'STOP'):
        gcs_finalize(event, None)


def ci_process_queue(input_queue):
    process_connection_setup('test-platform-results')

    for event in iter(input_queue.get, 'STOP'):
        gcs_finalize(event, None)


def cold_load_qe():
    qe_bucket = 'qe-private-deck'
    process_connection_setup(bucket=qe_bucket)

    queue = multiprocessing.Queue(os.cpu_count() * 300)
    worker_pool = [multiprocessing.Process(target=qe_process_queue, args=(queue,)) for _ in range(max(os.cpu_count() - 2, 1))]
    for worker in worker_pool:
        worker.start()

    object_count = 0
    for blob in global_storage_client.list_blobs(qe_bucket):
        event = {
            'bucket': qe_bucket,
            'name': blob.name
        }
        queue.put(event)
        object_count += 1
        if object_count % 10000 == 0:
            print(object_count)

    for worker in worker_pool:
        queue.put('STOP')

    for worker in worker_pool:
        worker.join()


def cold_load_intervals():
    process_connection_setup('test-platform-results')

    origin_ci_test_usage_table_id = 'openshift-gce-devel.ci_analysis_us.origin-ci-test_usage_analysis_intervals'
    query_relevant_storage_paths = f"""
    SELECT DISTINCT cs_object FROM `{origin_ci_test_usage_table_id}`
    WHERE time_micros > 1690897899000000 AND cs_object LIKE "%/e2e-timelines_everything%.json" AND cs_method IN ("PUT", "POST")
    """

    paths = global_bq_client.query(query_relevant_storage_paths)

    queue = multiprocessing.Queue(os.cpu_count() * 1000)
    worker_pool = [multiprocessing.Process(target=ci_process_queue, args=(queue,)) for _ in range(max(os.cpu_count() - 2, 1))]
    for worker in worker_pool:
        worker.start()

    object_count = 0
    for record in paths:
        blob_name = record['cs_object']
        print(f'Processing {blob_name}')
        event = {
            'bucket': 'origin-ci-test',
            'name': blob_name
        }
        queue.put(event)
        object_count += 1
        if object_count % 10000 == 0:
            print(object_count)

    for worker in worker_pool:
        queue.put('STOP')

    for worker in worker_pool:
        worker.join()


CI_OPERATOR_USING_NAMESPACE_PATTERN = re.compile(r"(Using namespace .*/(?P<ci_namespace>[^/]+))$")
CI_OPERATOR_USING_FARM_NAME_PATTERN = re.compile(r"(Using namespace .*\.(?P<ci_cluster>build\d+)\..*)$")
MAX_CI_OPERATOR_MSG_SIZE = 100 * 1024  # 100K
MAX_BUILD_LOG_TXT_BLOB_SIZE = 5 * 1024 * 1024


# Regular expression pattern to match ANSI escape sequences. When
# dealing with ci-operator logs, ANSI color codes are found in build-log.txt
# output.
ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*m')
BINARY_ANSI_ESCAPE_PATTERN = re.compile(rb'\x1b\[[0-9;]*[A-Za-z]')

BUILD_LOG_CI_OPERATOR_LOG_ENTRY_PREFIX = re.compile(rf"^(?:{ANSI_ESCAPE_PATTERN.pattern})?(?P<level>INFO|DEBUG|ERROR|WARN|ERRO|DEBU|TRACE|TRAC|FATAL|FATA)(?:{ANSI_ESCAPE_PATTERN.pattern})?\[(?P<timestamp>[-0-9:TZ]+)\](?:{ANSI_ESCAPE_PATTERN.pattern})? ")  # Capturing for decomposing prefix into parts

# If we are not given a structure ci-operator log line in build-log.txt, then this regex is used to
# determine whether we include the build-log.txt line or not. This is
# used because some build-log.txt files can be 50000 lines long and
# serve no diagnostic purpose.
INTERESTING_BUILD_LOG = re.compile(
    r"(?i)"                     # Case insensitive flag
    r"(?:\bERROR\b"             # Match 'ERROR'
    r"|\bWARNING\b"             # Match 'WARNING'
    r"|\bFATAL\b"               # Match 'FATAL'
    r"|\bTIMEOUT\b"             # Match 'TIMEOUT'
    r"|\bTIME-OUT\b"            # Match 'TIME-OUT'
    r"|[WEF]\d{8})"             # Match glog prefix for WARNING, ERROR, or FATAL (e.g., 'WYYYYMMDD')
)


class ExecutionTimer:

    def __init__(self, timer_name: str, timers: Optional[Dict[str, int]]):
        self.start_time = 0
        self.timer_name = timer_name
        self.timers = timers

    def __enter__(self):
        self.start_time = time.time_ns()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        end_time = time.time_ns()
        elapsed_time = end_time - self.start_time
        if self.timers is not None:
            if self.timer_name not in self.timers:
                self.timers[self.timer_name] = 0
            self.timers[self.timer_name] += elapsed_time



_nanosecond_size  = 1
_microsecond_size = 1000 * _nanosecond_size
_millisecond_size = 1000 * _microsecond_size
_second_size      = 1000 * _millisecond_size
_minute_size      = 60   * _second_size
_hour_size        = 60   * _minute_size
_day_size         = 24   * _hour_size
_week_size        = 7    * _day_size
_month_size       = 30   * _day_size
_year_size        = 365  * _day_size

units = {
    "ns": _nanosecond_size,
    "us": _microsecond_size,
    "µs": _microsecond_size,
    "μs": _microsecond_size,
    "ms": _millisecond_size,
    "s":  _second_size,
    "m":  _minute_size,
    "h":  _hour_size,
    "d":  _day_size,
    "w":  _week_size,
    "mm": _month_size,
    "y":  _year_size,
}

level_map = {
    'e': 'error',
    'w': 'warn',
    'd': 'debug',
    't': 'trace',
    'f': 'fatal',
    'i': 'info',
}

_duration_re = re.compile(r'([\d\.]+)([a-zµμ]+)')


class DurationError(ValueError):
    """duration error"""


def from_str(duration):
    """Parse a duration string to a datetime.timedelta"""

    original = duration

    if duration in ("0", "+0", "-0"):
        return datetime.timedelta()

    sign = 1
    if duration and duration[0] in '+-':
        if duration[0] == '-':
            sign = -1
        duration = duration[1:]

    matches = list(_duration_re.finditer(duration))
    if not matches:
        raise DurationError("Invalid duration {}".format(original))
    if matches[0].start() != 0 or matches[-1].end() != len(duration):
        raise DurationError(
            'Extra chars at start or end of duration {}'.format(original))

    total = 0
    for match in matches:
        value, unit = match.groups()
        if unit not in units:
            raise DurationError(
                "Unknown unit {} in duration {}".format(unit, original))
        try:
            total += float(value) * units[unit]
        except Exception:
            raise DurationError(
                "Invalid value {} in duration {}".format(value, original))

    microseconds = total / _microsecond_size
    return datetime.timedelta(microseconds=sign * microseconds)


def to_str(delta, extended=False):
    """Format a datetime.timedelta to a duration string"""

    total_seconds = delta.total_seconds()
    sign = "-" if total_seconds < 0 else ""
    nanoseconds = round(abs(total_seconds * _second_size), 0)

    if abs(total_seconds) < 1:
        result_str = _to_str_small(nanoseconds, extended)
    else:
        result_str = _to_str_large(nanoseconds, extended)

    return "{}{}".format(sign, result_str)


def _to_str_small(nanoseconds, extended):

    result_str = ""

    if not nanoseconds:
        return "0"

    milliseconds = int(nanoseconds / _millisecond_size)
    if milliseconds:
        nanoseconds -= _millisecond_size * milliseconds
        result_str += "{:g}ms".format(milliseconds)

    microseconds = int(nanoseconds / _microsecond_size)
    if microseconds:
        nanoseconds -= _microsecond_size * microseconds
        result_str += "{:g}us".format(microseconds)

    if nanoseconds:
        result_str += "{:g}ns".format(nanoseconds)

    return result_str


def _to_str_large(nanoseconds, extended):

    result_str = ""

    if extended:

        years = int(nanoseconds / _year_size)
        if years:
            nanoseconds -= _year_size * years
            result_str += "{:g}y".format(years)

        months = int(nanoseconds / _month_size)
        if months:
            nanoseconds -= _month_size * months
            result_str += "{:g}mm".format(months)

        days = int(nanoseconds / _day_size)
        if days:
            nanoseconds -= _day_size * days
            result_str += "{:g}d".format(days)

    hours = int(nanoseconds / _hour_size)
    if hours:
        nanoseconds -= _hour_size * hours
        result_str += "{:g}h".format(hours)

    minutes = int(nanoseconds / _minute_size)
    if minutes:
        nanoseconds -= _minute_size * minutes
        result_str += "{:g}m".format(minutes)

    seconds = float(nanoseconds) / float(_second_size)
    if seconds:
        nanoseconds -= _second_size * seconds
        result_str += "{:g}s".format(seconds)

    return result_str


def extract_and_convert_to_ms(multiline_string):
    # Search for the line that contains the time duration
    pattern = re.compile(r"Spent ([.0-9mhds]+) waiting for image pull secrets")
    match = pattern.search(multiline_string)

    if match:
        # Extract the time duration
        duration_str = match.group(1)
        # Convert the duration to milliseconds
        return int(from_str(duration_str).total_seconds() * 1000)
    else:
        return None


def process_ci_operator_log_path_pull_secret_load_time(bucket_name: str, ci_operator_log_path: str, timers: Optional[Dict[str, float]] = None) -> [Dict, int]:
    process_connection_setup(bucket_name)
    empty_result = (None, 0)

    # Example path: logs/branch-ci-openshift-cluster-monitoring-operator-master-images/1818971764282101760/build-log.txt
    # ..../<prowjob_job_name>/<prowjob_build_id>/artifacts/ci-operator.log

    path_components = ci_operator_log_path.rsplit('/', 4)
    prowjob_job_name = path_components[-4]
    prowjob_build_id = path_components[-3]

    if not prowjob_build_id_pattern.match(prowjob_build_id):  # This doesn't appear to be a build-log in the root of a prowjob.
        return empty_result

    prowjob_url = global_bucket_info.bucket_url_prefix + ci_operator_log_path.rsplit('/', 2)[0]

    # This used to be grabbed from finished.json, but it may not exist at the time build-logs.txt is written.
    # Just do a join if you reall need to know.
    prowjob_state = None

    row_size_estimate = 0
    try:

        b = global_result_storage_bucket_client.get_blob(ci_operator_log_path)
        if not b:
            return empty_result

        blob_created_at = b.time_created
        if b.size < MAX_BUILD_LOG_TXT_BLOB_SIZE:
            with ExecutionTimer('download', timers):
                ci_operator_log_json: str = b.download_as_text()
        else:
            return empty_result

        time_spent_ms = extract_and_convert_to_ms(ci_operator_log_json)
        if time_spent_ms is None:
            print(f'Unable to find time spent in {ci_operator_log_path}')
            return empty_result

        new_row = {
            'created': str(blob_created_at),
            'prowjob_build_id': prowjob_build_id,
            'prowjob_job_name': prowjob_job_name,
            'path': ci_operator_log_path,
            'file_size': b.size,
            'duration': time_spent_ms,
            'prowjob_url': prowjob_url,
            'schema_level': TEMP_CI_OPERATOR_SCHEMA_LEVEL,
            'prowjob_state': prowjob_state,
        }
        row_size_estimate += 1000

        return new_row, row_size_estimate

    except Exception as e:
        print(f'Failed on {ci_operator_log_path}: {e}')
        return empty_result


def process_build_log_txt_path(bucket_name: str, build_log_txt_path: str, timers: Optional[Dict[str, float]] = None) -> [Dict, int, Optional[List[JunitFile]]]:
    process_connection_setup(bucket_name)
    junit_files: List[JunitFile] = list()
    empty_result = (None, 0, junit_files)

    # Example path: logs/branch-ci-openshift-cluster-monitoring-operator-master-images/1818971764282101760/build-log.txt
    # ..../<prowjob_job_name>/<prowjob_build_id>/build-log.txt

    path_components = build_log_txt_path.rsplit('/', 4)
    prowjob_job_name = path_components[-3]
    prowjob_build_id = path_components[-2]

    if not prowjob_build_id_pattern.match(prowjob_build_id):  # This doesn't appear to be a build-log.txt in the root of a prowjob.
        return empty_result

    prowjob_url = global_bucket_info.bucket_url_prefix + build_log_txt_path.rsplit('/', 1)[0]

    prowjob_state = None
    ci_namespace = None
    try:
        finished_json_path = build_log_txt_path.rsplit('/', 1)[0] + '/' + 'finished.json'
        fj = global_result_storage_bucket_client.get_blob(finished_json_path)
        if fj:
            finished_json_content: str = fj.download_as_text()
            finished = json.loads(finished_json_content)
            prowjob_state = finished['result']
            if 'metadata' in finished and 'work-namespace' in finished['metadata']:
                # Not all finished.json have metadata.
                ci_namespace = finished['metadata']['work-namespace']
        else:
            print(f'finished.json does not exist yet; skipping build-log.txt for now')
            return empty_result
    except Exception as e:
        print(f'Failed to load finished.json {prowjob_url}: {e}')

    prowjob_junit_testcases: List[JunitTestcase] = list()
    testcase_prowjob_no_timeout = JunitTestcase(
        name='Job run should complete before timeout'
    )
    prowjob_junit_testcases.append(testcase_prowjob_no_timeout)
    prowjob_junit_file = JunitFile(
        gcs_path=str(pathlib.Path(build_log_txt_path).parent.joinpath('prowjob_junit.xml')),
        suite='prowjob-junit',
        properties=None,
        testcases=prowjob_junit_testcases,
    )
    junit_files.append(prowjob_junit_file)

    row_size_estimate = 0
    try:

        b = global_result_storage_bucket_client.get_blob(build_log_txt_path)
        if not b:
            return empty_result

        blob_created_at = b.time_created
        if b.size < MAX_BUILD_LOG_TXT_BLOB_SIZE:
            with ExecutionTimer('download', timers):
                build_log_txt_content: str = b.download_as_text()
        else:
            return empty_result

        with ExecutionTimer('splitlines', timers):
            split_results = build_log_txt_content.splitlines(keepends=True)

        in_ci_operator_entry = None

        log_entries = []
        for result in split_results:
            with ExecutionTimer('ci_operator_log_match', timers):
                match = BUILD_LOG_CI_OPERATOR_LOG_ENTRY_PREFIX.match(result)

            if 'Process did not finish before ' in result:
                testcase_prowjob_no_timeout.set_failure_msg(result)

            if match:
                if in_ci_operator_entry:
                    with ExecutionTimer('log_entries_append', timers):
                        in_ci_operator_entry['msg'] = ''.join(in_ci_operator_entry['msg'])  # Flatten the list before inserting
                        log_entries.append(in_ci_operator_entry)

                level = match.group('level').lower()
                normalized_level = level_map.get(level[0], level)

                timestamp = match.group('timestamp')
                msg = result[match.end():]
                if level in ["trace", "debug"]:
                    continue
                dt = datetime.datetime.fromisoformat(timestamp.rstrip('Z'))
                dt.replace(tzinfo=datetime.timezone.utc)

                if msg.startswith('Ran for '):
                    prowjob_duration_str = msg.strip().split(' ')[-1]
                    prowjob_duration = parse_go_duration(prowjob_duration_str)
                    testcase_prowjob_no_timeout.set_time(prowjob_duration)

                in_ci_operator_entry = {
                    'level': normalized_level,
                    'time': str(dt),
                    'msg': [msg],  # To prevent a large number of slow appends, just keep msgs in a list and ''.join at the end.
                }
            else:
                if in_ci_operator_entry:
                    with ExecutionTimer('msg_add', timers):
                        in_ci_operator_entry['msg'].append(result)
                else:
                    with ExecutionTimer('interesting_match', timers):
                        if not INTERESTING_BUILD_LOG.match(result[:50]):
                            continue

                    with ExecutionTimer('log_entries_append', timers):
                        log_entries.append({
                            'level': "info",
                            'time': str(b.time_created),
                            'msg': result,
                        })

        if in_ci_operator_entry:
            with ExecutionTimer('log_entries_append', timers):
                in_ci_operator_entry['msg'] = ''.join(in_ci_operator_entry['msg'])  # Flatten the list before inserting
                log_entries.append(in_ci_operator_entry)

        if not log_entries:
            return empty_result

        with ExecutionTimer('log_entries_scan', timers):
            ci_cluster = None
            final_entries = []
            for entry in log_entries:
                if 'msg' not in entry or 'level' not in entry:
                    continue

                msg: str = entry['msg'].lstrip()

                if msg.startswith('Using namespace '):
                    m = CI_OPERATOR_USING_NAMESPACE_PATTERN.match(msg)
                    if ci_namespace is None and m:
                        # If finished.json did not give us ci_namespace
                        ci_namespace = m.group('ci_namespace')

                    m = CI_OPERATOR_USING_FARM_NAME_PATTERN.match(msg)
                    if m:
                        ci_cluster = m.group('ci_cluster')
                    if 'build02.vmc' in msg:
                        ci_cluster = 'vsphere02'
                    if 'l2s4.p1' in msg:
                        ci_cluster = 'app.ci'

                if len(msg) > MAX_CI_OPERATOR_MSG_SIZE:
                    with ExecutionTimer('truncation', timers):
                        msg = msg[:(MAX_CI_OPERATOR_MSG_SIZE >> 1)] + "...TRUNCATED..." + msg[-1 * (MAX_CI_OPERATOR_MSG_SIZE >> 1):]
                        entry['msg'] = msg

                entry_as_str = json.dumps(entry)
                final_entries.append(entry)
                row_size_estimate += len(entry_as_str)

        row_size_estimate += 2048  # Add size of other fields once encoded into JSON.
        new_row = {
            'created': str(blob_created_at),
            'prowjob_build_id': prowjob_build_id,
            'prowjob_job_name': prowjob_job_name,
            'path': build_log_txt_path,
            'file_size': b.size,
            'ci_cluster': ci_cluster,
            'ci_namespace': ci_namespace,
            'logs': final_entries,
            'prowjob_url': prowjob_url,
            'schema_level': CI_OPERATOR_LOGS_JSON_SCHEMA_LEVEL,
            'prowjob_state': prowjob_state,
        }

        return new_row, row_size_estimate, junit_files

    except Exception as e:
        print(f'Failed on {build_log_txt_path}: {e}')
        return empty_result


INSERT_PAYLOAD_SIZE_LIMIT = 2 * 1024 * 1024


def build_log_txt_process_queue(input_queue):
    global global_bq_client
    rows_added = 0  # How many rows this particular worker has added

    rows_to_insert = []
    rows_to_insert_size_estimate = 0
    ci_operator_logs_json_table = None
    timers: Dict[str, int] = dict()

    def insert_available_rows(retries_remaining=3):
        global global_bq_client
        nonlocal rows_to_insert
        nonlocal rows_to_insert_size_estimate

        if not rows_to_insert:
            return

        try:
            with ExecutionTimer('insert_rows_json', timers):
                errors = global_bq_client.insert_rows_json(ci_operator_logs_json_table, rows_to_insert, retry=bigquery.DEFAULT_RETRY.with_deadline(30))
                if errors != []:
                    raise IOError(f"Encountered errors while inserting rows: {errors}")

            print(f"Worker {os.getpid()} successfully appended rows: {len(rows_to_insert)}")
            for key, value in timers.items():
                seconds = value / 1_000_000_000  # Convert to seconds
                print(f"    {os.getpid()}  {key}: {seconds:.3f} seconds")

            rows_to_insert = []
            rows_to_insert_size_estimate = 0

        except Exception as e:
            if retries_remaining == 0:
                raise
            print(f'Worker {os.getpid()} failed on insert; RESTARTING CLIENT: {e}')
            global_bq_client = bigquery.Client(project=global_bucket_info.bigquery_project)
            insert_available_rows(retries_remaining-1)

    for event in iter(input_queue.get, 'STOP'):
        bucket_name = event['bucket_name']
        process_connection_setup(bucket_name)  # Note that bucket must be the same for every event in the queue
        if ci_operator_logs_json_table is None:
            table_ref = global_bq_client.dataset('ci_analysis_us').table('ci_operator_logs_json')
            ci_operator_logs_json_table = global_bq_client.get_table(table_ref)

        build_log_txt_file_path = event['file_path']
        new_row, new_row_size_estimate, junit_files = process_build_log_txt_path(bucket_name, build_log_txt_file_path, timers=timers)
        write_junit_files(junit_files)
        if new_row is None:
            continue

        if rows_to_insert_size_estimate > 0 and rows_to_insert_size_estimate + new_row_size_estimate > INSERT_PAYLOAD_SIZE_LIMIT:
            # Insert old rows before appending new ones to stay under 10MB bigquery insert limit
            insert_available_rows()

        rows_to_insert.append(new_row)
        rows_to_insert_size_estimate += new_row_size_estimate

        rows_added += 1
        if rows_added % 100 == 0:
            print(f'Worker {os.getpid()} has processed {rows_added}')

    insert_available_rows()


def cold_load_build_log_txt(bucket_name):
    process_connection_setup(bucket_name)

    query_relevant_storage_paths = f"""
    SELECT 
        DISTINCT created, jobs.prowjob_url AS prowjob_url
    FROM 
        # Exclude prowjobs which have their prowjob_build_id already in the ci_operator_logs_json table.
        `{global_bucket_info.table_id_jobs}` AS jobs
    WHERE
        jobs.prowjob_start > DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 6 MONTH)  # GCS only stores back 6 months, so no point going back further.
        AND
        jobs.prowjob_build_id NOT IN (
            SELECT prowjob_build_id
            FROM `{global_bucket_info.table_id_ci_operator_logs_json}`
            WHERE schema_level = {CI_OPERATOR_LOGS_JSON_SCHEMA_LEVEL}
        )
    ORDER BY jobs.created DESC
    """

    prowjob_urls = global_bq_client.query(query_relevant_storage_paths)

    # DEBUG
    # prowjob_urls = [
    #     {
    #         'prowjob_url': 'https://prow.ci.openshift.org/view/gs/test-platform-results/logs/periodic-ci-openshift-release-master-ci-4.15-e2e-azure-ovn-serial/1781232660836782080'
    #     }
    # ]

    queue = multiprocessing.Queue(os.cpu_count() * 1000)  # Maximum number of events that can be waiting in the queue at a given time
    workers_per_cpu = 3  # Number of processes per CPU trying to use the streaming API to bigquery
    worker_pool = [multiprocessing.Process(target=build_log_txt_process_queue, args=(queue,)) for _ in range(workers_per_cpu * os.cpu_count())]
    # worker_pool = [multiprocessing.Process(target=build_log_txt_process_queue, args=(queue,)) for _ in range(1)]  # TESTING ONLY
    for worker in worker_pool:
        worker.start()

    prowjob_url_bucket_prefix = global_bucket_info.bucket_url_prefix

    object_count = 0
    for record in prowjob_urls:

        prowjob_url: str = record['prowjob_url']
        if not prowjob_url:
            continue

        if not prowjob_url.startswith(prowjob_url_bucket_prefix):
            print(f'Prowjob URL did not start with the expected prefix: {prowjob_url} (expected: {prowjob_url_bucket_prefix}')
            continue

        prowjob_url = prowjob_url.rstrip('/')  # The convention today is for the URL to not have a trailing slash, but remove, just in case.
        # Should be a URL like: https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/openstack-k8s-operators_openstack-baremetal-operator/192/pull-ci-openstack-k8s-operators-openstack-baremetal-operator-main-precommit-check/1820435531016704000
        # Notice that the prowjob_job_name is the second to last path element
        bucket_path_to_job_files = prowjob_url[len(prowjob_url_bucket_prefix):]  # Turn prowjob url to path in bucket; e.g. url => pr-logs/pull/openstack-k8s-operators_openstack-baremetal-operator/192/pull-ci-openstack-k8s-operators-openstack-baremetal-operator-main-precommit-check/1820435531016704000
        bucket_path_to_build_log_txt = f'{bucket_path_to_job_files}/build-log.txt'
        event = {
            'bucket_name': bucket_name,
            'file_path': bucket_path_to_build_log_txt,
        }
        queue.put(event)
        object_count += 1
        if object_count % 10000 == 0:
            print(f'prowjob records queued so far: {object_count}')

    for _ in worker_pool:
        queue.put('STOP')

    for worker in worker_pool:
        worker.join()


def ci_operator_load_time_process_queue(input_queue):
    global global_bq_client
    rows_added = 0  # How many rows this particular worker has added

    rows_to_insert = []
    rows_to_insert_size_estimate = 0
    temp_ci_operator_load_time_table = None
    timers: Dict[str, int] = dict()

    def insert_available_rows(retries_remaining=3):
        global global_bq_client
        nonlocal rows_to_insert
        nonlocal rows_to_insert_size_estimate

        if not rows_to_insert:
            return

        try:
            with ExecutionTimer('insert_rows_json', timers):
                errors = global_bq_client.insert_rows_json(temp_ci_operator_load_time_table, rows_to_insert, retry=bigquery.DEFAULT_RETRY.with_deadline(30))
                if errors != []:
                    raise IOError(f"Encountered errors while inserting rows: {errors}")

            print(f"Worker {os.getpid()} successfully appended rows: {len(rows_to_insert)}")
            for key, value in timers.items():
                seconds = value / 1_000_000_000  # Convert to seconds
                print(f"    {os.getpid()}  {key}: {seconds:.3f} seconds")

            rows_to_insert = []
            rows_to_insert_size_estimate = 0

        except Exception as e:
            if retries_remaining == 0:
                raise
            print(f'Worker {os.getpid()} failed on insert; RESTARTING CLIENT: {e}')
            global_bq_client = bigquery.Client(project=global_bucket_info.bigquery_project)
            insert_available_rows(retries_remaining-1)

    for event in iter(input_queue.get, 'STOP'):
        bucket_name = event['bucket_name']
        process_connection_setup(bucket_name)  # Note that bucket must be the same for every event in the queue
        if temp_ci_operator_load_time_table is None:
            table_ref = global_bq_client.dataset('ci_analysis_us').table('temp_ci_operator_load_time')
            temp_ci_operator_load_time_table = global_bq_client.get_table(table_ref)

        ci_operator_log_path = event['file_path']
        new_row, new_row_size_estimate = process_ci_operator_log_path_pull_secret_load_time(bucket_name, ci_operator_log_path, timers=timers)
        if new_row is None:
            continue

        if rows_to_insert_size_estimate > 0 and rows_to_insert_size_estimate + new_row_size_estimate > INSERT_PAYLOAD_SIZE_LIMIT:
            # Insert old rows before appending new ones to stay under 10MB bigquery insert limit
            insert_available_rows()

        rows_to_insert.append(new_row)
        rows_to_insert_size_estimate += new_row_size_estimate

        rows_added += 1
        if rows_added % 100 == 0:
            print(f'Worker {os.getpid()} has processed {rows_added}')

    insert_available_rows()


def analyze_ci_operator_pull_secret_load_time(bucket_name):
    process_connection_setup(bucket_name)

    query_relevant_storage_paths = f"""
    SELECT 
        DISTINCT(jobs.prowjob_url) AS prowjob_url
    FROM 
        # Exclude prowjobs which have their prowjob_build_id already in the ci_operator_logs_json table.
        `{global_bucket_info.table_id_jobs}` AS jobs
    WHERE
        jobs.prowjob_start > DATETIME_SUB(CURRENT_DATETIME(), INTERVAL 6 MONTH)  # GCS only stores back 6 months, so no point going back further.
        AND
        jobs.prowjob_build_id NOT IN (
            SELECT prowjob_build_id
            FROM openshift-gce-devel.ci_analysis_us.temp_ci_operator_load_time
            WHERE schema_level = {TEMP_CI_OPERATOR_SCHEMA_LEVEL}
        )
    """

    # Enable processing a small number of jobs
    debug_mode = True

    prowjob_urls = global_bq_client.query(query_relevant_storage_paths)

    if debug_mode:
        prowjob_urls = [
            {
                'prowjob_url': 'https://prow.ci.openshift.org/view/gs/test-platform-results/logs/periodic-ci-openshift-microshift-release-4.16-ocp-conformance-rhel-eus-nightly-arm/1828412150050197504',
            },
            {
                'prowjob_url': 'https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/stackrox_stackrox/11537/pull-ci-stackrox-stackrox-master-ocp-4-12-ui-e2e-tests/1801701408743886848'
            }
        ]

    queue = multiprocessing.Queue(os.cpu_count() * 1000)  # Maximum number of events that can be waiting in the queue at a given time
    workers_per_cpu = 1  # Number of processes per CPU trying to use the streaming API to bigquery

    if debug_mode:
        worker_pool = [multiprocessing.Process(target=ci_operator_load_time_process_queue, args=(queue,)) for _ in range(1)]  # TESTING ONLY
    else:
        worker_pool = [multiprocessing.Process(target=ci_operator_load_time_process_queue, args=(queue,)) for _ in range(workers_per_cpu * os.cpu_count())]

    for worker in worker_pool:
        worker.start()

    prowjob_url_bucket_prefix = global_bucket_info.bucket_url_prefix

    object_count = 0
    for record in prowjob_urls:

        prowjob_url: str = record['prowjob_url']
        if not prowjob_url:
            continue

        if not prowjob_url.startswith(prowjob_url_bucket_prefix):
            print(f'Prowjob URL did not start with the expected prefix: {prowjob_url} (expected: {prowjob_url_bucket_prefix}')
            continue

        prowjob_url = prowjob_url.rstrip('/')  # The convention today is for the URL to not have a trailing slash, but remove, just in case.
        # Should be a URL like: https://prow.ci.openshift.org/view/gs/test-platform-results/pr-logs/pull/openstack-k8s-operators_openstack-baremetal-operator/192/pull-ci-openstack-k8s-operators-openstack-baremetal-operator-main-precommit-check/1820435531016704000
        # Notice that the prowjob_job_name is the second to last path element
        bucket_path_to_job_files = prowjob_url[len(prowjob_url_bucket_prefix):]  # Turn prowjob url to path in bucket; e.g. url => pr-logs/pull/openstack-k8s-operators_openstack-baremetal-operator/192/pull-ci-openstack-k8s-operators-openstack-baremetal-operator-main-precommit-check/1820435531016704000
        ci_operator_logs_path = f'{bucket_path_to_job_files}/artifacts/ci-operator.log'
        event = {
            'bucket_name': bucket_name,
            'file_path': ci_operator_logs_path,
        }
        queue.put(event)
        object_count += 1
        if object_count % 10000 == 0:
            print(f'prowjob records queued so far: {object_count}')

    for _ in worker_pool:
        queue.put('STOP')

    for worker in worker_pool:
        worker.join()


if __name__ == '__main__':
    # outcome = parse_ci_operator_graph_resources_json('abcdefg', pathlib.Path("ci-operator-graphs/ci-operator-step-graph-1.json").read_text())
    # import yaml
    # print(yaml.dump(outcome))
    # parse_prowjob_json(pathlib.Path("prowjobs/payload-pr.json").read_text())

    #process_connection_setup()
    #parse_junit_from_gcs_file_path('logs/periodic-ci-openshift-release-master-ci-4.14-e2e-gcp-sdn/1640905778267164672/artifacts/e2e-gcp-sdn/openshift-e2e-test/artifacts/junit/junit_e2e__20230329-031207.xml')

    #cold_load_build_log_txt('test-platform-results')

    import requests
    url = "https://gcsweb-ci.apps.ci.l2s4.p1.openshiftapps.com/gcs/test-platform-results/logs/periodic-ci-openshift-release-master-konflux-nightly-4.19-e2e-aws-ovn-upgrade/1912429868163796992/artifacts/e2e-aws-ovn-upgrade/openshift-e2e-test/artifacts/junit/junit_e2e__20250416-112104.xml"

    response = requests.get(url)
    response.raise_for_status()  # Raises HTTPError for bad responses

    xml_content = response.text  # This will be a UTF-8 decoded string
    parse_junit_xml(xml_content, "test", "test", "test", "test")

    #process_releaseinfo_from_gcs_file_path('pr-logs/pull/openshift_release/40864/rehearse-40864-pull-ci-openshift-cluster-api-release-4.11-e2e-aws/1675182964247367680/artifacts/e2e-aws/gather-extra/artifacts/releaseinfo.json')
    #cold_load_junit()
    # cold_load_qe()

    #process_connection_setup()
    #process_job_intervals_from_gcs_file_path('pr-logs/pull/openshift_cluster-authentication-operator/638/pull-ci-openshift-cluster-authentication-operator-release-4.13-e2e-agnostic-upgrade/1715405306432851968/artifacts/e2e-agnostic-upgrade/openshift-e2e-test/artifacts/junit/e2e-timelines_everything_20231020-173307.json')
    # cold_load_intervals()

    # analyze_ci_operator_pull_secret_load_time('test-platform-results')
