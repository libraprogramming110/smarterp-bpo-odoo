import logging
import os
import re
import time

import requests

from odoo import api, models

_logger = logging.getLogger(__name__)

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent'

POSITIVE_WORDS = ['good', 'great', 'excellent', 'happy', 'thanks', 'thank you', 'love', 'satisfied', 'helpful', 'resolved']
NEGATIVE_WORDS = ['bad', 'poor', 'terrible', 'angry', 'unhappy', 'hate', 'slow', 'cancel', 'complain', 'refund', 'disappointed']


class SmarterpAi(models.AbstractModel):
    _name = 'smarterp.ai'
    _description = 'SmartERP AI helper (Gemini with keyword fallback)'

    @api.model
    def ask_gemini(self, prompt):
        """Return Gemini's text answer for prompt, or None if unavailable."""
        api_key = os.environ.get('GEMINI_API_KEY')
        if not api_key:
            _logger.warning('GEMINI_API_KEY is not set, skipping Gemini call')
            return None
        for attempt in range(2):
            try:
                response = requests.post(
                    GEMINI_URL,
                    headers={'x-goog-api-key': api_key},
                    json={'contents': [{'parts': [{'text': prompt}]}]},
                    timeout=15,
                )
                response.raise_for_status()
                return response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
            except (requests.ConnectionError, requests.Timeout) as e:
                error = type(e).__name__
                if not attempt:
                    time.sleep(2)  # brief network drop: try once more
            except requests.HTTPError as e:
                error = 'HTTP %s' % e.response.status_code
                if e.response.status_code not in (429, 500, 503):
                    break
                if not attempt:
                    time.sleep(3)  # Gemini busy or rate-limited: try once more
            except (requests.RequestException, ValueError, KeyError, IndexError) as e:
                error = type(e).__name__
                break
        _logger.warning('Gemini call failed: %s', error)
        return None

    @api.model
    def keyword_sentiment(self, text):
        """Label text positive/neutral/negative by counting keywords; score is -1..1."""
        text = (text or '').lower()
        # Match at the start of a word: "unhappy" doesn't count as "happy", "complaints" still counts.
        pos = sum(1 for w in POSITIVE_WORDS if re.search(r'\b%s' % re.escape(w), text))
        neg = sum(1 for w in NEGATIVE_WORDS if re.search(r'\b%s' % re.escape(w), text))
        score = (pos - neg) / (pos + neg) if pos + neg else 0.0
        if score > 0:
            label = 'positive'
        elif score < 0:
            label = 'negative'
        else:
            label = 'neutral'
        return {'label': label, 'score': score}

    @api.model
    def sentiment(self, text):
        """Try Gemini first, fall back to keywords."""
        answer = self.ask_gemini(
            'Classify the sentiment of this text. Answer with one word: '
            'positive, neutral or negative.\n\n' + (text or '')
        )
        label = (answer or '').strip().strip('.').lower()
        if label in ('positive', 'neutral', 'negative'):
            score = {'positive': 1.0, 'neutral': 0.0, 'negative': -1.0}[label]
            return {'label': label, 'score': score, 'source': 'gemini'}
        result = self.keyword_sentiment(text)
        result['source'] = 'keywords'
        return result
