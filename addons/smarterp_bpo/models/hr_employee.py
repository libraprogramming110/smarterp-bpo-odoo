from odoo import api, fields, models

# Weights add up to 100. Each input is first turned into a risk between 0 and 1,
# then multiplied by its weight. The final score is the sum, from 0 to 100.
WEIGHTS = {
    'tenure': 20,         # new hires and the 1-year mark are the riskiest
    'absences': 20,       # absences in the last 90 days
    'performance': 20,    # low last performance rating
    'overtime': 15,       # overtime hours per month
    'satisfaction': 15,   # low last satisfaction score
    'raise': 10,          # months since the last raise or promotion
}

# (last month of the band, risk 0-1). Anything past the last band uses TENURE_DEFAULT.
TENURE_BANDS = [
    (3, 1.0),    # 0-3 months: nesting period, highest risk
    (11, 0.6),   # 4-11 months
    (14, 0.8),   # 12-14 months: 1-year spike
    (23, 0.5),   # 15-23 months
]
TENURE_DEFAULT = 0.2  # 24+ months

ABSENCES_MAX = 6      # 6 or more absences in 90 days = full risk
OVERTIME_MAX = 40     # 40 or more overtime hours per month = full risk
RAISE_MAX = 24        # 24 or more months without a raise = full risk

HIGH_THRESHOLD = 70   # High at 70 or above
MEDIUM_THRESHOLD = 40  # Medium at 40 or above, Low below 40

RATING = [(str(i), str(i)) for i in range(1, 6)]


def _ratio(value, maximum):
    """Turn a count into a 0-1 risk, capped at the maximum."""
    return min(max(value or 0, 0) / maximum, 1.0)


def _tenure_risk(months):
    months = max(months or 0, 0)
    for last_month, risk in TENURE_BANDS:
        if months <= last_month:
            return risk
    return TENURE_DEFAULT


def _low_rating_risk(rating):
    """Rating 5 = no risk, rating 1 = full risk."""
    return (5 - int(rating or 3)) / 4


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    smarterp_tenure_months = fields.Integer(string='Tenure (months)')
    smarterp_absences_90d = fields.Integer(string='Absences (last 90 days)')
    smarterp_performance = fields.Selection(
        RATING, string='Performance (1-5)', default='3')
    smarterp_overtime_hours = fields.Integer(string='Overtime hours / month')
    smarterp_satisfaction = fields.Selection(
        RATING, string='Satisfaction (1-5)', default='3')
    smarterp_months_since_raise = fields.Integer(string='Months since last raise')
    smarterp_risk_score = fields.Integer(
        string='Attrition Risk Score', compute='_compute_smarterp_risk', store=True)
    smarterp_risk_level = fields.Selection(
        [('low', 'Low'), ('medium', 'Medium'), ('high', 'High')],
        string='Attrition Risk Level', compute='_compute_smarterp_risk', store=True)

    @api.depends(
        'smarterp_tenure_months',
        'smarterp_absences_90d',
        'smarterp_performance',
        'smarterp_overtime_hours',
        'smarterp_satisfaction',
        'smarterp_months_since_raise',
    )
    def _compute_smarterp_risk(self):
        for employee in self:
            risks = {
                'tenure': _tenure_risk(employee.smarterp_tenure_months),
                'absences': _ratio(employee.smarterp_absences_90d, ABSENCES_MAX),
                'performance': _low_rating_risk(employee.smarterp_performance),
                'overtime': _ratio(employee.smarterp_overtime_hours, OVERTIME_MAX),
                'satisfaction': _low_rating_risk(employee.smarterp_satisfaction),
                'raise': _ratio(employee.smarterp_months_since_raise, RAISE_MAX),
            }
            score = round(sum(WEIGHTS[key] * risks[key] for key in WEIGHTS))
            employee.smarterp_risk_score = score
            if score >= HIGH_THRESHOLD:
                employee.smarterp_risk_level = 'high'
            elif score >= MEDIUM_THRESHOLD:
                employee.smarterp_risk_level = 'medium'
            else:
                employee.smarterp_risk_level = 'low'
