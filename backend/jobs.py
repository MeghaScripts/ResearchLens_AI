"""
In-memory job store for live-progress analysis runs.

/api/analyze runs entirely synchronously — the client sends one request
and waits until the whole pipeline (PDF parse -> search -> keywords ->
domain classification -> gap detection -> knowledge graph -> trends ->
report) finishes before it hears anything back. That's honest about
completion, but it's not TRANSPARENT: the person watching a generic
"Analyzing..." spinner has no way to know which papers were actually
found, or which stage is currently running, until the very end.

This module backs a job-based alternative (/api/analyze/start +
/api/analyze/status/<job_id>) that runs the SAME pipeline in a
background thread and lets the frontend poll for real progress —
the actual paper titles/sources as soon as search_agent finds them,
and a plain-language line for each pipeline stage as it completes
(reusing each agent's own `explain()` text, not a fake animation).

Deliberately NOT a task queue (no Celery/Redis) — this app runs as a
single Flask process, so a locked dict is the honest implementation at
this scale, same reasoning as events/event_bus.py. If this ever needs
to survive a server restart or run across multiple processes, this is
the seam to replace with a real job queue; nothing above this module
needs to change since callers only use create_job/update/get/finish.
"""
import threading
import time
import uuid

_JOBS = {}
_LOCK = threading.Lock()

# Jobs older than this are dropped on the next create_job() call, so a
# long-running server doesn't accumulate finished jobs forever. Polling
# an analysis that legitimately takes a few minutes is well inside this.
_MAX_AGE_SECONDS = 3600


def create_job(uid):
    """Registers a new job owned by `uid` (the Firebase uid that started
    it — /api/analyze/status/<id> checks this before returning anything,
    so one user can't poll another user's in-progress analysis)."""
    _evict_old_jobs()
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job_id] = {
            'uid': uid,
            'status': 'running',        # 'running' | 'done' | 'error'
            'steps': [],                 # [{agent, summary, success}], in order
            'papers_count': 0,
            'papers_sample': [],         # [{title, source, year}] — first N found
            'result': None,
            'error': None,
            'created_at': time.time(),
        }
    return job_id


def add_step(job_id, agent, summary, success=True):
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job['steps'].append({'agent': agent, 'summary': summary, 'success': success})


def set_papers(job_id, papers, sample_size=60):
    """Called right after search_agent populates context['papers'] — this
    is the core of the transparency feature: the person sees the actual
    papers the pipeline is about to analyze, not just a step name."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job['papers_count'] = len(papers)
            job['papers_sample'] = [
                {'title': p.get('title') or '(untitled)',
                 'source': p.get('source') or '',
                 'year': p.get('year')}
                for p in papers[:sample_size]
            ]


def finish_job(job_id, result=None, error=None):
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job['status'] = 'error' if error else 'done'
            job['result'] = result
            job['error'] = error


def get_job(job_id, uid=None):
    """Returns a shallow copy of the job dict, or None if it doesn't
    exist. If `uid` is given, also returns None when the job belongs to
    someone else — callers should treat that the same as "not found"
    (404), never a 403, so job IDs can't be used to probe for existence
    of another user's analysis."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        if uid is not None and job.get('uid') != uid:
            return None
        return dict(job)


def _evict_old_jobs():
    cutoff = time.time() - _MAX_AGE_SECONDS
    with _LOCK:
        for jid in [j for j, v in _JOBS.items() if v['created_at'] < cutoff]:
            del _JOBS[jid]