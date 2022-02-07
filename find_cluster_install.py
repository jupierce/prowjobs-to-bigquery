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

    # For performance reasons, we union the regex into one big one. Setup sub expressions that should consume
    # up to, but not including, the closing quotation mark of msg.

    # Example: Running multi-stage test e2e-aws-serial
    url_pattern = r"(Using namespace (?P<url>[^\s\"]+))"
    # Example: Running step e2e-aws-serial-ipi-install-hosted-loki.
    combined_pattern = r"{\"level\":\"\w+\",\"msg\":\"(?:" + \
                       url_pattern + \
                       r")\",\"time\":\"(?P<dt>[^\"]+)\"}"

    print(combined_pattern)

    log_entry_pattern = re.compile(combined_pattern)

    def process_connection_setup():
        global process_sc
        global process_oct_bucket
        process_sc = storage.Client()
        process_oct_bucket = process_sc.bucket('origin-ci-test')

    def process_row(filename):
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

            if 'ci-op-ciwjljgy' in line:
                print(f'Found in file: {filename}')
                return filename
            else:
                # This is not the namespace we are looking for
                return None

        return None

    ci_operator_list_table_id = 'openshift-gce-devel-ci.prowjobs.cs_object_ci-operator_logs_2022_on_feb_7_2022'
    query_unique_log_file = f"""
    SELECT cs_object FROM `{ci_operator_list_table_id}`
    GROUP BY cs_object
    """  # GROUP BY prevents duplicate file entries from being counted

    paths = bq.query(query_unique_log_file)  # The whole boat
    #paths = bq.list_rows('openshift-gce-devel-ci.prowjobs.cs_object_ci-operator_logs_2022_on_feb_7_2022', max_results=50)  # limited boat
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
        print(f'Found {val}')
        pool.terminate()
        exit(0)

    print('Not found!')
    exit(1)
