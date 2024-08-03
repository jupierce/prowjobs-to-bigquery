#!/usr/bin/env python3

from datetime import datetime
from collections import defaultdict
from google.cloud import bigquery, storage
import csv
import multiprocessing as mp
import os


class EventCounter:
    def __init__(self):
        self.data = defaultdict(lambda: defaultdict(int))

    def add_event(self, event_type, date=None, by=1):
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        self.data[date][event_type] += by

    def get_event_count(self, event_type, date=None):
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return self.data[date][event_type]

    def get_all_counts(self, date=None):
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        return dict(self.data[date])

    def print_total_counts(self):
        total_counts = defaultdict(int)
        for counts in self.data.values():
            for event_type, count in counts.items():
                total_counts[event_type] += count

        print("Total counts of each event type:")
        for event_type, count in total_counts.items():
            print(f"{event_type}: {count}")

    def to_csv(self, filename):
        # Collect all event types
        event_types = set()
        for counts in self.data.values():
            event_types.update(counts.keys())

        event_types = sorted(event_types)

        with open(filename, 'w', newline='') as csvfile:
            fieldnames = ['date'] + event_types
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

            writer.writeheader()
            for date, counts in sorted(self.data.items()):
                row = {'date': date}
                row.update({event: counts.get(event, 0) for event in event_types})
                writer.writerow(row)

    def __str__(self):
        return str(dict(self.data))


SCAN_START_DATE = '2024-04-01'
SCAN_END_DATE = '2024-08-03'
PROWJOB_URL_BUCKET_PREFIX = 'https://prow.ci.openshift.org/view/gs/test-platform-results/'

# Global variable to hold the storage client in each worker
worker_client = None
processed_files = 0


def init_worker():
    global worker_client
    worker_client = bigquery.Client()


def process_entry(job_record):
    global worker_client, processed_files
    empty = (None, None, None, None,)
    if worker_client is None:
        init_worker()

    created_datetime = job_record['created']
    prowjob_cluster = job_record['prowjob_cluster']
    url = job_record['prowjob_url']
    if not url or not url.startswith(PROWJOB_URL_BUCKET_PREFIX):
        return empty

    bucket_path_to_job_files = url[len(PROWJOB_URL_BUCKET_PREFIX):]
    build_log_txt_path = f'{bucket_path_to_job_files}/build-log.txt'

    blob = bucket_client.get_blob(build_log_txt_path)
    if not blob:
        # File not found
        return empty

    processed_files += 1
    if processed_files % 1000 == 0:
        print(f'Worker {os.getpid()} has processed: {processed_files}')

    # print(f'Processing {build_log_txt_path}')
    try:
        content = blob.download_as_text()
        return url, prowjob_cluster, created_datetime.strftime("%Y-%m-%d"), content.count('HTTP status: 504 Gateway')
    except:
        # Error reading blob as text.
        pass


    return empty


if __name__ == '__main__':
    storage_client = storage.Client(project='openshift-gce-devel')
    bucket_client = storage_client.bucket('test-platform-results')

    bq_client = bigquery.Client(project='openshift-gce-devel')

    query = bq_client.query(query=f'''
SELECT created, prowjob_cluster, prowjob_url FROM `openshift-gce-devel.ci_analysis_us.jobs` WHERE 
prowjob_start BETWEEN DATETIME("{SCAN_START_DATE}") AND DATETIME_ADD("{SCAN_END_DATE}", INTERVAL 1 DAY)
AND (starts_with(prowjob_job_name, 'branch') or ends_with(prowjob_job_name, '-images'))
ORDER BY created ASC    
    ''')
    job_records = query.result()

    ec = EventCounter()

    manager = mp.Manager()
    queue = mp.Queue(os.cpu_count() * 5)

    with open('processed.txt', mode='w+') as urls:
        with mp.Pool(processes=os.cpu_count() * 5 - 1) as pool:
            results = [pool.apply_async(process_entry, args=(dict(item),)) for item in job_records]
            # Aggregate results from the pool
            for result in results:
                url, prowjob_cluster, day, count = result.get()
                if prowjob_cluster is None:
                    continue
                urls.write(f'{url}\n')
                ec.add_event(prowjob_cluster, date=day, by=count)

    ec.print_total_counts()
    ec.to_csv('output.csv')

