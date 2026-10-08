"""
Orchestrator Agent: registers agents by name and runs them in sequence
(or, for independent stages, in parallel) against a shared `context`
dict, collecting an execution trace.

The pipeline is still mostly linear (search -> parse -> analyze -> graph
-> report) because most stages genuinely depend on the previous one's
output. Where two stages are independent of each other — e.g. once
gap_detection_agent has populated context['analysis'], knowledge_graph
and trend analysis don't depend on each other — run_parallel lets them
run concurrently on a thread pool instead of forcing an arbitrary order.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from .base import AgentResult


class Orchestrator:
    def __init__(self, event_bus=None):
        self.agents = {}
        # Optional EventBus — if set, every agent completion is published
        # as an "agent.<name>.completed" event so other parts of the app
        # (e.g. the monitoring/notification layer) can react without the
        # orchestrator needing to know who's listening.
        self.event_bus = event_bus

    def register(self, agent):
        self.agents[agent.name] = agent
        return self

    def list_agents(self):
        return [{'name': a.name, 'description': a.description} for a in self.agents.values()]

    def _record(self, context, result):
        context.setdefault('_trace', []).append(result)
        # Live progress for /api/analyze/start: push each finished agent (and
        # the papers once search_agent has found them) into the job store.
        job_id = context.get('_job_id')
        if job_id:
            try:
                import jobs
                jobs.add_step(job_id, result.agent,
                              result.explanation or result.error or '', result.success)
                if result.agent == 'search_agent' and result.success:
                    jobs.set_papers(job_id, context.get('papers') or [])
            except Exception as e:
                print(f"job progress update failed: {e}")
        if self.event_bus:
            self.event_bus.publish(f"agent.{result.agent}.completed",
                                    {'success': result.success, 'agent': result.agent})

    def run_agent(self, name: str, context: dict) -> AgentResult:
        agent = self.agents.get(name)
        if not agent:
            raise ValueError(f"Unknown agent: {name}")
        result = agent.run(context)
        self._record(context, result)
        return result

    def run_pipeline(self, agent_names, context: dict):
        """Runs agents in order, stopping at the first failure. Returns the
        list of AgentResults (also available at context['_trace'])."""
        results = []
        for name in agent_names:
            result = self.run_agent(name, context)
            results.append(result)
            if not result.success:
                break
        return results

    def run_parallel(self, agent_names, context: dict):
        """Runs several independent agents concurrently against the same
        shared context, using a thread pool.

        Only safe for agents that (a) don't depend on each other's output
        and (b) write to distinct context keys. Threads release the GIL
        during network I/O (Groq calls, paper-API calls), so this is a
        real win for I/O-bound agents; pure in-process computation
        (networkx, TF-IDF) sees a smaller win but is still safe here.

        Returns {name: AgentResult}. Trace entries are appended under a
        lock as each thread finishes, so ordering across a parallel batch
        reflects completion order, not submission order.
        """
        agents = [self.agents[name] for name in agent_names]
        results = {}
        lock = threading.Lock()

        def _run(agent):
            result = agent.run(context)
            with lock:
                self._record(context, result)
            return agent.name, result

        with ThreadPoolExecutor(max_workers=max(1, len(agents))) as pool:
            for name, result in pool.map(_run, agents):
                results[name] = result
        return results