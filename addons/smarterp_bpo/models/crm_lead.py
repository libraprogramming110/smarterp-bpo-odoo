import logging
import re
import time

from odoo import api, fields, models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

# Each signal is turned into 0..1, or None when we don't know it yet. Weights sum to 100.
# Unknown signals are left out of the raw score; the less we know, the closer the score stays to 50.
SCORE_WEIGHTS = {
    'industry': 10,     # how well the industry fits BPO
    'corporate': 10,    # email on a company domain, not free mail
    'source': 10,       # where the lead came from
    'volume': 10,       # public reviews: lots of customers and unhappy ones means support pain
    'seats': 20,        # seats needed, full points at SEATS_FULL
    'timeline': 15,     # how soon they want to start
    'ai_chance': 15,    # Odoo's own ML win chance (automated_probability), always known
    'engagement': 10,   # inbound emails from the customer, full points at ENGAGEMENT_FULL
}
SEATS_FULL = 50
ENGAGEMENT_FULL = 3
REVIEWS_FULL = 500
TIMELINE_FACTOR = {'now': 1.0, 'quarter': 0.6, 'later': 0.2}
INDUSTRY_FACTOR = {'finance': 1.0, 'telecom': 1.0, 'ecommerce': 1.0, 'healthcare': 1.0, 'tech': 0.7, 'other': 0.3}
FREE_MAIL = ('gmail', 'yahoo', 'outlook', 'hotmail', 'icloud', 'aol', 'proton', 'live', 'ymail')
HOT_FROM = 70
WARM_FROM = 40
HIGH_CONFIDENCE_FROM = 70
MEDIUM_CONFIDENCE_FROM = 40
ANALYZE_MAX = 30    # leads per "Analyze selected" run, Gemini free tier allows 15 calls/min

FACTOR_LABELS = {
    'industry': 'industry fit',
    'corporate': 'corporate email',
    'source': 'lead source',
    'volume': 'public reviews',
    'seats': 'seats needed',
    'timeline': 'decision timeline',
    'ai_chance': 'Odoo AI win chance',
    'engagement': 'customer emails',
}
NEXT_STEPS = {
    'hot': 'Call this week and send a proposal with seat pricing.',
    'warm': 'Book a discovery call to confirm seats, budget and timeline.',
    'cold': 'Add to a nurture sequence and check back next quarter.',
    'needs_data': 'Research the company before scoring: seats, timeline and industry.',
}

REASON_RE = re.compile(r'reason\s*:\s*(.+?)\s*next\s*step\s*:\s*(.+)', re.I | re.S)

# Keyword fallback when Gemini can't read the inquiry.
SEATS_RE = re.compile(r'(\d+)\s*\+?\s*(?:agents?|seats?|staff|headcount|fte)\b'
                      r'|\b(?:agents?|seats?|staff|headcount|fte)\D{0,15}?(\d+)', re.I)
TIMELINE_WORDS = {
    'now': [r'asap', r'urgent', r'immediately', r'next month', r'this month', r'within (?:a|one|1) month'],
    'quarter': [r'quarter', r'3 months', r'next quarter', r'q[1-4]'],
    'later': [r'next year', r'later', r'exploring'],
}
INDUSTRY_WORDS = {
    'finance': [r'bank\w*', r'financ\w*', r'insurance'],
    'telecom': [r'telecom\w*', r'telco', r'mobile', r'broadband'],
    'ecommerce': [r'e-?commerce', r'shop\w*', r'store', r'retail'],
    'healthcare': [r'hospital', r'clinic\w*', r'health\w*'],
    'tech': [r'software', r'saas', r'app'],
}


class CrmLead(models.Model):
    _inherit = 'crm.lead'

    smarterp_seats_needed = fields.Integer('Seats Needed')
    smarterp_industry = fields.Selection([
        ('ecommerce', 'E-commerce / Retail'),
        ('finance', 'Banking / Finance'),
        ('healthcare', 'Healthcare'),
        ('telecom', 'Telecom'),
        ('tech', 'Tech / SaaS'),
        ('other', 'Other'),
    ], string='Industry')
    smarterp_timeline = fields.Selection([
        ('now', 'Within 1 month'),
        ('quarter', 'Within 3 months'),
        ('later', 'Later / unsure'),
    ], string='Decision Timeline')
    smarterp_public_rating = fields.Float('Public Rating (0-5)', digits=(2, 1))
    smarterp_review_count = fields.Integer('Review Count')
    smarterp_signals_source = fields.Selection([
        ('gemini', 'Gemini'),
        ('rules', 'Rules'),
    ], string='Read From Inquiry By')
    smarterp_corporate_email = fields.Selection([
        ('yes', 'Yes'),
        ('no', 'No'),
        ('unknown', 'Unknown'),
    ], string='Corporate Email', compute='_compute_smarterp_corporate_email')

    smarterp_score = fields.Integer('Lead Score', compute='_compute_smarterp_score', store=True)
    smarterp_tier = fields.Selection([
        ('hot', 'Hot'),
        ('warm', 'Warm'),
        ('cold', 'Cold'),
        ('needs_data', 'Needs Data'),  # Low confidence: too little known to call it hot or cold
    ], string='Tier', compute='_compute_smarterp_score', store=True)
    smarterp_confidence = fields.Integer('Confidence (%)', compute='_compute_smarterp_score', store=True)
    smarterp_confidence_level = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string='Confidence', compute='_compute_smarterp_score', store=True)
    smarterp_email_count = fields.Integer('Customer Emails', compute='_compute_smarterp_score', store=True)
    smarterp_reason = fields.Text('Why this score')
    smarterp_next_step = fields.Text('Next Step')
    smarterp_reason_source = fields.Selection([
        ('gemini', 'Gemini'),
        ('rules', 'Rules'),
    ], string='Reason Source')

    @api.depends('email_from')
    def _compute_smarterp_corporate_email(self):
        for lead in self:
            corporate = lead._smarterp_signals()['corporate']
            lead.smarterp_corporate_email = 'unknown' if corporate is None else ('yes' if corporate else 'no')

    def _smarterp_inbound_email_count(self):
        """Emails on this lead not written by one of our internal users."""
        messages = self.env['mail.message'].search([
            ('model', '=', 'crm.lead'), ('res_id', '=', self._origin.id or 0), ('message_type', '=', 'email'),
        ])
        return len(messages.filtered(lambda m: not m.author_id.user_ids.filtered(lambda u: not u.share)))

    def _smarterp_signals(self):
        """Value 0..1 per signal, or None when unknown."""
        self.ensure_one()
        email = (self.email_from or '').lower()
        source = (self.source_id.name or '').lower()
        rating, reviews = self.smarterp_public_rating, self.smarterp_review_count
        if not source:
            source_value = None
        elif 'referral' in source or 'partner' in source:
            source_value = 1.0
        elif 'website' in source or 'web form' in source:
            source_value = 0.7
        elif 'prospect list' in source:
            source_value = 0.4
        else:
            source_value = 0.3
        if not reviews and not rating:
            volume = None
        elif not rating:
            volume = min(reviews / REVIEWS_FULL, 1)  # no rating: judge on review volume only
        else:
            pain = 1.0 if rating <= 3.5 else 0.5 if rating <= 4.2 else 0.2
            volume = 0.6 * min(reviews / REVIEWS_FULL, 1) + 0.4 * pain
        emails = self.smarterp_email_count
        return {
            'industry': INDUSTRY_FACTOR.get(self.smarterp_industry),
            'corporate': (0.0 if email.split('@')[-1].startswith(FREE_MAIL) else 1.0) if '@' in email else None,
            'source': source_value,
            'volume': volume,
            'seats': min(self.smarterp_seats_needed / SEATS_FULL, 1) if self.smarterp_seats_needed > 0 else None,
            'timeline': TIMELINE_FACTOR.get(self.smarterp_timeline),
            'ai_chance': (self.automated_probability or 0) / 100,
            'engagement': min(emails / ENGAGEMENT_FULL, 1) if emails else None,
        }

    def _smarterp_raw_and_confidence(self):
        """Raw 0-100 score from the known signals only, and how much of the weight is known."""
        signals = self._smarterp_signals()
        known = {name: value for name, value in signals.items() if value is not None}
        confidence = sum(SCORE_WEIGHTS[name] for name in known)
        raw = 100 * sum(SCORE_WEIGHTS[name] * value for name, value in known.items()) / confidence
        return raw, confidence

    # Engagement is not in depends: _message_post_after_hook below recomputes when an email lands.
    @api.depends('smarterp_industry', 'email_from', 'source_id', 'source_id.name', 'smarterp_public_rating',
                 'smarterp_review_count', 'smarterp_seats_needed', 'smarterp_timeline', 'automated_probability')
    def _compute_smarterp_score(self):
        for lead in self:
            lead.smarterp_email_count = lead._smarterp_inbound_email_count()
            raw, confidence = lead._smarterp_raw_and_confidence()
            score = round(confidence / 100 * raw + (1 - confidence / 100) * 50)
            lead.smarterp_score = score
            lead.smarterp_confidence = confidence
            if confidence < MEDIUM_CONFIDENCE_FROM:
                lead.smarterp_tier = 'needs_data'
            elif score >= HOT_FROM:
                lead.smarterp_tier = 'hot'
            elif score >= WARM_FROM:
                lead.smarterp_tier = 'warm'
            else:
                lead.smarterp_tier = 'cold'
            if confidence >= HIGH_CONFIDENCE_FROM:
                lead.smarterp_confidence_level = 'high'
            elif confidence >= MEDIUM_CONFIDENCE_FROM:
                lead.smarterp_confidence_level = 'medium'
            else:
                lead.smarterp_confidence_level = 'low'

    def _message_post_after_hook(self, message):
        if message.message_type == 'email':
            for name in ('smarterp_email_count', 'smarterp_score', 'smarterp_tier',
                         'smarterp_confidence', 'smarterp_confidence_level'):
                self.env.add_to_compute(self._fields[name], self)
            # A customer reply: read it for seats, timeline and industry we don't know yet.
            from_customer = not message.author_id.user_ids.filtered(lambda u: not u.share)
            if from_customer and not self.env.context.get('smarterp_skip_ai'):
                for lead in self.filtered(lambda l: not (l.smarterp_seats_needed and l.smarterp_timeline
                                                         and l.smarterp_industry)):
                    lead._smarterp_extract_signals(overwrite=False, text=html2plaintext(message.body or ''))
        return super()._message_post_after_hook(message)

    @api.model_create_multi
    def create(self, vals_list):
        leads = super().create(vals_list)
        # Only one lead at a time (a web form or email), never bulk imports: Gemini has a rate limit.
        if len(leads) == 1 and not self.env.context.get('smarterp_skip_ai'):
            leads._smarterp_extract_signals(overwrite=False)
        return leads

    def _smarterp_inquiry_text(self):
        return html2plaintext(self.description or '').strip()

    def _smarterp_extract_signals(self, overwrite, text=None):
        """Read seats, timeline and industry from the inquiry text (or a customer email):
        Gemini first, keywords if it fails."""
        text = (text or '').strip() or self._smarterp_inquiry_text()
        if not text:
            return
        answer = self.env['smarterp.ai'].ask_gemini(
            'A customer sent this inquiry to a BPO (call center outsourcing) company. '
            'Answer in exactly three lines, no other text:\n'
            'seats: <number of agents/seats needed as an integer, or unknown>\n'
            'timeline: <now (within 1 month) | quarter (within 3 months) | later | unknown>\n'
            'industry: <ecommerce | finance | healthcare | telecom | tech | other | unknown>\n\n'
            'Inquiry:\n' + text
        )
        found = {}
        for line in (answer or '').lower().splitlines():
            key, _, value = line.strip(' *-').partition(':')
            value = value.strip(' *.')
            word = value.split(' ')[0]  # "now (within 1 month)" -> "now"
            if key == 'seats':
                number = re.match(r'\d+', value)
                found['seats'] = int(number.group()) if number else None
            elif key == 'timeline':
                found['timeline'] = word if word in TIMELINE_FACTOR else None
            elif key == 'industry':
                found['industry'] = word if word in INDUSTRY_FACTOR else None
        source = 'gemini'
        if not found:
            source = 'rules'
            lowered = text.lower()
            seats = SEATS_RE.search(text)
            found = {
                'seats': int(seats.group(1) or seats.group(2)) if seats else None,
                'timeline': next((t for t, words in TIMELINE_WORDS.items()
                                  if any(re.search(r'\b%s\b' % w, lowered) for w in words)), None),
                'industry': next((i for i, words in INDUSTRY_WORDS.items()
                                  if any(re.search(r'\b%s\b' % w, lowered) for w in words)), None),
            }
        vals = {}
        for key, field in (('seats', 'smarterp_seats_needed'), ('timeline', 'smarterp_timeline'),
                           ('industry', 'smarterp_industry')):
            if found.get(key) and (overwrite or not self[field]):
                vals[field] = found[key]
        if vals:
            vals['smarterp_signals_source'] = source
            self.write(vals)

    def action_smarterp_analyze(self):
        """List action: read the inquiry of each selected lead (fill only empty fields),
        then write a quick rules-based reason and next step. Re-score with AI upgrades it to Gemini."""
        leads = self.filtered(lambda lead: lead._smarterp_inquiry_text())
        for i, lead in enumerate(leads[:ANALYZE_MAX]):
            if i:
                time.sleep(4)  # stay under the Gemini free tier of 15 calls/min
            lead._smarterp_extract_signals(overwrite=False)
        for lead in self.filtered(lambda lead: lead.smarterp_reason_source != 'gemini'):
            lead.write({'smarterp_reason': lead._smarterp_rules_reason(),
                        'smarterp_next_step': lead._smarterp_rules_next_step(),
                        'smarterp_reason_source': 'rules'})
        message = '%d lead(s) with inquiry text analyzed.' % min(len(leads), ANALYZE_MAX)
        if len(leads) > ANALYZE_MAX:
            message += ' Max %d per run: %d skipped, run again on those.' % (ANALYZE_MAX, len(leads) - ANALYZE_MAX)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': 'Analyze selected', 'message': message, 'type': 'success',
            'next': {'type': 'ir.actions.act_window_close'},
        }}

    def _smarterp_level_label(self):
        return dict(self._fields['smarterp_confidence_level'].selection)[self.smarterp_confidence_level]

    def _smarterp_rules_reason(self):
        signals = self._smarterp_signals()
        points = {name: SCORE_WEIGHTS[name] * value for name, value in signals.items() if value is not None}
        top = sorted(points, key=points.get, reverse=True)[:2]
        tier = dict(self._fields['smarterp_tier'].selection)[self.smarterp_tier]
        reason = '%s lead (%d/100, %s confidence), driven mostly by %s.' % (
            tier, self.smarterp_score, self._smarterp_level_label().lower(),
            ' and '.join(FACTOR_LABELS[name] for name in top))
        unknown = [FACTOR_LABELS[name] for name, value in signals.items() if value is None]
        if unknown:
            reason += ' Unknown: %s.' % ', '.join(unknown)
        return reason

    def _smarterp_rules_next_step(self):
        if self.smarterp_confidence < HIGH_CONFIDENCE_FROM:
            signals = self._smarterp_signals()
            missing = next((name for name in ('seats', 'timeline', 'industry') if signals[name] is None), None)
            if missing:
                return 'Find out %s before investing more time.' % FACTOR_LABELS[missing].replace(' fit', '')
        return NEXT_STEPS[self.smarterp_tier]

    def _smarterp_prompt(self):
        signals = self._smarterp_signals()
        industry = dict(self._fields['smarterp_industry'].selection).get(self.smarterp_industry, 'unknown')
        timeline = dict(self._fields['smarterp_timeline'].selection).get(self.smarterp_timeline, 'unknown')
        corporate = dict(self._fields['smarterp_corporate_email'].selection)[self.smarterp_corporate_email]
        reviews = ('rating %.1f/5 from %d reviews' % (self.smarterp_public_rating, self.smarterp_review_count)
                   if signals['volume'] is not None else 'unknown')
        return (
            'You help a BPO (call center outsourcing) sales team. Here is a CRM lead:\n'
            f'- Name: {self.name}\n'
            f'- Company: {self.partner_name or self.partner_id.name or "unknown"}\n'
            f'- Industry: {industry}\n'
            f'- Corporate email: {corporate.lower()}\n'
            f'- Lead source: {self.source_id.name or "unknown"}\n'
            f'- Public reviews: {reviews}\n'
            f'- Seats needed: {self.smarterp_seats_needed or "unknown"}\n'
            f'- Decision timeline: {timeline}\n'
            f'- Odoo AI win chance: {self.automated_probability:.0f}%\n'
            f'- Emails received from the customer: {self.smarterp_email_count or "unknown"}\n'
            f'- Our lead score: {self.smarterp_score}/100, tier: {self.smarterp_tier}\n'
            f'- Confidence in the score: {self.smarterp_confidence}% ({self._smarterp_level_label()})\n\n'
            'If confidence is Low or Medium, the next step must name the most valuable missing information '
            'to find out.\n'
            'Answer in exactly two lines, no other text:\n'
            'Reason: <one sentence on why this lead has this score>\n'
            'Next step: <one concrete next action for the salesperson>'
        )

    def action_smarterp_rescore(self):
        """Re-read the inquiry, then ask Gemini why the lead scores what it does; rules if Gemini fails."""
        for lead in self:
            lead._smarterp_extract_signals(overwrite=True)
            answer = self.env['smarterp.ai'].ask_gemini(lead._smarterp_prompt())
            # Tolerant of markdown (**Reason:**) and of the text starting on the next line.
            match = REASON_RE.search((answer or '').replace('*', ''))
            reason, next_step = (match.group(1).strip(), match.group(2).strip()) if match else (None, None)
            if answer and not match:
                _logger.warning('Could not read Gemini reason answer: %r', answer[:300])
            if reason and next_step:
                lead.write({'smarterp_reason': reason, 'smarterp_next_step': next_step,
                            'smarterp_reason_source': 'gemini'})
            else:
                lead.write({'smarterp_reason': lead._smarterp_rules_reason(),
                            'smarterp_next_step': lead._smarterp_rules_next_step(),
                            'smarterp_reason_source': 'rules'})
        return True
