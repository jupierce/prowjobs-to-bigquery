#!/usr/bin/env python3

import datetime
import pathlib
import json
import os
import multiprocessing
import re
import hashlib
import traceback
from xml import sax

from io import StringIO
from typing import NamedTuple, List, Dict, Optional
from model import Model, Missing
from collections import defaultdict

from google.cloud import bigquery, storage

RELEASEINFO_SCHEMA_LEVEL = 2
CI_OPERATOR_LOGS_SCHEMA_LEVEL = 11
JUNIT_TABLE_SCHEMA_LEVEL = 14
JOB_INTERVALS_SCHEMA_LEVEL = 2


class BucketInfo(NamedTuple):
    """
    Depending on which GCS bucket was written to, we will be updating different datasets (potentially in different projects).
    """
    # The project in which the bucket resides. This can technically be accessed through
    # en environment variable, but that would require importing 'os' / function call which
    # could impact execution size/time & thus cost.
    bucket_project: str

    # As data is written to a bucket, this cloud function will append data to a set of tables in a destination dataset.
    # Writing to one bucket will trigger updates to
    dest_bigquery_dataset: str

    table_name_jobs: str
    table_name_junit: str
    table_name_junit_pr: str
    table_name_job_intervals: str
    table_name_job_releases: str
    table_name_ci_operator_logs: str

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
    def table_id_ci_operator_logs(self):
        return f'{self.dest_bigquery_dataset}.{self.table_name_ci_operator_logs}'


DEFAULT_TABLE_NAMES = {
    'table_name_jobs': 'jobs',
    'table_name_junit': 'junit',
    'table_name_junit_pr': 'junit_pr',
    'table_name_job_intervals': 'job_intervals',
    'table_name_job_releases': 'job_releases',
    'table_name_ci_operator_logs': 'ci_operator_logs'
}


# The list of buckets to react to and the datasets to update when writes are made to that bucket.
BUCKET_INFO_MAPPING = {
    'test-platform-results': BucketInfo(bucket_project='openshift-gce-devel', dest_bigquery_dataset='openshift-gce-devel.ci_analysis_us', **DEFAULT_TABLE_NAMES),
    'qe-private-deck': BucketInfo(bucket_project='openshift-ci-private', dest_bigquery_dataset='openshift-gce-devel.qe_testing', **DEFAULT_TABLE_NAMES),
    'origin-ci-private': BucketInfo(bucket_project='openshift-ci-private', dest_bigquery_dataset='openshift-gce-devel.ci_analysis_private', **DEFAULT_TABLE_NAMES),
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
        global_bq_client = bigquery.Client(project='openshift-gce-devel')


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

# extracts the first ocp version like '4.11' (in group 1) or 'master' or 'main' (in group 2)
branch_pattern = re.compile(r".*?\D+[-\/](\d\.\d+)\D?.*|.*\-(master|main)\-.*")


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
            message=item.get('message', None),
            locator=item.get('locator', None),
            payload=json.dumps(item),
        )
        record = dict(jr._asdict())
        records.append(record)

    return records


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


def get_nework_variants() -> List[VariantMatcher]:
    global NETWORK_VARIANTS
    if NETWORK_VARIANTS:
        return NETWORK_VARIANTS
    NETWORK_VARIANTS = []
    for network_name in ['sdn', 'ovn']:
        NETWORK_VARIANTS.append(VariantMatcher(network_name, f'-{network_name}'))
    return NETWORK_VARIANTS


def determine_prowjob_network(lowercase_prowjob_name: str, branch: str) -> str:
    for pv in get_nework_variants():
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

        if name == 'failure':
            self.test_success = False
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
            )
            self.record_dicts.append(record._asdict())

    def endElementNS(self, name, qname):
        self.endElement(name)

    def characters(self, content):
        pass


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
                handler.startElement(elem.tag, elem.attrib)
            if event == "end":
                handler.endElement(elem.tag)
                root.clear()
    else:
        sax.parseString(junit_xml_text, handler, SimpleErrorHandler())

    handler.setDocumentLocator(None)
    return handler.record_dicts


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


def parse_prowjob_from_gcs(file_path: str):
    b = global_result_storage_bucket_client.get_blob(file_path)
    if not b:
        return None

    prowjob_json_text = b.download_as_text()
    return parse_prowjob_json(prowjob_json_text)


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
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def process_pr_junit_file_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str):
    junit_records = parse_junit_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    if not junit_records:
        return
    bq = global_bq_client
    errors = []
    chunk_size = 1000  # bigquery will return 413 if incoming request is too large (10MB). Chunk the results if they are long
    remaining_records = junit_records
    while remaining_records:
        chunk, remaining_records = remaining_records[:chunk_size], remaining_records[chunk_size:]
        errors.extend(bq.insert_rows_json(global_bucket_info.table_id_junit_pr, chunk))
    if errors == []:
        print(f"New rows have been added: {len(junit_records)}.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def process_ci_operator_log_from_gcs(build_id: str, file_path: str):
    try:
        b = global_result_storage_bucket_client.get_blob(file_path)
        if not b:
            return None

        blob_created_at = b.time_created
        if b.size < 1000000:
            ci_operator_jsonl_str = b.download_as_text()
        else:
            ci_operator_jsonl_str = '"ingestion issue: ci-operator.log file too large"'

        # ci_operator_jsonl_str is a str containing a JSONL document. Parsing it in lambda could be CPU
        # intensive. So we offload that to bigquery by turning the JSONL into a
        # JSON array. With some SQL magic, bigquery can parse this JSON, and then
        # insert it into a JSON REPEATED column.

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("json_str", "STRING", ci_operator_jsonl_str),
            ]
        )

        bq = global_bq_client
        query_job = bq.query(f'''
DECLARE max_msg_size INT64 DEFAULT 100000;

INSERT INTO `openshift-gce-devel.ci_analysis_us.ci_operator_logs_json`
(
    created, 
    prowjob_build_id, 
    path, 
    file_size,
    ci_cluster,
    ci_namespace,
    entries
) 
WITH json_str_rows AS (

  # Split the jsonl table named json_str_rows with a column named lines where
  # each row contains a single json entry (still as a string)
  SELECT SPLIT(TRIM(@json_str), "\\n") as lines,

), json_obj_rows AS (

  # Create a table named json_obj_rows with a column named entry where
  # each line of json_str_rows is parsed into a JSON object.
  # Include another column named "entry_length" which is the length
  # of the string that created the JSON object.
  SELECT
    PARSE_JSON(line) as entry, LENGTH(line) as entry_length
  FROM
    json_str_rows, UNNEST(lines) as line

), json_truncated_obj_rows AS (

  # For each JSON object in json_obj_rows, see if the entry_length
  # is > the maximum size we permit, and truncate msg if it is.
  # Output a table named json_obj_rows with a single column entry
  # that has msg truncated, where necessary.
  # Only propagate entries that are level > debug.
  SELECT
    IF(entry_length < max_msg_size, entry, JSON_SET(entry, "$.msg", CONCAT(LEFT(JSON_VALUE(entry, "$.msg"), max_msg_size >> 1), "...TRUNCATED...", RIGHT(JSON_VALUE(entry, "$.msg"), max_msg_size >> 1)))) as entry
  FROM json_obj_rows
  WHERE JSON_VALUE(entry, "$.level") NOT IN ("debug", "trace")  
), job_facts AS (
  # Search for the "Using namespace " info msg which includes the build farm name build\d+
  # and the ci_namespace in which the job was run.
  # Example: Using namespace https://console.build01.ci.openshift.org/k8s/cluster/projects/ci-op-9vl5q5ch
  # The LEFT OUTER JOIN ON TRUE will ensure that a row is inserted with NULL for ci_cluster
  SELECT
    REGEXP_EXTRACT(JSON_VALUE(entry, "$.msg"), r'.*(build\\d+).*') as ci_cluster,
    REGEXP_EXTRACT(JSON_VALUE(entry, "$.msg"),  r'/([^/]+)$') as ci_namespace
  FROM json_truncated_obj_rows
  WHERE STARTS_WITH(JSON_VALUE(entry, "$.msg"), "Using namespace http")
  LIMIT 1
)
# Formulate a single row for insertion where the JSON entries are aggregated into an 
# array so that they can populated a REPEATED JSON column in the target table.
SELECT 
    TIMESTAMP("{str(blob_created_at)}"), 
    "{build_id}", 
    "{file_path}", 
    {b.size}, 
    job_facts.ci_cluster,
    job_facts.ci_namespace,
    ARRAY_AGG(json_truncated_obj_rows.entry)
FROM json_truncated_obj_rows LEFT OUTER JOIN job_facts ON TRUE
GROUP BY job_facts.ci_cluster, job_facts.ci_namespace

''', job_config=job_config)
        query_job.result()  # Wait for the job to complete
    except Exception as e:
        print(f'Error inserting ci-operator log file: {file_path}: {e}')


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
    chunk_size = 500  # bigquery will return 413 if incoming request is too large (10MB). Chunk the results if they are long
    remaining_records = record_dicts
    while remaining_records:
        chunk, remaining_records = remaining_records[:chunk_size], remaining_records[chunk_size:]
        errors.extend(bq.insert_rows_json(global_bucket_info.table_id_job_intervals, chunk))
    if errors == []:
        print(f"New rows have been added: {len(record_dicts)}.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def process_releaseinfo_from_gcs_file_path(file_path: str):
    record_dicts = parse_releaseinfo_from_gcs_file_path(file_path)
    if not record_dicts:
        return
    bq = global_bq_client
    errors = bq.insert_rows_json(global_bucket_info.table_id_job_releases, record_dicts)
    if errors == []:
        print("New rows have been added.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def process_prowjob_from_gcs(file_path: str):
    record_dict = parse_prowjob_from_gcs(file_path)
    if not record_dict:
        return
    bq = global_bq_client
    errors = bq.insert_rows_json(global_bucket_info.table_id_jobs, [record_dict])
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
    bucket: str = file["bucket"]
    gcs_file_name: str = file['name']

    if gcs_file_name.endswith("/prowjob.json"):
        process_connection_setup(bucket=bucket)
        process_prowjob_from_gcs(gcs_file_name)
    elif gcs_file_name.endswith('/finished.json'):
        process_connection_setup(bucket=bucket)
        # We need to find the prowjob build-id which should be 1 level down
        # branch-ci-openshift-jenkins-release-4.10-images/1604867803796475904/finished.json
        file_path = pathlib.Path(gcs_file_name)
        prowjob_build_id = file_path.parent.name
        ci_operator_log_path = file_path.parent.joinpath('artifacts/ci-operator.log')
        process_ci_operator_log_from_gcs(prowjob_build_id, str(ci_operator_log_path))
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
    elif 'e2e-timelines_everything_' in gcs_file_name and gcs_file_name.endswith('.json'):
        process_connection_setup(bucket=bucket)
        process_job_intervals_from_gcs_file_path(gcs_file_name)


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


PROWJOB_URL_BUCKET_PREFIX = 'https://prow.ci.openshift.org/view/gs/test-platform-results/'


def ci_operator_logs_json_process_queue(input_queue):
    process_connection_setup('test-platform-results')

    for event in iter(input_queue.get, 'STOP'):
        prowjob_build_id = event['prowjob_build_id']
        file_path = event['file_path']
        process_ci_operator_log_from_gcs(prowjob_build_id, file_path)


def cold_load_ci_operator_logs_json():
    process_connection_setup('test-platform-results')

    jobs_table_id = 'openshift-gce-devel.ci_analysis_us.jobs'
    query_relevant_storage_paths = f"""
    SELECT prowjob_build_id, prowjob_url FROM `{jobs_table_id}`
    WHERE prowjob_start > DATETIME("2024-08-02")
    """

    paths = global_bq_client.query(query_relevant_storage_paths)

    queue = multiprocessing.Queue(os.cpu_count() * 1000)
    LOADS_PER_CPU = 4  # Most of the job is waiting for the insert.result()
    worker_pool = [multiprocessing.Process(target=ci_operator_logs_json_process_queue, args=(queue,)) for _ in range(LOADS_PER_CPU * max(os.cpu_count() - 2, 1))]
    for worker in worker_pool:
        worker.start()

    object_count = 0
    for record in paths:
        prowjob_url = record['prowjob_url']
        prowjob_build_id = record['prowjob_build_id']
        if not prowjob_url:
            continue

        bucket_path_to_job_files = prowjob_url[len(PROWJOB_URL_BUCKET_PREFIX):]  # Turn prowjob url to path in bucket
        bucket_path_to_ci_operator_logs = f'{bucket_path_to_job_files}/artifacts/ci-operator.log'
        print(f'Processing {bucket_path_to_ci_operator_logs}')
        event = {
            'prowjob_build_id': prowjob_build_id,
            'file_path': bucket_path_to_ci_operator_logs
        }
        queue.put(event)
        object_count += 1
        if object_count % 10000 == 0:
            print(object_count)

    for worker in worker_pool:
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

    #cold_load_all_ci_operator_logs()

    #process_releaseinfo_from_gcs_file_path('pr-logs/pull/openshift_release/40864/rehearse-40864-pull-ci-openshift-cluster-api-release-4.11-e2e-aws/1675182964247367680/artifacts/e2e-aws/gather-extra/artifacts/releaseinfo.json')
    #cold_load_junit()
    # cold_load_qe()

    #process_connection_setup()
    #process_job_intervals_from_gcs_file_path('pr-logs/pull/openshift_cluster-authentication-operator/638/pull-ci-openshift-cluster-authentication-operator-release-4.13-e2e-agnostic-upgrade/1715405306432851968/artifacts/e2e-agnostic-upgrade/openshift-e2e-test/artifacts/junit/e2e-timelines_everything_20231020-173307.json')
    # cold_load_intervals()

    cold_load_ci_operator_logs_json()
