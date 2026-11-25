from odoo.tests import TransactionCase, tagged

from odoo.addons.smarterp_bpo.models.hr_employee import WEIGHTS

# A low-risk baseline. Each test changes only what it needs to.
LOW_RISK = {
    'smarterp_tenure_months': 36,
    'smarterp_absences_90d': 0,
    'smarterp_performance': '5',
    'smarterp_overtime_hours': 0,
    'smarterp_satisfaction': '5',
    'smarterp_months_since_raise': 3,
}


@tagged('post_install', '-at_install', 'smarterp')
class TestAgentAttrition(TransactionCase):

    def _agent(self, **overrides):
        values = dict(LOW_RISK, name='Test Agent')
        values.update(overrides)
        return self.env['hr.employee'].create(values)

    def test_weights_add_up_to_100(self):
        self.assertEqual(sum(WEIGHTS.values()), 100)

    def test_high_risk_agent_is_high(self):
        agent = self._agent(
            smarterp_tenure_months=2,
            smarterp_absences_90d=8,
            smarterp_performance='1',
            smarterp_overtime_hours=40,
            smarterp_satisfaction='1',
            smarterp_months_since_raise=30,
        )
        self.assertEqual(agent.smarterp_risk_level, 'high')
        self.assertGreaterEqual(agent.smarterp_risk_score, 70)
        self.assertLessEqual(agent.smarterp_risk_score, 100)

    def test_low_risk_agent_is_low(self):
        agent = self._agent()
        self.assertEqual(agent.smarterp_risk_level, 'low')
        self.assertLess(agent.smarterp_risk_score, 40)

    def test_middle_agent_is_medium(self):
        agent = self._agent(
            smarterp_tenure_months=18,
            smarterp_absences_90d=3,
            smarterp_performance='3',
            smarterp_overtime_hours=20,
            smarterp_satisfaction='3',
            smarterp_months_since_raise=12,
        )
        self.assertEqual(agent.smarterp_risk_level, 'medium')

    def test_more_overtime_raises_score(self):
        agent = self._agent(smarterp_overtime_hours=5)
        before = agent.smarterp_risk_score
        agent.smarterp_overtime_hours = 30
        self.assertGreater(agent.smarterp_risk_score, before)

    def test_new_hire_scores_higher_than_veteran(self):
        new_hire = self._agent(smarterp_tenure_months=2)
        veteran = self._agent(smarterp_tenure_months=36)
        self.assertGreater(new_hire.smarterp_risk_score, veteran.smarterp_risk_score)
