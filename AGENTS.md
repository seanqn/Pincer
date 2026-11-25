## Project
Pincer is intended to be an integrable debugger with durable restart/recovery
features. These features aim to be as capable as the underlying models allow.
Pincer's adapter will be agnostic and support integration from several LLMs, as well as locally run LLMs.

## Testing
Tests are stored in src/tests/ for Pincer's src components. Current testing depends on PyTest, but additional dependencies will require discretion when using a compatible testing framework.

## Requirements and Collaboration
- Agents collaborate with the requesting developer on architectural decisions when requested
- Agents notify the requesting developer if planned architectural decisions could intefere with existing architecture
- Agents aim to determine and find the optimal implementation for a task
- Agents notify the developer and provide feedback when a task's implementation and the existing architecture would conflict or there are potential improvements or additions to the existing architecture
- Pincer's business logic written and maintained in Python
