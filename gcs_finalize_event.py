#!/usr/bin/env python3
import datetime
import pathlib
import json
import multiprocessing
import re
import hashlib
import traceback
from xml import sax

from io import StringIO
from typing import NamedTuple, List, Dict, Optional
from model import Model, Missing, ListModel
from collections import defaultdict

from google.cloud import bigquery, storage

# Destination for parsed prowjob info
JOBS_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.jobs'

RELEASEINFO_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.job_releases'
RELEASEINFO_SCHEMA_LEVEL = 2

CI_OPERATOR_LOGS_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.ci_operator_logs'
CI_OPERATOR_LOGS_SCHEMA_LEVEL = 11

JUNIT_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.junit'
JUNIT_TABLE_SCHEMA_LEVEL = 14

JUNIT_PR_TABLE_ID = 'openshift-gce-devel.ci_analysis_us.junit_pr'

# Using globals is ugly, but when running in cold load mode, these will be set for each separate process.
# https://stackoverflow.com/questions/10117073/how-to-use-initializer-to-set-up-my-multiprocess-pool
global_storage_client = None
global_origin_ci_test_bucket_client = None

global_bq_client = None


use_ET = False
try:
    import xml.etree.ElementTree as ET
    use_ET =True
except:
    print('Unable to use ElementTree')


def process_connection_setup():
    global global_storage_client
    global global_origin_ci_test_bucket_client
    global global_bq_client
    global_storage_client = storage.Client(project='openshift-gce-devel')
    global_origin_ci_test_bucket_client = global_storage_client.bucket('origin-ci-test')
    global_bq_client = bigquery.Client()


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


class CiOperatorLogRecord(NamedTuple):
    schema_level: int
    file_path: str
    prowjob_build_id: str
    test_name: str  # multi-stage-test name when multi-stage
    step_name: Optional[str]  # if a step was being run
    build_name: Optional[str]  # if a build is being run
    slice_name: Optional[str]  # if a slice is being acquired
    lease_name: Optional[str]  # if a lease is being acquired
    prowjob_cluster: Optional[str]
    prowjob_cluster_namespace: Optional[str]
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
    r"(Building (?P<build_name_start>[^\"]+))",
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
    # Extract build farm and ci-op namespace
    r"(Using namespace .*\.(?P<prowjob_cluster>build\d+)\..*(?P<prowjob_cluster_namespace>ci-op.*))",
]

combined_pattern = r"{\"level\":\"\w+\",\"msg\":\"(?:" + \
                    '|'.join(sub_expressions) + \
                   r")\",\"time\":\"(?P<dt>[^\"]+)\"}"

log_entry_pattern = re.compile(combined_pattern)

# Group 1 extracts path up to prowjob id (e.g. 	branch-ci-openshift-jenkins-release-4.10-images/1604867803796475904 ).
# Group 2 extracts prowjob name
# Group 3 extracts the prowjob numeric id  (requires id be at least 12 digits to avoid finding PR number)
# Group 4 allows you to match different junit filenames you are interested in including
junit_path_pattern = re.compile(r"^(.*?\/([^\/]+)\/(\d{12,}))\/.*\/?(junit|e2e-monitor-tests)[^\/]+xml$")
test_id_pattern = re.compile(r"^.*{([a-f0-9]+)}.*$")

# Group 1 extracts path up to prowjob id (e.g. 	branch-ci-openshift-jenkins-release-4.10-images/1604867803796475904 ).
# Group 2 extracts prowjob name
# Group 3 extracts the prowjob numeric id (requires id be at least 12 digits to avoid finding PR number)
releaseinfo_path_pattern = re.compile(r"^(.*?\/([^\/]+)\/(\d{12,}))\/.*\/?releaseinfo\.json$")

# extracts the first ocp version like '4.11' (in group 1) or 'master' or 'main' (in group 2)
branch_pattern = re.compile(r".*?\D+[-\/](\d\.\d+)\D?.*|.*\-(master|main)\-.*")


def parse_releaseinfo_json(prowjob_name: str, prowjob_build_id: str, releaseinfo_json_text: str, is_pr: bool) -> Optional[List[Dict]]:
    try:
        releaseinfo_dict = json.loads(releaseinfo_json_text)
    except:
        print(f'Found invalid releaseinfo.json')
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


def parse_ci_operator_log_resources_text(ci_operator_log_file: str, prowjob_build_id: str, ci_operator_log_str: str) -> List[Dict]:

    def ms_diff_from_time_strs(start, stop) -> int:
        ts_start = datetime.datetime.strptime(start + ' UTC', '%Y-%m-%dT%H:%M:%S %Z')
        ts_stop = datetime.datetime.strptime(stop + ' UTC', '%Y-%m-%dT%H:%M:%S %Z')
        return int((ts_stop - ts_start) / datetime.timedelta(milliseconds=1))

    log_records: List[Dict] = list()
    prowjob_cluster = None
    prowjob_cluster_namespace = None
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

        if m.group('prowjob_cluster'):
            prowjob_cluster = m.group('prowjob_cluster')

        if m.group('prowjob_cluster_namespace'):
            prowjob_cluster_namespace = m.group('prowjob_cluster_namespace')

        # Leases
        if m.group('acquiring_test_name'):
            multi_stage_test_name = m.group('acquiring_test_name')
            for lease_name in m.group('acquiring_slice_names').split():
                lease_start[lease_name] = dt

        if m.group('acquired_slice_name'):
            slice_name = m.group('acquired_slice_name')
            lease_name = m.group('lease_names')
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
                slice_name=slice_name,
                lease_name=lease_name,
                started_at=start_time,
                finished_at=dt,
                duration_ms=ms_diff_from_time_strs(start_time, dt),
                prowjob_build_id=prowjob_build_id,
                test_name=multi_stage_test_name,
                success=True,
                prowjob_cluster=prowjob_cluster,
                prowjob_cluster_namespace=prowjob_cluster_namespace,
            )
            log_records.append(record._asdict())

        # Builds
        if m.group('build_name_start'):
            build_start[m.group('build_name_start')] = dt

        if m.group('build_name_outcome'):
            if m.group('build_name_outcome') in build_start:  # Will not be present if the build is cached.
                start_time = build_start.pop(m.group('build_name_outcome'))
                record = CiOperatorLogRecord(
                    schema_level=CI_OPERATOR_LOGS_SCHEMA_LEVEL,
                    file_path=ci_operator_log_file,
                    step_name=None,
                    build_name=m.group('build_name_outcome'),
                    slice_name=None,
                    lease_name=None,
                    started_at=start_time,
                    finished_at=dt,
                    duration_ms=ms_diff_from_time_strs(start_time, dt),
                    prowjob_build_id=prowjob_build_id,
                    test_name=multi_stage_test_name,
                    success='succeeded' in log_line,
                    prowjob_cluster=prowjob_cluster,
                    prowjob_cluster_namespace=prowjob_cluster_namespace,
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
                slice_name=None,
                lease_name=None,
                started_at=start_time,
                finished_at=dt,
                duration_ms=ms_diff_from_time_strs(start_time, dt),
                prowjob_build_id=prowjob_build_id,
                test_name=multi_stage_test_name,
                success='succeeded' in m.group('step_outcome'),
                prowjob_cluster=prowjob_cluster,
                prowjob_cluster_namespace=prowjob_cluster_namespace,
            )
            log_records.append(record._asdict())

    return log_records


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


def parse_ci_operator_log_resources_from_gcs(build_id: str, file_path: str) -> Optional[List[Dict]]:
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


junk_bytes_pattern = re.compile(r'[^\x20-\x7E]+')


def parse_releaseinfo_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str) -> Optional[List[Dict]]:
    b = global_origin_ci_test_bucket_client.get_blob(file_path)
    if not b:
        return None
    is_pr = '/pull/' in file_path
    releaseinfo_json_text = b.download_as_text()
    return parse_releaseinfo_json(prowjob_name, prowjob_build_id, releaseinfo_json_text, is_pr=is_pr)


def parse_junit_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str) -> List[Dict]:

    if file_path.endswith('junit_operator.xml'):
        # This is ci-operator's junit. Not important for TRT atm.
        return None

    b = global_origin_ci_test_bucket_client.get_blob(file_path)
    if not b:
        return None

    if b.size is not None and b.size == 0:
        # Lots of empty junit xml files it turns out..
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
    bq = bigquery.Client()
    errors = bq.insert_rows_json(JUNIT_TABLE_ID, junit_records)
    if errors == []:
        print(f"New rows have been added: {len(junit_records)}.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


def process_pr_junit_file_from_gcs(prowjob_name: str, prowjob_build_id: str, file_path: str):
    junit_records = parse_junit_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    if not junit_records:
        return
    bq = bigquery.Client()
    errors = []
    chunk_size = 1000  # bigquery will return 413 if incoming request is too large (10MB). Chunk the results if they are long
    remaining_records = junit_records
    while remaining_records:
        chunk, remaining_records = remaining_records[:chunk_size], remaining_records[chunk_size:]
        errors.extend(bq.insert_rows_json(JUNIT_PR_TABLE_ID, chunk))
    if errors == []:
        print(f"New rows have been added: {len(junit_records)}.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


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
                errors = global_bq_client.insert_rows_json(JUNIT_TABLE_ID, junit_records)
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
                errors = global_bq_client.insert_rows_json(JUNIT_PR_TABLE_ID, junit_records)
                if errors != []:
                    print(f'ERROR: thread could not insert records: {errors}')
                junit_records.clear()
            return junit_records
    except Exception as e:
        print(f'\n\nError while processing: {file_path}')
        traceback.print_exc()
    return []


def parse_releaseinfo_from_gcs_file_path(file_path: str) -> List[Dict]:
    try:
        releaseinfo_match = releaseinfo_path_pattern.match(file_path)
        if releaseinfo_match:
            prowjob_name = releaseinfo_match.group(2)
            prowjob_build_id = releaseinfo_match.group(3)
            return parse_releaseinfo_from_gcs(prowjob_name=prowjob_name, prowjob_build_id=prowjob_build_id, file_path=file_path)
    except Exception as e:
        print(f'\n\nError while processing releaseinfo: {file_path}')
        traceback.print_exc()
    return []


def process_releaseinfo_from_gcs_file_path(file_path: str):
    record_dicts = parse_releaseinfo_from_gcs_file_path(file_path)
    if not record_dicts:
        return
    bq = bigquery.Client()
    errors = bq.insert_rows_json(RELEASEINFO_TABLE_ID, record_dicts)
    if errors == []:
        print("New rows have been added.")
    else:
        raise IOError("Encountered errors while inserting rows: {}".format(errors))


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


def process_releaseinfo_from_gcs(build_id: str, file_path: str):
    releaseinfo_records = parse_releaseinfo_from_gcs(build_id, file_path)
    if not releaseinfo_records:
        return
    bq = bigquery.Client()
    errors = bq.insert_rows_json(CI_OPERATOR_LOGS_TABLE_ID, releaseinfo_records)
    if errors == []:
        print("New releaseinfo rows have been added.")
    else:
        raise IOError("Encountered errors while inserting releaseinfo rows: {}".format(errors))


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
    elif gcs_file_name.endswith('.xml'):
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
        process_releaseinfo_from_gcs_file_path(gcs_file_name)


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
            print(f"New rows have been added: {count}")
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
    WHERE time_micros > 1664582400000 AND cs_object LIKE "%/ci-operator.log"
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


def cold_load_junit():
    bq = bigquery.Client()

    remaining_paths = set()
    cache_file = pathlib.Path('cache')
    if cache_file.is_file():
        with cache_file.open('r', encoding='utf-8') as c:
            while True:
                line = c.readline()
                if not line:
                    break
                remaining_paths.add(line.strip())
    else:
        origin_ci_test_usage_table_id = 'openshift-gce-devel.ci_analysis_us.origin-ci-test_usage_analysis'
        query_all_storage_junit_paths = f"""
        SELECT DISTINCT cs_object FROM `{origin_ci_test_usage_table_id}`
        WHERE time_micros > 1664582400000 AND cs_object LIKE "%/junit%.xml" AND cs_method IN ("PUT", "POST") AND cs_object NOT LIKE "%/pull/%"
        """
        junit_xml_paths = bq.query(query_all_storage_junit_paths)

        query_all_processed_junit_paths = f"""
        SELECT DISTINCT file_path FROM `{JUNIT_TABLE_ID}` WHERE SCHEMA_LEVEL = {JUNIT_TABLE_SCHEMA_LEVEL}
        """
        junit_processed_xml_paths = bq.query(query_all_processed_junit_paths)

        all_paths = set(lp['cs_object'] for lp in junit_xml_paths)
        registered_paths = set(lp['file_path'] for lp in junit_processed_xml_paths)

        print(f'All found paths: {len(all_paths)}')
        print(f'Already registered paths: {len(registered_paths)}')
        already_done = all_paths.intersection(registered_paths)
        print(f'Already registered: {len(already_done)}')
        already_done = None
        remaining_paths = set(all_paths.difference(registered_paths))
        with cache_file.open('w+', encoding='utf-8') as c:
            for p in remaining_paths:
                c.write(f'{p}\n')

    inserts = []
    total_inserts = 0

    def send_inserts():
        if not inserts:  # No items to insert?
            return 0
        count = len(inserts)
        try:
            errors = bq.insert_rows_json(JUNIT_TABLE_ID, inserts)
            if errors == []:
                print(f"New rows have been added: {count}")
            else:
                raise IOError("Encountered errors while inserting rows: {}".format(errors))
        except Exception as e:
            print(f'Error inserting rows: {e}')
        inserts.clear()
        return count

    print(f'All remaining: {len(remaining_paths)}')

    total_file_count = len(remaining_paths)
    print(f'{total_file_count} files need to be processed')

    while len(remaining_paths) > 0:
        p_count = multiprocessing.cpu_count()
        pool = multiprocessing.Pool(p_count, process_connection_setup)
        portion_to_process = list(remaining_paths)[:5000]
        vals = pool.imap_unordered(parse_junit_from_gcs_file_path, portion_to_process, chunksize=5)
        for val in vals:
            if not val:
                continue

            inserts.extend(val)
            if len(inserts) > 1000:
                total_inserts += send_inserts()

        total_inserts += send_inserts()
        remaining_paths.difference_update(portion_to_process)
        print(f'Files remaining: {len(remaining_paths)}')
        print(f'Total number of rows inserted: {total_inserts}')
        print('CLOSING POOL')
        pool.close()
        pool.join()


def cold_load_junit_pr():
    bq = bigquery.Client()

    remaining_paths = set()
    cache_file = pathlib.Path('cache')
    if cache_file.is_file():
        with cache_file.open('r', encoding='utf-8') as c:
            while True:
                line = c.readline()
                if not line:
                    break
                remaining_paths.add(line.strip())
    else:
        origin_ci_test_usage_table_id = 'openshift-gce-devel.ci_analysis_us.origin-ci-test_usage_analysis'
        query_all_storage_junit_paths = f"""
        SELECT DISTINCT cs_object FROM `{origin_ci_test_usage_table_id}`
        WHERE time_micros > 1680901873000000 AND cs_object LIKE "%/junit%.xml" AND cs_method IN ("PUT", "POST") AND cs_object LIKE "%/pull/%"
        """

        junit_xml_paths = bq.query(query_all_storage_junit_paths)

        query_all_processed_junit_paths = f"""
        SELECT DISTINCT file_path FROM `{JUNIT_PR_TABLE_ID}` WHERE SCHEMA_LEVEL = {JUNIT_TABLE_SCHEMA_LEVEL}
        """
        junit_processed_xml_paths = bq.query(query_all_processed_junit_paths)

        all_paths = set(lp['cs_object'] for lp in junit_xml_paths)
        registered_paths = set(lp['file_path'] for lp in junit_processed_xml_paths)

        print(f'All found paths: {len(all_paths)}')
        print(f'Already registered paths: {len(registered_paths)}')
        already_done = all_paths.intersection(registered_paths)
        print(f'Already registered: {len(already_done)}')
        remaining_paths = set(all_paths.difference(registered_paths))
        with cache_file.open('w+', encoding='utf-8') as c:
            for p in remaining_paths:
                c.write(f'{p}\n')

    inserts = []
    total_inserts = 0

    def send_inserts():
        nonlocal inserts
        if not inserts:  # No items to insert?
            return 0
        subset = inserts[:1000]  # Prevent update too large errors
        inserts = inserts[1000:]
        count = len(subset)
        try:
            errors = bq.insert_rows_json(JUNIT_PR_TABLE_ID, subset)
            if errors == []:
                print(f"New rows have been added: {count}")
            else:
                raise IOError("Encountered errors while inserting rows: {}".format(errors))
        except Exception as e:
            print(f'Error inserting rows: {e}')
        return count

    print(f'All remaining: {len(remaining_paths)}')

    total_file_count = len(remaining_paths)
    print(f'{total_file_count} files need to be processed')

    while len(remaining_paths) > 0:
        p_count = multiprocessing.cpu_count()
        pool = multiprocessing.Pool(p_count, process_connection_setup)
        portion_to_process = list(remaining_paths)[:5000]
        vals = pool.imap_unordered(parse_junit_pr_from_gcs_file_path, portion_to_process, chunksize=5)
        for val in vals:
            if not val:
                continue

            inserts.extend(val)
            while len(inserts) > 1000:
                total_inserts += send_inserts()

        total_inserts += send_inserts()
        remaining_paths.difference_update(portion_to_process)
        print(f'Files remaining: {len(remaining_paths)}')
        print(f'Total number of rows inserted: {total_inserts}')
        print('CLOSING POOL')
        pool.close()
        pool.join()


if __name__ == '__main__':
    # outcome = parse_ci_operator_graph_resources_json('abcdefg', pathlib.Path("ci-operator-graphs/ci-operator-step-graph-1.json").read_text())
    # import yaml
    # print(yaml.dump(outcome))
    # parse_prowjob_json(pathlib.Path("prowjobs/payload-pr.json").read_text())

    process_connection_setup()
    #parse_junit_from_gcs_file_path('logs/periodic-ci-openshift-release-master-ci-4.14-e2e-gcp-sdn/1640905778267164672/artifacts/e2e-gcp-sdn/openshift-e2e-test/artifacts/junit/junit_e2e__20230329-031207.xml')

    #cold_load_all_ci_operator_logs()

    #process_releaseinfo_from_gcs_file_path('pr-logs/pull/openshift_release/40864/rehearse-40864-pull-ci-openshift-cluster-api-release-4.11-e2e-aws/1675182964247367680/artifacts/e2e-aws/gather-extra/artifacts/releaseinfo.json')
    #cold_load_junit()
    cold_load_junit_pr()
