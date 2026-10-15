# UI deployment infrastructure

`main.bicep` is an empty resource-group-scoped entry point. It does not
provision the Agent Landing Zone platform.

Provision the platform through the
[central deployment guide](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/deploy-full-stack/).
For component-only redeployment, the UI's `azure.yaml` runs
`scripts/deploy.ps1` or `scripts/deploy.sh` as its predeploy hook.
The scripts use the existing platform resources and can build an image in
ACR and update the frontend Container App. They are not offline checks.

Use the platform's subscription, resource group, and `azd` environment.
Initialize or select that environment before running `azd env refresh`
and `azd deploy`. The deployment scripts prefer a non-empty shell
`APP_CONFIG_ENDPOINT` over the `azd` environment value and warn if they
differ. Check the endpoint before deploying.

See [configuration](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/configuration/)
and [operations](https://azure.github.io/AI-Landing-Zones/agent-landing-zone/operations/)
for the operator workflow.
