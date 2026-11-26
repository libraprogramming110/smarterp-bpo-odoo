from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

COMPLAINTS = [
    'We are unhappy, response times are slow again.',
    'Another complaint: the agents gave a poor answer to our customers.',
    'This is terrible. We may cancel and are talking to other vendors.',
]
PRAISE = [
    'Great work this month, thank you!',
    'Our customers are happy with the team, excellent service.',
]


@tagged('post_install', '-at_install', 'smarterp')
class TestChurnPrediction(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ai_class = type(self.env['smarterp.ai'])
        self.today = fields.Date.today()

    def _client(self, **vals):
        return self.env['res.partner'].create(dict({'name': 'Client', 'is_company': True,
                                                    'smarterp_is_bpo_client': True}, **vals))

    def _post(self, partner, body, days_ago=1):
        # smarterp_skip_ai: post without the automatic analysis, the test runs it when it wants.
        message = partner.with_context(smarterp_skip_ai=True).message_post(
            body=body, message_type='email', author_id=partner.id)
        message.date = fields.Datetime.now() - timedelta(days=days_ago)

    def test_at_risk_client_is_red(self):
        client = self._client(smarterp_contract_end=self.today + timedelta(days=30),
                              smarterp_previous_seats=100, smarterp_current_seats=50)
        for body in COMPLAINTS:
            self._post(client, body)
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            client.action_smarterp_reanalyze()
        self.assertEqual(client.smarterp_complaint_count, 3)
        self.assertEqual(client.smarterp_risk_phrases, 'yes')
        self.assertEqual(client.smarterp_health, 'red', client.smarterp_churn_score)

    def test_healthy_client_is_green(self):
        client = self._client(smarterp_contract_end=self.today + timedelta(days=365),
                              smarterp_previous_seats=50, smarterp_current_seats=60)
        for body in PRAISE:
            self._post(client, body)
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            client.action_smarterp_reanalyze()
        self.assertEqual(client.smarterp_complaint_count, 0)
        self.assertEqual(client.smarterp_risk_phrases, 'no')
        self.assertEqual(client.smarterp_health, 'green', client.smarterp_churn_score)
        self.assertEqual(client.smarterp_confidence_level, 'high')

    def test_unknown_is_not_zero(self):
        client = self._client()
        self.assertEqual(client.smarterp_confidence, 0)
        self.assertEqual(client.smarterp_confidence_level, 'low')
        self.assertEqual(client.smarterp_churn_score, 30)
        healthy = self._client(smarterp_contract_end=self.today + timedelta(days=365),
                               smarterp_previous_seats=50, smarterp_current_seats=60)
        self.assertLess(healthy.smarterp_churn_score, client.smarterp_churn_score)

    def test_reanalyze_with_gemini(self):
        client = self._client(smarterp_contract_end=self.today + timedelta(days=60))
        self._post(client, COMPLAINTS[2])
        answer = ('**Message 1:** negative\n**Risk phrases:** yes\n**Reason:**\nThey are talking to other vendors.\n\n'
                  '**Retention action:**\nBook a call with their operations head this week.')
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer) as ask:
            client.action_smarterp_reanalyze()
        ask.assert_called_once()
        self.assertEqual(client.smarterp_reason, 'They are talking to other vendors.')
        self.assertEqual(client.smarterp_retention_action, 'Book a call with their operations head this week.')
        self.assertEqual(client.smarterp_reason_source, 'gemini')
        self.assertAlmostEqual(client.smarterp_sentiment, -1.0)
        self.assertEqual(client.smarterp_risk_phrases, 'yes')

    def test_gemini_labels_drive_complaints_and_trend(self):
        client = self._client()
        # No keywords in these: only Gemini can tell they are complaints.
        self._post(client, 'Your agents keep dropping our calls and nobody follows up.', days_ago=2)
        self._post(client, 'Calls went unanswered for hours again yesterday.', days_ago=5)
        self._post(client, 'Thanks for the quick onboarding last month.', days_ago=50)
        answer = ('Message 1: negative\nMessage 2: negative\nMessage 3: positive\nRisk phrases: no\n'
                  'Reason: Service has slipped recently.\nRetention action: Fix call handling this week.')
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer):
            client.action_smarterp_reanalyze()
        self.assertEqual(client.smarterp_complaint_count, 2)
        self.assertEqual(client.smarterp_sentiment_trend, 'worse')
        self.assertAlmostEqual(client.smarterp_sentiment, -0.33, places=2)

    def test_labels_kept_when_reason_unreadable(self):
        client = self._client()
        self._post(client, 'Calls went unanswered for hours again yesterday.')
        answer = 'Message 1: negative\nRisk phrases: no\nSummary: service is slipping.'
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer):
            client.action_smarterp_reanalyze()
        self.assertEqual(client.smarterp_complaint_count, 1)
        self.assertEqual(client.smarterp_sentiment, -1.0)
        self.assertEqual(client.smarterp_reason_source, 'rules')

    def test_client_email_is_analyzed_automatically(self):
        client = self._client(smarterp_contract_end=self.today + timedelta(days=60))
        answer = ('Message 1: negative\nRisk phrases: yes\nReason: They plan to leave.\n'
                  'Retention action: Call them today.')
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer) as ask:
            client.message_post(body='We are moving to another vendor.', message_type='email', author_id=client.id)
        ask.assert_called_once()
        self.assertEqual(client.smarterp_reason_source, 'gemini')
        self.assertEqual(client.smarterp_risk_phrases, 'yes')
        self.assertEqual(client.smarterp_complaint_count, 1)

    def test_gemini_none_falls_back_to_rules(self):
        client = self._client(smarterp_contract_end=self.today + timedelta(days=60))
        self._post(client, COMPLAINTS[0])
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            client.action_smarterp_reanalyze()
        self.assertEqual(client.smarterp_reason_source, 'rules')
        self.assertLess(client.smarterp_sentiment, 0)
        self.assertIn('Unknown: sentiment trend, seats trend, call quality.', client.smarterp_reason)
        self.assertTrue(client.smarterp_retention_action)

    def test_no_messages_never_calls_gemini(self):
        client = self._client(smarterp_contract_end=self.today + timedelta(days=60))
        with patch.object(self.ai_class, 'ask_gemini', return_value=None) as ask:
            client.action_smarterp_reanalyze()
        ask.assert_not_called()
        self.assertEqual(client.smarterp_reason_source, 'rules')
        self.assertIn('Unknown: message sentiment', client.smarterp_reason)

    def test_seats_drop_raises_risk(self):
        end = self.today + timedelta(days=120)
        dropped = self._client(smarterp_contract_end=end, smarterp_previous_seats=100, smarterp_current_seats=60)
        steady = self._client(smarterp_contract_end=end, smarterp_previous_seats=100, smarterp_current_seats=100)
        self.assertEqual(dropped.smarterp_confidence, steady.smarterp_confidence)
        self.assertGreater(dropped.smarterp_churn_score, steady.smarterp_churn_score)

    def test_poor_qa_calls_raise_risk(self):
        end = self.today + timedelta(days=120)
        no_calls = self._client(smarterp_contract_end=end)
        poor_calls = self._client(smarterp_contract_end=end)
        call = self.env['smarterp.qa.transcript'].create({
            'name': 'CALL-1', 'partner_id': poor_calls.id, 'transcript': 'Customer: Hello?\nAgent: Bye.'})
        # Gemini off and no scorecard keywords in the call: every criterion Missed, QA score 0.
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            call.action_grade()
        self.assertEqual(call.final_score, 0)
        self.assertEqual(poor_calls.smarterp_qa_score, 0)
        self.assertEqual(poor_calls.smarterp_confidence, no_calls.smarterp_confidence + 10)
        self.assertGreater(poor_calls.smarterp_churn_score, no_calls.smarterp_churn_score)

    def test_non_client_is_not_scored(self):
        partner = self.env['res.partner'].create({'name': 'Supplier'})
        self.assertFalse(partner.smarterp_health)
        self.assertEqual(partner.smarterp_churn_score, 0)
