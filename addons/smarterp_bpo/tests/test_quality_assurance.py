from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

# The default scorecard: name, weight, critical, keywords.
CRITERIA = [
    ('Greeting', 10, False, 'thank you for calling, my name is'),
    ('Identity verification', 15, True, 'verify, date of birth'),
    ('Understanding the issue', 15, False, 'just to confirm'),
    ('Empathy & tone', 15, False, "i'm sorry, i understand"),
    ('Resolution', 25, False, 'resolved, refund has been'),
    ('Compliance disclosures', 10, True, 'this call may be recorded'),
    ('Closing', 10, False, 'anything else'),
]
TRANSCRIPT = (
    'Agent: Thank you for calling Acme, my name is Ana. This call may be recorded for quality.\n'
    'Customer: My bill is wrong again, this is terrible, I am really angry.\n'
    "Agent: I'm sorry about that. Just to confirm, you were charged twice in May?\n"
    'Customer: Yes.\n'
    'Agent: The refund has been processed, you will see it in 3 days. Anything else?\n'
    'Customer: No, thanks, that was great.'
)


def answer(ratings, start='negative', end='positive'):
    """A Gemini answer in the asked format, with markdown like the real one; ratings line up with CRITERIA."""
    lines = ['**Criterion %d:** %s | "quote %d"' % (i, rating, i) for i, rating in enumerate(ratings, 1)]
    lines += ['Sentiment start: %s' % start, 'Sentiment end: %s' % end,
              'Strength: Warm apology, "I\'m sorry about that".', 'Improvement: Verify the caller first.']
    return '\n'.join(lines)


@tagged('post_install', '-at_install', 'smarterp')
class TestQualityAssurance(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ai_class = type(self.env['smarterp.ai'])
        # Own scorecard, so an edited one in the database doesn't change the numbers.
        Criterion = self.env['smarterp.qa.criterion']
        Criterion.search([]).active = False
        for i, (name, weight, critical, keywords) in enumerate(CRITERIA):
            Criterion.create({'name': name, 'weight': weight, 'critical': critical, 'keywords': keywords,
                              'sequence': i, 'description': 'Good %s.' % name.lower()})

    def _grade(self, gemini_answer, text=TRANSCRIPT):
        call = self.env['smarterp.qa.transcript'].create({'name': 'CALL-1', 'transcript': text})
        with patch.object(self.ai_class, 'ask_gemini', return_value=gemini_answer) as ask:
            call.action_grade()
        ask.assert_called_once()
        return call

    def test_all_met_passes(self):
        call = self._grade(answer(['Met'] * 7))
        self.assertEqual(call.ai_score, 100)
        self.assertEqual(call.final_score, 100)
        self.assertEqual(call.outcome, 'pass')
        self.assertFalse(call.auto_fail)
        self.assertEqual(call.grade_source, 'gemini')
        self.assertEqual(len(call.result_ids), 7)
        self.assertEqual(call.result_ids[0].evidence, 'quote 1')
        self.assertEqual(call.sentiment_shift, 'improved')
        self.assertIn('Improvement: Verify the caller first.', call.coaching_note)
        # Not reviewed yet: still on the review queue.
        self.assertTrue(call.needs_review)

    def test_missed_critical_auto_fails(self):
        call = self._grade(answer(['Met', 'Missed', 'Met', 'Met', 'Met', 'Met', 'Met']))
        self.assertEqual(call.ai_score, 85)
        self.assertTrue(call.auto_fail)
        self.assertEqual(call.outcome, 'fail')
        self.assertTrue(call.needs_review)

    def test_gemini_none_uses_keywords(self):
        call = self._grade(None)
        ratings = dict(zip(call.result_ids.mapped('criterion_id.name'), call.result_ids.mapped('rating')))
        self.assertEqual(ratings['Greeting'], 'met')
        self.assertEqual(ratings['Identity verification'], 'missed')
        self.assertEqual(ratings['Resolution'], 'met')
        self.assertIn('refund has been processed', call.result_ids.filtered(
            lambda r: r.criterion_id.name == 'Resolution').evidence)
        self.assertEqual(call.ai_score, 85)
        self.assertTrue(call.auto_fail)
        self.assertEqual(call.grade_source, 'rules')
        self.assertTrue(call.needs_review)
        self.assertEqual(call.sentiment_start, 'negative')
        self.assertEqual(call.sentiment_end, 'positive')
        self.assertIn('Improvement: Identity verification', call.coaching_note)
        self.assertIn('Strength: Resolution', call.coaching_note)

    def test_unrated_criterion_falls_back_and_regrade_replaces(self):
        # Gemini skipped criterion 7 (Closing): keywords rate it, the grade counts as rules.
        call = self._grade(answer(['Met'] * 6))
        self.assertEqual(call.grade_source, 'rules')
        self.assertEqual(call.result_ids[-1].rating, 'met')
        self.assertIn('Anything else?', call.result_ids[-1].evidence)
        with patch.object(self.ai_class, 'ask_gemini', return_value=answer(['Met'] * 7)):
            call.action_grade()
        self.assertEqual(len(call.result_ids), 7)
        self.assertEqual(call.grade_source, 'gemini')

    def test_partial_counts_half(self):
        # Resolution (25) Partial, Empathy (15) Missed: 100 - 12.5 - 15 = 72.5, rounds to 72.
        call = self._grade(answer(['Met', 'Met', 'Met', 'Missed', 'partial', 'Met', 'Met']))
        self.assertEqual(call.result_ids[4].rating, 'partial')
        self.assertEqual(call.ai_score, 72)
        self.assertFalse(call.auto_fail)
        self.assertEqual(call.outcome, 'fail')

    def test_override_and_review(self):
        call = self._grade(answer(['Met', 'Met', 'Met', 'Missed', 'Partial', 'Met', 'Met']))
        call.write({'override_score': 90, 'reviewed': True, 'reviewer_id': self.env.user.id})
        self.assertEqual(call.ai_score, 72)
        self.assertEqual(call.final_score, 90)
        self.assertEqual(call.outcome, 'pass')
        self.assertFalse(call.needs_review)

    def test_create_never_calls_gemini(self):
        with patch.object(self.ai_class, 'ask_gemini', return_value=None) as ask:
            calls = self.env['smarterp.qa.transcript'].create([
                {'name': 'CALL-%d' % i, 'transcript': TRANSCRIPT} for i in range(3)])
        ask.assert_not_called()
        self.assertFalse(calls.result_ids)
        self.assertFalse(calls[0].outcome)
