# pylint: disable=missing-docstring,broad-except,import-outside-toplevel

import json
import logging
from email import policy
from email.generator import Generator
from email.message import Message
from email.parser import Parser
from importlib import reload
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple, Union

import pkg_resources
import requests
import spacy
from requests import HTTPError
from spacy.language import Language

from libratom.lib import MboxArchive, PffArchive
from libratom.lib.base import Archive
from libratom.lib.constants import RATOM_SPACY_MODEL_MAX_LENGTH, SPACY_MODEL_NAMES
from libratom.lib.exceptions import FileTypeError
from libratom.lib.pff import pff_msg_to_string

logger = logging.getLogger(__name__)


def get_ratom_settings() -> List[Tuple[str, Union[int, str]]]:
    return [
        (key, value) for key, value in globals().items() if key.startswith("RATOM_")
    ]


def open_mail_archive(path: Path, **kwargs) -> Optional[Union[PffArchive, MboxArchive]]:

    extension_type_mapping = {".pst": PffArchive, ".mbox": MboxArchive}

    try:
        archive_class = extension_type_mapping[path.suffix]
    except KeyError:
        raise FileTypeError(f"Unable to open {path}. Unsupported file type.")

    return archive_class(path, **kwargs)


def get_set_of_files(path: Path) -> Set[Path]:
    if path.is_dir():
        return set(path.glob("**/*.pst")).union(set(path.glob("**/*.mbox")))

    return {path}


def get_spacy_models() -> Dict[str, List[str]]:

    releases = {}

    paginated_url = "https://api.github.com/repos/explosion/spacy-models/releases?page=1&per_page=100"

    try:
        while paginated_url:
            response = requests.get(url=paginated_url)

            if not response.ok:
                response.raise_for_status()

            # Get name-version pairs
            for release in json.loads(response.content):
                name, version = release["tag_name"].split("-", maxsplit=1)

                # Skip alpha/beta versions
                if "a" in version or "b" in version:
                    continue

                releases[name] = [*releases.get(name, []), version]

            # Get the next page of results
            try:
                paginated_url = response.links["next"]["url"]
            except (AttributeError, KeyError):
                break

    except HTTPError:
        releases = {name: [] for name in SPACY_MODEL_NAMES}

    return releases


def load_spacy_model(spacy_model_name: str) -> Tuple[Optional[Language], Optional[str]]:
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


def extract_message_from_archive(archive: Archive, msg_id: int) -> Message:
    """
    Extracts a message from an open Archive object
    """

    msg = archive.get_message_by_id(msg_id)

    # mbox archive
    if isinstance(archive, MboxArchive):
        return msg

    # pst archive
    return Parser(policy=policy.default).parsestr(pff_msg_to_string(msg))


def export_messages_from_file(
    src_file: Path, msg_ids: Iterable[int], dest_folder: Path = None
) -> None:
    """
    Writes .eml files in a destination directory given a mailbox file (PST or mbox) and a list of message IDs
    """

    dest_folder = (dest_folder or Path.cwd()) / src_file.stem
    dest_folder.mkdir(parents=True, exist_ok=True)

    with open_mail_archive(src_file) as archive:
        for msg_id in msg_ids:
            try:
                msg = extract_message_from_archive(archive, int(msg_id))

                with (dest_folder / f"{msg_id}.eml").open(mode="w") as eml_file:
                    Generator(eml_file).flatten(msg)

            except Exception as exc:
                logger.warning(
                    f"Skipping message {msg_id} from {src_file}, reason: {exc}",
                    exc_info=True,
                )
