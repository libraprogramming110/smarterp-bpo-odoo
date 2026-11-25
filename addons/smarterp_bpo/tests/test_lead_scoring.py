from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

INQUIRY = '<p>We need about 40 agents starting next month.</p>'


@tagged('post_install', '-at_install', 'smarterp')
class TestLeadScoring(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ai_class = type(self.env['smarterp.ai'])
        self.outbound_vals = {
            'name': 'Outbound telecom prospect',
            'smarterp_industry': 'telecom',
            'email_from': 'info@telecom.example',
            'source_id': self.env.ref('smarterp_bpo.utm_source_prospect_list').id,
            'smarterp_public_rating': 3.1,
            'smarterp_review_count': 1200,
        }

    def test_outbound_lead_gets_medium_confidence(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        # Odoo's win chance is learned from whatever leads are in the database: pin it so the test doesn't drift.
        lead.automated_probability = 30
        self.assertEqual(lead.smarterp_confidence_level, 'medium')
        self.assertTrue(40 <= lead.smarterp_score <= 65, lead.smarterp_score)

    def test_unknown_is_not_zero(self):
        unknown = self.env['crm.lead'].create({'name': 'No seats'})
        one_seat = self.env['crm.lead'].create({'name': 'One seat', 'smarterp_seats_needed': 1})
        self.assertGreater(unknown.smarterp_score, one_seat.smarterp_score)
        self.assertLess(unknown.smarterp_confidence, one_seat.smarterp_confidence)
        self.assertLessEqual(abs(unknown.smarterp_score - 50), abs(one_seat.smarterp_score - 50))

    def test_free_mail_scores_lower_than_corporate(self):
        corporate = self.env['crm.lead'].create(self.outbound_vals)
        free = self.env['crm.lead'].create(dict(self.outbound_vals, email_from='someone@gmail.com'))
        self.assertEqual(corporate.smarterp_corporate_email, 'yes')
        self.assertEqual(free.smarterp_corporate_email, 'no')
        self.assertGreater(corporate.smarterp_score, free.smarterp_score)

    def test_single_create_reads_inquiry_with_gemini(self):
        before = self.env['crm.lead'].create({'name': 'No inquiry'}).smarterp_confidence
        answer = 'seats: 40\ntimeline: now\nindustry: finance'
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer):
            lead = self.env['crm.lead'].create({'name': 'Inbound', 'description': INQUIRY})
        self.assertEqual(lead.smarterp_seats_needed, 40)
        self.assertEqual(lead.smarterp_timeline, 'now')
        self.assertEqual(lead.smarterp_industry, 'finance')
        self.assertEqual(lead.smarterp_signals_source, 'gemini')
        self.assertGreater(lead.smarterp_confidence, before)

    def test_gemini_none_falls_back_to_keywords(self):
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            lead = self.env['crm.lead'].create({'name': 'Inbound', 'description': INQUIRY})
        self.assertEqual(lead.smarterp_seats_needed, 40)
        self.assertEqual(lead.smarterp_timeline, 'now')
        self.assertEqual(lead.smarterp_signals_source, 'rules')

    def test_bulk_create_never_calls_gemini(self):
        with patch.object(self.ai_class, 'ask_gemini', return_value=None) as ask:
            self.env['crm.lead'].create([
                {'name': 'Import 1', 'description': INQUIRY},
                {'name': 'Import 2', 'description': INQUIRY},
            ])
        ask.assert_not_called()

    def test_rescore_with_gemini(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        answer = 'Reason: Big telecom with poor reviews.\nNext step: Find out how many seats they need.'
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer):
            lead.action_smarterp_rescore()
        self.assertEqual(lead.smarterp_reason, 'Big telecom with poor reviews.')
        self.assertEqual(lead.smarterp_next_step, 'Find out how many seats they need.')
        self.assertEqual(lead.smarterp_reason_source, 'gemini')

    def test_rescore_reads_markdown_and_next_line_answers(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        answer = '**Reason:**\nBig telecom with poor reviews.\n\n**Next step:**\nAsk how many seats they need.'
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer):
            lead.action_smarterp_rescore()
        self.assertEqual(lead.smarterp_reason, 'Big telecom with poor reviews.')
        self.assertEqual(lead.smarterp_next_step, 'Ask how many seats they need.')
        self.assertEqual(lead.smarterp_reason_source, 'gemini')

    def test_rescore_gemini_none_falls_back_to_rules(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            lead.action_smarterp_rescore()
        self.assertEqual(lead.smarterp_reason_source, 'rules')
        self.assertIn('medium confidence', lead.smarterp_reason)
        self.assertIn('Unknown: seats needed', lead.smarterp_reason)
        self.assertEqual(lead.smarterp_next_step, 'Find out seats needed before investing more time.')

    def test_low_confidence_is_needs_data(self):
        lead = self.env['crm.lead'].create({'name': 'Barely known'})
        self.assertEqual(lead.smarterp_confidence_level, 'low')
        self.assertEqual(lead.smarterp_tier, 'needs_data')

    def test_analyze_fills_reason_and_next_step(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            lead.action_smarterp_analyze()
        self.assertEqual(lead.smarterp_reason_source, 'rules')
        self.assertTrue(lead.smarterp_reason)
        self.assertEqual(lead.smarterp_next_step, 'Find out seats needed before investing more time.')

    def test_engagement_counts_inbound_email(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        self.assertEqual(lead.smarterp_email_count, 0)
        confidence = lead.smarterp_confidence
        customer = self.env['res.partner'].create({'name': 'Customer', 'email': 'info@telecom.example'})
        with patch.object(self.ai_class, 'ask_gemini', return_value=None):
            lead.message_post(body='Can you send pricing?', message_type='email', author_id=customer.id)
        self.assertEqual(lead.smarterp_email_count, 1)
        self.assertEqual(lead.smarterp_confidence, confidence + 10)

    def test_customer_reply_is_read_for_signals(self):
        lead = self.env['crm.lead'].create(self.outbound_vals)
        before = lead.smarterp_score
        customer = self.env['res.partner'].create({'name': 'Customer', 'email': 'info@telecom.example'})
        answer = 'seats: 40\ntimeline: now\nindustry: telecom'
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer):
            lead.message_post(body=INQUIRY, message_type='email', author_id=customer.id)
        self.assertEqual(lead.smarterp_seats_needed, 40)
        self.assertEqual(lead.smarterp_timeline, 'now')
        self.assertEqual(lead.smarterp_signals_source, 'gemini')
        self.assertGreater(lead.smarterp_score, before)
