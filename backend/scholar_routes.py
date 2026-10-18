"""
scholar_routes.py -- Flask wiring for the Google Scholar (ScrapingBee) and
OpenAlex-budget integrations. app.py needs ONE call:

    from scholar_routes import register_scholar_routes
    register_scholar_routes(app, require_auth)

Routes (all require login):
  GET  /api/integrations/status   OpenAlex budget state + Scholar config/usage
  POST /api/scholar/search        {query, pages?, year_from?}
  POST /api/scholar/check-gap     {gap_title, description?, year_from?}

It also registers "Google Scholar" in academic_sources.KEYED_SOURCES so it
can be ticked as a paper source in standard mode. It is deliberately NOT
added to KEYED_SOURCE_ENV_VARS, so the deep-research collector never
auto-includes it (it pages sources heavily and would burn ScrapingBee
credits); fetch_google_scholar is also hard-capped in pages as a backstop.
"""
from flask import jsonify, request

import openalex_client
import scholar_scrapingbee as scholar


def _register_source():
    try:
        import academic_sources
        academic_sources.KEYED_SOURCES.setdefault('Google Scholar', scholar.fetch_google_scholar)
    except Exception as e:                       # never break app start-up
        print(f"[scholar] could not register Google Scholar as a paper source: {e}")


def register_scholar_routes(app, require_auth):
    _register_source()

    @app.route('/api/integrations/status', methods=['GET'])
    @require_auth
    def integrations_status():
        return jsonify({'openalex': openalex_client.status(), 'scholar': scholar.status()})

    @app.route('/api/scholar/search', methods=['POST'])
    @require_auth
    def scholar_search():
        data = request.get_json(silent=True) or {}
        query = (data.get('query') or '').strip()
        if not query:
            return jsonify({'error': 'query is required'}), 400
        try:
            results = scholar.search(query, pages=data.get('pages', 1), year_from=data.get('year_from'))
        except scholar.ScholarNotConfigured as e:
            return jsonify({'error': str(e), 'error_type': 'not_configured'}), 503
        except scholar.ScholarBlocked as e:
            return jsonify({'error': str(e), 'error_type': 'blocked'}), 502
        except scholar.ScholarError as e:
            return jsonify({'error': str(e)}), 502
        return jsonify({'query': query, 'count': len(results), 'results': results})

    @app.route('/api/scholar/check-gap', methods=['POST'])
    @require_auth
    def scholar_check_gap():
        data = request.get_json(silent=True) or {}
        title = (data.get('gap_title') or '').strip()
        if not title:
            return jsonify({'error': 'gap_title is required'}), 400
        try:
            out = scholar.check_gap(title, data.get('description', ''), year_from=data.get('year_from'))
        except scholar.ScholarNotConfigured as e:
            return jsonify({'error': str(e), 'error_type': 'not_configured'}), 503
        except scholar.ScholarBlocked as e:
            return jsonify({'error': str(e), 'error_type': 'blocked'}), 502
        except scholar.ScholarError as e:
            return jsonify({'error': str(e)}), 502
        return jsonify(out)
