# AMS Solutions - AI Python Challenge

## Objective
Build a robust, production-minded AI notification service
using FastAPI and the supplied mock AI provider.

## Engineering principles
- Python 3.12, FastAPI, Pydantic v2 and async HTTPX.
- Clean Code, SOLID, pragmatic hexagonal architecture.
- TDD with pytest and deterministic fixtures.
- Type hints, dependency injection and clear boundaries.
- Prefer maintainability over unnecessary complexity.

## AI engineering
- Design and document a robust system prompt.
- Treat every AI response as untrusted input.
- Validate and normalize extracted information.
- Handle Markdown, malformed JSON and alternative field names.
- Never invent missing recipients or message content.
- Handle refusals and ambiguous responses explicitly.
- Prevent prompt injection and unsafe execution.
- Never bypass the AI extraction endpoint.
- Never hardcode behavior for benchmark prompts.

## Reliability
- Preserve the original API contracts.
- Use bounded asynchronous processing.
- Handle AI latency, timeouts and transient failures.
- Prevent duplicate processing.
- Document delivery guarantees and limitations.
- Use persistence and crash recovery where justified.

## Testing
- Test successful extraction and notification.
- Test malformed output, missing data and refusals.
- Test concurrent requests and failure recovery.
- Run original k6 tests unchanged.
- Validate real processing outcomes beyond HTTP checks.

## Git workflow
- Work directly on main of my personal fork.
- Never modify or push to upstream.
- Create automatic atomic commits after verified changes.
- Follow Conventional Commits.
- Run relevant tests before committing.
- Never commit secrets or generated artifacts.
- Do not push to origin without explicit approval.

## Workflow
1. Analyze the repository.
2. Propose architecture and milestones.
3. Wait for approval.
4. Implement incrementally using TDD.
5. Run integration and performance tests.
6. Audit and document the complete solution.

## Communication
- Communicate with me in Spanish.
- Write code and documentation in English.
- Explain architectural decisions and trade-offs.
- Suggest cheaper models for routine tasks.
