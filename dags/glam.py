"""
Desktop ETL for https://glam.telemetry.mozilla.org/

Generates and runs a series of BQ queries, see
[bigquery_etl/glam](https://github.com/mozilla/bigquery-etl/tree/main/bigquery_etl/glam)
in bigquery-etl and the
[glam_subdags](https://github.com/mozilla/telemetry-airflow/tree/main/dags/glam_subdags)
in telemetry-airflow.
"""

from datetime import datetime, timedelta

from airflow import DAG
from operators.gcp_container_operator import GKENatPodOperator
from operators.task_sensor import ExternalTaskCompletedSensor
from airflow.models import Variable
from airflow.operators.subdag_operator import SubDagOperator

from glam_subdags.extract import extracts_subdag, extract_user_counts
from glam_subdags.histograms import histogram_aggregates_subdag
from glam_subdags.general import repeated_subdag
from glam_subdags.generate_query import generate_and_run_desktop_query
from utils.gcp import bigquery_etl_query, gke_command
from utils.tags import Tag


project_id = "moz-fx-data-shared-prod"
dataset_id = "telemetry_derived"
tmp_project = "moz-fx-data-shared-prod"  # for temporary tables in analysis dataset
default_args = {
    "owner": "msamuel@mozilla.com",
    "depends_on_past": False,
    "start_date": datetime(2019, 10, 22),
    "email": [
        "telemetry-alerts@mozilla.com",
        "msamuel@mozilla.com",
        "akommasani@mozilla.com",
    ],
    "email_on_failure": True,
    "email_on_retry": True,
    "retries": 1,
    "retry_delay": timedelta(minutes=30),
}

GLAM_DAG = "glam"
GLAM_CLIENTS_HISTOGRAM_AGGREGATES_SUBDAG = "clients_histogram_aggregates"
PERCENT_RELEASE_WINDOWS_SAMPLING = "10"

tags = [Tag.ImpactTier.tier_2]

dag = DAG(
    GLAM_DAG,
    default_args=default_args,
    schedule_interval="0 2 * * *",
    doc_md=__doc__,
    tags=tags,
)


# Make sure all the data for the given day has arrived before running.
wait_for_main_ping = ExternalTaskCompletedSensor(
    task_id="wait_for_main_ping",
    external_dag_id="copy_deduplicate",
    external_task_id="copy_deduplicate_main_ping",
    execution_delta=timedelta(hours=1),
    check_existence=True,
    mode="reschedule",
    pool="DATA_ENG_EXTERNALTASKSENSOR",
    email_on_retry=False,
    dag=dag,
)

latest_versions = bigquery_etl_query(
    task_id="latest_versions",
    destination_table="latest_versions",
    dataset_id=dataset_id,
    project_id=project_id,
    owner="msamuel@mozilla.com",
    date_partition_parameter=None,
    arguments=("--replace",),
    dag=dag,
)

# This task runs first and replaces the relevant partition, followed
# by the next two tasks that append to the same partition of the same table.
clients_daily_scalar_aggregates = generate_and_run_desktop_query(
    task_id="clients_daily_scalar_aggregates",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=True,
    probe_type="scalar",
    dag=dag,
)

clients_daily_keyed_scalar_aggregates = generate_and_run_desktop_query(
    task_id="clients_daily_keyed_scalar_aggregates",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=False,
    probe_type="keyed_scalar",
    dag=dag,
)

clients_daily_keyed_boolean_aggregates = generate_and_run_desktop_query(
    task_id="clients_daily_keyed_boolean_aggregates",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=False,
    probe_type="keyed_boolean",
    dag=dag,
)

clients_scalar_aggregates = bigquery_etl_query(
    task_id="clients_scalar_aggregates",
    destination_table="clients_scalar_aggregates_v1",
    dataset_id=dataset_id,
    project_id=project_id,
    owner="msamuel@mozilla.com",
    depends_on_past=True,
    arguments=("--replace",),
    dag=dag,
)

scalar_percentiles = gke_command(
    task_id="scalar_percentiles",
    command=[
        "python3", "script/glam/run_scalar_agg_clustered_query.py",
        "--submission-date", "{{ds}}",
        "--dst-table", "scalar_percentiles_v1",
        "--project", project_id,
        "--tmp-project", tmp_project,
        "--dataset", dataset_id,
    ],
    docker_image="gcr.io/moz-fx-data-airflow-prod-88e0/bigquery-etl:latest",
    dag=dag,
)


# This task runs first and replaces the relevant partition, followed
# by the next task below that appends to the same partition of the same table.
clients_daily_histogram_aggregates_parent = generate_and_run_desktop_query(
    task_id="clients_daily_histogram_aggregates_parent",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=True,
    probe_type="histogram",
    process="parent",
    get_logs=False,
    dag=dag,
)

clients_daily_histogram_aggregates_content = generate_and_run_desktop_query(
    task_id="clients_daily_histogram_aggregates_content",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=False,
    probe_type="histogram",
    process="content",
    get_logs=False,
    dag=dag,
)

clients_daily_histogram_aggregates_gpu = generate_and_run_desktop_query(
    task_id="clients_daily_histogram_aggregates_gpu",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=False,
    probe_type="histogram",
    process="gpu",
    get_logs=False,
    dag=dag,
)

clients_daily_keyed_histogram_aggregates = generate_and_run_desktop_query(
    task_id="clients_daily_keyed_histogram_aggregates",
    project_id=project_id,
    source_dataset_id=dataset_id,
    sample_size=PERCENT_RELEASE_WINDOWS_SAMPLING,
    overwrite=False,
    probe_type="keyed_histogram",
    get_logs=False,
    dag=dag,
)

clients_histogram_aggregates = SubDagOperator(
    subdag=histogram_aggregates_subdag(
        GLAM_DAG,
        GLAM_CLIENTS_HISTOGRAM_AGGREGATES_SUBDAG,
        default_args,
        dag.schedule_interval,
        dataset_id,
    ),
    task_id=GLAM_CLIENTS_HISTOGRAM_AGGREGATES_SUBDAG,
    dag=dag,
)

histogram_percentiles = bigquery_etl_query(
    task_id="histogram_percentiles",
    destination_table="histogram_percentiles_v1",
    dataset_id=dataset_id,
    project_id=project_id,
    owner="msamuel@mozilla.com",
    date_partition_parameter=None,
    arguments=("--replace", "--clustering_fields=metric,channel"),
    dag=dag,
)

glam_user_counts = bigquery_etl_query(
    task_id="glam_user_counts",
    destination_table="glam_user_counts_v1",
    dataset_id=dataset_id,
    project_id=project_id,
    owner="msamuel@mozilla.com",
    date_partition_parameter=None,
    parameters=("submission_date:DATE:{{ds}}",),
    arguments=("--replace",),
    dag=dag,
)

glam_sample_counts = bigquery_etl_query(
    task_id="glam_sample_counts",
    destination_table="glam_sample_counts_v1",
    dataset_id=dataset_id,
    project_id=project_id,
    owner="akommasani@mozilla.com",
    date_partition_parameter=None,
    parameters=("submission_date:DATE:{{ds}}",),
    arguments=("--replace",),
    dag=dag,

)
client_scalar_probe_counts = gke_command(
    task_id="client_scalar_probe_counts",
    command=[
        "python3", "script/glam/run_scalar_agg_clustered_query.py",
        "--submission-date", "{{ds}}",
        "--dst-table", "clients_scalar_probe_counts_v1",
        "--project", project_id,
        "--tmp-project", tmp_project,
        "--dataset", dataset_id,
    ],
    docker_image="gcr.io/moz-fx-data-airflow-prod-88e0/bigquery-etl:latest",
    dag=dag,
)

# SubdagOperator uses a SequentialExecutor by default
# so its tasks will run sequentially.
# Note: In 2.0, SubDagOperator is changed to use airflow scheduler instead of
# backfill to schedule tasks in the subdag. User no longer need to specify
# the executor in SubDagOperator. (We don't but the assumption that Sequential
# Executor is used is now wrong)
clients_histogram_bucket_counts = SubDagOperator(
    subdag=repeated_subdag(
        GLAM_DAG,
        "clients_histogram_bucket_counts",
        default_args,
        dag.schedule_interval,
        dataset_id,
        ("submission_date:DATE:{{ds}}",),
        25,
        None,
    ),
    task_id="clients_histogram_bucket_counts",
    dag=dag,
)

clients_histogram_probe_counts = bigquery_etl_query(
    task_id="clients_histogram_probe_counts",
    destination_table="clients_histogram_probe_counts_v1",
    dataset_id=dataset_id,
    project_id=project_id,
    owner="msamuel@mozilla.com",
    date_partition_parameter=None,
    arguments=("--replace", "--clustering_fields=metric,channel"),
    dag=dag,
)

extract_counts = SubDagOperator(
    subdag=extract_user_counts(
        GLAM_DAG,
        "extract_user_counts",
        default_args,
        dag.schedule_interval,
        dataset_id,
        "user_counts",
        "counts"
    ),
    task_id="extract_user_counts",
    dag=dag
)

extracts_per_channel = SubDagOperator(
    subdag=extracts_subdag(
        GLAM_DAG,
        "extracts",
        default_args,
        dag.schedule_interval,
        dataset_id
    ),
    task_id="extracts",
    dag=dag,
)

# Move logic from Glam deployment's GKE Cronjob to this dag for better dependency timing
glam_import_image = 'gcr.io/moz-fx-dataops-images-global/gcp-pipelines/glam/glam-production/glam:2022.03.0-17'

base_docker_args = ['/venv/bin/python', 'manage.py']

env_vars = dict(
    DATABASE_URL = Variable.get("glam_secret__database_url"),
    DJANGO_SECRET_KEY = Variable.get("glam_secret__django_secret_key"),
    DJANGO_CONFIGURATION = "Prod",
    DJANGO_DEBUG = "False",
    DJANGO_SETTINGS_MODULE = "glam.settings",
    GOOGLE_CLOUD_PROJECT = "moz-fx-data-glam-prod-fca7"
)

glam_import_desktop_aggs_beta = GKENatPodOperator(
    task_id = 'glam_import_desktop_aggs_beta',
    name = 'glam_import_desktop_aggs_beta',
    image = glam_import_image,
    arguments = base_docker_args + ['import_desktop_aggs', 'beta'],
    env_vars = env_vars,
    dag=dag)

glam_import_desktop_aggs_nightly = GKENatPodOperator(
    task_id = 'glam_import_desktop_aggs_nightly',
    name = 'glam_import_desktop_aggs_nightly',
    image = glam_import_image,
    arguments = base_docker_args + ['import_desktop_aggs', 'nightly'],
    env_vars = env_vars,
    dag=dag)

glam_import_desktop_aggs_release = GKENatPodOperator(
    task_id = 'glam_import_desktop_aggs_release',
    name = 'glam_import_desktop_aggs_release',
    image = glam_import_image,
    arguments = base_docker_args + ['import_desktop_aggs', 'release'],
    env_vars = env_vars,
    dag=dag)

glam_import_user_counts = GKENatPodOperator(
    task_id = 'glam_import_user_counts',
    name = 'glam_import_user_counts',
    image = glam_import_image,
    arguments = base_docker_args + ['import_user_counts'],
    env_vars = env_vars,
    dag=dag)

glam_import_probes = GKENatPodOperator(
    task_id = 'glam_import_probes',
    name = 'glam_import_probes',
    image = glam_import_image,
    arguments = base_docker_args + ['import_probes'],
    env_vars = env_vars,
    dag=dag)


wait_for_main_ping >> latest_versions

latest_versions >> clients_daily_scalar_aggregates
clients_daily_scalar_aggregates >> clients_daily_keyed_scalar_aggregates
clients_daily_scalar_aggregates >> clients_daily_keyed_boolean_aggregates
clients_daily_keyed_boolean_aggregates >> clients_scalar_aggregates
clients_daily_keyed_scalar_aggregates >> clients_scalar_aggregates
clients_scalar_aggregates >> scalar_percentiles
# workaround resources exceeded exception
# client_scalar_probe_counts is not dependent on scalar_percentiles
scalar_percentiles >> client_scalar_probe_counts

latest_versions >> clients_daily_histogram_aggregates_parent
clients_daily_histogram_aggregates_parent >> clients_daily_histogram_aggregates_content
clients_daily_histogram_aggregates_parent >> clients_daily_histogram_aggregates_gpu
clients_daily_histogram_aggregates_parent >> clients_daily_keyed_histogram_aggregates
clients_daily_histogram_aggregates_content >> clients_histogram_aggregates
clients_daily_histogram_aggregates_gpu >> clients_histogram_aggregates
clients_daily_keyed_histogram_aggregates >> clients_histogram_aggregates

clients_histogram_aggregates >> clients_histogram_bucket_counts
clients_histogram_aggregates >> glam_user_counts
clients_histogram_aggregates >> glam_sample_counts


clients_histogram_bucket_counts >> clients_histogram_probe_counts
clients_histogram_probe_counts >> histogram_percentiles

clients_scalar_aggregates >> glam_user_counts
glam_user_counts >> extract_counts


extract_counts >> extracts_per_channel
client_scalar_probe_counts >> extracts_per_channel
scalar_percentiles >> extracts_per_channel
histogram_percentiles >> extracts_per_channel
glam_sample_counts >> extracts_per_channel

extracts_per_channel >> glam_import_desktop_aggs_beta
extracts_per_channel >> glam_import_desktop_aggs_nightly
extracts_per_channel >> glam_import_user_counts
extracts_per_channel >> glam_import_probes
glam_import_desktop_aggs_release
