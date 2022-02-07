#!/usr/bin/env python3

import multiprocessing

from google.cloud import bigquery, storage
from io import StringIO
from datetime import datetime
from typing import List
import re

# ci-operator.log files are read by multiple processes
# they need individual connections to the cloud storage client
# https://stackoverflow.com/questions/10117073/how-to-use-initializer-to-set-up-my-multiprocess-pool
process_sc = None
process_oct_bucket = None

if __name__ == '__main__':

    bq = bigquery.Client()

    schema = [
        bigquery.SchemaField("filename", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("datetime", "DATETIME", mode="REQUIRED"),
        bigquery.SchemaField("repo", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("org", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("base_branch", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("base_commit", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("pr", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("pr_commit", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("pr_owner", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("template", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("has_slices", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("has_multi_stage_tests", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("slices", "STRING", mode="REPEATED"),
        bigquery.SchemaField("leases", "STRING", mode="REPEATED"),
        bigquery.SchemaField("multi_stage_tests", "STRING", mode="REPEATED"),
        bigquery.SchemaField("steps", "STRING", mode="REPEATED"),
        bigquery.SchemaField("pj_labels", "STRING", mode="REPEATED"),
        bigquery.SchemaField("pj_annotations", "STRING", mode="REPEATED"),
    ]

    analysis_table_id = 'openshift-gce-devel-ci.prowjobs.analysis2'
    create_table = False
    if create_table:
        table = bigquery.Table(analysis_table_id, schema=schema)
        table = bq.create_table(table)  # Make an API request
        exit(0)

    # For performance reasons, we union the regex into one big one. Setup sub expressions that should consume
    # up to, but not including, the closing quotation mark of msg.

    # Example: Resolved source https://github.com/openshift/oc to mast-er@7642a3ac, merging: #982 cc4e9b84 @openshift-bot
    resolved_source_pattern = r"(Resolved source (?P<repo>[^\s]+) to (?P<base_branch>[^@]+)@(?P<base_commit>[^,]+), merging: #(?P<pr>\d+) (?P<pr_commit>\w+) (?P<pr_owner>@[^\s\"]+))"
    # Example: Acquired 1 lease(s) for aws-quota-slice: [us-east-1--aws-quota-slice-28]
    acquired_leases_pattern = r"(Acquired (?P<lease_count>\d+) lease.s. for (?P<slice_name>[^:]+): (?P<lease_names>[^\s\"]+))"
    # Example: Running multi-stage test e2e-aws-serial
    multi_stage_pattern = r"(Running multi-stage test (?P<multi_stage_name>[^\s\"]+))"
    # Example: Running step e2e-aws-serial-ipi-install-hosted-loki.
    step_pattern = r"(Running step (?P<step_name>[^\s]+)\.)"
    # Example: Executing template e2e-aws
    template_pattern = r"(Executing template (?P<template>[^\s\"]+))"

    combined_pattern = r"{\"level\":\"\w+\",\"msg\":\"(?:" + \
                       resolved_source_pattern + '|' + \
                       acquired_leases_pattern + '|' + \
                       multi_stage_pattern + '|' + \
                       template_pattern + '|' + \
                       step_pattern + \
                       r")\",\"time\":\"(?P<dt>[^\"]+)\"}"

    print(combined_pattern)

    log_entry_pattern = re.compile(combined_pattern)

    def send_inserts(l: List):
        if not l:  # No items to insert?
            return 0
        count = len(l)
        errors = bq.insert_rows_json(analysis_table_id, inserts)
        if errors == []:
            print("New rows have been added.")
        else:
            raise IOError("Encountered errors while inserting rows: {}".format(errors))
        l.clear()
        return count

    def process_connection_setup():
        global process_sc
        global process_oct_bucket
        process_sc = storage.Client()
        process_oct_bucket = process_sc.bucket('origin-ci-test')

    def process_row(filename):
        pj_labels = []
        pj_annotations = []
        to_insert = {
            "filename": filename,
            "datetime": None,
            "repo": None,
            "org": None,
            "base_branch": None,
            "base_commit": None,
            "pr": None,
            "pr_commit": None,
            "pr_owner": None,
            "template": None,
            "slices": [],
            "has_slices": "no",
            "leases": [],
            "multi_stage_tests": [],
            "has_multi_stage_tests": "no",            
            "steps": [],
            "pj_labels": pj_labels,
            "pj_annotations": pj_annotations,
        }
        b = process_oct_bucket.get_blob(filename)
        
        if not b:
            # File has been removed
            return None

        time_set = False
        ci_operator_log_content = b.download_as_text()
        for line in StringIO(ci_operator_log_content):
            m = log_entry_pattern.match(line)
            if not m:
                # Not a log entry we are interested in
                continue

            dt = m.group('dt') # datetime

            if not time_set:
                #to_insert['datetime'] = datetime.strptime(dt, '%Y-%m-%dT%H:%M:%SZ')
                to_insert['datetime'] = dt.rstrip('Z')
                time_set = True

            if m.group('repo'):  # Matched a "Resolve source "
                to_insert['repo'] = m.group('repo')
                to_insert['org'] = m.group('repo').split('/')[3]
                to_insert['base_branch'] = m.group('base_branch')
                to_insert['base_commit'] = m.group('base_commit')
                to_insert['pr'] = m.group('pr')
                to_insert['pr_commit'] = m.group('pr_commit')
                to_insert['pr_owner'] = m.group('pr_owner')

            if m.group('lease_count'):
                count = int(m.group('lease_count'))
                slice_name = m.group('slice_name')
                lease_names = m.group('lease_names')
                to_insert['slices'].extend([slice_name] * count)
                to_insert['leases'].append(lease_names)

            if m.group('multi_stage_name'):
                to_insert['multi_stage_tests'].append(m.group('multi_stage_name'))

            if m.group('step_name'):
                to_insert['steps'].append(m.group('step_name'))

            if m.group('template'):
                to_insert['template'] = m.group('template')
                to_insert['steps'].append(m.group('template') + ":ipi-install-rbac")  # To make template jobs register in most queries, emulate a step
                # After executing a template, there are bunch of logs I don't want to spend time
                # processing. Should be no useful data as far as our table goes. So break the loop.
                break

        if not time_set:
            # No line in our file matched the pattern, ignore it.
            return None

        to_insert['has_multi_stage_tests'] = 'yes' if to_insert['multi_stage_tests'] else 'no'
        to_insert['has_slices'] = 'yes' if to_insert['slices'] else 'no'

        pj_json_path = filename.rsplit('/', 2)[0] + '/prowjob.json'
        pj_json_blob = process_oct_bucket.get_blob(pj_json_path)
        if pj_json_blob:
            # Found the prowjob.json file we expect relative to the ci-operator.log file 
            pj_json_log_content = pj_json_blob.download_as_text()
            in_labels = False
            found_labels = False
            in_annotations = False
            found_annotations = False
            for line in StringIO(pj_json_log_content):
                if found_annotations and found_labels:
                    # We have what we've come for, skip the rest for speed
                    break
                
                line = line.strip()
                
                if line.startswith('"labels": {'):
                    in_labels = True
                    continue
                
                if line.startswith('"annotations": {'):
                    in_annotations = True
                    continue
                
                if in_labels:
                    if line.startswith('}'):
                        found_labels = True
                        in_labels = False
                    else:
                        pj_labels.append(line)
                    continue
                
                if in_annotations:
                    if line.startswith('}'):
                        found_annotations = True
                        in_annotations = False
                    else:
                        pj_annotations.append(line)
                    continue

        return to_insert

    ci_operator_list_table_id = 'openshift-gce-devel-ci.prowjobs.cs_object_ci-operator_logs'
    query_unique_log_file = f"""
    SELECT cs_object FROM `{ci_operator_list_table_id}`
    GROUP BY cs_object
    """  # GROUP BY prevents duplicate file entries from being counted

    paths = bq.query(query_unique_log_file)  # The whole boat
    #paths = bq.list_rows('openshift-gce-devel-ci.prowjobs.cs_object_ci-operator_logs', max_results=50)  # limited boat
    #paths = [{'cs_object': 'pr-logs/pull/openshift_oc/982/pull-ci-openshift-oc-master-e2e-aws-serial/1463900310752727040/artifacts/ci-operator.log' }]
    #paths = [{'cs_object': 'logs/release-openshift-origin-installer-e2e-aws-shared-vpc-4.3/1467320397148983296/artifacts/ci-operator.log' }] # Template example

    inserts = []
    total_inserts = 0

    # Pool can serialize a row for the other processes, so just extract the filanem
    def cs_object_generator(row_generator):
        for r in row_generator:
            yield r['cs_object']

    # Have multiple processes per processor since read from GCS will take a few I/O beats
    pool = multiprocessing.Pool(multiprocessing.cpu_count()*12, process_connection_setup)  
    vals = pool.imap_unordered(process_row, cs_object_generator(paths), chunksize=1000)
    for val in vals:
        if not val:
            continue
        inserts.append(val)
        if len(inserts) > 1000:
            total_inserts += send_inserts(inserts)
            print(f'Rows inserted so far: {total_inserts}')

    total_inserts += send_inserts(inserts)
    print(f'Total number of rows inserted: {total_inserts}')

