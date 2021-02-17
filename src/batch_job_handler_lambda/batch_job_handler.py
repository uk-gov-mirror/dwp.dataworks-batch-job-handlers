#!/usr/bin/env python3

"""Batch Job Lambda."""
import argparse
import boto3
import json
import logging
import os
import sys
import socket
import re

FAILED_JOB_STATUS = "FAILED"
PENDING_JOB_STATUS = "PENDING"
RUNNABLE_JOB_STATUS = "RUNNABLE"
STARTING_JOB_STATUS = "STARTING"
SUCCEEDED_JOB_STATUS = "SUCCEEDED"

IGNORED_JOB_STATUSES = [PENDING_JOB_STATUS, RUNNABLE_JOB_STATUS, STARTING_JOB_STATUS]

JOB_NAME_KEY = "jobName"
JOB_STATUS_KEY = "jobStatus"
JOB_QUEUE_KEY = "jobQueue"

ERROR_NOTIFICATION_TYPE = "Error"
WARNING_NOTIFICATION_TYPE = "Warning"
INFORMATION_NOTIFICATION_TYPE = "Information"

CRITICAL_SEVERITY = "Critical"
HIGH_SEVERITY = "High"
MEDIUM_SEVERITY = "Medium"

REGEX_PDM_OBJECT_TAGGING_JOB_QUEUE_ARN = re.compile("^.*/pdm_object_tagger$")
REGEX_UCFS_CLAIMANT_JOB_QUEUE_ARN = re.compile("^.*/ucfs_claimant_api$")
TRIMMER_JOB_NAME = re.compile("^.*/k2hb_reconciliation_trimmer$")
REGEX_TRIMMER_JOB_QUEUE_ARN = re.compile("^.*/k2hb_reconciliation_trimmer$")

log_level = os.environ["LOG_LEVEL"].upper() if "LOG_LEVEL" in os.environ else "INFO"


# Initialise logging
def setup_logging(logger_level):
    """Set the default logger with json output."""
    the_logger = logging.getLogger()
    for old_handler in the_logger.handlers:
        the_logger.removeHandler(old_handler)

    new_handler = logging.StreamHandler(sys.stdout)
    hostname = socket.gethostname()

    json_format = (
        f'{{ "timestamp": "%(asctime)s", "log_level": "%(levelname)s", "message": "%(message)s", '
        f'"environment": "{args.environment}", "application": "{args.application}", '
        f'"module": "%(module)s", "process":"%(process)s", '
        f'"thread": "[%(thread)s]", "host": "{hostname}" }}'
    )

    new_handler.setFormatter(logging.Formatter(json_format))
    the_logger.addHandler(new_handler)
    new_level = logging.getLevelName(logger_level)
    the_logger.setLevel(new_level)

    if the_logger.isEnabledFor(logging.DEBUG):
        # Log everything from boto3
        boto3.set_stream_logger()
        the_logger.debug(f'Using boto3", "version": "{boto3.__version__}')

    return the_logger


def get_parameters():
    """Parse the supplied command line arguments.

    Returns:
        args: The parsed and validated command line arguments

    """
    parser = argparse.ArgumentParser(
        description="Start up and shut down ASGs on demand"
    )

    # Parse command line inputs and set defaults
    parser.add_argument("--aws-profile", default="default")
    parser.add_argument("--aws-region", default="eu-west-2")
    parser.add_argument("--sns-topic", help="SNS topic ARN")
    parser.add_argument("--environment", help="Environment value", default="NOT_SET")
    parser.add_argument("--application", help="Application", default="NOT_SET")

    _args = parser.parse_args()

    # Override arguments with environment variables where set
    if "AWS_PROFILE" in os.environ:
        _args.aws_profile = os.environ["AWS_PROFILE"]

    if "AWS_REGION" in os.environ:
        _args.aws_region = os.environ["AWS_REGION"]

    if "SNS_TOPIC" in os.environ:
        _args.sns_topic = os.environ["SNS_TOPIC"]

    if "ENVIRONMENT" in os.environ:
        _args.environment = os.environ["ENVIRONMENT"]

    if "APPLICATION" in os.environ:
        _args.application = os.environ["APPLICATION"]

    return _args


args = get_parameters()
logger = setup_logging(log_level)


def handler(event, context):
    """Handle the event from AWS.

    Args:
        event (Object): The event details from AWS
        context (Object): The context info from AWS

    """

    dumped_event = get_escaped_json_string(event)
    logger.info(f'SNS Event", "sns_event": {dumped_event}, "mode": "handler')

    try:
        boto3.setup_default_session(
            profile_name=args.aws_profile, region_name=args.aws_region
        )
    except Exception as e:
        logger.error(e)

    sns_client = boto3.client("sns")

    if not args.sns_topic:
        raise Exception("Required argument SNS_TOPIC is unset")

    details_dict = get_and_validate_job_details(
        event,
    )

    job_name = details_dict[JOB_NAME_KEY]
    job_status = details_dict[JOB_STATUS_KEY]
    job_queue = details_dict[JOB_QUEUE_KEY]

    if job_status in IGNORED_JOB_STATUSES:
        logger.info(
            f'Exiting normally as job status warrants no notification", ' +
            f'"job_name": "{job_name}, "job_queue": "{job_queue}, "job_status": "{job_status}'
        )
        sys.exit(0)

    severity = get_severity(job_queue, job_status, job_name)
    notification_type = get_notification_type(job_queue, job_status, job_name)

    payload = generate_monitoring_message_payload(
        job_queue,
        job_status,
        job_name,
        severity,
        notification_type,
    )

    send_sns_message(
        sns_client,
        payload,
        args.sns_topic,
        job_queue,
        job_status,
        job_name,
    )


def generate_monitoring_message_payload(
    job_queue,
    job_name,
    job_status,
    severity,
    notification_type,
):
    """Generates a payload for a monitoring message.

    Arguments:
        job_queue (dict): The job queue arn
        job_name (string): batch job name
        job_status (string): the status of the job
        export_date (string): the date of the export
        severity (string): the severity of the alert
        notification_type (string): the notification type of the alert

    """
    payload = {
        "severity": severity,
        "notification_type": notification_type,
        "slack_username": "AWS Batch Job Notification",
        "title_text": f"Job changed to - _{job_status}_",
        "custom_elements": [
            {"key": "Job name", "value": job_name},
            {"key": "Job queue", "value": job_queue},
        ],
    }

    dumped_payload = get_escaped_json_string(payload)
    logger.info(
        f'Generated monitoring SNS payload", "payload": {dumped_payload}, ' +
        f'"job_queue": "{job_queue}, "job_name": "{job_name}, "job_status": "{job_status}'
    )

    return payload


def send_sns_message(
    sns_client,
    payload,
    sns_topic_arn,
    job_queue,
    job_status,
    job_name,
):
    """Publishes the message to sns.

    Arguments:
        sns_client (client): The boto3 client for SQS
        payload (dict): the payload to post to SNS
        sns_topic_arn (string): the arn for the SNS topic
        job_queue (dict): The job queue arn
        job_status (dict): The job status
        job_name (dict): The job name

    """
    global logger

    json_message = json.dumps(payload)

    dumped_payload = get_escaped_json_string(payload)
    logger.info(
        f'Publishing payload to SNS", "payload": {dumped_payload}, "sns_topic_arn": "{sns_topic_arn}", ' +
        f'"job_queue": "{job_queue}, "job_name": "{job_name}, "job_status": "{job_status}'
    )

    return sns_client.publish(TopicArn=sns_topic_arn, Message=json_message)


def get_and_validate_job_details(event):
    """Get the job name from the SNS event.

    Arguments:
        event (dict): The SNS event
    """
    message = json.loads(event["Records"][0]["Sns"]["Message"])

    dumped_message = get_escaped_json_string(message)
    logger.info(
        f'Validating message", "message": {dumped_message}, ' +
        f'"job_queue": "{job_queue}, "job_name": "{job_name}, "job_status": "{job_status}'
    )

    if "detail" not in message:
        raise KeyError("Message contains no 'detail' key")

    details_dict = message["detail"]
    required_keys = [JOB_NAME_KEY, JOB_STATUS_KEY, JOB_QUEUE_KEY]

    for required_key in required_keys:
        if required_key not in details_dict:
            raise KeyError(f"Details dict contains no '{required_key}' key")

    logger.info(
        f'Message has been validated", "message": {dumped_message}, "job_queue": "{details_dict[JOB_QUEUE_KEY]}, ' +
        f'"job_name": "{details_dict[JOB_NAME_KEY]}, "job_status": "{details_dict[JOB_STATUS_KEY]}'
    )

    return details_dict


def get_severity(job_queue, job_status, job_name):
    """Get the severity of the given alert.

    Arguments:
        job_queue (dict): The job queue arn
        job_status (dict): The job status
        job_name (dict): The job name
    """
    severity = MEDIUM_SEVERITY

    if job_status in [SUCCEEDED_JOB_STATUS]:
        severity = HIGH_SEVERITY
    elif job_status in [FAILED_JOB_STATUS]:
        severity = (
            CRITICAL_SEVERITY
            if REGEX_PDM_OBJECT_TAGGING_JOB_QUEUE_ARN.match(job_queue)
            else HIGH_SEVERITY
        )
    
    logger.info(
        f'Generated severity", "severity": "{severity}", "job_name": "{job_name}, ' +
        f'"job_queue": "{job_queue}, "job_name": "{job_name}, "job_status": "{job_status}'
    )

    return severity


def get_notification_type(job_queue, job_status, job_name):
    """Get the type of the given alert.

    Arguments:
        job_queue (dict): The job queue arn
        job_status (dict): The job status
        job_name (dict): The job name
    """
    notification_type = INFORMATION_NOTIFICATION_TYPE

    if job_status in [FAILED_JOB_STATUS]:
        notification_type = (
            ERROR_NOTIFICATION_TYPE
            if REGEX_PDM_OBJECT_TAGGING_JOB_QUEUE_ARN.match(job_queue)
            else WARNING_NOTIFICATION_TYPE
        )
    
    logger.info(
        f'Generated notification type", "notification_type": "{notification_type}", ' +
        f'"job_queue": "{job_queue}, "job_name": "{job_name}, "job_status": "{job_status}'
    )

    return notification_type


def get_escaped_json_string(json_string):
    try:
        escaped_string = json.dumps(json.dumps(json_string))
    except:
        escaped_string = json.dumps(json_string)

    return escaped_string


if __name__ == "__main__":
    try:
        args = get_parameters()
        logger = setup_logging("INFO")

        boto3.setup_default_session(
            profile_name=args.aws_profile, region_name=args.aws_region
        )
        logger.info(os.getcwd())
        json_content = json.loads(open("resources/event.json", "r").read())
        handler(json_content, None)
    except Exception as err:
        logger.error(f'Exception occurred for invocation", "error_message": {err}')