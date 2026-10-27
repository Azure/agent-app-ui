<!--
page_type: sample
languages:
- azdeveloper
- powershell
- bicep
products:
- azure
- azure-ai-foundry
- azure-openai
- azure-ai-search
urlFragment: agent-app-ui
name: Agent Landing Zone Web UI
description: Chainlit web UI for Agent Landing Zone, delivering conversational agent experiences on Microsoft Foundry and Microsoft Agent Framework.
-->
# Agent Landing Zone Web UI

The [Agent Landing Zone](https://github.com/Azure/agent-landing-zone) web client
uses [Chainlit](https://www.chainlit.io/) to stream agent responses, display
citations, and collect feedback. The orchestrator agent runs on Microsoft
Foundry Hosted Agents by default. Set `CHAT_BACKEND=orchestrator` explicitly
to use the Container Apps backend. Hosted failures never trigger an automatic
switch to that backend.

## Set up and deploy

Start with the [central product documentation](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/):

- [Deploy the full stack](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/deploy-full-stack/).
- [Configuration](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/configuration/).
- [Hosted Agents and delegated authentication](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/hosted-agents/).
- [Custom applications](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/build-your-own-app/).
- [Operations](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/operations/)
  and [troubleshooting](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/troubleshooting/).

The documentation source is
[`Azure/AI-Landing-Zones`, `main`, `docs/agent-landing-zone`](https://github.com/Azure/AI-Landing-Zones/tree/main/docs/agent-landing-zone).
This component does not provision the platform.

Conversation attachments require the optional ingestion app.
`DEPLOY_INGESTION_CONTAINER_APP=false` hides upload controls and rejects
attachments. A missing setting preserves legacy eligibility. `true` preserves
authentication and conversation checks. Invalid values fail configuration
validation. There is no replacement native conversation upload path. See
[ingestion selection](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/configuration/#ingestion-application-selection)
for supported deployments.

## Component guides

- [Python development](docs/python-development.md): local setup, module
  ownership, backend contracts, staged assets, and validation commands.
- [Chainlit Copilot embedding](docs/copilot-embedding.md): opt-in portal
  integration, authentication, uploads, and session security.
- [Engineering framework](docs/agentic-engineering.md): contributor guidance
  for the repository's Copilot assets.
- [Support](SUPPORT.md) and [security reporting](SECURITY.md).
- [Contributing](https://github.com/Azure/agent-landing-zone/blob/main/CONTRIBUTING.md).

For presentation changes, use `public/theme.json`, `public/custom.css`, and
`.chainlit/config.toml`.

## Trademarks

This project may contain trademarks or logos. Authorized use of Microsoft
trademarks or logos must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/en-us/legal/intellectualproperty/trademarks/usage/general).
Modified versions must not imply sponsorship or cause confusion. Third-party
trademarks are subject to their own policies.
