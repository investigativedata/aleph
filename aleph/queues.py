import logging
import json
import threading
from random import randrange

import pika
from servicelayer.rate_limit import RateLimit
from servicelayer.cache import make_key
from servicelayer.taskqueue import (
    Dataset,
    NO_COLLECTION,
    PREFIX,
)

from aleph.core import kv, rabbitmq_conn
from aleph.settings import SETTINGS
from aleph.model import Entity
from aleph.util import random_id

log = logging.getLogger(__name__)


lock = threading.Lock()


def flush_queue():
    try:
        channel = rabbitmq_conn.channel()
        for queue in SETTINGS.ALEPH_STAGES:
            try:
                channel.queue_purge(queue)
            except ValueError:
                logging.exception(f"Error while flushing the {queue} queue")
        channel.close()
    except pika.exceptions.AMQPError:
        logging.exception("Error while flushing task queue")
    for key in kv.scan_iter(make_key(PREFIX, "*")):
        kv.delete(key)


def dataset_from_collection(collection):
    """servicelayer dataset from a collection"""
    if collection is None:
        return NO_COLLECTION
    return str(collection.id)


def get_rate_limit(resource, limit=100, interval=60, unit=1):
    return RateLimit(kv, resource, limit=limit, interval=interval, unit=unit)


# ToDo: Move this to servicelayer??
def queue_task(collection, stage, job_id=None, context=None, **payload):
    task_id = random_id()
    priority = randrange(1, SETTINGS.RABBITMQ_MAX_PRIORITY + 1)
    body = {
        "collection_id": dataset_from_collection(collection),
        "job_id": job_id or random_id(),
        "task_id": task_id,
        "operation": stage,
        "context": context,
        "payload": payload,
        "priority": priority,
    }
    try:
        channel = rabbitmq_conn.channel()
        channel.confirm_delivery()
        channel.basic_publish(
            exchange="",
            routing_key=stage,
            body=json.dumps(body),
            properties=pika.BasicProperties(
                delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE, priority=priority
            ),
        )
        dataset = Dataset(conn=kv, name=dataset_from_collection(collection))
        dataset.add_task(task_id, stage)
    except (pika.exceptions.UnroutableError, pika.exceptions.AMQPConnectionError):
        log.exception("Error while queuing task")
    finally:
        try:
            channel = rabbitmq_conn.channel()
            channel.close()
        except pika.exceptions.ChannelWrongStateError:
            log.debug("Excplicitly closing RabbitMQ channel failed.")

    if SETTINGS.TESTING and lock.acquire(False):
        from aleph.worker import get_worker

        worker = get_worker()
        worker.process(blocking=False)
        lock.release()


def get_status(collection):
    dataset = dataset_from_collection(collection)
    return Dataset(kv, dataset).get_status()


def get_active_dataset_status():
    data = Dataset.get_active_dataset_status(kv)
    return data


def get_context(collection, pipeline):
    """Set some task context variables that configure the ingestors."""
    from aleph.logic.aggregator import get_aggregator_name

    return {
        "languages": collection.languages,
        "ftmstore": get_aggregator_name(collection),
        "namespace": collection.foreign_id,
        "pipeline": pipeline,
    }


def cancel_queue(collection):
    dataset = dataset_from_collection(collection)
    Dataset(kv, dataset).cancel()


def ingest_entity(collection, proxy, job_id=None, index=True):
    """Send the given entity proxy to the ingest-file service."""

    log.debug("Ingest entity [%s]: %s", proxy.id, proxy.caption)
    pipeline = list(SETTINGS.INGEST_PIPELINE)
    if index:
        pipeline.append(SETTINGS.STAGE_INDEX)
    context = get_context(collection, pipeline)

    queue_task(collection, SETTINGS.STAGE_INGEST, job_id, context, **proxy.to_dict())


def pipeline_entity(collection, proxy, job_id=None):
    """Send an entity through the ingestion pipeline, minus the ingestor itself."""
    log.debug("Pipeline entity [%s]: %s", proxy.id, proxy.caption)
    pipeline = []
    if not SETTINGS.TESTING:
        if proxy.schema.is_a(Entity.ANALYZABLE):
            pipeline.extend(SETTINGS.INGEST_PIPELINE)
    pipeline.append(SETTINGS.STAGE_INDEX)
    context = get_context(collection, pipeline)

    operation = pipeline.pop(0)
    payload = {"entity_ids": [proxy.id]}

    queue_task(collection, operation, job_id, context, **payload)
