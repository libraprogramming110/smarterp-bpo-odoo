import logging
import re
import time

from odoo import Command, api, fields, models

_logger = logging.getLogger(__name__)

# Each criterion is rated Met / Partial / Missed; the score is the weighted share of points out of 100.
RATING_POINTS = {'met': 1.0, 'partial': 0.5, 'missed': 0.0}
PASS_FROM = 80
GRADE_MAX = 30     # transcripts per "Grade selected" run, Gemini free tier allows 15 calls/min
EVIDENCE_MAX = 300
SENTIMENT_VALUES = {'positive': 1, 'neutral': 0, 'negative': -1}
SENTIMENTS = [('positive', 'Positive'), ('neutral', 'Neutral'), ('negative', 'Negative')]

CRITERION_RE = re.compile(r'criterion\s*(\d+)\s*:\s*(met|partial|missed)\b[ \t]*\|?[ \t]*(.*)', re.I)
SENTIMENT_START_RE = re.compile(r'sentiment\s*start\s*:\s*(positive|neutral|negative)', re.I)
SENTIMENT_END_RE = re.compile(r'sentiment\s*end\s*:\s*(positive|neutral|negative)', re.I)
STRENGTH_RE = re.compile(r'strength\s*:\s*(.+)', re.I)
IMPROVEMENT_RE = re.compile(r'improvement\s*:\s*(.+)', re.I)
SENTENCE_SPLIT_RE = re.compile(r'(?<=[.!?])\s+|\n')


class SmarterpQaCriterion(models.Model):
    _name = 'smarterp.qa.criterion'
    _description = 'QA Scorecard Criterion'
    _order = 'sequence, id'

    name = fields.Char(required=True)
    description = fields.Text('What good looks like')
    weight = fields.Integer(default=10)
    critical = fields.Boolean(help='Missing a critical criterion fails the call whatever the score.')
    keywords = fields.Char('Fallback Keywords', help='Comma-separated. Used when Gemini is unavailable: '
                                                     'a keyword in the transcript counts as Met.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)


class SmarterpQaResult(models.Model):
    _name = 'smarterp.qa.result'
    _description = 'QA Result per Criterion'
    _order = 'sequence, id'

    transcript_id = fields.Many2one('smarterp.qa.transcript', required=True, ondelete='cascade')
    criterion_id = fields.Many2one('smarterp.qa.criterion', required=True, ondelete='cascade')
    sequence = fields.Integer(related='criterion_id.sequence')
    critical = fields.Boolean(related='criterion_id.critical')
    weight = fields.Integer(related='criterion_id.weight')
    rating = fields.Selection([
        ('met', 'Met'),
        ('partial', 'Partial'),
        ('missed', 'Missed'),
    ], required=True)
    evidence = fields.Text()


class SmarterpQaTranscript(models.Model):
    _name = 'smarterp.qa.transcript'
    _description = 'Call Transcript'
    _order = 'call_date desc, id desc'

    name = fields.Char('Reference', required=True)
    call_date = fields.Date(default=fields.Date.today, required=True)
    duration = fields.Integer('Duration (min)')
    agent_id = fields.Many2one('hr.employee', 'Agent')
    partner_id = fields.Many2one('res.partner', 'Client')
    transcript = fields.Text()
    result_ids = fields.One2many('smarterp.qa.result', 'transcript_id', 'Results')

    ai_score = fields.Integer('AI QA Score', compute='_compute_score', store=True)
    auto_fail = fields.Boolean('Auto-fail', compute='_compute_score', store=True,
                               help='A critical criterion was missed.')
    outcome = fields.Selection([
        ('pass', 'Pass'),
        ('fail', 'Fail'),
    ], string='Pass/Fail', compute='_compute_score', store=True)
    # Written by grading.
    sentiment_start = fields.Selection(SENTIMENTS, 'Customer Sentiment (start)', readonly=True)
    sentiment_end = fields.Selection(SENTIMENTS, 'Customer Sentiment (end)', readonly=True)
    sentiment_shift = fields.Selection([
        ('improved', 'Improved'),
        ('same', 'Same'),
        ('worsened', 'Worsened'),
    ], compute='_compute_sentiment_shift', store=True)
    coaching_note = fields.Text(readonly=True)
    grade_source = fields.Selection([
        ('gemini', 'Gemini'),
        ('rules', 'Rules'),
    ], readonly=True)

    reviewed = fields.Boolean()
    reviewer_id = fields.Many2one('res.users', 'Reviewer')
    override_score = fields.Integer(help='Reviewer score 1-100; leave 0 to keep the AI score.')
    final_score = fields.Integer(compute='_compute_score', store=True, aggregator='avg')
    needs_review = fields.Boolean(compute='_compute_score', store=True)

    @api.depends('result_ids.rating', 'result_ids.criterion_id.weight', 'result_ids.criterion_id.critical',
                 'override_score', 'reviewed', 'grade_source')
    def _compute_score(self):
        for call in self:
            weight = sum(call.result_ids.mapped('weight'))
            points = sum(r.weight * RATING_POINTS[r.rating] for r in call.result_ids)
            call.ai_score = round(100 * points / weight) if weight else 0
            call.auto_fail = any(r.critical and r.rating == 'missed' for r in call.result_ids)
            call.final_score = call.override_score or call.ai_score
            if not call.result_ids:
                call.outcome = False
            elif call.final_score >= PASS_FROM and not call.auto_fail:
                call.outcome = 'pass'
            else:
                call.outcome = 'fail'
            call.needs_review = call.outcome != 'pass' or call.grade_source != 'gemini' or not call.reviewed

    @api.depends('sentiment_start', 'sentiment_end')
    def _compute_sentiment_shift(self):
        for call in self:
            if not (call.sentiment_start and call.sentiment_end):
                call.sentiment_shift = False
                continue
            change = SENTIMENT_VALUES[call.sentiment_end] - SENTIMENT_VALUES[call.sentiment_start]
            call.sentiment_shift = 'improved' if change > 0 else 'worsened' if change < 0 else 'same'

    def _sentences(self):
        return [s.strip() for s in SENTENCE_SPLIT_RE.split(self.transcript or '') if s.strip()]

    def _prompt(self, criteria):
        lines = '\n'.join('%d. %s%s: %s' % (i, c.name, ' (critical)' if c.critical else '', c.description or '')
                          for i, c in enumerate(criteria, 1))
        return (
            'You are a quality assurance reviewer at a BPO (call center outsourcing) company. '
            'Grade this call transcript against the scorecard.\n\n'
            'Scorecard, number. name: what good looks like:\n' + lines + '\n\n'
            'Transcript:\n' + (self.transcript or '') + '\n\n'
            'Answer with one line per criterion, then four more lines, no other text:\n'
            'Criterion <number>: <Met | Partial | Missed> | "<short quote from the transcript as evidence, '
            'or what is missing>"\n'
            'Sentiment start: <positive | neutral | negative, the customer at the start of the call>\n'
            'Sentiment end: <positive | neutral | negative, the customer at the end of the call>\n'
            'Strength: <one thing the agent did well, with a short quote>\n'
            'Improvement: <one thing the agent should do better next time, with a short quote>'
        )

    def _keyword_rating(self, criterion, sentences):
        """Met with the first sentence holding a keyword, else Missed."""
        keywords = [k.strip().lower() for k in (criterion.keywords or '').split(',') if k.strip()]
        for sentence in sentences:
            if any(k in sentence.lower() for k in keywords):
                return 'met', sentence[:EVIDENCE_MAX]
        return 'missed', 'No sign of this in the transcript (keyword check).'

    def _grade(self):
        """Gemini first (one call), keywords for any criterion it didn't rate. Replaces the result lines."""
        self.ensure_one()
        criteria = self.env['smarterp.qa.criterion'].search([])
        ai = self.env['smarterp.ai']
        answer = (ai.ask_gemini(self._prompt(criteria)) or '').replace('*', '')
        rated = {int(n): (rating.lower(), quote.strip().strip('"“”\'').strip())
                 for n, rating, quote in CRITERION_RE.findall(answer)}
        all_rated = bool(answer) and all(i in rated for i in range(1, len(criteria) + 1))
        if answer and not all_rated:
            _logger.warning('Could not fully read Gemini QA answer: %r', answer[:1500])
        sentences = self._sentences()
        results = []
        for i, criterion in enumerate(criteria, 1):
            rating, evidence = rated.get(i) or self._keyword_rating(criterion, sentences)
            results.append({'criterion_id': criterion.id, 'rating': rating, 'evidence': evidence[:EVIDENCE_MAX]})
        # Sentiment: Gemini's labels, else keywords on the first and last third of the call.
        third = max(len(sentences) // 3, 1)
        start = SENTIMENT_START_RE.search(answer)
        end = SENTIMENT_END_RE.search(answer)
        vals = {
            'result_ids': [Command.clear()] + [Command.create(r) for r in results],
            'sentiment_start': (start.group(1).lower() if start
                                else ai.keyword_sentiment(' '.join(sentences[:third]))['label']),
            'sentiment_end': (end.group(1).lower() if end
                              else ai.keyword_sentiment(' '.join(sentences[-third:]))['label']),
            'grade_source': 'gemini' if all_rated else 'rules',
        }
        strength = STRENGTH_RE.search(answer)
        improvement = IMPROVEMENT_RE.search(answer)
        if strength and improvement:
            vals['coaching_note'] = 'Strength: %s\nImprovement: %s' % (strength.group(1).strip(),
                                                                       improvement.group(1).strip())
        else:
            vals['coaching_note'] = self._rules_coaching(criteria, results)
        self.write(vals)

    def _rules_coaching(self, criteria, results):
        """Name the heaviest criterion met and the heaviest missed (critical first)."""
        pairs = list(zip(criteria, results))
        met = [p for p in pairs if p[1]['rating'] == 'met']
        missed = [p for p in pairs if p[1]['rating'] != 'met']
        note = []
        if met:
            best = max(met, key=lambda p: p[0].weight)
            note.append('Strength: %s, "%s"' % (best[0].name, best[1]['evidence']))
        else:
            note.append('Strength: none of the scorecard criteria were clearly met.')
        if missed:
            worst = max(missed, key=lambda p: (p[0].critical, p[0].weight, p[1]['rating'] == 'missed'))
            note.append('Improvement: %s. What good looks like: %s' % (worst[0].name, worst[0].description or ''))
        else:
            note.append('Improvement: keep this standard on every call.')
        return '\n'.join(note)

    def action_grade(self):
        """Form button: grade the transcript with Gemini against the scorecard."""
        for call in self:
            call._grade()
        return True

    def action_grade_selected(self):
        """List action: grade up to GRADE_MAX selected transcripts, spaced out for the Gemini rate limit."""
        for i, call in enumerate(self[:GRADE_MAX]):
            if i:
                time.sleep(4)  # stay under the Gemini free tier of 15 calls/min
            call._grade()
        message = '%d transcript(s) graded.' % min(len(self), GRADE_MAX)
        if len(self) > GRADE_MAX:
            message += ' Max %d per run: %d skipped, run again on those.' % (GRADE_MAX, len(self) - GRADE_MAX)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': 'Grade selected', 'message': message, 'type': 'success',
            'next': {'type': 'ir.actions.act_window_close'},
        }}
