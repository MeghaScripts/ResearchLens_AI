# ResearchLens AI — Project Documentation

| Document | Purpose |
|---|---|
| [SDLC_AGILE.md](SDLC_AGILE.md) | Full SDLC cycle, Agile model, backlog, iterations, Definition of Done |
| [CICD_DEPLOYMENT.md](CICD_DEPLOYMENT.md) | CI/CD, Docker, monitoring, releases and step-by-step deployment |
| [SRS.md](SRS.md) | Software Requirements Specification (what the system must do) |
| [DESIGN.md](DESIGN.md) | Architecture, data-flow, sequence, ER and agent diagrams (Mermaid) |
| [TEST_PLAN.md](TEST_PLAN.md) | Test strategy, test cases, traceability to requirements, current status |
| [../DEPLOY.md](../DEPLOY.md) | Deployment to Render |
| [../AGENTS.md](../AGENTS.md) | Developer notes on the agents |

Diagrams are written in Mermaid; GitHub, VS Code (Markdown Preview Mermaid Support) and https://mermaid.live render them.

## Development process
The project follows the full SDLC (planning, requirements, design, implementation, testing, deployment, maintenance) using an **iterative Agile** approach with a prioritised backlog, review and retrospective each iteration. See [SDLC_AGILE.md](SDLC_AGILE.md).

```
Requirements (SRS) -> Design (DESIGN) -> Implementation -> Testing (TEST_PLAN) -> Deployment (Render) -> Maintenance
        ^                                                                                                  |
        +------------------------------------- feedback / bug reports -------------------------------------+
```
