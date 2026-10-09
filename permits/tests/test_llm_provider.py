"""The permit LLM is optional by design — pin the fallback contract.

The permit AI features are advisory and must keep working with no LLM at all:
every caller treats ``chat_completion`` returning ``None`` as "use the
deterministic template". That only holds if the provider is inert when
unconfigured and never raises, so both are asserted here.
"""

from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase

from permits.ai.provider import (
    AI_DISCLAIMER,
    LlmConfig,
    chat_completion,
    get_llm_config,
)

FULL_ENV = {
    'PERMITS_LLM_URL': 'https://llm.example.test/v1/chat/completions',
    'PERMITS_LLM_API_KEY': 'sk-test',
}


class LlmConfigTests(SimpleTestCase):
    def test_unconfigured_without_a_url(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(get_llm_config())

    def test_unconfigured_without_a_key(self):
        with mock.patch.dict(
            os.environ, {'PERMITS_LLM_URL': FULL_ENV['PERMITS_LLM_URL']}, clear=True
        ):
            self.assertIsNone(get_llm_config())

    def test_blank_values_are_not_configuration(self):
        with mock.patch.dict(
            os.environ,
            {'PERMITS_LLM_URL': '   ', 'PERMITS_LLM_API_KEY': '  '},
            clear=True,
        ):
            self.assertIsNone(get_llm_config())

    def test_configured_with_url_and_key(self):
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            cfg = get_llm_config()
        self.assertIsInstance(cfg, LlmConfig)
        self.assertEqual(cfg.url, FULL_ENV['PERMITS_LLM_URL'])
        self.assertEqual(cfg.api_key, 'sk-test')

    def test_the_default_model_is_named(self):
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            self.assertEqual(get_llm_config().model, 'gpt-4o-mini')

    def test_the_model_can_be_overridden(self):
        with mock.patch.dict(
            os.environ, {**FULL_ENV, 'PERMITS_LLM_MODEL': 'gemini-2.5-flash'}, clear=True
        ):
            self.assertEqual(get_llm_config().model, 'gemini-2.5-flash')


class ChatCompletionTests(SimpleTestCase):
    def test_no_network_call_when_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch('requests.post') as post:
                self.assertIsNone(chat_completion('sys', 'user'))
        post.assert_not_called()

    def test_a_successful_reply_is_returned(self):
        response = mock.Mock(
            status_code=200,
            json=lambda: {'choices': [{'message': {'content': '  Draft text.  '}}]},
        )
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            with mock.patch('requests.post', return_value=response) as post:
                text = chat_completion('sys', 'user')
        self.assertEqual(text, 'Draft text.')
        self.assertIn('Bearer sk-test', post.call_args.kwargs['headers']['Authorization'])

    def test_an_empty_choice_falls_back(self):
        response = mock.Mock(status_code=200, json=lambda: {'choices': []})
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            with mock.patch('requests.post', return_value=response):
                self.assertIsNone(chat_completion('sys', 'user'))

    def test_blank_content_falls_back(self):
        response = mock.Mock(
            status_code=200,
            json=lambda: {'choices': [{'message': {'content': '   '}}]},
        )
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            with mock.patch('requests.post', return_value=response):
                self.assertIsNone(chat_completion('sys', 'user'))

    def test_a_non_2xx_falls_back(self):
        response = mock.Mock(status_code=429, json=lambda: {})
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            with mock.patch('requests.post', return_value=response):
                self.assertIsNone(chat_completion('sys', 'user'))

    def test_a_network_failure_falls_back(self):
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            with mock.patch('requests.post', side_effect=OSError('timed out')):
                self.assertIsNone(chat_completion('sys', 'user'))

    def test_malformed_json_falls_back(self):
        response = mock.Mock(status_code=200)
        response.json.side_effect = ValueError('not json')
        with mock.patch.dict(os.environ, FULL_ENV, clear=True):
            with mock.patch('requests.post', return_value=response):
                self.assertIsNone(chat_completion('sys', 'user'))

    def test_the_disclaimer_marks_the_output_as_advisory(self):
        self.assertIn('AI-generated', AI_DISCLAIMER)
        self.assertIn('deterministic', AI_DISCLAIMER)
