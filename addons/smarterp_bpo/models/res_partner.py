import logging
import re
import time
from datetime import timedelta

from odoo import api, fields, models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)

# Each risk signal is turned into 0..1 (1 = about to leave), or None when we don't know it yet.
# Weights sum to 100, so confidence is the known weight out of 100.
# Unknown signals are left out of the raw score; the less we know, the closer the score stays to BASELINE_RISK.
SCORE_WEIGHTS = {
    'sentiment': 20,     # client message sentiment over the last SENTIMENT_DAYS
    'trend': 10,         # sentiment of the last TREND_DAYS vs the days before, worse = risk
    'complaints': 15,    # negative client messages in the last SENTIMENT_DAYS, full at COMPLAINTS_FULL
    'risk_phrases': 10,  # "other vendors", "terminate", "RFP", "cancel"...
    'seats': 15,         # current vs previous seats, full at SEATS_DROP_FULL drop
    'renewal': 15,       # how soon the contract ends
    'silence': 5,        # days since the last client message, full at SILENCE_FULL
    'quality': 10,       # average final QA score of the client's calls in the last QA_DAYS, 0 = full risk
}
BASELINE_RISK = 30
SENTIMENT_DAYS = 90
TREND_DAYS = 30
TREND_STEP = 0.2     # average sentiment change that counts as better or worse
TREND_FACTOR = {'worse': 1.0, 'stable': 0.0, 'better': 0.0}
COMPLAINTS_FULL = 3
SEATS_DROP_FULL = 0.4
RENEWAL_FULL_DAYS = 90
RENEWAL_HALF_DAYS = 180
SILENCE_FULL = 60
QA_DAYS = 90
RED_FROM = 70
YELLOW_FROM = 40
HIGH_CONFIDENCE_FROM = 70
MEDIUM_CONFIDENCE_FROM = 40
PROMPT_MESSAGES = 10
ANALYZE_MAX = 30    # clients per "Analyze selected" run, Gemini free tier allows 15 calls/min

FACTOR_LABELS = {
    'sentiment': 'message sentiment',
    'trend': 'sentiment trend',
    'complaints': 'complaints',
    'risk_phrases': 'risk phrases',
    'seats': 'seats trend',
    'renewal': 'renewal window',
    'silence': 'silence',
    'quality': 'call quality',
}
RETENTION_ACTIONS = {
    'sentiment': 'Call the client this week to hear their concerns and agree on fixes.',
    'trend': 'Book a service review: their tone is getting worse.',
    'complaints': 'Escalate the open complaints to the account manager and close them within a week.',
    'risk_phrases': 'Set up an executive call now: they are talking about leaving or other vendors.',
    'seats': 'Meet the client to understand the seat cuts and offer a right-sized plan.',
    'renewal': 'Start renewal talks now with a renewal offer.',
    'silence': 'Check in with the client: we have not heard from them in a while.',
    'quality': 'Review the failed QA calls with the team lead and share a coaching plan with the client.',
    'green': 'Keep the regular check-ins and ask about growth or a referral.',
}
RISK_PHRASES = ['other vendor', 'another vendor', 'terminate', 'termination', 'rfp', 'cancel',
                'switch provider', 'switching provider', 'new provider', 'not renew', 'competitor']

REASON_RE = re.compile(r'reason\s*:\s*(.+?)\s*(?:retention\s*action|retention|action|next\s*step)\s*:\s*(.+)',
                       re.I | re.S)
LABEL_RE = re.compile(r'message\s*(\d+)\s*:\s*(positive|neutral|negative)', re.I)
RISK_PHRASES_RE = re.compile(r'risk\s*phrases?\s*:\s*(yes|no)', re.I)
SENTIMENT_LABELS = {'positive': 1.0, 'neutral': 0.0, 'negative': -1.0}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    smarterp_is_bpo_client = fields.Boolean('BPO Client')
    smarterp_contract_end = fields.Date('Contract End')
    smarterp_current_seats = fields.Integer('Current Seats')
    smarterp_previous_seats = fields.Integer('Previous Seats')

    # Written together by the last analysis; risk phrases empty means not analyzed (sentiment unknown).
    smarterp_sentiment = fields.Float('Message Sentiment (-1..1)', digits=(3, 2), readonly=True)
    smarterp_sentiment_trend = fields.Selection([
        ('worse', 'Worse'),
        ('stable', 'Stable'),
        ('better', 'Better'),
    ], string='Sentiment Trend', readonly=True)
    smarterp_risk_phrases = fields.Selection([
        ('yes', 'Yes'),
        ('no', 'No'),
    ], string='Risk Phrases', readonly=True)

    smarterp_churn_score = fields.Integer('Churn Risk', compute='_compute_smarterp_churn', store=True)
    smarterp_health = fields.Selection([
        ('red', 'Red'),
        ('yellow', 'Yellow'),
        ('green', 'Green'),
    ], string='Health', compute='_compute_smarterp_churn', store=True)
    smarterp_confidence = fields.Integer('Confidence (%)', compute='_compute_smarterp_churn', store=True)
    smarterp_confidence_level = fields.Selection([
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ], string='Confidence', compute='_compute_smarterp_churn', store=True)
    smarterp_complaint_count = fields.Integer('Complaints (90 days)', readonly=True)  # set by the analysis
    smarterp_last_client_message = fields.Datetime('Last Client Message', compute='_compute_smarterp_churn',
                                                   store=True)
    # QA transcripts of this client: grading or reviewing one recomputes the churn score through the depends.
    smarterp_qa_transcript_ids = fields.One2many('smarterp.qa.transcript', 'partner_id', 'QA Calls')
    smarterp_qa_score = fields.Integer('Avg QA Score (90 days)', compute='_compute_smarterp_churn', store=True)
    smarterp_reason = fields.Text('Why this risk')
    smarterp_retention_action = fields.Text('Retention Action')
    smarterp_reason_source = fields.Selection([
        ('gemini', 'Gemini'),
        ('rules', 'Rules'),
    ], string='Reason Source')

    def _smarterp_client_messages(self):
        """Emails on this partner not written by one of our internal users, newest first."""
        messages = self.env['mail.message'].search([
            ('model', '=', 'res.partner'), ('res_id', '=', self._origin.id or 0), ('message_type', '=', 'email'),
        ], order='date desc')
        return messages.filtered(lambda m: not m.author_id.user_ids.filtered(lambda u: not u.share))

    def _smarterp_recent_messages(self):
        since = fields.Datetime.now() - timedelta(days=SENTIMENT_DAYS)
        return self._smarterp_client_messages().filtered(lambda m: m.date >= since)

    @api.model
    def _smarterp_text(self, message):
        return html2plaintext(message.body or '').strip()

    def _smarterp_qa_average(self):
        """Average final QA score of graded calls in the last QA_DAYS, or None when there are none."""
        since = fields.Date.today() - timedelta(days=QA_DAYS)
        calls = self.smarterp_qa_transcript_ids.filtered(lambda t: t.outcome and t.call_date >= since)
        return sum(calls.mapped('final_score')) / len(calls) if calls else None

    def _smarterp_signals(self):
        """Risk 0..1 per signal, or None when unknown."""
        self.ensure_one()
        qa_average = self._smarterp_qa_average()
        sentiment = (1 - self.smarterp_sentiment) / 2 if self.smarterp_risk_phrases else None
        seats = None
        if self.smarterp_previous_seats > 0:
            drop = (self.smarterp_previous_seats - self.smarterp_current_seats) / self.smarterp_previous_seats
            seats = min(max(drop, 0) / SEATS_DROP_FULL, 1)
        renewal = None
        if self.smarterp_contract_end:
            days = (self.smarterp_contract_end - fields.Date.today()).days
            renewal = 1.0 if days <= RENEWAL_FULL_DAYS else 0.5 if days <= RENEWAL_HALF_DAYS else 0.0
        last = self.smarterp_last_client_message
        return {
            'sentiment': sentiment,
            'trend': TREND_FACTOR.get(self.smarterp_sentiment_trend),
            'complaints': (min(self.smarterp_complaint_count / COMPLAINTS_FULL, 1)
                           if self.smarterp_risk_phrases else None),
            'risk_phrases': {'yes': 1.0, 'no': 0.0}.get(self.smarterp_risk_phrases),
            'seats': seats,
            'renewal': renewal,
            'silence': min((fields.Datetime.now() - last).days / SILENCE_FULL, 1) if last else None,
            'quality': (100 - qa_average) / 100 if qa_average is not None else None,
        }

    def _smarterp_raw_and_confidence(self):
        """Raw 0-100 risk from the known signals only, and how much of the weight (out of 100) is known."""
        signals = self._smarterp_signals()
        known = {name: value for name, value in signals.items() if value is not None}
        confidence = sum(SCORE_WEIGHTS[name] for name in known)
        if not confidence:
            return 0, 0
        raw = 100 * sum(SCORE_WEIGHTS[name] * value for name, value in known.items()) / confidence
        return raw, confidence

    # Messages are not in depends: _message_post_after_hook below recomputes when an email lands.
    @api.depends('smarterp_is_bpo_client', 'smarterp_contract_end', 'smarterp_current_seats',
                 'smarterp_previous_seats', 'smarterp_sentiment', 'smarterp_sentiment_trend',
                 'smarterp_risk_phrases', 'smarterp_complaint_count',
                 'smarterp_qa_transcript_ids.final_score', 'smarterp_qa_transcript_ids.outcome',
                 'smarterp_qa_transcript_ids.call_date')
    def _compute_smarterp_churn(self):
        for partner in self:
            if not partner.smarterp_is_bpo_client:
                partner.update({'smarterp_churn_score': 0, 'smarterp_health': False, 'smarterp_confidence': 0,
                                'smarterp_confidence_level': False, 'smarterp_last_client_message': False,
                                'smarterp_qa_score': 0})
                continue
            partner.smarterp_last_client_message = partner._smarterp_client_messages()[:1].date
            partner.smarterp_qa_score = round(partner._smarterp_qa_average() or 0)
            raw, confidence = partner._smarterp_raw_and_confidence()
            score = round(confidence / 100 * raw + (1 - confidence / 100) * BASELINE_RISK)
            partner.smarterp_churn_score = score
            partner.smarterp_confidence = confidence
            if score >= RED_FROM:
                partner.smarterp_health = 'red'
            elif score >= YELLOW_FROM:
                partner.smarterp_health = 'yellow'
            else:
                partner.smarterp_health = 'green'
            if confidence >= HIGH_CONFIDENCE_FROM:
                partner.smarterp_confidence_level = 'high'
            elif confidence >= MEDIUM_CONFIDENCE_FROM:
                partner.smarterp_confidence_level = 'medium'
            else:
                partner.smarterp_confidence_level = 'low'

    def _message_post_after_hook(self, message):
        if message.message_type == 'email':
            for name in ('smarterp_last_client_message', 'smarterp_churn_score',
                         'smarterp_health', 'smarterp_confidence', 'smarterp_confidence_level'):
                self.env.add_to_compute(self._fields[name], self)
        result = super()._message_post_after_hook(message)
        # A client email: analyze it right away (one Gemini call), so the risk updates with no click.
        from_client = message.message_type == 'email' and not message.author_id.user_ids.filtered(
            lambda u: not u.share)
        if from_client and not self.env.context.get('smarterp_skip_ai'):
            for partner in self.filtered('smarterp_is_bpo_client'):
                partner._smarterp_analyze_churn()
        return result

    def _smarterp_trend(self, messages, scores):
        """Average sentiment of the last TREND_DAYS vs the rest of the window (scores line up with messages)."""
        since = fields.Datetime.now() - timedelta(days=TREND_DAYS)
        recent = [score for message, score in zip(messages, scores) if message.date >= since]
        prior = [score for message, score in zip(messages, scores) if message.date < since]
        if not recent or not prior:
            return False
        change = sum(recent) / len(recent) - sum(prior) / len(prior)
        return 'worse' if change <= -TREND_STEP else 'better' if change >= TREND_STEP else 'stable'

    def _smarterp_prompt(self, messages):
        days = (self.smarterp_contract_end - fields.Date.today()).days if self.smarterp_contract_end else None
        contract = '%s (%d days away)' % (self.smarterp_contract_end, days) if days is not None else 'unknown'
        seats = ('current %d, previous %d' % (self.smarterp_current_seats, self.smarterp_previous_seats)
                 if self.smarterp_previous_seats else 'unknown')
        lines = '\n'.join('Message %d [%s]: %s' % (i, m.date.date(), self._smarterp_text(m)[:500])
                          for i, m in enumerate(messages[:PROMPT_MESSAGES], 1))
        return (
            'You help a BPO (call center outsourcing) account team spot clients that may leave. '
            'Here is a client:\n'
            f'- Name: {self.name}\n'
            f'- Contract ends: {contract}\n'
            f'- Seats: {seats}\n'
            f'- Complaints in the last {SENTIMENT_DAYS} days: {self.smarterp_complaint_count}\n\n'
            'Last messages from the client, newest first:\n' + lines + '\n\n'
            'Answer with one line per message, then three more lines, no other text:\n'
            'Message <number>: <positive | neutral | negative>\n'
            'Risk phrases: <yes if they mention leaving, cancelling, terminating, other vendors, an RFP '
            'or switching provider, else no>\n'
            'Reason: <one sentence on why this client is or is not at risk of leaving>\n'
            'Retention action: <one concrete next step for the account manager>'
        )

    def _smarterp_analyze_churn(self):
        """Read the recent client messages: Gemini first, keywords if it fails. One Gemini call per client."""
        self.ensure_one()
        messages = self._smarterp_recent_messages()
        if not messages:
            # Nothing to read: sentiment stays unknown, no Gemini call.
            self.write({'smarterp_sentiment': 0, 'smarterp_sentiment_trend': False, 'smarterp_risk_phrases': False})
            self._smarterp_write_rules_reason()
            return
        ai = self.env['smarterp.ai']
        answer = (ai.ask_gemini(self._smarterp_prompt(messages)) or '').replace('*', '')
        # Tolerant of markdown (**Reason:**) and of the text starting on the next line.
        match = REASON_RE.search(answer)
        labels = {int(number): label.lower() for number, label in LABEL_RE.findall(answer)}
        if answer and not (match and labels):
            _logger.warning('Could not fully read Gemini churn answer: %r', answer[:1500])
        # One sentiment per message: Gemini's label, keywords for any message it didn't label (or if it failed).
        # The labels are used even when the reason part can't be read.
        scores = [SENTIMENT_LABELS[labels[i]] if i in labels
                  else ai.keyword_sentiment(self._smarterp_text(m))['score']
                  for i, m in enumerate(messages, 1)]
        lowered = ' '.join(self._smarterp_text(m) for m in messages).lower()
        keyword_phrases = any(re.search(r'\b%s' % re.escape(p), lowered) for p in RISK_PHRASES)
        phrases = RISK_PHRASES_RE.search(answer)
        vals = {
            'smarterp_sentiment': sum(scores) / len(scores),
            'smarterp_sentiment_trend': self._smarterp_trend(messages, scores),
            'smarterp_complaint_count': sum(1 for score in scores if score < 0),
            'smarterp_risk_phrases': phrases.group(1).lower() if phrases else ('yes' if keyword_phrases else 'no'),
        }
        if match:
            vals.update({'smarterp_reason': match.group(1).strip(),
                         'smarterp_retention_action': match.group(2).strip(),
                         'smarterp_reason_source': 'gemini'})
            self.write(vals)
            return
        self.write(vals)
        self._smarterp_write_rules_reason()

    def _smarterp_level_label(self):
        return dict(self._fields['smarterp_confidence_level'].selection)[self.smarterp_confidence_level]

    def _smarterp_write_rules_reason(self):
        signals = self._smarterp_signals()
        points = {name: SCORE_WEIGHTS[name] * value for name, value in signals.items() if value}
        top = sorted(points, key=points.get, reverse=True)[:2]
        health = dict(self._fields['smarterp_health'].selection)[self.smarterp_health]
        reason = '%s risk (%d/100, %s confidence), ' % (
            health, self.smarterp_churn_score, self._smarterp_level_label().lower())
        reason += ('driven mostly by %s.' % ' and '.join(FACTOR_LABELS[name] for name in top)
                   if top else 'no risk signals found.')
        unknown = [FACTOR_LABELS[name] for name, value in signals.items() if value is None]
        if unknown:
            reason += ' Unknown: %s.' % ', '.join(unknown)
        if self.smarterp_confidence < MEDIUM_CONFIDENCE_FROM and (signals['renewal'] is None
                                                                  or signals['seats'] is None):
            action = 'Fill in the contract end and seats to get a reliable risk score.'
        elif self.smarterp_health == 'green' or not top:
            action = RETENTION_ACTIONS['green']
        else:
            action = RETENTION_ACTIONS[top[0]]
        self.write({'smarterp_reason': reason, 'smarterp_retention_action': action, 'smarterp_reason_source': 'rules'})

    def action_smarterp_reanalyze(self):
        """Form button: read the client's messages with Gemini and explain the churn risk."""
        for partner in self.filtered('smarterp_is_bpo_client'):
            partner._smarterp_analyze_churn()
        return True

    def action_smarterp_analyze_churn(self):
        """List action: analyze up to ANALYZE_MAX selected BPO clients, spaced out for the Gemini rate limit."""
        clients = self.filtered('smarterp_is_bpo_client')
        for i, partner in enumerate(clients[:ANALYZE_MAX]):
            if i:
                time.sleep(4)  # stay under the Gemini free tier of 15 calls/min
            partner._smarterp_analyze_churn()
        message = '%d BPO client(s) analyzed.' % min(len(clients), ANALYZE_MAX)
        if len(clients) > ANALYZE_MAX:
            message += ' Max %d per run: %d skipped, run again on those.' % (ANALYZE_MAX, len(clients) - ANALYZE_MAX)
        if len(self) > len(clients):
            message += ' %d not marked as BPO client, skipped.' % (len(self) - len(clients))
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': 'Analyze selected', 'message': message, 'type': 'success',
            'next': {'type': 'ir.actions.act_window_close'},
        }}
