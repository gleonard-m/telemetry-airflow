from datetime import datetime, timedelta
from typing import Any, Dict

from airflow import DAG
from airflow.operators.python_operator import PythonOperator
from airflow.hooks.base import BaseHook
from operators.backport.fivetran.operator import FivetranOperator
from operators.backport.fivetran.sensor import FivetranSensor
from utils.tags import Tag
from utils.acoustic.acoustic_client import AcousticClient


def _generate_acoustic_report(conn_id: str, report_type: str, config: Dict[Any, Any], *args, **kwargs):
    """
    A wrapper function for retrieving Acoustic connection details from Airflow instantiating AcousticClient and generating report.
    """

    if config["request_params"]["date_start"] == config["request_params"]["date_end"]:
        err_msg = "It appears start and end date are exactly the same. This is undesired and will result in data being generated for 0 second time range."
        raise ValueError(err_msg)

    conn = BaseHook.get_connection(conn_id)

    acoustic_connection = {
        "base_url": conn.host,
        "client_id": conn.login,
        "client_secret": conn.password,
        "refresh_token": conn.extra_dejson["refresh_token"],
    }

    acoustic_client = AcousticClient(**acoustic_connection)
    acoustic_client.generate_report(request_template=config["request_template"], template_params=config["request_params"], report_type=report_type)

    return


DOCS = """
### fivetran_acoustic

#### Description

DAG triggers acoustic report generation, then corresponding Fivetran connector to consume the generated report
from Acoustic SFTP server and waits until its completion.

#### Owner

kignasiak@mozilla.com
"""

DAG_OWNER = "kignasiak@mozilla.com"

ACOUSTIC_CONNECTION_ID = "acoustic"

EXEC_START = '{{ macros.ds_format(ds, "%Y-%m-%d", "%m/%d/%Y %H:%M:%S") }}'
EXEC_END = '{{ macros.ds_format(next_ds, "%Y-%m-%d", "%m/%d/%Y %H:%M:%S") }}'

REQUEST_TEMPLATE_LOC = "dags/utils/acoustic/request_templates"

REPORTS_CONFIG = {
    "raw_recipient_export": {
        "request_template": f"{REQUEST_TEMPLATE_LOC}/reporting_raw_recipient_data_export.xml.jinja",
        "request_params": {
            "export_format": 0,
            "date_start": EXEC_START,
            "date_end": EXEC_END,
        },
    },
    "contact_export": {
        "request_template": f"{REQUEST_TEMPLATE_LOC}/export_database.xml.jinja",
        "request_params": {
            "list_id": "{{ var.value.fivetran_acoustic_contact_export_list_id }}",  # list_name: "Main Contact Table revision 3"
            "export_type": "ALL",
            "export_format": "CSV",
            "visibility": 1,  # 0 (Private) or 1 (Shared)
            "date_start": EXEC_START,
            "date_end": EXEC_END,
            "columns": "\n".join([
                f"<COLUMN>{column}</COLUMN>" for column in [
                    "email",
                    "basket_token",
                    "sfdc_id",
                    "double_opt_in",
                    "has_opted_out_of_email",
                    "email_format",
                    "email_lang",
                    "fxa_created_date",
                    "fxa_first_service",
                    "fxa_id",
                    "fxa_account_deleted",
                    "email_id",
                    "mailing_country",
                    "cohort",
                    "sub_mozilla_foundation",
                    "sub_common_voice",
                    "sub_hubs",
                    "sub_mixed_reality",
                    "sub_internet_health_report",
                    "sub_miti",
                    "sub_mozilla_fellowship_awardee_alumni",
                    "sub_mozilla_festival",
                    "sub_mozilla_technology",
                    "sub_mozillians_nda",
                    "sub_firefox_accounts_journey",
                    "sub_knowledge_is_power",
                    "sub_take_action_for_the_internet",
                    "sub_test_pilot",
                    "sub_firefox_news",
                    "vpn_waitlist_geo",
                    "vpn_waitlist_platform",
                    "sub_about_mozilla",
                    "sub_apps_and_hacks",
                    "sub_rally",
                    "sub_firefox_sweepstakes",
                    "relay_waitlist_geo",
                    "RECIPIENT_ID",
                    "Last Modified Date",
                ]
            ])
        },
    },
}


DEFAULT_ARGS = {
    "owner": DAG_OWNER,
    "email": [DAG_OWNER],
    "depends_on_past": False,
    "start_date": datetime(2021, 1, 20),
    "email_on_failure": True,
    "email_on_retry": False,
    "retries": 1,  # at this point we can probably be confident user intervention is required
    "retry_delay": timedelta(minutes=30),
}

TAGS = [Tag.ImpactTier.tier_1]

for report_type, _config in REPORTS_CONFIG.items():
    dag_id = f'fivetran_acoustic_{report_type}'

    with DAG(
        dag_id=dag_id,
        default_args=DEFAULT_ARGS,
        doc_md=DOCS,
        schedule_interval="0 8 * * *",  # Fivetran seems to operate in PST timezone (UTC -8)
        tags=TAGS,
        catchup=True,
        max_active_runs=1,  # to make sure that we don't attempt to trigger multiple Fivetran when one already running.
    ) as dag:
        generate_report = PythonOperator(
            task_id="generate_acoustic_report",
            python_callable=_generate_acoustic_report,
            op_args=[ACOUSTIC_CONNECTION_ID, report_type, _config],
        )

        sync_trigger = FivetranOperator(
            task_id='trigger_fivetran_connector',
            connector_id=f"{{{{ var.value.fivetran_acoustic_{report_type}_connector_id }}}}",
        )

        sync_wait = FivetranSensor(
            task_id='wait_for_fivetran_connector_completion',
            connector_id=f"{{{{ var.value.fivetran_acoustic_{report_type}_connector_id }}}}",
            poke_interval=30
        )

        # TODO: trigger corresponding BQETL Dag
        # TODO: Wait for that Dag to finish

        generate_report >> sync_trigger >> sync_wait
        globals()[dag_id] = dag
