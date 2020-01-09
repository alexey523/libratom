# pylint: disable=broad-except,invalid-name,protected-access,consider-using-ternary,import-outside-toplevel,too-many-locals
"""
Set of utility functions that use spaCy to perform named entity recognition
"""

import logging
import multiprocessing
import os
from collections import namedtuple
from datetime import datetime
from importlib import reload
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import pkg_resources
import spacy
from spacy.language import Language
from sqlalchemy.orm.session import Session

from libratom.lib.base import AttachmentMetadata
from libratom.lib.concurrency import get_messages, imap_job, worker_init
from libratom.models import Attachment, Entity, FileReport, Message

logger = logging.getLogger(__name__)

OUTPUT_FILENAME_TEMPLATE = "{}_entities_{}.sqlite3"

# Allow these to be set through the environment
RATOM_MSG_BATCH_SIZE = int(os.environ.get("RATOM_MSG_BATCH_SIZE", 100))
RATOM_DB_COMMIT_BATCH_SIZE = int(os.environ.get("RATOM_DB_COMMIT_BATCH_SIZE", 10_000))

# Interval between progress updates in the message generator
MSG_PROGRESS_STEP = int(os.environ.get("RATOM_MSG_PROGRESS_STEP", 10))

# Use the same default as spacy: https://github.com/explosion/spaCy/blob/v2.1.6/spacy/language.py#L130-L149
RATOM_SPACY_MODEL_MAX_LENGTH = int(
    os.environ.get("RATOM_SPACY_MODEL_MAX_LENGTH", 1_000_000)
)

# Spacy trained model names
SPACY_MODEL_NAMES = [
    "de_core_news_sm",
    "es_core_news_sm",
    "es_core_news_md",
    "pt_core_news_sm",
    "it_core_news_sm",
    "nl_core_news_sm",
    "en_core_web_sm",
    "en_core_web_md",
    "en_core_web_lg",
    "fr_core_news_sm",
    "fr_core_news_md",
]

SPACY_MODELS = namedtuple("SpacyModels", SPACY_MODEL_NAMES)(*SPACY_MODEL_NAMES)


@imap_job
def process_message(
    filepath: str,
    message_id: int,
    message: str,
    attachments: List[AttachmentMetadata],
    spacy_model: Language,
) -> Tuple[Dict, Optional[str]]:
    """
    Job function for the worker processes
    """

    # Return basic types to avoid serialization issues
    res = {
        "filepath": filepath,
        "message_id": message_id,
        "processing_start_time": datetime.utcnow(),
        "attachments": attachments,
    }

    try:
        # Extract entities from the message
        doc = spacy_model(message)
        res["entities"] = [(ent.text, ent.label_) for ent in doc.ents]

        res["processing_end_time"] = datetime.utcnow()

        return res, None

    except Exception as exc:
        return res, str(exc)


def load_spacy_model(spacy_model_name: str) -> Tuple[Optional[Language], Optional[int]]:
    """
    Loads and returns a given spaCy model

    If the model is not present, an attempt will be made to download and install it
    """

    try:
        spacy_model = spacy.load(spacy_model_name)

    except OSError as exc:
        logger.info(f"Unable to load spacy model {spacy_model_name}")

        if "E050" in str(exc):
            # https://github.com/explosion/spaCy/blob/v2.1.6/spacy/errors.py#L207
            # Model not found, try installing it
            logger.info(f"Downloading {spacy_model_name}")

            from spacy.cli.download import msg as spacy_msg

            # Download quietly
            spacy_msg.no_print = True
            try:
                spacy.cli.download(spacy_model_name, False, "--quiet")
            except SystemExit:
                logger.error(f"Unable to install spacy model {spacy_model_name}")
                return None, None

            # Now try loading it again
            reload(pkg_resources)
            spacy_model = spacy.load(spacy_model_name)

        else:
            logger.exception(exc)
            return None, None

    # Try to get spaCy model version
    try:
        spacy_model_version = pkg_resources.get_distribution(spacy_model_name).version
    except Exception as exc:
        spacy_model_version = None
        logger.info(
            f"Unable to get spaCy model version for {spacy_model_name}, error: {exc}"
        )

    # Set text length limit for model
    spacy_model.max_length = RATOM_SPACY_MODEL_MAX_LENGTH

    return spacy_model, spacy_model_version


def extract_entities(
    files: Iterable[Path],
    session: Session,
    spacy_model: Language,
    jobs: int = None,
    progress_callback: Callable = None,
    **kwargs,
) -> int:
    """
    Main entity extraction function that extracts named entities from a given iterable of files

    Spawns multiples processes via multiprocessing.Pool
    """

    # Confirm environment settings
    for key, value in globals().items():
        if key.startswith("RATOM_"):
            logger.debug(f"{key}: {value}")

    # Default progress callback to no-op
    update_progress = progress_callback or (lambda *_, **__: None)

    # Load the file_report table for local lookup
    _file_reports = session.query(FileReport).all()  # noqa: F841

    # Start of multiprocessing
    with multiprocessing.Pool(processes=jobs, initializer=worker_init) as pool:

        logger.debug(f"Starting pool with {pool._processes} processes")

        new_entities = []
        msg_count = 0

        try:

            for msg_count, worker_output in enumerate(
                pool.imap_unordered(
                    process_message,
                    get_messages(files, spacy_model=spacy_model, **kwargs),
                    chunksize=RATOM_MSG_BATCH_SIZE,
                ),
                start=1,
            ):

                # Unpack worker job output
                res, error = worker_output

                if error:
                    logger.info(
                        "Skipping message {message_id} from file {filepath}".format(
                            **res
                        )
                    )
                    logger.debug(
                        "File: {filepath}, message ID: {message_id}, {error}".format(
                            **res, error=error
                        )
                    )

                    continue

                # Extract results
                entities = res.pop("entities")
                message_id = res.pop("message_id")
                filepath = res.pop("filepath")
                attachments = res.pop("attachments")

                # Create new message instance
                message = Message(pff_identifier=message_id, **res)

                # Link message to a file_report
                try:
                    file_report = (
                        session.query(FileReport).filter_by(path=filepath).one()
                    )
                except Exception as exc:
                    file_report = None
                    logger.info(
                        f"Unable to link message id {message_id} to a file. Error: {exc}"
                    )

                message.file_report = file_report
                session.add(message)

                # Record attachment info
                session.add_all(
                    [
                        Attachment(
                            **attachment._asdict(),
                            message=message,
                            file_report=file_report,
                        )
                        for attachment in attachments
                    ]
                )

                # Record entities info
                for entity in entities:
                    new_entities.append(
                        Entity(
                            text=entity[0],
                            label_=entity[1],
                            filepath=filepath,
                            message=message,
                            file_report=file_report,
                        )
                    )

                # Commit if we reach a certain amount of new entities
                if len(new_entities) >= RATOM_DB_COMMIT_BATCH_SIZE:
                    session.add_all(new_entities)
                    new_entities = []
                    try:
                        session.commit()
                    except Exception as exc:
                        logger.exception(exc)
                        session.rollback()

                # Update progress every N messages
                if not msg_count % MSG_PROGRESS_STEP:
                    update_progress(MSG_PROGRESS_STEP)

            # Add remaining new entities
            session.add_all(new_entities)

            # Update progress with remaining message count
            update_progress(msg_count % MSG_PROGRESS_STEP)

        except KeyboardInterrupt:
            logger.warning("Cancelling running task")
            logger.info("Partial results written to database")
            logger.info("Terminating workers")

            # Clean up process pool
            pool.terminate()
            pool.join()

            return 1

    return 0
