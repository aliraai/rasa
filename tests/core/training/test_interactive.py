import asyncio
import json
import os
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Text, Tuple, Callable

import mock
import pytest
from _pytest.monkeypatch import MonkeyPatch
from aioresponses import aioresponses
from mock import Mock

import rasa.shared.utils.io
import rasa.utils.io
from rasa.core.actions import action
from rasa.core.training import interactive
from rasa.shared.constants import INTENT_MESSAGE_PREFIX, DEFAULT_SENDER_ID
from rasa.shared.core.constants import ACTION_LISTEN_NAME
from rasa.shared.core.domain import Domain
from rasa.shared.core.events import BotUttered, ActionExecuted
from rasa.shared.core.trackers import DialogueStateTracker
from rasa.shared.core.training_data.story_reader.markdown_story_reader import (
    MarkdownStoryReader,
)
from rasa.shared.core.training_data.story_reader.yaml_story_reader import (
    YAMLStoryReader,
)
from rasa.shared.importers.rasa import TrainingDataImporter
from rasa.shared.nlu.constants import TEXT
from rasa.shared.nlu.training_data.formats import RasaYAMLReader, MarkdownReader
from rasa.shared.nlu.training_data.loading import RASA, MARKDOWN, UNK, RASA_YAML
from rasa.shared.nlu.training_data.message import Message
from rasa.utils.endpoints import EndpointConfig
from tests import utilities


@pytest.fixture
def mock_endpoint() -> EndpointConfig:
    return EndpointConfig("https://example.com")


@pytest.fixture
def mock_file_importer(
    stack_config_path: Text, nlu_data_path: Text, stories_path: Text, domain_path: Text
):
    domain_path = domain_path
    return TrainingDataImporter.load_from_config(
        stack_config_path, domain_path, [nlu_data_path, stories_path]
    )


async def test_send_message(mock_endpoint: EndpointConfig):
    sender_id = uuid.uuid4().hex

    url = f"{mock_endpoint.url}/conversations/{sender_id}/messages"
    with aioresponses() as mocked:
        mocked.post(url, payload={})

        await interactive.send_message(mock_endpoint, sender_id, "Hello")

        r = utilities.latest_request(mocked, "post", url)

        assert r

        expected = {"sender": "user", "text": "Hello", "parse_data": None}

        assert utilities.json_of_latest_request(r) == expected


async def test_request_prediction(mock_endpoint: EndpointConfig):
    sender_id = uuid.uuid4().hex

    url = f"{mock_endpoint.url}/conversations/{sender_id}/predict"

    with aioresponses() as mocked:
        mocked.post(url, payload={})

        await interactive.request_prediction(mock_endpoint, sender_id)

        assert utilities.latest_request(mocked, "post", url) is not None


def test_bot_output_format():
    message = {
        "event": "bot",
        "text": "Hello!",
        "data": {
            "image": "http://example.com/myimage.png",
            "attachment": "My Attachment",
            "buttons": [
                {"title": "yes", "payload": "/yes"},
                {"title": "no", "payload": "/no", "extra": "extra"},
            ],
            "elements": [
                {
                    "title": "element1",
                    "buttons": [{"title": "button1", "payload": "/button1"}],
                },
                {
                    "title": "element2",
                    "buttons": [{"title": "button2", "payload": "/button2"}],
                },
            ],
            "quick_replies": [
                {
                    "title": "quick_reply1",
                    "buttons": [{"title": "button3", "payload": "/button3"}],
                },
                {
                    "title": "quick_reply2",
                    "buttons": [{"title": "button4", "payload": "/button4"}],
                },
            ],
        },
    }
    from rasa.shared.core.events import Event

    bot_event = Event.from_parameters(message)

    assert isinstance(bot_event, BotUttered)

    formatted = interactive.format_bot_output(bot_event)
    assert formatted == (
        "Hello!\n"
        "Image: http://example.com/myimage.png\n"
        "Attachment: My Attachment\n"
        "Buttons:\n"
        "1: yes (/yes)\n"
        '2: no (/no) - {"extra": "extra"}\n'
        "Type out your own message...\n"
        "Elements:\n"
        '1: element1 - {"buttons": '
        '[{"payload": "/button1", "title": "button1"}]'
        '}\n2: element2 - {"buttons": '
        '[{"payload": "/button2", "title": "button2"}]'
        "}\nQuick replies:\n"
        '1: quick_reply1 - {"buttons": '
        '[{"payload": "/button3", "title": "button3"}'
        ']}\n2: quick_reply2 - {"buttons": '
        '[{"payload": "/button4", "title": "button4"}'
        "]}"
    )


def test_latest_user_message():
    tracker_dump = "data/test_trackers/tracker_moodbot.json"
    tracker_json = json.loads(rasa.shared.utils.io.read_file(tracker_dump))

    m = interactive.latest_user_message(tracker_json.get("events"))

    assert m is not None
    assert m["event"] == "user"
    assert m["text"] == "/mood_great"


def test_latest_user_message_on_no_events():
    m = interactive.latest_user_message([])

    assert m is None


def test_all_events_before_user_msg():
    tracker_dump = "data/test_trackers/tracker_moodbot.json"
    tracker_json = json.loads(rasa.shared.utils.io.read_file(tracker_dump))
    evts = tracker_json.get("events")

    m = interactive.all_events_before_latest_user_msg(evts)

    assert m is not None
    assert m == evts[:4]


def test_all_events_before_user_msg_on_no_events():
    assert interactive.all_events_before_latest_user_msg([]) == []


async def test_print_history(mock_endpoint):
    tracker_dump = rasa.shared.utils.io.read_file(
        "data/test_trackers/tracker_moodbot.json"
    )

    sender_id = uuid.uuid4().hex

    url = "{}/conversations/{}/tracker?include_events=AFTER_RESTART".format(
        mock_endpoint.url, sender_id
    )
    with aioresponses() as mocked:
        mocked.get(url, body=tracker_dump, headers={"Accept": "application/json"})

        await interactive._print_history(sender_id, mock_endpoint)

        assert utilities.latest_request(mocked, "get", url) is not None


async def test_is_listening_for_messages(mock_endpoint):
    tracker_dump = rasa.shared.utils.io.read_file(
        "data/test_trackers/tracker_moodbot.json"
    )

    sender_id = uuid.uuid4().hex

    url = "{}/conversations/{}/tracker?include_events=APPLIED".format(
        mock_endpoint.url, sender_id
    )
    with aioresponses() as mocked:
        mocked.get(url, body=tracker_dump, headers={"Content-Type": "application/json"})

        is_listening = await interactive.is_listening_for_message(
            sender_id, mock_endpoint
        )

        assert is_listening


def test_splitting_conversation_at_restarts():
    tracker_dump = "data/test_trackers/tracker_moodbot.json"
    evts = json.loads(rasa.shared.utils.io.read_file(tracker_dump)).get("events")
    evts_wo_restarts = evts[:]
    evts.insert(2, {"event": "restart"})
    evts.append({"event": "restart"})

    split = interactive._split_conversation_at_restarts(evts)
    assert len(split) == 2
    assert [e for s in split for e in s] == evts_wo_restarts
    assert len(split[0]) == 2
    assert len(split[0]) == 2


def test_as_md_message():
    parse_data = {
        "text": "Hello there rasa.",
        "entities": [{"start": 12, "end": 16, "entity": "name", "value": "rasa"}],
        "intent": {"name": "greeting", "confidence": 0.9},
    }
    md = interactive._as_md_message(parse_data)
    assert md == "Hello there [rasa](name)."


@pytest.mark.parametrize(
    "parse_original, parse_annotated, expected_entities",
    [
        (
            {
                "text": "Hello there rasa, it's me, paula.",
                "entities": [
                    {
                        "start": 12,
                        "end": 16,
                        "entity": "name1",
                        "value": "rasa",
                        "extractor": "batman",
                    }
                ],
                "intent": {"name": "greeting", "confidence": 0.9},
            },
            {
                "text": "Hello there rasa, it's me, paula.",
                "entities": [
                    {"start": 12, "end": 16, "entity": "name1", "value": "rasa"},
                    {"start": 26, "end": 31, "entity": "name2", "value": "paula"},
                ],
                "intent": {"name": "greeting", "confidence": 0.9},
            },
            [
                {
                    "start": 12,
                    "end": 16,
                    "entity": "name1",
                    "value": "rasa",
                    "extractor": "batman",
                },
                {"start": 26, "end": 31, "entity": "name2", "value": "paula"},
            ],
        ),
        (
            {
                "text": "I am flying from Berlin to London.",
                "entities": [
                    {
                        "start": 17,
                        "end": 23,
                        "entity": "location",
                        "role": "from",
                        "value": "Berlin",
                        "extractor": "DIETClassifier",
                    }
                ],
                "intent": {"name": "inform", "confidence": 0.9},
            },
            {
                "text": "I am flying from Berlin to London.",
                "entities": [
                    {
                        "start": 17,
                        "end": 23,
                        "entity": "location",
                        "value": "Berlin",
                        "role": "from",
                    },
                    {
                        "start": 27,
                        "end": 33,
                        "entity": "location",
                        "value": "London",
                        "role": "to",
                    },
                ],
                "intent": {"name": "inform", "confidence": 0.9},
            },
            [
                {
                    "start": 17,
                    "end": 23,
                    "entity": "location",
                    "value": "Berlin",
                    "role": "from",
                },
                {
                    "start": 27,
                    "end": 33,
                    "entity": "location",
                    "value": "London",
                    "role": "to",
                },
            ],
        ),
        (
            {
                "text": "A large pepperoni and a small mushroom.",
                "entities": [
                    {
                        "start": 2,
                        "end": 7,
                        "entity": "size",
                        "group": "1",
                        "value": "large",
                        "extractor": "DIETClassifier",
                    },
                    {
                        "start": 24,
                        "end": 29,
                        "entity": "size",
                        "value": "small",
                        "extractor": "DIETClassifier",
                    },
                ],
                "intent": {"name": "inform", "confidence": 0.9},
            },
            {
                "text": "A large pepperoni and a small mushroom.",
                "entities": [
                    {
                        "start": 2,
                        "end": 7,
                        "entity": "size",
                        "group": "1",
                        "value": "large",
                    },
                    {
                        "start": 8,
                        "end": 17,
                        "entity": "toppings",
                        "group": "1",
                        "value": "pepperoni",
                    },
                    {
                        "start": 30,
                        "end": 38,
                        "entity": "toppings",
                        "group": "1",
                        "value": "mushroom",
                    },
                    {
                        "start": 24,
                        "end": 29,
                        "entity": "size",
                        "group": "2",
                        "value": "small",
                    },
                ],
                "intent": {"name": "inform", "confidence": 0.9},
            },
            [
                {
                    "start": 2,
                    "end": 7,
                    "entity": "size",
                    "group": "1",
                    "value": "large",
                },
                {
                    "start": 8,
                    "end": 17,
                    "entity": "toppings",
                    "group": "1",
                    "value": "pepperoni",
                },
                {
                    "start": 30,
                    "end": 38,
                    "entity": "toppings",
                    "group": "1",
                    "value": "mushroom",
                },
                {
                    "start": 24,
                    "end": 29,
                    "entity": "size",
                    "group": "2",
                    "value": "small",
                },
            ],
        ),
    ],
)
def test__merge_annotated_and_original_entities(
    parse_original: Dict[Text, Any],
    parse_annotated: Dict[Text, Any],
    expected_entities: List[Dict[Text, Any]],
):
    entities = interactive._merge_annotated_and_original_entities(
        parse_annotated, parse_original
    )
    assert entities == expected_entities


def test_validate_user_message():
    parse_data = {
        "text": "Hello there rasa.",
        "parse_data": {
            "entities": [{"start": 12, "end": 16, "entity": "name", "value": "rasa"}],
            "intent": {"name": "greeting", "confidence": 0.9},
        },
    }
    assert interactive._validate_user_regex(parse_data, ["greeting", "goodbye"])
    assert not interactive._validate_user_regex(parse_data, ["goodbye"])


async def test_undo_latest_msg(mock_endpoint):
    tracker_dump = rasa.shared.utils.io.read_file(
        "data/test_trackers/tracker_moodbot.json"
    )

    sender_id = uuid.uuid4().hex

    url = "{}/conversations/{}/tracker?include_events=ALL".format(
        mock_endpoint.url, sender_id
    )
    append_url = "{}/conversations/{}/tracker/events".format(
        mock_endpoint.url, sender_id
    )
    with aioresponses() as mocked:
        mocked.get(url, body=tracker_dump)
        mocked.post(append_url)

        await interactive._undo_latest(sender_id, mock_endpoint)

        r = utilities.latest_request(mocked, "post", append_url)

        assert r

        # this should be the events the interactive call send to the endpoint
        # these events should have the last utterance omitted
        corrected_event = utilities.json_of_latest_request(r)
        assert corrected_event["event"] == "undo"


@pytest.mark.parametrize(
    "test_file_story, validator_story, test_file_nlu, validator_nlu, test_file_domain",
    [
        (
            "stories.yml",
            YAMLStoryReader.is_stories_file,
            "nlu.yml",
            RasaYAMLReader.is_yaml_nlu_file,
            "domain.yml",
        ),
        (
            "stories.md",
            MarkdownStoryReader.is_stories_file,
            "nlu.md",
            MarkdownReader.is_markdown_nlu_file,
            "domain.yml",
        ),
    ],
)
async def test_write_stories_to_file(
    test_file_story: Text,
    validator_story: Callable[[Text], bool],
    test_file_nlu: Text,
    validator_nlu: Callable[[Text], bool],
    test_file_domain: Text,
    mock_endpoint: EndpointConfig,
    tmp_path,
):
    tracker_dump = rasa.shared.utils.io.read_file(
        "data/test_trackers/tracker_moodbot_with_new_utterances.json"
    )

    sender_id = uuid.uuid4().hex

    url = f"{mock_endpoint.url}/conversations/{sender_id}/tracker?include_events=ALL"
    append_url = f"{mock_endpoint.url}/conversations/{sender_id}/tracker/events"
    domain_url = f"{mock_endpoint.url}/domain"

    target_files = [
        {"name": str(tmp_path / test_file_story), "validator": validator_story},
        {"name": str(tmp_path / test_file_nlu), "validator": validator_nlu},
        {"name": str(tmp_path / test_file_domain), "validator": lambda path: True},
    ]

    def info() -> Tuple[Text, Text, Text]:
        return target_files[0]["name"], target_files[1]["name"], target_files[2]["name"]

    with aioresponses() as mocked:
        mocked.get(url, body=tracker_dump)
        mocked.post(append_url)
        mocked.get(domain_url, payload={})

        interactive._request_export_info = info
        await interactive._write_data_to_file(sender_id, mock_endpoint)

    for target_file in target_files:
        assert os.path.exists(target_file["name"])
        assert target_file["validator"](target_file["name"])


def test_utter_custom_message():
    test_event = """
      {
      "data": {
        "attachment": null,
        "buttons": null,
        "elements": [
          {
            "a": "b"
          }
        ]
      },
      "event": "bot",
      "text": null,
      "timestamp": 1542649219.331037
    }
    """
    actual = interactive._chat_history_table([json.loads(test_event)])

    assert json.dumps({"a": "b"}) in actual


async def test_interactive_domain_persistence(
    mock_endpoint: EndpointConfig, tmp_path: Path
):
    # Test method interactive._write_domain_to_file

    tracker_dump = "data/test_trackers/tracker_moodbot.json"
    tracker_json = rasa.shared.utils.io.read_json_file(tracker_dump)

    events = tracker_json.get("events", [])

    domain_path = str(tmp_path / "interactive_domain_save.yml")

    url = f"{mock_endpoint.url}/domain"
    with aioresponses() as mocked:
        mocked.get(url, payload={})

        serialised_domain = await interactive.retrieve_domain(mock_endpoint)
        old_domain = Domain.from_dict(serialised_domain)

        interactive._write_domain_to_file(domain_path, events, old_domain)

    saved_domain = rasa.shared.utils.io.read_config_file(domain_path)

    for default_action in action.default_actions():
        assert default_action.name() not in saved_domain["actions"]


async def test_write_domain_to_file_with_form(tmp_path: Path):
    domain_path = str(tmp_path / "domain.yml")
    form_name = "my_form"
    old_domain = Domain.from_yaml(
        f"""
    actions:
    - utter_greet
    - utter_goodbye
    forms:
    - {form_name}
    intents:
    - greet
    """
    )

    events = [ActionExecuted(form_name), ActionExecuted(ACTION_LISTEN_NAME)]
    events = [e.as_dict() for e in events]

    interactive._write_domain_to_file(domain_path, events, old_domain)

    assert set(Domain.from_path(domain_path).action_names_or_texts) == set(
        old_domain.action_names_or_texts
    )


async def test_filter_intents_before_save_nlu_file(domain_path: Text):
    # Test method interactive._filter_messages
    from random import choice

    greet = {"text": "How are you?", "intent": "greet", "text_features": [0.5]}
    goodbye = {"text": "I am inevitable", "intent": "goodbye", "text_features": [0.5]}
    test_msgs = [Message(data=greet), Message(data=goodbye)]

    domain_file = domain_path
    domain = Domain.load(domain_file)
    intents = domain.intents

    msgs = test_msgs.copy()
    if intents:
        another_greet = greet.copy()
        another_greet[TEXT] = INTENT_MESSAGE_PREFIX + choice(intents)
        msgs.append(Message(data=another_greet))

    assert test_msgs == interactive._filter_messages(msgs)


@pytest.mark.parametrize(
    "path, expected_format",
    [
        ("bla.json", RASA),
        ("other.md", MARKDOWN),
        ("other.yml", RASA_YAML),
        ("unknown", UNK),
    ],
)
def test_get_nlu_target_format(path: Text, expected_format: Text):
    assert interactive._get_nlu_target_format(path) == expected_format


@pytest.mark.parametrize(
    "trackers, expected_trackers",
    [
        ([DialogueStateTracker.from_events("one", [])], [deque([]), DEFAULT_SENDER_ID]),
        (
            [
                str(i)
                for i in range(
                    interactive.MAX_NUMBER_OF_TRAINING_STORIES_FOR_VISUALIZATION + 1
                )
            ],
            [DEFAULT_SENDER_ID],
        ),
    ],
)
async def test_initial_plotting_call(
    mock_endpoint: EndpointConfig,
    monkeypatch: MonkeyPatch,
    trackers: List[Text],
    expected_trackers: List[Text],
    mock_file_importer: TrainingDataImporter,
):
    get_training_trackers = Mock(return_value=trackers)
    monkeypatch.setattr(
        interactive, "_get_training_trackers", asyncio.coroutine(get_training_trackers)
    )

    monkeypatch.setattr(interactive.utils, "is_limit_reached", lambda _, __: True)

    plot_trackers = Mock()
    monkeypatch.setattr(interactive, "_plot_trackers", asyncio.coroutine(plot_trackers))

    url = f"{mock_endpoint.url}/domain"
    with aioresponses() as mocked:
        mocked.get(url, payload={})

        await interactive.record_messages(mock_endpoint, mock_file_importer)

    get_training_trackers.assert_called_once()
    plot_trackers.assert_called_once_with(
        expected_trackers, interactive.DEFAULT_STORY_GRAPH_FILE, mock_endpoint
    )


async def test_not_getting_trackers_when_skipping_visualization(
    mock_endpoint: EndpointConfig, monkeypatch: MonkeyPatch
):
    get_trackers = Mock()
    monkeypatch.setattr(interactive, "_get_tracker_events_to_plot", get_trackers)

    monkeypatch.setattr(interactive.utils, "is_limit_reached", lambda _, __: True)

    url = f"{mock_endpoint.url}/domain"
    with aioresponses() as mocked:
        mocked.get(url, payload={})

        await interactive.record_messages(
            mock_endpoint, mock_file_importer, skip_visualization=True
        )

    get_trackers.assert_not_called()


class QuestionaryConfirmMock:
    def __init__(self, tries: int) -> None:
        self.tries = tries

    def __call__(self, text: Text) -> "QuestionaryConfirmMock":
        return self

    def ask(self) -> bool:
        self.tries -= 1
        if self.tries == 0:
            return False
        else:
            return True


def test_retry_on_error_success(monkeypatch: MonkeyPatch):
    monkeypatch.setattr(interactive.questionary, "confirm", QuestionaryConfirmMock(3))

    m = Mock(return_value=None)
    interactive._retry_on_error(m, "export_path", 1, a=2)
    m.assert_called_once_with("export_path", 1, a=2)


def test_retry_on_error_three_retries(monkeypatch: MonkeyPatch):
    monkeypatch.setattr(interactive.questionary, "confirm", QuestionaryConfirmMock(3))

    m = Mock(side_effect=PermissionError())
    with pytest.raises(PermissionError):
        interactive._retry_on_error(m, "export_path", 1, a=2)
    c = mock.call("export_path", 1, a=2)
    m.assert_has_calls([c, c, c])
