from unittest.mock import patch

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'smarterp')
class TestAiHelper(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ai = self.env['smarterp.ai']

    def test_gemini_answer_parsed(self):
        with patch.object(type(self.ai), 'ask_gemini', return_value='Negative.'):
            result = self.ai.sentiment('Whatever')
        self.assertEqual(result, {'label': 'negative', 'score': -1.0, 'source': 'gemini'})

    def test_gemini_none_falls_back_to_keywords(self):
        with patch.object(type(self.ai), 'ask_gemini', return_value=None):
            result = self.ai.sentiment('Great service, thanks!')
        self.assertEqual(result['source'], 'keywords')
        self.assertEqual(result['label'], 'positive')

    def test_gemini_unparseable_falls_back_to_keywords(self):
        with patch.object(type(self.ai), 'ask_gemini', return_value='I am not sure'):
            result = self.ai.sentiment('Terrible, I want a refund')
        self.assertEqual(result['source'], 'keywords')
        self.assertEqual(result['label'], 'negative')

    def test_keyword_labels(self):
        self.assertEqual(self.ai.keyword_sentiment('Excellent and helpful agent')['label'], 'positive')
        self.assertEqual(self.ai.keyword_sentiment('Very bad and slow support')['label'], 'negative')
        self.assertEqual(self.ai.keyword_sentiment('Please call me on Monday')['label'], 'neutral')
