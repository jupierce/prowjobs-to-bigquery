-- BigQuery table schema for ci-operator-metrics.json
-- This table stores comprehensive CI operator metrics including step-level events, builds, images, 
-- pods, nodes, leases, and platform insights
--
-- Key features:
-- - 'events' array for step-level execution tracking with structured event data
-- - Field descriptions added via OPTIONS for fields with clear semantics
-- - Partitioned by date for efficient querying
-- - Clustered by prowjob_job_name and prowjob_build_id for optimal query performance
-- - Raw test_platform_insights array preserved alongside promoted tpi_* fields for flexibility
-- - Resilient parsing: handles missing fields and schema evolution gracefully

-- Create/Replace table in ci_analysis_us dataset
CREATE OR REPLACE TABLE `openshift-gce-devel.ci_analysis_us.ci_operator_metrics` (
  -- Metadata and identification
  schema_level INT64 OPTIONS(description="Schema version for this table, used to track data structure changes over time"),
  created TIMESTAMP OPTIONS(description="Timestamp when the ci-operator-metrics.json file was created in GCS"),
  prowjob_build_id STRING OPTIONS(description="Unique numeric identifier for the prowjob build, used to correlate data across tables"),
  prowjob_job_name STRING OPTIONS(description="Name of the prowjob that generated these metrics (e.g., pull-ci-openshift-origin-main-e2e-gcp-ovn)"),
  prowjob_url STRING OPTIONS(description="URL to view the prowjob results and artifacts in the CI dashboard"),
  path STRING OPTIONS(description="Full GCS path to the ci-operator-metrics.json source file"),
  
  -- Events (step-level execution events)
  events ARRAY<STRUCT<
    level STRING OPTIONS(description="Log level of the event (e.g., Info, Warning, Error)"),
    source STRING OPTIONS(description="Source component that generated the event (e.g., steps.inputImageTagStep)"),
    locator STRUCT<
      type STRING OPTIONS(description="Type of locator (e.g., Step)"),
      name STRING OPTIONS(description="Name of the step or component"),
      keys JSON OPTIONS(description="Additional keys including objects array and stepName")
    > OPTIONS(description="Location/context information for the event"),
    message STRUCT<
      reason STRING OPTIONS(description="Reason for the event (e.g., Finished, Failed)"),
      cause STRING OPTIONS(description="Cause or error message if applicable"),
      humanMessage STRING OPTIONS(description="Human-readable message describing the event"),
      annotations JSON OPTIONS(description="Additional annotations including duration_seconds and success flag")
    > OPTIONS(description="Event message with reason, cause, and annotations"),
    `from` TIMESTAMP OPTIONS(description="Event start timestamp"),
    `to` TIMESTAMP OPTIONS(description="Event end timestamp"),
    timestamp TIMESTAMP OPTIONS(description="When this event was recorded")
  >> OPTIONS(description="Step-level execution events tracking the progress and results of individual CI steps"),
  
  -- Build Metrics (openshift_builds)
  openshift_builds ARRAY<STRUCT<
    namespace STRING,
    name STRING,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    duration_seconds FLOAT64,
    status STRING,
    reason STRING,
    output_image STRING,
    for_image STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Image Stream Metrics (images)
  -- Contains both ImageStreamEvent and TagImportEvent
  images ARRAY<STRUCT<
    namespace STRING,
    image_stream_name STRING,
    full_name STRING,
    -- Fields for ImageStreamEvent
    success BOOL,
    error STRING,
    image_stream_details JSON,
    -- Fields for TagImportEvent  
    tag_name STRING,
    full_tag_name STRING,
    source_image STRING,
    source_image_kind STRING,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    duration_seconds FLOAT64,
    retry_count INT64,
    -- Common fields
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Test Platform Insights - Raw storage for all insights
  -- This stores all insight events as-is, providing flexibility for:
  -- 1. Future insights not yet modeled in tpi_ fields
  -- 2. Additional context fields in existing insights
  -- 3. Complete raw data preservation
  test_platform_insights ARRAY<STRUCT<
    name STRING OPTIONS(description="Name/type of the insight event (e.g., started, configuration, execution_completed)"),
    additional_context JSON OPTIONS(description="Full context data for this insight event as JSON, preserving all fields even if not promoted to tpi_ fields"),
    timestamp TIMESTAMP OPTIONS(description="When this insight event occurred")
  >> OPTIONS(description="Complete raw storage of all test platform insight events, useful for querying new insight types or additional context not yet modeled"),
  
  -- Test Platform Insights (promoted from test_platform_insights to top-level fields)
  -- Each insight type becomes a top-level struct for direct querying
  -- Note: This data may duplicate what's in test_platform_insights array above
  
  -- Insight: started
  tpi_started STRUCT<
    job_spec STRUCT<
      branch STRING OPTIONS(description="Git branch being tested"),
      buildid STRING OPTIONS(description="Build identifier matching prowjob_build_id"),
      job STRING OPTIONS(description="Prowjob name"),
      org STRING OPTIONS(description="GitHub organization"),
      prowjobid STRING OPTIONS(description="Prowjob UUID"),
      pulls ARRAY<STRUCT<
        author STRING OPTIONS(description="PR author username"),
        number INT64 OPTIONS(description="Pull request number"),
        sha STRING OPTIONS(description="Git commit SHA for this PR")
      >> OPTIONS(description="List of pull requests being tested (for presubmits)"),
      repo STRING OPTIONS(description="GitHub repository name"),
      target STRING OPTIONS(description="Target test to run"),
      type STRING OPTIONS(description="Prowjob type: presubmit, postsubmit, periodic, or batch")
    > OPTIONS(description="Job specification containing details about what is being tested"),
    timestamp TIMESTAMP OPTIONS(description="When the prowjob execution started")
  > OPTIONS(description="Prowjob start event with full job specification"),
  
  -- Insight: namespace_initialized
  tpi_namespace_initialized STRUCT<
    duration_seconds FLOAT64,
    namespace STRING,
    timestamp TIMESTAMP
  >,
  
  -- Insight: namespace_created
  tpi_namespace_created STRUCT<
    namespace STRING,
    timestamp TIMESTAMP
  >,
  
  -- Insight: configuration
  tpi_configuration STRUCT<
    base_namespace STRING,
    branch STRING,
    cluster_info STRUCT<
      cluster_id STRING,
      cluster_profiles ARRAY<JSON>,
      console_host STRING,
      node_name STRING
    >,
    org STRING,
    promote BOOL,
    repo STRING,
    targets ARRAY<STRING>,
    variant STRING,
    timestamp TIMESTAMP
  >,
  
  -- Insight: lease_credentials (single occurrence, only if leases are used)
  tpi_lease_credentials STRUCT<
    lease_server STRING OPTIONS(description="URL of the lease server (e.g., Boskos)"),
    username STRING OPTIONS(description="Username used to acquire leases"),
    timestamp TIMESTAMP OPTIONS(description="When lease credentials were obtained")
  > OPTIONS(description="Lease credentials obtained from lease server, recorded once if leases are used"),
  
  -- Insight: execution_started
  tpi_execution_started STRUCT<
    started_after FLOAT64,
    timestamp TIMESTAMP
  >,
  
  -- Insight: execution_completed
  tpi_execution_completed STRUCT<
    duration_seconds FLOAT64 OPTIONS(description="Total execution duration in seconds from start to completion"),
    success BOOL OPTIONS(description="Whether the prowjob execution completed successfully (true) or failed (false)"),
    error_count INT64 OPTIONS(description="Number of errors that occurred during execution (only present when success=false)"),
    timestamp TIMESTAMP OPTIONS(description="When the prowjob execution completed")
  > OPTIONS(description="Prowjob completion event with duration, success status, and optional error count"),
  
  -- Insight: namespace_artifacts (single occurrence)
  tpi_namespace_artifacts STRUCT<
    namespace STRING OPTIONS(description="Namespace from which artifacts were collected"),
    timestamp TIMESTAMP OPTIONS(description="When namespace artifacts were collected")
  > OPTIONS(description="Namespace artifacts collection event, recorded once during cleanup"),
  
  -- Insight: lease_released (single occurrence, only if leases were used)
  tpi_lease_released STRUCT<
    released_count INT64 OPTIONS(description="Number of leases that were released"),
    timestamp TIMESTAMP OPTIONS(description="When leases were released")
  > OPTIONS(description="Lease release event, recorded once if leases needed to be released"),
  
  -- Insight: step_started (multiple occurrences, one per step)
  tpi_step_started ARRAY<STRUCT<
    step_name STRING OPTIONS(description="Name of the step that started"),
    description STRING OPTIONS(description="Human-readable description of the step"),
    timestamp TIMESTAMP OPTIONS(description="When the step started")
  >> OPTIONS(description="Step start events, one per step executed in the CI run"),
  
  -- Insight: step_completed (multiple occurrences, one per step)
  tpi_step_completed ARRAY<STRUCT<
    step_name STRING OPTIONS(description="Name of the step that completed"),
    description STRING OPTIONS(description="Human-readable description of the step"),
    duration_seconds FLOAT64 OPTIONS(description="Step execution duration in seconds"),
    success BOOL OPTIONS(description="Whether the step completed successfully"),
    timestamp TIMESTAMP OPTIONS(description="When the step completed")
  >> OPTIONS(description="Step completion events, one per step executed in the CI run"),
  
  -- Lease Metrics (leases)
  leases ARRAY<STRUCT<
    name STRING,
    slice STRING,
    region STRING,
    raw_lease_name STRING,
    acquisition_duration_seconds FLOAT64,
    leases_remaining_at_acquisition INT64,
    leases_total INT64,
    timestamp TIMESTAMP
  >>,
  
  -- Node Metrics (nodes)
  nodes ARRAY<STRUCT<
    node STRING,
    arch STRING,
    machine_type STRING,
    machine_id STRING,
    age_seconds INT64 OPTIONS(description="Node age in seconds (current time minus node creation timestamp)"),
    ci_workload STRING OPTIONS(description="CI workload type from labels['ci-workload'] (e.g., 'builds', 'tests')"),
    resources STRUCT<
      capacity STRUCT<
        cpu STRING,
        memory STRING,
        ephemeral_storage STRING,
        pods STRING
      >,
      allocatable STRUCT<
        cpu STRING,
        memory STRING,
        ephemeral_storage STRING,
        pods STRING
      >
    >,
    usage_stats STRUCT<
      min_cpu_milli INT64,
      max_cpu_milli INT64,
      avg_cpu_milli INT64,
      min_memory_bytes INT64,
      max_memory_bytes INT64,
      avg_memory_bytes INT64
    >,
    labels JSON,
    timestamp TIMESTAMP,
    poll_started TIMESTAMP,
    workloads ARRAY<STRING>,
    watch_history ARRAY<STRUCT<
      start_time TIMESTAMP,
      end_time TIMESTAMP
    >>
  >>,
  
  -- Pod Lifecycle Metrics (pods)
  pods ARRAY<STRUCT<
    pod_name STRING OPTIONS(description="Name of the Kubernetes pod"),
    namespace STRING OPTIONS(description="Kubernetes namespace where the pod ran"),
    creation_time TIMESTAMP OPTIONS(description="When the pod was created"),
    start_time TIMESTAMP OPTIONS(description="When the pod started running"),
    completion_time TIMESTAMP OPTIONS(description="When the pod completed execution"),
    condition_transition_times JSON OPTIONS(description="Timestamps for pod condition transitions (ContainersReady, Initialized, PodScheduled, Ready)"),
    scheduling_latency INT64 OPTIONS(description="Time from creation to scheduling in nanoseconds"),
    initialization_latency INT64 OPTIONS(description="Time from scheduling to initialization in nanoseconds"),
    ready_latency INT64 OPTIONS(description="Time from creation to ready state in nanoseconds"),
    completion_latency INT64 OPTIONS(description="Time from creation to completion in nanoseconds"),
    pod_phase STRING OPTIONS(description="Final pod phase: Pending, Running, Succeeded, Failed, or Unknown"),
    init_container_restarts INT64 OPTIONS(description="Total number of init container restarts"),
    init_container_last_error STRING OPTIONS(description="Last error from init containers if any"),
    timestamp TIMESTAMP OPTIONS(description="When this metric event was recorded")
  >> OPTIONS(description="Pod lifecycle events and performance metrics tracking pod scheduling, initialization, and execution")
)
PARTITION BY DATE(created)
CLUSTER BY prowjob_job_name, prowjob_build_id
OPTIONS(
  description="CI Operator metrics data from ci-operator-metrics.json files, including step-level events, build, image, pod, node, lease, and platform insights",
  require_partition_filter=false
);

-- Create indexes for common query patterns
-- Note: BigQuery doesn't have traditional indexes, but clustering serves a similar purpose
-- The table is clustered by prowjob_job_name and prowjob_build_id for optimal performance on job-based queries

-- Example queries:

-- Query 1: Get all metrics for a specific prowjob
-- SELECT * FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE prowjob_build_id = 'your_build_id';

-- Query 2: Get pod metrics with high latency
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   pod
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(pods) AS pod
-- WHERE pod.ready_latency > 300000000000; -- 5 minutes in nanoseconds

-- Query 3: Get image import durations
-- SELECT 
--   prowjob_build_id,
--   image.full_tag_name,
--   image.duration_seconds
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(images) AS image
-- WHERE image.duration_seconds IS NOT NULL
-- ORDER BY image.duration_seconds DESC;

-- Query 4: Analyze node resource usage
-- SELECT 
--   prowjob_build_id,
--   node.node,
--   node.machine_type,
--   node.usage_stats.avg_cpu_milli,
--   node.usage_stats.avg_memory_bytes
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(nodes) AS node
-- WHERE created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);

-- Query 5: Lease acquisition time analysis
-- SELECT 
--   prowjob_job_name,
--   AVG(lease.acquisition_duration_seconds) as avg_acquisition_time,
--   COUNT(*) as total_leases
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(leases) AS lease
-- WHERE created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY prowjob_job_name
-- ORDER BY avg_acquisition_time DESC;

-- Query 6: Query test platform insights - cluster info
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   tpi_configuration.cluster_info.cluster_id,
--   tpi_configuration.cluster_info.console_host,
--   tpi_started.job_spec.job,
--   tpi_started.job_spec.org,
--   tpi_started.job_spec.repo,
--   tpi_execution_completed.duration_seconds,
--   tpi_execution_completed.success
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_configuration.cluster_info.cluster_id IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);

-- Query 7: Analyze execution durations by job type
-- SELECT 
--   tpi_started.job_spec.type as job_type,
--   tpi_started.job_spec.job as job_name,
--   AVG(tpi_execution_completed.duration_seconds) as avg_duration,
--   COUNT(*) as total_runs,
--   COUNTIF(tpi_execution_completed.success = true) as successful_runs,
--   COUNTIF(tpi_execution_completed.success = false) as failed_runs
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_execution_completed.duration_seconds IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY job_type, job_name
-- ORDER BY avg_duration DESC;

-- Query 8: Find PR jobs with their authors
-- SELECT 
--   prowjob_build_id,
--   tpi_started.job_spec.job,
--   tpi_started.job_spec.org,
--   tpi_started.job_spec.repo,
--   pull.number as pr_number,
--   pull.author as pr_author,
--   pull.sha as pr_sha
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(tpi_started.job_spec.pulls) AS pull
-- WHERE tpi_started.job_spec.type = 'presubmit'
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);

-- Query 9: Namespace initialization performance
-- SELECT 
--   prowjob_job_name,
--   AVG(tpi_namespace_initialized.duration_seconds) as avg_ns_init_duration,
--   AVG(tpi_execution_started.started_after) as avg_started_after,
--   COUNT(*) as total_jobs
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_namespace_initialized.duration_seconds IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY prowjob_job_name
-- ORDER BY avg_ns_init_duration DESC
-- LIMIT 100;

-- Query 10: Lease server usage analysis
-- SELECT 
--   tpi_lease_credentials.lease_server,
--   tpi_lease_credentials.username,
--   COUNT(*) as usage_count
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_lease_credentials.lease_server IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
-- GROUP BY lease_server, username
-- ORDER BY usage_count DESC;

-- Query 11: Namespace artifacts collection timing
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   tpi_namespace_artifacts.namespace,
--   tpi_namespace_artifacts.timestamp
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_namespace_artifacts.namespace IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
-- ORDER BY tpi_namespace_artifacts.timestamp DESC;

-- Query 12: Lease lifecycle tracking
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   tpi_lease_credentials.timestamp as lease_acquired,
--   tpi_lease_released.timestamp as lease_released,
--   tpi_lease_released.released_count,
--   TIMESTAMP_DIFF(tpi_lease_released.timestamp, tpi_lease_credentials.timestamp, SECOND) as lease_duration_seconds
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_lease_credentials IS NOT NULL
-- AND tpi_lease_released IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);

-- Query 13: Query raw test_platform_insights for new/unmapped insights
-- This is useful when new insight types are added that aren't yet in tpi_ fields
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   insight.name as insight_name,
--   insight.additional_context,
--   insight.timestamp
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(test_platform_insights) AS insight
-- WHERE insight.name NOT IN ('started', 'namespace_initialized', 'namespace_created', 
--                             'configuration', 'execution_started', 'execution_completed')
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY);

-- Query 14: Analyze insight event timeline for a specific job
-- Shows all insights in chronological order
-- SELECT 
--   prowjob_build_id,
--   insight.name as insight_name,
--   insight.timestamp,
--   insight.additional_context
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(test_platform_insights) AS insight
-- WHERE prowjob_build_id = 'your_build_id'
-- ORDER BY insight.timestamp;

-- Query 15: Analyze step execution events
-- Shows step-level events with durations
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   event.locator.name as step_name,
--   event.source,
--   event.message.reason,
--   event.message.humanMessage,
--   JSON_EXTRACT_SCALAR(event.message.annotations, '$.duration_seconds') as duration_seconds,
--   JSON_EXTRACT_SCALAR(event.message.annotations, '$.success') as success,
--   event.`from` as step_start,
--   event.`to` as step_end,
--   TIMESTAMP_DIFF(event.`to`, event.`from`, SECOND) as step_duration_seconds
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(events) AS event
-- WHERE created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
-- ORDER BY event.`from`;

-- Query 16: Find failed steps
-- SELECT 
--   prowjob_build_id,
--   prowjob_job_name,
--   prowjob_url,
--   event.locator.name as step_name,
--   event.message.reason,
--   event.message.cause,
--   event.message.humanMessage,
--   TIMESTAMP_DIFF(event.to, event.from, SECOND) as step_duration_seconds
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(events) AS event
-- WHERE JSON_EXTRACT_SCALAR(event.message.annotations, '$.success') = 'false'
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- ORDER BY event.timestamp DESC;

-- Query 17: Analyze step performance by source
-- SELECT 
--   event.source,
--   COUNT(*) as total_steps,
--   AVG(CAST(JSON_EXTRACT_SCALAR(event.message.annotations, '$.duration_seconds') AS FLOAT64)) as avg_duration,
--   MAX(CAST(JSON_EXTRACT_SCALAR(event.message.annotations, '$.duration_seconds') AS FLOAT64)) as max_duration,
--   COUNTIF(JSON_EXTRACT_SCALAR(event.message.annotations, '$.success') = 'true') as successful_steps,
--   COUNTIF(JSON_EXTRACT_SCALAR(event.message.annotations, '$.success') = 'false') as failed_steps
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(events) AS event
-- WHERE created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY event.source
-- ORDER BY avg_duration DESC;

-- Query 18: Analyze step completion from insights
-- Using the promoted tpi_step_completed field for direct querying
-- SELECT 
--   prowjob_job_name,
--   step.step_name,
--   AVG(step.duration_seconds) as avg_duration,
--   COUNT(*) as total_runs,
--   COUNTIF(step.success = true) as successful_runs,
--   COUNTIF(step.success = false) as failed_runs
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(tpi_step_completed) AS step
-- WHERE created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY prowjob_job_name, step.step_name
-- ORDER BY avg_duration DESC
-- LIMIT 100;

-- Query 19: Track error rates and counts
-- SELECT 
--   prowjob_job_name,
--   COUNT(*) as total_runs,
--   COUNTIF(tpi_execution_completed.success = true) as successful_runs,
--   COUNTIF(tpi_execution_completed.success = false) as failed_runs,
--   AVG(tpi_execution_completed.error_count) as avg_errors_when_failed,
--   AVG(tpi_execution_completed.duration_seconds) as avg_duration
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`
-- WHERE tpi_execution_completed IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY prowjob_job_name
-- ORDER BY failed_runs DESC;

-- Query 20: Analyze node workload types
-- Using the promoted ci_workload field for direct querying
-- SELECT 
--   node.ci_workload,
--   node.machine_type,
--   COUNT(*) as usage_count,
--   AVG(node.usage_stats.avg_cpu_milli) as avg_cpu,
--   AVG(node.usage_stats.avg_memory_bytes) / (1024*1024*1024) as avg_memory_gb
-- FROM `openshift-gce-devel.ci_analysis_us.ci_operator_metrics`,
-- UNNEST(nodes) AS node
-- WHERE node.ci_workload IS NOT NULL
-- AND created > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
-- GROUP BY node.ci_workload, node.machine_type
-- ORDER BY usage_count DESC;

-- =============================================================================
-- Create/Replace table in ci_analysis_qe dataset
-- =============================================================================
CREATE OR REPLACE TABLE `openshift-gce-devel.ci_analysis_qe.ci_operator_metrics` (
  -- Metadata and identification
  schema_level INT64 OPTIONS(description="Schema version for this table, used to track data structure changes over time"),
  created TIMESTAMP OPTIONS(description="Timestamp when the ci-operator-metrics.json file was created in GCS"),
  prowjob_build_id STRING OPTIONS(description="Unique numeric identifier for the prowjob build, used to correlate data across tables"),
  prowjob_job_name STRING OPTIONS(description="Name of the prowjob that generated these metrics (e.g., pull-ci-openshift-origin-main-e2e-gcp-ovn)"),
  prowjob_url STRING OPTIONS(description="URL to view the prowjob results and artifacts in the CI dashboard"),
  path STRING OPTIONS(description="Full GCS path to the ci-operator-metrics.json source file"),
  
  -- Events (step-level execution events)
  events ARRAY<STRUCT<
    level STRING OPTIONS(description="Log level of the event (e.g., Info, Warning, Error)"),
    source STRING OPTIONS(description="Source component that generated the event (e.g., steps.inputImageTagStep)"),
    locator STRUCT<
      type STRING OPTIONS(description="Type of locator (e.g., Step)"),
      name STRING OPTIONS(description="Name of the step or component"),
      keys JSON OPTIONS(description="Additional keys including objects array and stepName")
    > OPTIONS(description="Location/context information for the event"),
    message STRUCT<
      reason STRING OPTIONS(description="Reason for the event (e.g., Finished, Failed)"),
      cause STRING OPTIONS(description="Cause or error message if applicable"),
      humanMessage STRING OPTIONS(description="Human-readable message describing the event"),
      annotations JSON OPTIONS(description="Additional annotations including duration_seconds and success flag")
    > OPTIONS(description="Event message with reason, cause, and annotations"),
    `from` TIMESTAMP OPTIONS(description="Event start timestamp"),
    `to` TIMESTAMP OPTIONS(description="Event end timestamp"),
    timestamp TIMESTAMP OPTIONS(description="When this event was recorded")
  >> OPTIONS(description="Step-level execution events tracking the progress and results of individual CI steps"),
  
  -- Test Platform Insights - Raw storage for all insights
  test_platform_insights ARRAY<STRUCT<
    name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Test Platform Insights (promoted fields)
  tpi_started STRUCT<
    job_spec STRUCT<
      branch STRING,
      buildid STRING,
      job STRING,
      org STRING,
      prowjobid STRING,
      pulls ARRAY<STRUCT<
        author STRING,
        number INT64,
        sha STRING
      >>,
      repo STRING,
      target STRING,
      type STRING
    >,
    timestamp TIMESTAMP
  >,
  tpi_namespace_initialized STRUCT<
    duration_seconds FLOAT64,
    namespace STRING,
    timestamp TIMESTAMP
  >,
  tpi_namespace_created STRUCT<
    namespace STRING,
    timestamp TIMESTAMP
  >,
  tpi_configuration STRUCT<
    base_namespace STRING,
    branch STRING,
    cluster_info STRUCT<
      cluster_id STRING,
      cluster_profiles ARRAY<JSON>,
      console_host STRING,
      node_name STRING
    >,
    org STRING,
    promote BOOL,
    repo STRING,
    targets ARRAY<STRING>,
    variant STRING,
    timestamp TIMESTAMP
  >,
  tpi_lease_credentials ARRAY<STRUCT<
    lease_server STRING,
    username STRING,
    timestamp TIMESTAMP
  >>,
  tpi_execution_started STRUCT<
    started_after FLOAT64,
    timestamp TIMESTAMP
  >,
  tpi_execution_completed STRUCT<
    duration_seconds FLOAT64,
    success BOOL,
    timestamp TIMESTAMP
  >,
  tpi_namespace_artifacts ARRAY<STRUCT<
    namespace STRING,
    timestamp TIMESTAMP
  >>,
  tpi_lease_released ARRAY<STRUCT<
    timestamp TIMESTAMP
  >>,
  tpi_step_started ARRAY<STRUCT<
    step_name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  tpi_step_completed ARRAY<STRUCT<
    step_name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  tpi_secret_created ARRAY<STRUCT<
    secret_name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Build Metrics
  openshift_builds ARRAY<STRUCT<
    namespace STRING,
    name STRING,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    duration_seconds FLOAT64,
    status STRING,
    reason STRING,
    output_image STRING,
    for_image STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Image Stream Metrics
  images ARRAY<STRUCT<
    namespace STRING,
    image_stream_name STRING,
    full_name STRING,
    success BOOL,
    error STRING,
    image_stream_details JSON,
    tag_name STRING,
    full_tag_name STRING,
    source_image STRING,
    source_image_kind STRING,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    duration_seconds FLOAT64,
    retry_count INT64,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Lease Metrics
  leases ARRAY<STRUCT<
    name STRING,
    slice STRING,
    region STRING,
    raw_lease_name STRING,
    acquisition_duration_seconds FLOAT64,
    leases_remaining_at_acquisition INT64,
    leases_total INT64,
    timestamp TIMESTAMP
  >>,
  
  -- Node Metrics
  nodes ARRAY<STRUCT<
    node STRING,
    arch STRING,
    machine_type STRING,
    machine_id STRING,
    age_seconds INT64 OPTIONS(description="Node age in seconds (current time minus node creation timestamp)"),
    ci_workload STRING OPTIONS(description="CI workload type from labels['ci-workload'] (e.g., 'builds', 'tests')"),
    resources STRUCT<
      capacity STRUCT<
        cpu STRING,
        memory STRING,
        ephemeral_storage STRING,
        pods STRING
      >,
      allocatable STRUCT<
        cpu STRING,
        memory STRING,
        ephemeral_storage STRING,
        pods STRING
      >
    >,
    usage_stats STRUCT<
      min_cpu_milli INT64,
      max_cpu_milli INT64,
      avg_cpu_milli INT64,
      min_memory_bytes INT64,
      max_memory_bytes INT64,
      avg_memory_bytes INT64
    >,
    labels JSON,
    timestamp TIMESTAMP,
    poll_started TIMESTAMP,
    workloads ARRAY<STRING>,
    watch_history ARRAY<STRUCT<
      start_time TIMESTAMP,
      end_time TIMESTAMP
    >>
  >>,
  
  -- Pod Lifecycle Metrics
  pods ARRAY<STRUCT<
    pod_name STRING,
    namespace STRING,
    creation_time TIMESTAMP,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    condition_transition_times JSON,
    scheduling_latency INT64,
    initialization_latency INT64,
    ready_latency INT64,
    completion_latency INT64,
    pod_phase STRING,
    init_container_restarts INT64,
    init_container_last_error STRING,
    timestamp TIMESTAMP
  >>
)
PARTITION BY DATE(created)
CLUSTER BY prowjob_job_name, prowjob_build_id
OPTIONS(
  description="CI Operator metrics data from ci-operator-metrics.json files (QE dataset)",
  require_partition_filter=false
);

-- =============================================================================
-- Create/Replace table in ci_analysis_private dataset
-- =============================================================================
CREATE OR REPLACE TABLE `openshift-gce-devel.ci_analysis_private.ci_operator_metrics` (
  -- Metadata and identification
  schema_level INT64 OPTIONS(description="Schema version for this table, used to track data structure changes over time"),
  created TIMESTAMP OPTIONS(description="Timestamp when the ci-operator-metrics.json file was created in GCS"),
  prowjob_build_id STRING OPTIONS(description="Unique numeric identifier for the prowjob build, used to correlate data across tables"),
  prowjob_job_name STRING OPTIONS(description="Name of the prowjob that generated these metrics (e.g., pull-ci-openshift-origin-main-e2e-gcp-ovn)"),
  prowjob_url STRING OPTIONS(description="URL to view the prowjob results and artifacts in the CI dashboard"),
  path STRING OPTIONS(description="Full GCS path to the ci-operator-metrics.json source file"),
  
  -- Events (step-level execution events)
  events ARRAY<STRUCT<
    level STRING OPTIONS(description="Log level of the event (e.g., Info, Warning, Error)"),
    source STRING OPTIONS(description="Source component that generated the event (e.g., steps.inputImageTagStep)"),
    locator STRUCT<
      type STRING OPTIONS(description="Type of locator (e.g., Step)"),
      name STRING OPTIONS(description="Name of the step or component"),
      keys JSON OPTIONS(description="Additional keys including objects array and stepName")
    > OPTIONS(description="Location/context information for the event"),
    message STRUCT<
      reason STRING OPTIONS(description="Reason for the event (e.g., Finished, Failed)"),
      cause STRING OPTIONS(description="Cause or error message if applicable"),
      humanMessage STRING OPTIONS(description="Human-readable message describing the event"),
      annotations JSON OPTIONS(description="Additional annotations including duration_seconds and success flag")
    > OPTIONS(description="Event message with reason, cause, and annotations"),
    `from` TIMESTAMP OPTIONS(description="Event start timestamp"),
    `to` TIMESTAMP OPTIONS(description="Event end timestamp"),
    timestamp TIMESTAMP OPTIONS(description="When this event was recorded")
  >> OPTIONS(description="Step-level execution events tracking the progress and results of individual CI steps"),
  
  -- Test Platform Insights - Raw storage for all insights
  test_platform_insights ARRAY<STRUCT<
    name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Test Platform Insights (promoted fields)
  tpi_started STRUCT<
    job_spec STRUCT<
      branch STRING,
      buildid STRING,
      job STRING,
      org STRING,
      prowjobid STRING,
      pulls ARRAY<STRUCT<
        author STRING,
        number INT64,
        sha STRING
      >>,
      repo STRING,
      target STRING,
      type STRING
    >,
    timestamp TIMESTAMP
  >,
  tpi_namespace_initialized STRUCT<
    duration_seconds FLOAT64,
    namespace STRING,
    timestamp TIMESTAMP
  >,
  tpi_namespace_created STRUCT<
    namespace STRING,
    timestamp TIMESTAMP
  >,
  tpi_configuration STRUCT<
    base_namespace STRING,
    branch STRING,
    cluster_info STRUCT<
      cluster_id STRING,
      cluster_profiles ARRAY<JSON>,
      console_host STRING,
      node_name STRING
    >,
    org STRING,
    promote BOOL,
    repo STRING,
    targets ARRAY<STRING>,
    variant STRING,
    timestamp TIMESTAMP
  >,
  tpi_lease_credentials ARRAY<STRUCT<
    lease_server STRING,
    username STRING,
    timestamp TIMESTAMP
  >>,
  tpi_execution_started STRUCT<
    started_after FLOAT64,
    timestamp TIMESTAMP
  >,
  tpi_execution_completed STRUCT<
    duration_seconds FLOAT64,
    success BOOL,
    timestamp TIMESTAMP
  >,
  tpi_namespace_artifacts ARRAY<STRUCT<
    namespace STRING,
    timestamp TIMESTAMP
  >>,
  tpi_lease_released ARRAY<STRUCT<
    timestamp TIMESTAMP
  >>,
  tpi_step_started ARRAY<STRUCT<
    step_name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  tpi_step_completed ARRAY<STRUCT<
    step_name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  tpi_secret_created ARRAY<STRUCT<
    secret_name STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Build Metrics
  openshift_builds ARRAY<STRUCT<
    namespace STRING,
    name STRING,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    duration_seconds FLOAT64,
    status STRING,
    reason STRING,
    output_image STRING,
    for_image STRING,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Image Stream Metrics
  images ARRAY<STRUCT<
    namespace STRING,
    image_stream_name STRING,
    full_name STRING,
    success BOOL,
    error STRING,
    image_stream_details JSON,
    tag_name STRING,
    full_tag_name STRING,
    source_image STRING,
    source_image_kind STRING,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    duration_seconds FLOAT64,
    retry_count INT64,
    additional_context JSON,
    timestamp TIMESTAMP
  >>,
  
  -- Lease Metrics
  leases ARRAY<STRUCT<
    name STRING,
    slice STRING,
    region STRING,
    raw_lease_name STRING,
    acquisition_duration_seconds FLOAT64,
    leases_remaining_at_acquisition INT64,
    leases_total INT64,
    timestamp TIMESTAMP
  >>,
  
  -- Node Metrics
  nodes ARRAY<STRUCT<
    node STRING,
    arch STRING,
    machine_type STRING,
    machine_id STRING,
    age_seconds INT64 OPTIONS(description="Node age in seconds (current time minus node creation timestamp)"),
    ci_workload STRING OPTIONS(description="CI workload type from labels['ci-workload'] (e.g., 'builds', 'tests')"),
    resources STRUCT<
      capacity STRUCT<
        cpu STRING,
        memory STRING,
        ephemeral_storage STRING,
        pods STRING
      >,
      allocatable STRUCT<
        cpu STRING,
        memory STRING,
        ephemeral_storage STRING,
        pods STRING
      >
    >,
    usage_stats STRUCT<
      min_cpu_milli INT64,
      max_cpu_milli INT64,
      avg_cpu_milli INT64,
      min_memory_bytes INT64,
      max_memory_bytes INT64,
      avg_memory_bytes INT64
    >,
    labels JSON,
    timestamp TIMESTAMP,
    poll_started TIMESTAMP,
    workloads ARRAY<STRING>,
    watch_history ARRAY<STRUCT<
      start_time TIMESTAMP,
      end_time TIMESTAMP
    >>
  >>,
  
  -- Pod Lifecycle Metrics
  pods ARRAY<STRUCT<
    pod_name STRING,
    namespace STRING,
    creation_time TIMESTAMP,
    start_time TIMESTAMP,
    completion_time TIMESTAMP,
    condition_transition_times JSON,
    scheduling_latency INT64,
    initialization_latency INT64,
    ready_latency INT64,
    completion_latency INT64,
    pod_phase STRING,
    init_container_restarts INT64,
    init_container_last_error STRING,
    timestamp TIMESTAMP
  >>
)
PARTITION BY DATE(created)
CLUSTER BY prowjob_job_name, prowjob_build_id
OPTIONS(
  description="CI Operator metrics data from ci-operator-metrics.json files (Private dataset)",
  require_partition_filter=false
);
